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

MEMES_URL = "https://phoebehub.top/data/memes.json"
BASE_URL = "https://phoebehub.top"
PLUGIN_NAME = "astrbot_plugin_phoebehub"


class PhoebeHubPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        config = config or {}

        self.cache_ttl = int(config.get("cache_ttl", 300))
        self.trigger_keywords = config.get("trigger_keywords", ["啾比", "jiubi", "jbi"])
        self.proxy = config.get("proxy", "") or None
        self.cache_max_hours = int(config.get("cache_max_hours", 24))

        self.image_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "images"
        )
        self.image_dir.mkdir(parents=True, exist_ok=True)

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
