"""astrbot_plugin_pindo — 拼豆图纸生成器插件。

收图 → 按品牌色板生成带色号与用量统计的拼豆图纸 PNG。
引擎移植自 LunarXuan/Pindo（GPL-3.0），本插件同样以 GPL-3.0 发布。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import AstrBotConfig, Context, Star
import astrbot.api.message_components as Comp

import pindo_core
from pindo_core import BRAND_LABELS, BRAND_ORDER, COMMAND_RE, resolve_brand
from web_server import SiteServer

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # 兜底：AstrBot 改路径时插件仍可用
    import tempfile

    def get_astrbot_temp_path() -> str:
        return tempfile.gettempdir()

DEFAULT_BASE_WIDTH = 35
DEFAULT_MAX_COLORS = 16


class PindoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.default_brand = str(config.get("default_brand", "mard"))
        self.watermark = str(config.get("robot_watermark", "")).strip()
        self.wait_timeout = int(config.get("wait_timeout", 30))
        self.max_long_edge = int(config.get("max_long_edge", 120))
        self._pending: dict[tuple[str, str], tuple[asyncio.TimerHandle, str]] = {}
        self._site: SiteServer | None = None

    async def initialize(self):
        if bool(self.config.get("enable_site", True)):
            static_dir = Path(__file__).resolve().parent / "webui"
            try:
                self._site = SiteServer(
                    static_dir,
                    host=str(self.config.get("site_host", "0.0.0.0")),
                    port=int(self.config.get("site_port", 8765)),
                )
                await self._site.start()
            except Exception as e:
                logger.error(f"Pindo WebUI 启动失败（插件其他功能不受影响）: {e}", exc_info=True)
                self._site = None

    async def terminate(self):
        for timer, _brand in list(self._pending.values()):
            timer.cancel()
        self._pending.clear()
        if self._site:
            try:
                await self._site.stop()
            except Exception as e:
                logger.warning(f"Pindo WebUI 停止异常（已忽略）: {e}")
            self._site = None

    # ---------------------------------------------------------- 命令

    @filter.regex(COMMAND_RE.pattern)
    async def pindou(self, event: AstrMessageEvent):
        """拼豆：把图片转成拼豆图纸（拼豆 / 拼豆 MARD / 拼豆品牌方）"""
        raw = event.message_str or ""
        m = COMMAND_RE.match(raw)
        arg_brand = m.group(1) if m else None
        if arg_brand == "品牌方":
            lines = [f"{i}. {BRAND_LABELS[bid]}" for i, bid in enumerate(BRAND_ORDER, 1)]
            yield event.plain_result("\n".join(lines) + "\n发送「拼豆 序号」或「拼豆 品牌名」选择品牌")
            return

        brand_id = (resolve_brand(arg_brand) if arg_brand else None) or self._default_brand()
        if arg_brand and resolve_brand(arg_brand) is None:
            yield event.plain_result(f"未知的品牌「{arg_brand}」，发送「拼豆品牌方」查看可选品牌")
            return

        image = self._extract_first_image(event)
        if image is None:
            image = await self._avatar_image(event)
        if image is not None:
            async for r in self._process(event, image, brand_id):
                yield r
            return

        self._start_waiting(event, brand_id)
        yield event.plain_result(f"请在 {self.wait_timeout} 秒内发送要处理的图片（发送「撤销」取消）")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """等待补图窗口：只认发起人本人的后续消息。"""
        if COMMAND_RE.match(event.message_str or ""):
            return
        key = self._event_key(event)
        pending = self._pending.get(key)
        if pending is None:
            return
        text = (event.message_str or "").strip()
        if text == "撤销":
            self._clear_pending(key)
            yield event.plain_result("已取消")
            return
        image = self._extract_first_image(event)
        if image is None:
            return
        self._clear_pending(key)
        async for r in self._process(event, image, pending[1]):
            yield r

    # ---------------------------------------------------------- 核心

    def _default_brand(self) -> str:
        return resolve_brand(self.default_brand) or "mard"

    @staticmethod
    def _event_key(event: AstrMessageEvent) -> tuple[str, str]:
        return event.unified_msg_origin, str(event.get_sender_id() or "")

    @staticmethod
    def _extract_first_image(event: AstrMessageEvent) -> Comp.Image | None:
        """优先级：消息内图片 > 引用图。群头像由命令入口单独兜底。"""
        chain = event.get_messages()
        for seg in chain:
            if isinstance(seg, Comp.Image):
                return seg
        for seg in chain:
            reply_chain = getattr(seg, "chain", None)
            if isinstance(seg, Comp.Reply) and reply_chain:
                for sub in reply_chain:
                    if isinstance(sub, Comp.Image):
                        return sub
        return None

    def _start_waiting(self, event: AstrMessageEvent, brand_id: str) -> None:
        key = self._event_key(event)
        self._clear_pending(key)
        loop = asyncio.get_running_loop()
        self._pending[key] = (loop.call_later(self.wait_timeout, self._expire, key), brand_id)

    def _expire(self, key: tuple[str, str]) -> None:
        """超时静默结束，不发送任何提示。"""
        self._pending.pop(key, None)

    def _clear_pending(self, key: tuple[str, str]) -> None:
        entry = self._pending.pop(key, None)
        if entry:
            entry[0].cancel()

    async def _avatar_image(self, event: AstrMessageEvent) -> Comp.Image | None:
        """QQ 平台兜底来源：发起人自己的群头像。"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return None
            sender_id = event.get_sender_id()
            if not sender_id or not str(sender_id).isdigit():
                return None
            return Comp.Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=640")
        except Exception:
            return None

    async def _process(self, event: AstrMessageEvent, image: Comp.Image, brand_id: str):
        try:
            path = await image.convert_to_file_path()
        except Exception as e:
            logger.warning(f"Pindo 图片获取失败: {e}")
            yield event.plain_result("图片下载失败，请换一张或重新发送")
            return

        out_dir = Path(get_astrbot_temp_path()) / "pindou"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"pindou_{uuid.uuid4().hex}.png"

        try:
            def work() -> None:
                result = pindo_core.generate(
                    path, brand_id,
                    base_width=DEFAULT_BASE_WIDTH,
                    max_long_edge=self.max_long_edge,
                    max_colors=DEFAULT_MAX_COLORS,
                )
                rendered = pindo_core.render_pattern_png(result, watermark=self.watermark)
                rendered.save(out_path, format="PNG")

            await asyncio.to_thread(work)
        except Exception as e:
            name = type(e).__name__
            if name in ("UnidentifiedImageError", "DecompressionBombError") or "cannot identify image" in str(e):
                yield event.plain_result("图片解码失败，请发送常见的图片格式（JPG/PNG/WebP）")
            else:
                logger.error(f"Pindo 图纸生成失败: {e}", exc_info=True)
                yield event.plain_result("图片处理失败，请换一张图片试试")
            return

        yield event.image_result(str(out_path))
