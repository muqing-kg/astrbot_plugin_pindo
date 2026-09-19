"""插件内 WebUI 静态站点服务（aiohttp）。

参照 astrbot_plugin_group_chat_plus 的 WebPanelServer 模式：
AppRunner + TCPSite，端口占用重试，任何异常不向上抛、不拖垮插件主功能。
aiohttp 是 AstrBot 框架自带依赖，无需在 requirements.txt 声明。

支持站点标题自定义：html 与 manifest.json 在分发时把默认标题替换为
配置值（其余资源原样直出，路径穿越由解析后前缀校验拦截）。
"""
from __future__ import annotations

import asyncio
import errno
from pathlib import Path

from aiohttp import web

try:
    from astrbot.api import logger
except ImportError:  # 本地自检环境无 astrbot
    import logging
    logger = logging.getLogger("pindo")


MAX_RETRY = 3
RETRY_DELAY = 2
DEFAULT_SITE_TITLE = "Pindo 拼豆图纸生成器"


class SiteServer:
    """托管插件内置的 Pindo 静态站点。"""

    def __init__(self, static_dir: str | Path, host: str = "0.0.0.0", port: int = 8765,
                 site_title: str = ""):
        self.static_dir = Path(static_dir).resolve()
        self.host = host
        self.port = port
        self.site_title = site_title.strip()
        self.runner: web.AppRunner | None = None
        self.app = web.Application()
        self._setup_routes()

    # ---------------------------------------------------------- 响应构造

    def _rewritten(self, file: Path) -> web.Response:
        """读取文件；配置了自定义标题时替换默认标题后返回。"""
        data = file.read_bytes()
        if self.site_title and self.site_title != DEFAULT_SITE_TITLE:
            data = data.replace(DEFAULT_SITE_TITLE.encode("utf-8"),
                                self.site_title.encode("utf-8"))
        ctype = "text/html" if file.suffix == ".html" else "application/manifest+json"
        return web.Response(body=data, content_type=ctype, charset="utf-8")

    def _safe_static_file(self, rel: str) -> Path | None:
        target = (self.static_dir / rel.lstrip("/")).resolve()
        if not target.is_relative_to(self.static_dir) or not target.is_file():
            return None
        return target

    # ---------------------------------------------------------- 路由

    def _setup_routes(self) -> None:
        not_found = self.static_dir / "404.html"

        @web.middleware
        async def not_found_middleware(request: web.Request, handler) -> web.StreamResponse:
            try:
                return await handler(request)
            except web.HTTPNotFound:
                if not_found.exists():
                    return self._rewritten(not_found)
                raise

        async def serve_index(_request: web.Request) -> web.Response:
            index = self.static_dir / "index.html"
            if index.exists():
                return self._rewritten(index)
            return web.Response(status=404, text="Pindo 静态资源缺失，请重新安装插件")

        async def serve_focus(_request: web.Request) -> web.Response:
            focus_index = self.static_dir / "focus" / "index.html"
            if focus_index.exists():
                return self._rewritten(focus_index)
            raise web.HTTPNotFound

        async def serve_manifest(_request: web.Request) -> web.Response:
            manifest = self.static_dir / "manifest.json"
            if manifest.exists():
                return self._rewritten(manifest)
            raise web.HTTPNotFound

        async def serve_html(request: web.Request) -> web.Response:
            target = self._safe_static_file(request.match_info.get("path", ""))
            if target is None:
                raise web.HTTPNotFound
            return self._rewritten(target)

        self.app.middlewares.append(not_found_middleware)
        self.app.router.add_get("/", serve_index)
        self.app.router.add_get("/focus", serve_focus)
        self.app.router.add_get("/manifest.json", serve_manifest)
        self.app.router.add_get(r"/{path:.*\.html}", serve_html)
        if self.static_dir.is_dir():
            self.app.router.add_static("/", path=str(self.static_dir), show_index=False)

    # ---------------------------------------------------------- 生命周期

    async def start(self) -> None:
        for attempt in range(1, MAX_RETRY + 1):
            try:
                self.runner = web.AppRunner(self.app)
                await self.runner.setup()
                host: str | None = None if self.host in ("0.0.0.0", "::") else self.host
                site = web.TCPSite(self.runner, host, self.port)
                await site.start()
                logger.info(f"Pindo WebUI 已启动: http://{self.host}:{self.port} （静态目录 {self.static_dir}）")
                return
            except OSError as e:
                if self.runner:
                    try:
                        await self.runner.cleanup()
                    except Exception:
                        pass
                    self.runner = None
                if getattr(e, "errno", 0) == errno.EADDRINUSE:
                    if attempt < MAX_RETRY:
                        logger.warning(f"Pindo WebUI 端口 {self.port} 被占用，{RETRY_DELAY} 秒后重试（{attempt}/{MAX_RETRY}）")
                        await asyncio.sleep(RETRY_DELAY)
                    else:
                        logger.error(f"Pindo WebUI 启动失败：端口 {self.port} 持续被占用，已放弃")
                else:
                    logger.error(f"Pindo WebUI 启动失败: {e}，已放弃")
                    return
            except Exception as e:
                logger.error(f"Pindo WebUI 启动遇到未知错误: {e}", exc_info=True)
                if self.runner:
                    try:
                        await self.runner.cleanup()
                    except Exception:
                        pass
                    self.runner = None
                return

    async def stop(self) -> None:
        if self.runner:
            try:
                await self.runner.cleanup()
                logger.info("Pindo WebUI 已停止")
            except Exception as e:
                logger.warning(f"Pindo WebUI 停止时出错（已忽略）: {e}")
            finally:
                self.runner = None
