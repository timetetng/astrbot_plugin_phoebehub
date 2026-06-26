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
        self.download_local = bool(config.get("download_local", False))
        self.trigger_keywords = config.get(
            "trigger_keywords", ["啾比", "jiubi", "jbi"]
        )
        self.proxy = config.get("proxy", "") or None

        # Local image cache directory
        self.image_dir: Path | None = None
        if self.download_local:
            self.image_dir = (
                get_astrbot_data_path()
                / "plugin_data"
                / PLUGIN_NAME
                / "images"
            )
            self.image_dir.mkdir(parents=True, exist_ok=True)

        # In-memory memes.json cache
        self._cache_data = None
        self._cache_time = 0.0

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw_text = event.message_str.strip()
        if raw_text not in self.trigger_keywords:
            return

        try:
            meme = await self._get_random_meme()
            if meme is None:
                yield event.plain_result("暂时没有表情包哦~菲比还在收集中！")
                event.stop_event()
                return

            title = meme.get("title", "菲比")
            url_path = meme["url"]

            if self.download_local and self.image_dir:
                local_path = await self._ensure_local_image(url_path, meme)
                if local_path:
                    yield event.chain_result([
                        Comp.Plain(f"啾比~ {title}\n"),
                        Comp.Image.fromFileSystem(str(local_path)),
                    ])
                else:
                    yield event.plain_result(f"啾比~ {title}")
                    yield event.image_result(f"{BASE_URL}/{url_path}")
            else:
                yield event.plain_result(f"啾比~ {title}")
                yield event.image_result(f"{BASE_URL}/{url_path}")

            event.stop_event()
        except httpx.HTTPError:
            logger.error("获取菲比表情包失败: 网络请求异常")
            yield event.plain_result("获取表情包失败，网络好像出了点问题~")
            event.stop_event()
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"获取菲比表情包失败: 数据解析异常 {e}")
            yield event.plain_result("表情包数据解析失败，请稍后再试~")
            event.stop_event()
        except Exception as e:
            logger.error(f"获取菲比表情包失败: {e}")
            yield event.plain_result("呜哇，出错了~ 请稍后再试。")
            event.stop_event()

    def _client(self) -> httpx.AsyncClient:
        kwargs = {"timeout": 30}
        if self.proxy:
            kwargs["proxies"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    async def _get_random_meme(self) -> dict | None:
        now = time.time()
        if self._cache_data is None or now - self._cache_time > self.cache_ttl:
            async with self._client() as client:
                resp = await client.get(MEMES_URL, timeout=10)
                resp.raise_for_status()
                self._cache_data = resp.json()
                self._cache_time = now

        memes = self._cache_data.get("memes", [])
        return random.choice(memes) if memes else None

    async def _ensure_local_image(self, url_path: str, meme: dict) -> Path | None:
        ext = Path(url_path).suffix
        local_path = self.image_dir / f"{meme['id']}{ext}"

        if local_path.exists():
            return local_path

        try:
            async with self._client() as client:
                resp = await client.get(f"{BASE_URL}/{url_path}")
                resp.raise_for_status()
                local_path.write_bytes(resp.content)
                logger.info(f"已缓存图片到本地: {local_path}")
                return local_path
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            return None

    async def terminate(self):
        pass
