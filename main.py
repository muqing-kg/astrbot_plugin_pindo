"""astrbot_plugin_pindo — 拼豆图纸生成器插件。

收图 → 按品牌色板生成带色号与用量统计的拼豆图纸 PNG。
引擎移植自 LunarXuan/Pindo（GPL-3.0），本插件同样以 GPL-3.0 发布。

回复一律经 event.send(MessageChain) 直发：绕过框架结果装饰管线
（不引用、不 @、不受其他插件 on_decorating_result 钩子影响），
并在处理后 stop_event 阻断后续插件与 LLM 兜底。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

try:  # AstrBotConfig：新版在 astrbot.api，部分版本从 star 命名空间导出
    from astrbot.api.star import AstrBotConfig
except ImportError:
    from astrbot.api import AstrBotConfig

try:  # MessageChain：v4 由 astrbot.api.event 导出，兜底 core 路径
    from astrbot.api.event import MessageChain
except ImportError:
    from astrbot.core.message.message_event_result import MessageChain

from . import pindo_core
from .pindo_core import BRAND_LABELS, BRAND_ORDER, COMMAND_RE, resolve_brand
from .web_server import SiteServer

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

    # ---------------------------------------------------------- 直发回复

    async def _reply_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain().message(text))

    async def _reply_image(self, event: AstrMessageEvent, path: Path) -> None:
        await event.send(MessageChain().file_image(str(path)))

    # ---------------------------------------------------------- 命令

    @filter.regex(COMMAND_RE.pattern)
    async def pindou(self, event: AstrMessageEvent):
        """拼豆：把图片转成拼豆图纸（拼豆 / 拼豆 MARD / 拼豆品牌方）"""
        raw = event.message_str or ""
        m = COMMAND_RE.match(raw)
        arg = (m.group(1) or m.group(2)) if m else None
        if arg == "品牌方":
            lines = [f"{i}. {BRAND_LABELS[bid]}" for i, bid in enumerate(BRAND_ORDER, 1)]
            await self._reply_text(event, "\n".join(lines) + "\n发送「拼豆 序号」或「拼豆 品牌名」选择品牌")
            event.stop_event()
            return

        brand_id = (resolve_brand(arg) if arg else None) or self._default_brand()
        if arg and resolve_brand(arg) is None:
            await self._reply_text(event, f"未知的品牌「{arg}」，发送「拼豆品牌方」查看可选品牌")
            event.stop_event()
            return

        image = self._extract_first_image(event)
        if image is None:
            image = await self._avatar_image(event)
        if image is not None:
            await self._process(event, image, brand_id)
            event.stop_event()
            return

        self._start_waiting(event, brand_id)
        await self._reply_text(event, f"请在 {self.wait_timeout} 秒内发送要处理的图片（发送「撤销」取消）")
        event.stop_event()

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
            await self._reply_text(event, "已取消")
            event.stop_event()
            return
        image = self._extract_first_image(event)
        if image is None:
            return
        self._clear_pending(key)
        await self._process(event, image, pending[1])
        event.stop_event()

    # ---------------------------------------------------------- 核心

    def _default_brand(self) -> str:
        return resolve_brand(self.default_brand) or "mard"

    @staticmethod
    def _event_key(event: AstrMessageEvent) -> tuple[str, str]:
        return event.unified_msg_origin, str(event.get_sender_id() or "")

    @staticmethod
    def _extract_first_image(event: AstrMessageEvent) -> Comp.Image | None:
        """优先级：消息内图片 > 引用图。@用户头像由命令入口单独兜底。"""
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

    @staticmethod
    def _extract_at_qq(event: AstrMessageEvent) -> str | None:
        """消息中 @ 的目标用户 QQ 号；未 @ 任何人时返回 None（不读取头像）。"""
        for seg in event.get_messages():
            if isinstance(seg, Comp.At):
                qq = str(getattr(seg, "qq", "") or "")
                if qq.isdigit():
                    return qq
        return None

    async def _avatar_image(self, event: AstrMessageEvent) -> Comp.Image | None:
        """头像来源：仅当消息 @ 了某位用户（QQ 平台），取被 @ 者的头像。"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return None
            at_qq = self._extract_at_qq(event)
            if not at_qq:
                return None
            return Comp.Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&nk={at_qq}&s=640")
        except Exception:
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

    async def _process(self, event: AstrMessageEvent, image: Comp.Image, brand_id: str) -> None:
        try:
            path = await image.convert_to_file_path()
        except Exception as e:
            logger.warning(f"Pindo 图片获取失败: {e}")
            await self._reply_text(event, "图片下载失败，请换一张或重新发送")
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
                await self._reply_text(event, "图片解码失败，请发送常见的图片格式（JPG/PNG/WebP）")
            else:
                logger.error(f"Pindo 图纸生成失败: {e}", exc_info=True)
                await self._reply_text(event, "图片处理失败，请换一张图片试试")
            return

        await self._reply_image(event, out_path)
