"""PR automation: push staged meme images to a GitHub repo via Git Data API.

Pure async, no AstrBot dependency.
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass
class MemeEntry:
    """A single entry to add to memes.json, built from a staged sidecar."""

    title: str
    filename: str
    is_gif: bool
    description: str = ""
    user_hint: str = ""


def _next_id(memes: list[dict]) -> int:
    """Return max id + 1, or 1 if empty."""
    return max((m.get("id", 0) for m in memes), default=0) + 1


def build_entry(sidecar: dict) -> MemeEntry:
    """Build a MemeEntry from a sidecar dict (from the .json written by /传比)."""
    name = sidecar.get("name", "未命名")
    fmt = sidecar.get("fmt", "webp")
    return MemeEntry(
        title=name,
        filename=f"{name}.{fmt}",
        is_gif=(fmt == "gif"),
        description=sidecar.get("ai_description", ""),
        user_hint=sidecar.get("user_hint", ""),
    )


def _format_today() -> str:
    return time.strftime("%Y-%m-%d")


class PhoebeHubPR:
    """GitHub API client for pushing staged meme files and creating PRs."""

    def __init__(
        self,
        token: str,
        *,
        proxy: str | None = None,
        upstream_owner: str = "Kato-Shoko705",
        upstream_repo: str = "Phoebe-Hub",
    ):
        self.token = token
        self.proxy = proxy
        self.upstream_owner = upstream_owner
        self.upstream_repo = upstream_repo
        self.api_base = "https://api.github.com"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict = {"headers": self._headers, "timeout": 60}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    async def get_user(self) -> dict:
        async with self._client() as c:
            r = await c.get(f"{self.api_base}/user")
            r.raise_for_status()
            return r.json()

    async def get_main_sha(self, owner: str, repo: str) -> str:
        async with self._client() as c:
            r = await c.get(f"{self.api_base}/repos/{owner}/{repo}/git/ref/heads/main")
            r.raise_for_status()
            return r.json()["object"]["sha"]

    async def get_memes_json(self, owner: str, repo: str) -> tuple[dict, str]:
        """Fetch memes.json.  Returns (full_root_dict, file_sha).

        Root dict contains keys like ``memes``, ``pending``, ``nextId``.
        """
        async with self._client() as c:
            r = await c.get(
                f"{self.api_base}/repos/{owner}/{repo}/contents/data/memes.json"
            )
            r.raise_for_status()
            body = r.json()
            raw = base64.b64decode(body["content"]).decode()
            return json.loads(raw), body["sha"]

    async def create_blob(self, owner: str, repo: str, content: bytes) -> str:
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{owner}/{repo}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            r.raise_for_status()
            return r.json()["sha"]

    async def create_tree(
        self, owner: str, repo: str, base_tree: str, entries: list[dict]
    ) -> str:
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{owner}/{repo}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            )
            r.raise_for_status()
            return r.json()["sha"]

    async def create_commit(
        self, owner: str, repo: str, tree_sha: str, parent_sha: str, message: str
    ) -> str:
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{owner}/{repo}/git/commits",
                json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
            )
            r.raise_for_status()
            return r.json()["sha"]

    async def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            r.raise_for_status()

    async def create_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict:
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
            r.raise_for_status()
            return r.json()

    async def repo_exists(self, owner: str, repo: str) -> bool:
        async with self._client() as c:
            r = await c.get(f"{self.api_base}/repos/{owner}/{repo}")
            return r.status_code == 200

    async def ensure_fork(self, fork_owner: str) -> dict:
        """Create a fork of the upstream repo under fork_owner if it doesn't exist yet."""
        if await self.repo_exists(fork_owner, self.upstream_repo):
            async with self._client() as c:
                r = await c.get(
                    f"{self.api_base}/repos/{fork_owner}/{self.upstream_repo}"
                )
                r.raise_for_status()
                return r.json()

        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{self.upstream_owner}/{self.upstream_repo}/forks"
            )
            # 202 = provisioning, resource will be ready shortly
            if r.status_code == 202:
                return r.json()
            r.raise_for_status()
            return r.json()

    async def sync_fork_branch(self, fork_owner: str, branch: str = "main") -> None:
        """Sync a fork's branch with upstream via merge-upstream API.

        This is a no-op (not an error) if the branch is already up to date.
        """
        async with self._client() as c:
            r = await c.post(
                f"{self.api_base}/repos/{fork_owner}/{self.upstream_repo}/merge-upstream",
                json={"branch": branch},
            )
            # 200 = merge succeeded; 409 = already up to date (not an error)
            if r.status_code == 409:
                return
            r.raise_for_status()

    async def list_prs_from_fork(
        self, fork_owner: str, state: str = "all", per_page: int = 20
    ) -> list[dict]:
        """List PRs from a fork owner to the upstream repo.

        ``state``: ``"open"``, ``"closed"``, or ``"all"``.
        Returns PR dicts sorted by GitHub's default (most recently updated first).
        """
        async with self._client() as c:
            r = await c.get(
                f"{self.api_base}/repos/{self.upstream_owner}/{self.upstream_repo}/pulls",
                params={"state": state, "per_page": min(per_page, 100)},
            )
            r.raise_for_status()
            all_prs = r.json()
        return [
            pr for pr in all_prs
            if pr.get("head", {}).get("user", {}).get("login") == fork_owner
        ]

    # ------------------------------------------------------------------
    # Core: push staged files as a commit, optionally create PR
    # ------------------------------------------------------------------

    async def submit(
        self,
        staging_dir: Path,
        *,
        target_owner: str,
        target_repo: str = "Phoebe-Hub",
        create_pr: bool = False,
        pr_target_owner: str | None = None,
        pr_target_repo: str | None = None,
    ) -> dict:
        """Push staged files to target repo as a single commit on a new branch.

        When *pr_target_owner* differs from *target_owner*, the PR head is
        set to ``target_owner:branch`` (cross-repo / fork PR).
        """
        # 1. Collect staged files
        image_files: list[Path] = []
        sidecars: dict[str, Path] = {}
        for f in sorted(staging_dir.iterdir()):
            if f.suffix == ".json":
                stem = f.stem
                for ext in (".webp", ".gif", ".jpg", ".jpeg", ".png"):
                    if stem.endswith(ext):
                        stem = stem[: -len(ext)]
                        break
                sidecars[stem] = f
            elif f.suffix in (".webp", ".gif", ".jpg", ".jpeg", ".png"):
                image_files.append(f)

        if not image_files:
            return {"ok": False, "branch": None, "pr_url": None,
                    "message": "staging 目录为空，没有可提交的图片。"}

        # 2a. If creating a PR, check for existing open PRs from this fork
        if create_pr:
            existing = await self.list_prs_from_fork(target_owner, state="open")
            if existing:
                pr_url = existing[0].get("html_url", "")
                return {
                    "ok": False, "branch": None, "pr_url": pr_url,
                    "message": (
                        f"已有未关闭的 PR ({pr_url})，"
                        f"请等待合并或关闭后再提交新 PR。"
                    ),
                }

        # 2. Build memes.json entries from sidecars
        new_entries: list[MemeEntry] = []
        for img in image_files:
            stem_base = img.stem
            sc = sidecars.get(stem_base)
            if sc and sc.exists():
                data = json.loads(sc.read_text(encoding="utf-8"))
                new_entries.append(build_entry(data))
            else:
                new_entries.append(
                    MemeEntry(title=stem_base, filename=img.name,
                              is_gif=img.suffix == ".gif")
                )

        # 3. Fetch current memes.json from target repo (synced fork)
        today = _format_today()
        root, _ = await self.get_memes_json(target_owner, target_repo)
        existing_memes: list[dict] = root.get("memes", [])

        next_id = root.get("nextId", _next_id(existing_memes))
        for entry in new_entries:
            existing_memes.append({
                "id": next_id,
                "title": entry.title,
                "url": f"images/{entry.filename}",
                "category": ["static", "cute"],
                "views": 0,
                "downloads": 0,
                "date": today,
                "isGif": entry.is_gif,
                "hot": 0,
                "tags": [],
            })
            next_id += 1

        root["memes"] = existing_memes
        root["nextId"] = next_id
        updated_memes_json = json.dumps(root, ensure_ascii=False, indent=2)

        # 5. Get base tree SHA
        main_sha = await self.get_main_sha(target_owner, target_repo)

        # 6. Create blobs
        tree_entries = []
        for img in image_files:
            blob_sha = await self.create_blob(target_owner, target_repo,
                                              img.read_bytes())
            tree_entries.append({
                "path": f"images/{img.name}", "mode": "100644",
                "type": "blob", "sha": blob_sha,
            })

        memes_blob_sha = await self.create_blob(
            target_owner, target_repo, updated_memes_json.encode())
        tree_entries.append({
            "path": "data/memes.json", "mode": "100644",
            "type": "blob", "sha": memes_blob_sha,
        })

        # 7. Create tree
        tree_sha = await self.create_tree(target_owner, target_repo,
                                          main_sha, tree_entries)

        # 8. Create commit
        names = ", ".join(e.title for e in new_entries)
        commit_msg = f"feat: 新增 {len(new_entries)} 个表情包\n\n{names}\n\n由 AstrBot phoebehub 插件自动提交"
        commit_sha = await self.create_commit(target_owner, target_repo,
                                              tree_sha, main_sha, commit_msg)

        # 9. Create branch
        suffix = secrets.token_hex(4)
        branch = f"upload-{int(time.time())}-{suffix}"
        await self.create_branch(target_owner, target_repo, branch, commit_sha)

        result: dict = {
            "ok": True,
            "branch": branch,
            "commit_sha": commit_sha,
            "pr_url": None,
            "message": f"已推送到 {target_owner}/{target_repo} 的 {branch} 分支",
            "files": [img.name for img in image_files],
            "entries": [(e.title, e.description) for e in new_entries],
        }

        # 10. Optionally create PR (cross-repo if pr_target_owner is set)
        if create_pr:
            try:
                pr_owner = pr_target_owner or target_owner
                pr_repo = pr_target_repo or target_repo
                # Cross-repo PR requires fork_owner:branch format for head
                pr_head = f"{target_owner}:{branch}" if pr_target_owner else branch

                pr_body_lines = ["新增表情包："]
                for entry in new_entries:
                    line = f"- {entry.title}"
                    if entry.description:
                        line += f" — {entry.description}"
                    pr_body_lines.append(line)

                pr_title = f"feat: 新增 {len(new_entries)} 个表情包"
                pr = await self.create_pr(
                    owner=pr_owner, repo=pr_repo,
                    title=pr_title,
                    body="\n".join(pr_body_lines),
                    head=pr_head,
                )
                result["pr_url"] = pr.get("html_url")
                result["message"] += f"\nPR: {pr.get('html_url')}"
            except Exception as e:
                result["message"] += f"\n创建 PR 失败: {e}"

        return result

    # ------------------------------------------------------------------
    # Full PR submission orchestration
    # ------------------------------------------------------------------

    async def submit_pr(self, staging_dir: Path) -> dict:
        """Orchestrate: ensure fork exists → sync → commit → PR to upstream.

        This is the production entrypoint called by ``/pr提交``.
        """
        user = await self.get_user()
        fork_owner = user["login"]

        # 0. Reject if there's already an open PR from this fork
        existing = await self.list_prs_from_fork(fork_owner, state="open")
        if existing:
            pr_url = existing[0].get("html_url", "")
            return {
                "ok": False, "branch": None, "pr_url": pr_url,
                "message": (
                    f"已有未关闭的 PR ({pr_url})，"
                    f"请等待合并或关闭后再提交新 PR。"
                ),
            }

        # 1. Ensure fork exists
        await self.ensure_fork(fork_owner)

        # 2. Sync fork's main with upstream so memes.json is up to date
        await self.sync_fork_branch(fork_owner, "main")

        # 3. Push staged files and create PR to upstream
        return await self.submit(
            staging_dir,
            target_owner=fork_owner,
            target_repo=self.upstream_repo,
            create_pr=True,
            pr_target_owner=self.upstream_owner,
            pr_target_repo=self.upstream_repo,
        )



