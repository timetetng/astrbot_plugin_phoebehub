"""Vision-based meme naming + description via AstrBot's LLM provider.

Single responsibility: take a local image path + optional hint, return a
short Chinese name and a one-line description, or None if the model fails.

Failures are non-fatal — callers fall back to manual naming.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from astrbot.api.star import Context


GIF_SAMPLE_FRAMES = 5  # frames extracted from animated gifs for vision input
SAMPLE_DIR = Path(tempfile.gettempdir()) / "phoebehub_caption_samples"


@dataclass
class CaptionResult:
    name: str
    description: str


_SYSTEM_PROMPT = """你是菲比表情包的命名助手。请观察图片，给出：

1. `name`：中文或英文的简短名字（2~12 个字），用于文件名。描述图片主要内容，例如 "开心菲比"、"吃瓜菲比"、"哭泣菲比"。不要加后缀，不要包含扩展名。
2. `description`：一句话描述图片的画面或情绪，用于 PR 提交说明。15~60 字。

严格按下面的 JSON 格式输出（不要加任何解释、不要 markdown 代码块）：
{"name": "...", "description": "..."}"""


_USER_PROMPT_TPL = """请为这张表情包命名。
{user_hint_block}
要求：
- name 要简短、辨识度高，适合作为文件名
- description 描述画面内容或情绪
- 严格返回 JSON，不要其它文字"""


def _extract_json(text: str) -> dict | None:
    """Strip markdown fences / leading prose and json.loads. Tolerant."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Last resort: grab the first {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _sanitize_name(name: str) -> str:
    """Mirror main.py's name rules so the model output fits the filename slot."""
    safe = "".join(
        c for c in name
        if c.isalnum() or c in "_-. " or "\u4e00" <= c <= "\u9fff"
    )
    return safe.strip().strip(".")[:32] or "未命名"


def _is_animated(img: Image.Image) -> bool:
    return getattr(img, "is_animated", False)


def _sample_gif_frames(src: Path, n: int = GIF_SAMPLE_FRAMES) -> list[Path]:
    """Extract `n` evenly-spaced frames from an animated gif as temp PNGs.

    Returns absolute paths. Caller is responsible for cleanup; for the bot's
    short-lived caption call, /tmp is fine (rebooted periodically).
    """
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    try:
        img = Image.open(src)
        img.load()
    except Exception:
        return out

    total = getattr(img, "n_frames", 1)
    if total <= 1:
        img.close()
        return out

    # Pick n indices spread across the timeline. For n=5 and total=24 → [0,6,12,17,23].
    if n >= total:
        indices = list(range(total))
    else:
        step = total / n
        indices = [int(i * step) for i in range(n)]
        # Always include the last frame so the loop closure is visible.
        indices[-1] = total - 1

    base = f"{src.stem}_{src.stat().st_mtime_ns}"
    try:
        for i, idx in enumerate(indices):
            img.seek(idx)
            frame = img.convert("RGB").copy()
            dst = SAMPLE_DIR / f"{base}_{i}.png"
            frame.save(dst, "PNG", optimize=True)
            out.append(dst)
    finally:
        img.close()
    return out


def _cleanup_samples(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


async def caption_image(
    context: "Context",
    provider_id: str,
    image_path: str | Path,
    *,
    user_hint: str = "",
) -> CaptionResult | None:
    """Call the configured vision-capable provider. Returns None on any failure."""
    if not provider_id:
        return None

    image_path = Path(image_path)
    if not image_path.exists():
        return None

    # Decide what images to feed the model.
    sample_paths: list[Path] = []
    try:
        try:
            with Image.open(image_path) as probe:
                animated = _is_animated(probe)
        except Exception:
            animated = False

        if animated:
            sample_paths = _sample_gif_frames(image_path)
            image_urls = [str(p) for p in sample_paths] or [str(image_path)]
        else:
            image_urls = [str(image_path)]
    except Exception:
        image_urls = [str(image_path)]

    user_hint_block = (
        f"\n用户提供的名字参考：「{user_hint}」。可以采纳、修改或完全忽略。\n"
        if user_hint.strip()
        else ""
    )
    if animated and sample_paths:
        user_hint_block += (
            f"\n注意：这是动图，提供了 {len(sample_paths)} 帧，请综合整段动画再命名。\n"
        )

    try:
        resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=_USER_PROMPT_TPL.format(user_hint_block=user_hint_block),
            system_prompt=_SYSTEM_PROMPT,
            image_urls=image_urls,
        )
    except Exception:
        return None
    finally:
        if sample_paths:
            _cleanup_samples(sample_paths)

    # Per docs: response.completion_text is the plain string.
    text = getattr(resp, "completion_text", "") or ""
    if not text and getattr(resp, "result_chain", None):
        try:
            text = resp.result_chain.get_plain_text()
        except Exception:
            text = ""

    parsed = _extract_json(text)
    if not parsed:
        return None

    name = _sanitize_name(str(parsed.get("name", "")))
    description = str(parsed.get("description", "")).strip()[:200]
    if not name:
        return None

    return CaptionResult(name=name, description=description)


def _selftest() -> None:
    assert _sanitize_name("开心菲比！") == "开心菲比"
    assert _sanitize_name("..leading dots") == "leading dots"
    assert _sanitize_name("") == "未命名"
    assert _sanitize_name("a" * 100) == "a" * 32

    # JSON variants the model might emit.
    assert _extract_json('{"name": "x", "description": "y"}') == {"name": "x", "description": "y"}
    assert _extract_json("```json\n{\"name\": \"x\"}\n```") == {"name": "x"}
    assert _extract_json('好的，看图后命名：{"name": "开心菲比", "description": "开心笑"}') == {
        "name": "开心菲比", "description": "开心笑"
    }
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None

    # Animated GIF sampling: 5-frame extraction from a 10-frame gif.
    import tempfile

    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        gif_src = td / "anim.gif"
        frames = []
        for i in range(10):
            f = Image.new("RGB", (40, 40), (i * 25, 0, 0))
            frames.append(f)
        frames[0].save(gif_src, save_all=True, append_images=frames[1:], duration=80, loop=0)

        samples = _sample_gif_frames(gif_src, GIF_SAMPLE_FRAMES)
        assert len(samples) == 5, f"expected 5 frames, got {len(samples)}"
        for p in samples:
            assert p.exists()
            assert p.suffix == ".png"
        _cleanup_samples(samples)
        for p in samples:
            assert not p.exists()
        print("gif sampling ok: 5 frames extracted and cleaned")

    print("captioner selftest passed")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m captioner --selftest", file=sys.stderr)
        sys.exit(1)