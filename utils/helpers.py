"""Stateless helpers extracted from main.py: constants, fuzzy matching, file ops."""

from __future__ import annotations

import difflib
import json
import time
from pathlib import Path

MEMES_URL = "https://phoebehub.top/data/memes.json"
BASE_URL = "https://phoebehub.top"
PLUGIN_NAME = "astrbot_plugin_phoebehub"

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


def load_synonyms(base_dir: Path) -> dict:
    """Load and build bidirectional synonym map from meme_synonyms.json."""
    syn_path = base_dir / "meme_synonyms.json"
    if not syn_path.exists():
        return {}
    try:
        with open(syn_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    syns: dict[str, set[str]] = {}
    for word, syn_list in raw.items():
        if word not in syns:
            syns[word] = set()
        for s in syn_list:
            syns[word].add(s)
            if s not in syns:
                syns[s] = set()
            syns[s].add(word)
    return syns


def collect_staging_names(staging_dir: Path, cache_data: dict | None) -> set[str]:
    """Filenames already taken — locally staged or remote. Used for dedup."""
    taken: set[str] = set()
    if staging_dir.exists():
        for f in staging_dir.iterdir():
            if f.is_file() and f.suffix != ".json":
                taken.add(f.name)
    if cache_data:
        for m in cache_data.get("memes", []):
            url = m.get("url", "")
            if url:
                taken.add(Path(url).name)
    return taken


def clean_expired_cache(image_dir: Path, max_hours: int) -> int:
    """Remove cached image files older than max_hours. Returns count."""
    if max_hours <= 0 or not image_dir.exists():
        return 0
    cutoff = time.time() - max_hours * 3600
    count = 0
    for f in image_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    return count


# ------------------------------------------------------------------
# Fuzzy matching
# ------------------------------------------------------------------


def fuzzy_match(
    keyword: str, memes: list, limit: int, algorithm: str, synonyms: dict
) -> list:
    """Entry point for fuzzy matching against meme titles."""
    if not _HAS_RAPIDFUZZ:
        return fuzzy_match_fallback(keyword, memes, limit)
    if algorithm == "v1":
        return fuzzy_match_rapidfuzz_v1(keyword, memes, limit, synonyms)
    return fuzzy_match_rapidfuzz_v2(keyword, memes, limit, synonyms)


def fuzzy_match_rapidfuzz_v1(
    keyword: str, memes: list, limit: int, synonyms: dict
) -> list:
    kw_norm = _s2t(keyword, "zh-tw").lower()
    kw_tokens = set(jieba.cut(keyword)) if jieba else set()

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


def fuzzy_match_rapidfuzz_v2(
    keyword: str, memes: list, limit: int, synonyms: dict
) -> list:
    kw_norm = _s2t(keyword, "zh-tw").lower()
    kw_tokens = set(jieba.cut(keyword)) if jieba else set()

    best = []
    for m in memes:
        title = m.get("title", "")
        if not title:
            continue
        title_norm = _s2t(title, "zh-tw").lower()

        if kw_norm in title_norm:
            base = 1.0
        else:
            ratio_score = _fuzz.ratio(kw_norm, title_norm) / 100
            token_set_score = _fuzz.token_set_ratio(kw_norm, title_norm) / 100
            if token_set_score > ratio_score * 1.5:
                token_set_score = ratio_score * 1.5
            base = max(ratio_score, token_set_score)

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


def fuzzy_match_fallback(keyword: str, memes: list, limit: int) -> list:
    from astrbot.api import logger

    logger.warning(
        "[phoebehub] rapidfuzz/zhconv 未安装，已降级为 difflib 搜索，"
        "效果较差。请执行: uv add rapidfuzz zhconv jieba"
    )
    titles = [m.get("title", "") for m in memes if m.get("title")]
    close = difflib.get_close_matches(keyword, titles, n=limit, cutoff=0.3)
    return [
        (t, difflib.SequenceMatcher(None, keyword.lower(), t.lower()).ratio())
        for t in close
    ][:limit]
