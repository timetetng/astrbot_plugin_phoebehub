import asyncio
import difflib
import json
import random
import time
from pathlib import Path
import httpx

try:
    import jieba
except ImportError:
    jieba = None
try:
    from rapidfuzz import fuzz as _fuzz
    from zhconv import convert as _s2t

    _HAS_RAPIDFUZZ = True
except ImportError:
    _fuzz = None
    _s2t = None
    _HAS_RAPIDFUZZ = False
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .phoebehub_preprocess import process as preprocess_image, unique_name
from .phoebehub_captioner import caption_image
from .phoebehub_pr import PhoebeHubPR

MEMES_URL = "https://phoebehub.top/data/memes.json"
BASE_URL = "https://phoebehub.top"
PLUGIN_NAME = "astrbot_plugin_phoebehub"


class PhoebeHubPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        config = config or {}

        self.cache_ttl = int(config.get("cache_ttl", 300))
        self.trigger_keywords = config.get("trigger_keywords", ["啾比"])
        self.proxy = config.get("proxy", "") or None
        self.cache_max_hours = int(config.get("cache_max_hours", 24))
        self.vision_provider_id = config.get("vision_provider_id", "") or ""
        self.github_token = config.get("github_token", "") or ""
        self.github_target_owner = config.get("github_target_owner", "") or ""

        self.image_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "images"
        )
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self.staging_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "staging"
        )
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        cleaned = self._clean_expired_cache()
        if cleaned:
            logger.info(f"[phoebehub] 启动时清理了 {cleaned} 个过期缓存文件")

        self._cache_data = None
        self._cache_time = 0.0
        self._synonyms = None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw_text = event.message_str.strip()
        if raw_text not in self.trigger_keywords:
            return

        t_start = time.time()
        try:
            meme = await self._get_random_meme()
            if meme is None:
                yield event.plain_result("暂时没有表情包哦~菲比还在收集中！")
                event.stop_event()
                return

            title = meme.get("title", "菲比")
            local_path = await self._ensure_local_image(meme["url"], meme)

            if local_path is None:
                logger.error("[phoebehub] 图片下载失败")
                yield event.plain_result("图片下载失败了~ 稍后再试试吧。")
                event.stop_event()
                return

            yield event.chain_result(
                [
                    Comp.Plain(f"{title}\n"),
                    Comp.Image.fromFileSystem(str(local_path)),
                ]
            )

            logger.info(
                f"[phoebehub] 完成: {title} ({local_path.name}), "
                f"总耗时={time.time() - t_start:.1f}s"
            )
            event.stop_event()

        except httpx.HTTPError as e:
            logger.error(f"[phoebehub] 网络请求异常: {e}")
            yield event.plain_result("获取表情包失败，网络好像出了点问题~")
            event.stop_event()
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"[phoebehub] 数据解析异常: {e}")
            yield event.plain_result("表情包数据解析失败，请稍后再试~")
            event.stop_event()
        except Exception as e:
            logger.error(f"[phoebehub] 未知错误: {e}")
            yield event.plain_result("呜哇，出错了~ 请稍后再试。")
            event.stop_event()

    @filter.command("搜比")
    async def search_meme(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=2)
        keyword = parts[1] if len(parts) > 1 else ""
        if not keyword:
            yield event.plain_result("请提供搜索关键词，例如：/搜比 好馋 5")
            event.stop_event()
            return

        limit = 3
        if len(parts) > 2:
            try:
                limit = max(1, min(int(parts[2]), 10))
            except ValueError:
                pass

        try:
            memes = await self._load_memes()
            if not memes:
                yield event.plain_result("暂时没有表情包哦~菲比还在收集中！")
                event.stop_event()
                return

            kw = keyword.strip()
            results = self._fuzzy_match(kw, memes, limit)

            if results:
                best_score = results[0][1]
                header = (
                    "找到以下表情包："
                    if best_score == 1.0
                    else "未找到完全匹配的表情包，以下为相似结果："
                )
                lines = [header]
                chain = []
                for i, (title, score) in enumerate(results, 1):
                    lines.append(f"{i}. {title}（相似度 {score:.0%}）")
                msg = "\n".join(lines)
                chain.append(Comp.Plain(msg + "\n"))
                for title, _ in results:
                    meme = next((m for m in memes if m.get("title") == title), None)
                    if meme:
                        path = await self._ensure_local_image(meme["url"], meme)
                        if path:
                            chain.append(Comp.Image.fromFileSystem(str(path)))
                yield event.chain_result(chain)
            else:
                yield event.plain_result(
                    f"没有找到与「{kw}」相关的表情包，请换个搜索词试试~"
                )

            event.stop_event()

        except Exception as e:
            logger.error(f"[phoebehub] 搜索异常: {e}")
            yield event.plain_result("搜索出错了，请稍后再试~")
            event.stop_event()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("传比")
    async def upload_meme(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        name = parts[1].strip() if len(parts) > 1 else ""
        safe = ""
        if name:
            # Filter out path separators and shell-unfriendly chars; allow CJK + ASCII letters/digits/_-.
            safe = "".join(c for c in name if c.isalnum() or c in "_-. " or "\u4e00" <= c <= "\u9fff")
            safe = safe.strip().strip(".")

        if not safe and not self.vision_provider_id:
            yield event.plain_result(
                "请提供表情包名字，例如：/传比 开心菲比\n"
                "（或在配置里设置 vision_provider_id 以启用 AI 自动命名）"
            )
            event.stop_event()
            return
        if name and not safe:
            yield event.plain_result("名字里至少要有一个有效字符哦~")
            event.stop_event()
            return

        images = [c for c in event.message_obj.message if isinstance(c, Comp.Image)]

        if not images:
            for comp in event.message_obj.message:
                if isinstance(comp, Comp.Reply) and comp.chain:
                    images = [c for c in comp.chain if isinstance(c, Comp.Image)]
                    if images:
                        break

        if not images:
            yield event.plain_result("没找到图片，请把图片和 /传比 一起发出来，或回复一条带图片的消息~")
            event.stop_event()
            return

        taken = self._collect_staging_names()
        uploader = f"{event.get_platform_name() or 'unknown'}:{event.get_sender_id()}"

        results = []
        for idx, img in enumerate(images):
            try:
                src_path = await img.convert_to_file_path()
            except Exception as e:
                logger.warning(f"[phoebehub] 图片下载失败: {e}")
                results.append(("fail", f"下载失败: {e}"))
                continue

            # Caption before preprocess so the model sees the original quality.
            ai_name = ""
            ai_description = ""
            user_hint_for_model = safe if safe else ""
            if len(images) > 1 and safe:
                user_hint_for_model = f"{safe}{idx + 1}"

            if self.vision_provider_id:
                cap = await caption_image(
                    self.context,
                    self.vision_provider_id,
                    src_path,
                    user_hint=user_hint_for_model,
                )
                if cap:
                    ai_name = cap.name
                    ai_description = cap.description

            # User-provided name always takes priority; AI only supplies description.
            if safe:
                stem = safe if len(images) == 1 else f"{safe}{idx + 1}"
            elif ai_name:
                stem = ai_name
            else:
                stem = f"未命名{idx + 1}"
            try:
                proc = preprocess_image(Path(src_path), self.staging_dir, name_stem=stem)
            except Exception as e:
                logger.error(f"[phoebehub] 预处理失败: {e}")
                results.append(("fail", f"{stem}: 预处理失败"))
                continue

            # Dedupe against existing staged filenames + remote meme titles.
            ext = proc.fmt
            final_name = unique_name(taken, stem, ext)
            taken.add(final_name)

            # Rename the file to the deduped name (process() used stem verbatim).
            final_path = self.staging_dir / final_name
            if proc.path != final_path:
                proc.path.rename(final_path)

            sidecar = {
                "name": stem,
                "fmt": ext,
                "original_bytes": proc.original_bytes,
                "final_bytes": proc.final_bytes,
                "width": proc.width,
                "height": proc.height,
                "uploaded_by": uploader,
                "uploaded_at": int(time.time()),
                "source_filename": Path(src_path).name,
                "note": proc.note,
                "ai_description": ai_description,
                "user_hint": safe,
            }
            (self.staging_dir / f"{final_name}.json").write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            desc_snippet = f"  「{ai_description}」" if ai_description else ""
            results.append((
                "ok",
                f"{final_name}  {proc.original_bytes // 1024}KB→{proc.final_bytes // 1024}KB  {proc.width}x{proc.height}{desc_snippet}",
            ))
            logger.info(
                f"[phoebehub] staged: {final_name} "
                f"({proc.original_bytes} → {proc.final_bytes} bytes) by {uploader}"
            )

        lines = []
        for status, text in results:
            mark = "✓" if status == "ok" else "✗"
            lines.append(f"{mark} {text}")
        ok_count = sum(1 for s, _ in results if s == "ok")
        header = (
            f"已暂存 {ok_count}/{len(results)} 张，待 /pr提交 时打包发送。"
            if ok_count
            else "全部失败"
        )
        yield event.plain_result("\n".join([header, *lines]))
        event.stop_event()

    @filter.command("传比列表")
    async def list_staging(self, event: AstrMessageEvent):
        images = sorted(
            f for f in self.staging_dir.iterdir()
            if f.is_file() and f.suffix in (".webp", ".gif", ".jpg", ".jpeg", ".png")
        )
        if not images:
            yield event.plain_result("staging 目录为空，没有待提交的图片～")
            event.stop_event()
            return

        lines = ["当前 staging 中的图片："]
        for idx, img in enumerate(images, 1):
            sidecar_path = self.staging_dir / f"{img.name}.json"
            desc = ""
            if sidecar_path.exists():
                try:
                    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    desc = data.get("ai_description", "")
                except Exception:
                    pass
            desc_snippet = f"  「{desc}」" if desc else ""
            lines.append(f"#{idx}  {img.name}{desc_snippet}")
        lines.append("")
        lines.append("使用 /传比改 <序号> [名字=xxx] [描述=xxx] 修改")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("传比改")
    async def edit_staging(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result("用法：/传比改 <序号> [名字=xxx] [描述=xxx]")
            event.stop_event()
            return

        try:
            idx = int(parts[1]) - 1
        except ValueError:
            yield event.plain_result("序号必须是数字")
            event.stop_event()
            return

        images = sorted(
            f for f in self.staging_dir.iterdir()
            if f.is_file() and f.suffix in (".webp", ".gif", ".jpg", ".jpeg", ".png")
        )
        if idx < 0 or idx >= len(images):
            yield event.plain_result(f"序号超出范围，当前共 {len(images)} 张图片")
            event.stop_event()
            return

        img = images[idx]
        sidecar_path = self.staging_dir / f"{img.name}.json"
        if not sidecar_path.exists():
            yield event.plain_result(f"找不到 {img.name} 的元数据文件")
            event.stop_event()
            return

        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as e:
            yield event.plain_result(f"读取元数据失败: {e}")
            event.stop_event()
            return

        old_name = sidecar.get("name", "")
        fmt = sidecar.get("fmt", img.suffix.lstrip("."))

        if len(parts) < 3:
            desc = sidecar.get("ai_description", "")
            info = [f"当前 #{parts[1]}：", f"  文件：{img.name}", f"  名字：{old_name}"]
            if desc:
                info.append(f"  描述：{desc}")
            yield event.plain_result("\n".join(info))
            event.stop_event()
            return

        new_name = None
        new_desc = None
        for pair in parts[2].split():
            if "=" not in pair:
                continue
            key, val = pair.split("=", 1)
            val = val.strip()
            if key == "名字" and val:
                new_name = val
            elif key == "描述" and val:
                new_desc = val

        if new_name is None and new_desc is None:
            yield event.plain_result("未识别到有效的修改项，格式：名字=xxx 描述=xxx")
            event.stop_event()
            return

        changes = []

        if new_name is not None:
            new_filename = f"{new_name}.{fmt}"
            existing = {
                f.name for f in self.staging_dir.iterdir()
                if f.is_file() and f.name != img.name and f.suffix != ".json"
            }
            if new_filename in existing:
                yield event.plain_result(f"名字「{new_name}」已在 staging 中，请换一个")
                event.stop_event()
                return

            old_filename = img.name
            new_img_path = self.staging_dir / new_filename
            new_sidecar_path = self.staging_dir / f"{new_filename}.json"

            img.rename(new_img_path)
            sidecar_path.rename(new_sidecar_path)

            sidecar["name"] = new_name

            changes.append(f"名字: {old_name} → {new_name}")
            changes.append(f"文件: {old_filename} → {new_filename}")

            img = new_img_path
            sidecar_path = new_sidecar_path

        if new_desc is not None:
            old_desc = sidecar.get("ai_description", "")
            sidecar["ai_description"] = new_desc
            changes.append(f"描述: {old_desc or '空'} → {new_desc}")

        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        yield event.plain_result("修改完成：\n" + "\n".join(changes))
        event.stop_event()

    @filter.command("pr提交")
    async def pr_submit(self, event: AstrMessageEvent):
        if not self.github_token:
            yield event.plain_result("请先在插件配置中设置 github_token 才能自动提交 PR～")
            event.stop_event()
            return

        staged = [f for f in self.staging_dir.iterdir() if f.is_file()]
        images = [f for f in staged if f.suffix in (".webp", ".gif", ".jpg", ".jpeg", ".png")]
        if not images:
            yield event.plain_result("staging 目录为空，请先用 /传比 上传图片～")
            event.stop_event()
            return

        yield event.plain_result(f"正在提交 {len(images)} 张图片到 GitHub，请稍候…")

        target_owner = self.github_target_owner or ""
        try:
            pr_client = PhoebeHubPR(
                self.github_token,
                proxy=self.proxy,
            )

            if target_owner:
                result = await pr_client.submit(
                    self.staging_dir,
                    target_owner=target_owner,
                    target_repo="Phoebe-Hub",
                    create_pr=True,
                )
            else:
                result = await pr_client.submit_pr(self.staging_dir)

        except Exception as e:
            logger.error(f"[phoebehub] PR 提交失败: {e}")
            yield event.plain_result(f"提交失败: {e}")
            event.stop_event()
            return

        if result["ok"]:
            lines = [
                f"✓ 已提交 {len(images)} 张图片",
            ]
            branch_info = result.get("branch")
            if branch_info:
                lines.append(f"  分支: {branch_info}")
            if result.get("pr_url"):
                lines.append(f"  PR: {result['pr_url']}")
            else:
                pr_owner = target_owner or "fork"
                lines.append(f"  目标: {pr_owner}/Phoebe-Hub")

            yield event.plain_result("\n".join(lines))

            for f in staged:
                f.unlink()
            logger.info("[phoebehub] staging 已清理")
        else:
            yield event.plain_result(f"提交失败: {result['message']}")

        event.stop_event()

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": 120}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def _load_memes(self) -> list:
        now = time.time()
        if self._cache_data is None or now - self._cache_time > self.cache_ttl:
            t0 = time.time()
            async with self._client() as client:
                resp = await client.get(MEMES_URL, timeout=15)
                resp.raise_for_status()
                self._cache_data = resp.json()
            memes = self._cache_data.get("memes", [])
            logger.info(
                f"[phoebehub] memes.json 加载完成: "
                f"{len(memes)} 个, "
                f"耗时={time.time() - t0:.1f}s"
            )
            self._cache_time = now
        return self._cache_data.get("memes", [])

    async def _get_random_meme(self) -> dict | None:
        memes = await self._load_memes()
        return random.choice(memes) if memes else None

    def _fuzzy_match(self, keyword: str, memes: list, limit: int = 3) -> list:
        if _HAS_RAPIDFUZZ:
            return self._fuzzy_match_rapidfuzz(keyword, memes, limit)
        logger.warning(
            "[phoebehub] rapidfuzz/zhconv 未安装，已降级为 difflib 搜索，"
            "效果较差。请执行: uv add rapidfuzz zhconv jieba"
        )
        return self._fuzzy_match_fallback(keyword, memes, limit)

    def _fuzzy_match_rapidfuzz(self, keyword: str, memes: list, limit: int = 3) -> list:
        kw_norm = _s2t(keyword, "zh-tw").lower()
        kw_tokens = set(jieba.cut(keyword)) if jieba else set()
        synonyms = self._load_synonyms()

        best = []
        for m in memes:
            title = m.get("title", "")
            if not title:
                continue

            title_norm = _s2t(title, "zh-tw").lower()
            base = (
                max(
                    _fuzz.ratio(kw_norm, title_norm),
                    _fuzz.partial_ratio(kw_norm, title_norm),
                    _fuzz.token_sort_ratio(kw_norm, title_norm),
                    _fuzz.token_set_ratio(kw_norm, title_norm),
                )
                / 100
            )

            syn_score = 0.0
            if base < 0.6 and kw_tokens and synonyms:
                title_tokens = set(jieba.cut(title))
                for qt in kw_tokens:
                    if qt in synonyms and synonyms[qt] & title_tokens:
                        syn_score = 0.7
                        break

            score = max(base, syn_score)
            if score >= 0.4:
                best.append((title, round(score, 3)))

        best.sort(key=lambda x: -x[1])
        return best[:limit]

    def _fuzzy_match_fallback(self, keyword: str, memes: list, limit: int = 3) -> list:
        titles = [m.get("title", "") for m in memes if m.get("title")]
        close = difflib.get_close_matches(keyword, titles, n=limit, cutoff=0.3)
        return [
            (t, difflib.SequenceMatcher(None, keyword.lower(), t.lower()).ratio())
            for t in close
        ][:limit]

    def _load_synonyms(self) -> dict:
        if self._synonyms is not None:
            return self._synonyms

        syn_path = Path(__file__).parent / "meme_synonyms.json"
        if not syn_path.exists():
            self._synonyms = {}
            return self._synonyms

        try:
            with open(syn_path, encoding="utf-8") as f:
                raw = json.load(f)

            syns = {}
            for word, syn_list in raw.items():
                if word not in syns:
                    syns[word] = set()
                for s in syn_list:
                    syns[word].add(s)
                    if s not in syns:
                        syns[s] = set()
                    syns[s].add(word)

            self._synonyms = syns
        except Exception as e:
            logger.warning(f"[phoebehub] 同义词文件加载失败: {e}")
            self._synonyms = {}

        return self._synonyms

    def _collect_staging_names(self) -> set[str]:
        """Filenames (with ext) already taken — either locally staged or remote.
        Used to dedup uploads so PR filenames don't collide."""
        taken: set[str] = set()
        if self.staging_dir.exists():
            for f in self.staging_dir.iterdir():
                if f.is_file() and f.suffix != ".json":
                    taken.add(f.name)

        # Pull from the in-memory cache if available; otherwise empty.
        if self._cache_data:
            for m in self._cache_data.get("memes", []):
                url = m.get("url", "")
                if url:
                    taken.add(Path(url).name)

        return taken

    async def _ensure_local_image(self, url_path: str, meme: dict) -> Path | None:
        local_path = self.image_dir / f"{meme['id']}{Path(url_path).suffix}"
        if local_path.exists():
            return local_path

        max_retries = 3
        for attempt in range(max_retries):
            t0 = time.time()
            try:
                async with self._client() as client:
                    resp = await client.get(f"{BASE_URL}/{url_path}")
                    resp.raise_for_status()
                    local_path.write_bytes(resp.content)
                logger.info(
                    f"[phoebehub] 图片下载完成: {local_path.name}, "
                    f"{len(resp.content)} bytes, "
                    f"耗时={time.time() - t0:.1f}s"
                )
                return local_path
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2**attempt
                    logger.warning(
                        f"[phoebehub] 图片下载失败(第{attempt + 1}/{max_retries}次): {e}, "
                        f"{wait}s后重试..."
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"[phoebehub] 图片下载失败(已重试{max_retries}次): {e}"
                    )

        if local_path.exists():
            logger.warning(f"[phoebehub] 使用本地缓存保底: {local_path.name}")
            return local_path

        return None

    def _clean_expired_cache(self) -> int:
        if self.cache_max_hours <= 0 or not self.image_dir.exists():
            return 0
        cutoff = time.time() - self.cache_max_hours * 3600
        count = 0
        for f in self.image_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        return count

    async def terminate(self):
        pass
