import json
import random
import time
from pathlib import Path
import httpx
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
        self.trigger_keywords = config.get(
            "trigger_keywords", ["啾比", "jiubi", "jbi"]
        )
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

            yield event.chain_result([
                Comp.Plain(f"{title}\n"),
                Comp.Image.fromFileSystem(str(local_path)),
            ])

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

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": 120}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def _get_random_meme(self) -> dict | None:
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

        memes = self._cache_data.get("memes", [])
        return random.choice(memes) if memes else None

    async def _ensure_local_image(self, url_path: str, meme: dict) -> Path | None:
        local_path = self.image_dir / f"{meme['id']}{Path(url_path).suffix}"
        if local_path.exists():
            return local_path

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
            logger.error(f"[phoebehub] 图片下载失败: {e}")
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
