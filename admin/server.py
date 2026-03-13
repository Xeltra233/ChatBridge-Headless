"""
管理UI后端 - 提供Web管理界面的API
"""

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


class AdminServer:
    def __init__(
        self,
        settings: Dict[str, Any],
        log_queue: asyncio.Queue,
        forwarder=None,
        browser_manager=None,
    ):
        self.settings = settings
        self.log_queue = log_queue
        self.forwarder = forwarder
        self.browser_manager = browser_manager
        self.admin_config = settings["admin_ui"]

        # session管理（7天有效期）
        self.sessions: Dict[str, float] = {}
        self.session_ttl = 7 * 24 * 3600

        # 日志缓冲（最多保留1000条）
        self.log_buffer = []
        self.max_log_buffer = 1000

        # SSE订阅者
        self.sse_subscribers = set()

        # 启动日志消费任务
        asyncio.create_task(self._consume_logs())

    async def _consume_logs(self):
        """消费日志队列，推送到SSE订阅者"""
        while True:
            try:
                log_entry = await self.log_queue.get()

                # 格式化日志
                formatted = {
                    "time": time.strftime("%H:%M:%S"),
                    "source": log_entry.get("source", "system"),
                    "level": log_entry.get("level", "info"),
                    "message": log_entry.get("message", ""),
                }

                # 加入缓冲
                self.log_buffer.append(formatted)
                if len(self.log_buffer) > self.max_log_buffer:
                    self.log_buffer = self.log_buffer[-self.max_log_buffer :]

                # 推送到所有SSE订阅者
                dead = set()
                for queue in self.sse_subscribers:
                    try:
                        queue.put_nowait(formatted)
                    except Exception:
                        dead.add(queue)
                self.sse_subscribers -= dead

            except Exception as e:
                logger.error(f"消费日志失败: {e}")
                await asyncio.sleep(0.1)

    def _check_session(self, request: web.Request) -> bool:
        """验证session"""
        token = request.cookies.get("admin_session")
        if not token:
            return False
        expire_time = self.sessions.get(token)
        if not expire_time:
            return False
        if time.time() > expire_time:
            del self.sessions[token]
            return False
        return True

    def _require_auth(self, handler):
        """认证装饰器"""

        async def wrapper(request: web.Request):
            if not self._check_session(request):
                raise web.HTTPFound("/login")
            return await handler(request)

        return wrapper

    async def start(self) -> web.AppRunner:
        """启动管理UI服务器"""
        app = web.Application()

        # 静态文件目录
        admin_dir = os.path.join(os.path.dirname(__file__))

        # 路由
        app.router.add_get("/", self._require_auth(self.handle_index))
        app.router.add_get("/login", self.handle_login_page)
        app.router.add_post("/api/login", self.handle_login)
        app.router.add_post("/api/logout", self.handle_logout)
        app.router.add_get("/api/status", self._require_auth(self.handle_status))
        app.router.add_get(
            "/api/screenshot", self._require_auth(self.handle_screenshot)
        )
        app.router.add_post(
            "/api/browser/restart", self._require_auth(self.handle_browser_restart)
        )
        app.router.add_get(
            "/api/settings", self._require_auth(self.handle_get_settings)
        )
        app.router.add_post(
            "/api/settings", self._require_auth(self.handle_save_settings)
        )
        app.router.add_get(
            "/api/logs/history", self._require_auth(self.handle_log_history)
        )
        app.router.add_get(
            "/api/logs/stream", self._require_auth(self.handle_log_stream)
        )
        app.router.add_static("/static", admin_dir)

        host = self.admin_config["host"]
        port = self.admin_config["port"]

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"管理UI启动在 http://{host}:{port}")
        return runner

    async def handle_login_page(self, request: web.Request) -> web.Response:
        """返回登录页面"""
        if self._check_session(request):
            raise web.HTTPFound("/")
        html_path = os.path.join(os.path.dirname(__file__), "login.html")
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def handle_index(self, request: web.Request) -> web.Response:
        """返回主页面"""
        html_path = os.path.join(os.path.dirname(__file__), "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def handle_login(self, request: web.Request) -> web.Response:
        """处理登录请求"""
        try:
            data = await request.json()
            password = data.get("password", "")

            if password != self.admin_config["password"]:
                return web.json_response(
                    {"success": False, "message": "密码错误"}, status=401
                )

            # 生成session token
            token = secrets.token_hex(32)
            self.sessions[token] = time.time() + self.session_ttl

            response = web.json_response({"success": True})
            response.set_cookie(
                "admin_session",
                token,
                max_age=self.session_ttl,
                httponly=True,
                samesite="Lax",
            )
            return response

        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_logout(self, request: web.Request) -> web.Response:
        """处理登出"""
        token = request.cookies.get("admin_session")
        if token and token in self.sessions:
            del self.sessions[token]
        response = web.json_response({"success": True})
        response.del_cookie("admin_session")
        return response

    async def handle_status(self, request: web.Request) -> web.Response:
        """获取系统状态"""
        status = {
            "browser": self.browser_manager.get_status()
            if self.browser_manager
            else {},
            "forwarder": self.forwarder.get_status() if self.forwarder else {},
        }
        return web.json_response(status)

    async def handle_screenshot(self, request: web.Request) -> web.Response:
        """获取浏览器截图"""
        if not self.browser_manager:
            return web.Response(status=503, text="Browser manager not available")

        screenshot_b64 = await self.browser_manager.get_screenshot_base64()
        if not screenshot_b64:
            return web.Response(status=404, text="No screenshot available")

        return web.json_response({"screenshot": screenshot_b64})

    async def handle_browser_restart(self, request: web.Request) -> web.Response:
        """重启浏览器"""
        if not self.browser_manager:
            return web.json_response(
                {"success": False, "message": "Browser manager not available"},
                status=503,
            )

        asyncio.create_task(self.browser_manager.restart())
        return web.json_response({"success": True, "message": "浏览器正在重启..."})

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        """获取当前设置"""
        settings_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "settings.json"
        )
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                content = f.read()
            return web.json_response({"success": True, "content": content})
        except FileNotFoundError:
            return web.json_response(
                {"success": False, "message": "settings.json 不存在"}, status=404
            )

    async def handle_save_settings(self, request: web.Request) -> web.Response:
        """保存设置"""
        settings_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "settings.json"
        )
        try:
            data = await request.json()
            content = data.get("content", "")

            # 验证JSON格式
            json.loads(content)

            with open(settings_path, "w", encoding="utf-8") as f:
                f.write(content)

            return web.json_response(
                {"success": True, "message": "设置已保存，重启服务后生效"}
            )

        except json.JSONDecodeError as e:
            return web.json_response(
                {"success": False, "message": f"JSON格式错误: {str(e)}"}, status=400
            )
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_log_history(self, request: web.Request) -> web.Response:
        """获取历史日志"""
        return web.json_response({"logs": self.log_buffer})

    async def handle_log_stream(self, request: web.Request) -> web.Response:
        """SSE实时日志流"""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        queue = asyncio.Queue()
        self.sse_subscribers.add(queue)

        try:
            # 先发送历史日志
            for log in self.log_buffer[-100:]:
                data = json.dumps(log, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode())

            # 持续推送新日志
            while True:
                try:
                    log = await asyncio.wait_for(queue.get(), timeout=30)
                    data = json.dumps(log, ensure_ascii=False)
                    await response.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    # 发送心跳
                    await response.write(b": heartbeat\n\n")

        except Exception:
            pass
        finally:
            self.sse_subscribers.discard(queue)

        return response
