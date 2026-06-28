"""Image preprocessing for meme uploads.

Pure functions, no AstrBot dependency. Tested via `python -m preprocess --selftest`.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# ponytail: 2MB ceiling per phoebehub convention. Hard cap to avoid runaway loops.
MAX_BYTES = 2 * 1024 * 1024
WEBP_QUALITY = 80
GIF_MAX_FRAMES = 200  # ponytail: anything bigger is almost certainly a video mislabeled as gif


@dataclass
class ProcessResult:
    path: Path
    original_bytes: int
    final_bytes: int
    fmt: str  # "webp" | "gif"
    width: int
    height: int
    note: str = ""  # any human-readable caveat


def _is_animated_gif(img: Image.Image) -> bool:
    return getattr(img, "is_animated", False)


def _has_gifsicle() -> bool:
    return shutil.which("gifsicle") is not None


def _gifsicle_optimize(src: Path, dst: Path) -> bool:
    # ponytail: gifsicle -O3 typically cuts 30-60% on palette-heavy gifs. Best-effort.
    try:
        subprocess.run(
            ["gifsicle", "-O3", "--lossy=30", "-o", str(dst), str(src)],
            check=True,
            timeout=30,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _resize_to_fit(img: Image.Image, max_pixels: int) -> Image.Image:
    w, h = img.size
    if w * h <= max_pixels:
        return img
    scale = (max_pixels / (w * h)) ** 0.5
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return img.resize((nw, nh), Image.LANCZOS)


def _save_webp_iter(src_img: Image.Image, dst: Path) -> int:
    """Try webp at decreasing sizes until under MAX_BYTES. Returns final bytes."""
    img = src_img.convert("RGBA") if src_img.mode in ("RGBA", "LA", "P") else src_img.convert("RGB")
    # Strip EXIF by re-encoding through a fresh image (no getexif() round-trip).
    img.info.pop("exif", None)

    # ponytail: three resize tiers — 0.5MP, 0.25MP, 0.12MP. Stickers don't need 1MP+;
    # adversarial noise gets huge at high res. Quality stays at 80, not downgraded.
    for max_px in (500_000, 250_000, 120_000):
        candidate = _resize_to_fit(img, max_px)
        candidate.save(dst, "WEBP", quality=WEBP_QUALITY, method=6)
        if dst.stat().st_size <= MAX_BYTES:
            return dst.stat().st_size
    return dst.stat().st_size


def _save_gif_iter(src_img: Image.Image, src_path: Path, dst: Path) -> int:
    """Save animated gif. Try gifsicle first, then Pillow optimize, then resize."""
    if _has_gifsicle() and _gifsicle_optimize(src_path, dst):
        if dst.stat().st_size <= MAX_BYTES:
            return dst.stat().st_size

    # Pillow fallback: copy + optimize. Frame-count cap to bound work.
    frames = getattr(src_img, "n_frames", 1)
    if frames > GIF_MAX_FRAMES:
        # ponytail: too many frames, decimate by skipping every other.
        every = max(1, frames // GIF_MAX_FRAMES)
        src_img.seek(0)
        out = Image.new(src_img.mode, src_img.size)
        out_frames = []
        for i in range(0, frames, every):
            src_img.seek(i)
            out_frames.append(src_img.convert("RGB").copy())
        out_frames[0].save(
            dst, format="GIF", save_all=True, append_images=out_frames[1:],
            optimize=True, duration=src_img.info.get("duration", 100), loop=0,
        )
    else:
            src_img.seek(0)
            out_frames = []
            for i in range(frames):
                src_img.seek(i)
                out_frames.append(src_img.convert("RGB").copy())
            out_frames[0].save(
                dst, format="GIF", save_all=True, append_images=out_frames[1:],
                optimize=True, duration=src_img.info.get("duration", 100), loop=0,
            )

    if dst.stat().st_size <= MAX_BYTES:
        return dst.stat().st_size

    # Last resort: resize each frame down.
    scale = 0.75
    while scale > 0.3 and dst.stat().st_size > MAX_BYTES:
        new_w = max(1, int(src_img.width * scale))
        new_h = max(1, int(src_img.height * scale))
        src_img.seek(0)
        out_frames = []
        for i in range(frames):
            src_img.seek(i)
            out_frames.append(src_img.convert("RGB").resize((new_w, new_h), Image.LANCZOS))
        out_frames[0].save(
            dst, format="GIF", save_all=True, append_images=out_frames[1:],
            optimize=True, duration=src_img.info.get("duration", 100), loop=0,
        )
        scale -= 0.15

    return dst.stat().st_size


def process(src: Path, dst_dir: Path, *, name_stem: str) -> ProcessResult:
    """Process an image into a webp (static) or gif (animated) under MAX_BYTES.

    `name_stem` is the user-chosen filename without extension. Final filename
    is `<name_stem>.webp` or `<name_stem>.gif` in `dst_dir`.
    """
    src = Path(src)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    original_bytes = src.stat().st_size

    img = Image.open(src)
    img.load()  # ponytail: force decode now so we can seek on animated gifs

    is_anim = _is_animated_gif(img)

    if is_anim:
        dst = dst_dir / f"{name_stem}.gif"
        tmp = dst_dir / f".{name_stem}.gif.tmp"
        final_bytes = _save_gif_iter(img, src, tmp)
        tmp.replace(dst)
        fmt = "gif"
        note = "animated gif"
    else:
        dst = dst_dir / f"{name_stem}.webp"
        tmp = dst_dir / f".{name_stem}.webp.tmp"
        final_bytes = _save_webp_iter(img, tmp)
        tmp.replace(dst)
        fmt = "webp"
        note = "converted to webp" if src.suffix.lower() != ".webp" else ""

    img.close()
    final = Image.open(dst)
    w, h = final.size
    final.close()

    return ProcessResult(
        path=dst,
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        fmt=fmt,
        width=w,
        height=h,
        note=note,
    )


def unique_name(taken: set[str], stem: str, ext: str) -> str:
    """Return `<stem>` or `<stem><n>` where n is the smallest int making the name
    not collide with anything in `taken` (compared as `<name>.<ext>`)."""
    full = f"{stem}.{ext}"
    if full not in taken:
        return full
    n = 1
    while True:
        candidate = f"{stem}{n}.{ext}"
        if candidate not in taken:
            return candidate
        n += 1
        if n > 9999:  # ponytail: bail; if you've staged 9999 dupes something else is wrong.
            raise RuntimeError(f"too many duplicates for stem={stem}")


def _selftest() -> None:
    """Tiny end-to-end check. Run: `python -m preprocess --selftest`"""
    import json
    import tempfile

    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # 1. Static oversize RGB → should compress to webp well under 2MB
        big = Image.new("RGB", (3000, 3000), (255, 0, 0))
        # Draw noise so it's not solid (solid compresses unrealistically small).
        import random
        rng = random.Random(0)
        for x in range(0, 3000, 4):
            for y in range(0, 3000, 4):
                big.putpixel((x, y), (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
        src = td / "big.png"
        big.save(src)

        out_dir = td / "out"
        r = process(src, out_dir, name_stem="测试")
        assert r.fmt == "webp", f"static should be webp, got {r.fmt}"
        assert r.final_bytes <= MAX_BYTES, f"{r.final_bytes} > {MAX_BYTES}"
        assert r.path.suffix == ".webp"
        print(f"static ok: {r.original_bytes} → {r.final_bytes} bytes ({r.width}x{r.height})")

        # 2. Animated gif → should stay gif
        g = Image.new("RGB", (200, 200), (0, 255, 0))
        frames = [g.copy() for _ in range(8)]
        for i, f in enumerate(frames):
            for x in range(200):
                f.putpixel((x, (i * 25 + x) % 200), (255, 0, 0))
        gif_src = td / "anim.gif"
        frames[0].save(gif_src, save_all=True, append_images=frames[1:], duration=80, loop=0)
        r2 = process(gif_src, out_dir, name_stem="动图")
        assert r2.fmt == "gif", f"animated should be gif, got {r2.fmt}"
        assert r2.final_bytes <= MAX_BYTES, f"{r2.final_bytes} > {MAX_BYTES}"
        print(f"animated ok: {r2.original_bytes} → {r2.final_bytes} bytes")

        # 3. unique_name dedup
        taken = {"开心菲比.webp", "开心菲比.gif"}
        assert unique_name(taken, "开心菲比", "webp") == "开心菲比1.webp"
        taken.add("开心菲比1.webp")
        assert unique_name(taken, "开心菲比", "webp") == "开心菲比2.webp"
        assert unique_name(set(), "新图", "gif") == "新图.gif"
        print("unique_name ok")

    print("selftest passed")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m preprocess --selftest", file=sys.stderr)
        sys.exit(1)