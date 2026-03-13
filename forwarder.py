"""
ChatBridge Forwarder - 处理WebSocket和API转发

架构：
  NapCat → 用户API(8003) → 排队 → HTTP长轮询 → SillyTavern扩展 → SillyTavern自己调LLM
"""

import json
import asyncio
import websockets
import logging
import urllib.parse
import uuid
from aiohttp import web
from typing import Dict, Any, Optional, Set

logger = logging.getLogger(__name__)


class ChatBridgeForwarder:
    def __init__(
        self, settings: Dict[str, Any], log_queue: Optional[asyncio.Queue] = None
    ):
        self.settings = settings
        self.ws_clients: Set[Any] = set()
        self.response_futures: Dict[str, asyncio.Future] = {}
        self.response_queues: Dict[str, asyncio.Queue] = {}
        self.log_queue = log_queue or asyncio.Queue()

        # 排队机制：同一时间只允许一个请求在处理
        self._request_lock = asyncio.Lock()
        self._pending_count = 0

        # 重试配置
        retry_cfg = settings.get("retry", {})
        self._max_retries = retry_cfg.get("max_retries", 3)
        self._retry_delay = retry_cfg.get("retry_delay", 5)
        self._timeout = retry_cfg.get("timeout", 120)

        # HTTP长轮询：等待ST扩展来取任务的队列
        self._http_poll_queue: asyncio.Queue = asyncio.Queue()
        # HTTP长轮询：ST扩展回传响应的队列 {request_id: Queue}
        self._http_response_queues: Dict[str, asyncio.Queue] = {}

    def reload_settings(self, new_settings: Dict[str, Any]):
        """热加载配置（不重启服务）"""
        self.settings = new_settings

        # 更新重试配置
        retry_cfg = new_settings.get("retry", {})
        self._max_retries = retry_cfg.get("max_retries", 3)
        self._retry_delay = retry_cfg.get("retry_delay", 5)
        self._timeout = retry_cfg.get("timeout", 120)

        self.log("配置已热加载：token/api_key/retry 立即生效，端口变更需重启服务")

    def log(self, message: str, level: str = "info"):
        """记录日志并推送到管理UI"""
        log_msg = f"[Forwarder] {message}"
        if level == "info":
            logger.info(log_msg)
        elif level == "warning":
            logger.warning(log_msg)
        elif level == "error":
            logger.error(log_msg)
        elif level == "debug":
            logger.debug(log_msg)

        try:
            self.log_queue.put_nowait(
                {
                    "source": "forwarder",
                    "level": level,
                    "message": message,
                    "timestamp": asyncio.get_event_loop().time(),
                }
            )
        except Exception:
            pass

    async def start_websocket_server(self):
        """启动WebSocket服务器"""
        host = self.settings["websocket"]["host"]
        port = self.settings["websocket"]["port"]
        server = websockets.serve(self.handle_websocket, host, port)
        self.log(f"WebSocket服务器启动在 ws://{host}:{port}")
        return server

    async def handle_websocket(self, websocket):
        """处理WebSocket连接（SillyTavern扩展连接此处）"""
        # Token验证
        token = self.settings["websocket"].get("token", "")
        if token:
            query_string = websocket.request.query_string
            params = urllib.parse.parse_qs(query_string)
            client_token = params.get("token", [""])[0]
            if client_token != token:
                self.log(f"Token验证失败，客户端Token: {client_token}", "warning")
                await websocket.close(code=1008, reason="Token验证失败")
                return
            self.log("Token验证通过")

        self.ws_clients.add(websocket)
        self.log(f"WebSocket客户端已连接，当前连接数: {len(self.ws_clients)}")

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    self.log(f"收到WebSocket消息类型: {data.get('type')}")

                    # 处理ST的响应（非流式）
                    if data.get("type") == "st_response":
                        request_id = data.get("id")
                        if request_id in self.response_futures:
                            future = self.response_futures[request_id]
                            if not future.done():
                                future.set_result(data.get("content"))

                except json.JSONDecodeError:
                    self.log("无效的WebSocket消息格式", "error")
        finally:
            self.ws_clients.discard(websocket)
            self.log(f"WebSocket客户端已断开，剩余连接数: {len(self.ws_clients)}")

    async def start_user_api_server(self) -> web.AppRunner:
        """启动用户API服务器（NapCat调用此处，ST扩展也连此处）"""

        @web.middleware
        async def cors_middleware(request: web.Request, handler):
            """为所有响应添加 CORS 头，允许浏览器扩展跨域请求"""
            if request.method == "OPTIONS":
                response = web.Response(status=204)
            else:
                response = await handler(request)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, X-Token, Authorization"
            )
            return response

        app = web.Application(middlewares=[cors_middleware])
        app.router.add_post("/v1/chat/completions", self.handle_user_api)
        app.router.add_get("/v1/models", self.handle_models_stub)
        app.router.add_get("/models", self.handle_models_stub)
        # ST扩展 HTTP 长轮询端点
        app.router.add_get("/st/poll", self.handle_st_poll)
        app.router.add_post("/st/response", self.handle_st_response)
        app.router.add_post("/st/connect", self.handle_st_connect)
        # OPTIONS 预检（CORS 预检请求）
        app.router.add_route("OPTIONS", "/{path_info:.*}", self._handle_options)

        host = self.settings["user_api"]["host"]
        port = self.settings["user_api"]["port"]

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        self.log(f"用户API服务器启动在 http://{host}:{port}")
        return runner

    async def _handle_options(self, request: web.Request) -> web.Response:
        """处理 CORS 预检请求"""
        return web.Response(status=204)

    async def handle_models_stub(self, request: web.Request) -> web.Response:
        """返回一个固定的模型列表（占位）"""
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": "sillytavern",
                        "object": "model",
                        "created": 0,
                        "owned_by": "chatbridge",
                    }
                ],
            }
        )

    async def handle_user_api(self, request: web.Request) -> web.Response:
        """处理来自NapCat的API请求，排队转发给SillyTavern"""
        # API密钥验证
        expected_key = self.settings["user_api"]["api_key"]
        if (
            expected_key
            and request.headers.get("Authorization") != f"Bearer {expected_key}"
        ):
            return web.Response(status=401, text="无效的API密钥")

        try:
            request_data = await request.json()
        except Exception as e:
            return web.Response(status=400, text=f"请求格式错误: {str(e)}")

        request_id = str(uuid.uuid4())
        is_stream = request_data.get("stream", False)

        # 排队等待
        self._pending_count += 1
        queue_pos = self._pending_count
        self.log(f"请求入队 ID={request_id[:8]}，当前队列位置: {queue_pos}")

        try:
            async with self._request_lock:
                self._pending_count -= 1
                self.log(f"请求开始处理 ID={request_id[:8]}，stream={is_stream}")
                return await self._process_request(
                    request, request_data, request_id, is_stream
                )
        except Exception as e:
            self._pending_count = max(0, self._pending_count - 1)
            self.log(f"处理请求失败: {str(e)}", "error")
            return web.Response(status=500, text=f"Internal Server Error: {str(e)}")

    async def _process_request(
        self,
        request: web.Request,
        request_data: dict,
        request_id: str,
        is_stream: bool,
    ) -> web.Response:
        """实际处理单个请求（已持有锁），失败时自动重试"""
        ws_message = {
            "type": "user_request",
            "id": request_id,
            "content": request_data,
        }

        last_error = ""
        for attempt in range(1, self._max_retries + 1):
            if attempt > 1:
                self.log(
                    f"第 {attempt}/{self._max_retries} 次重试 ID={request_id[:8]}，等待 {self._retry_delay}s..."
                )
                await asyncio.sleep(self._retry_delay)

            try:
                if is_stream:
                    result = await self._process_stream(request, ws_message, request_id)
                else:
                    result = await self._process_nonstream(ws_message, request_id)
                return result
            except asyncio.TimeoutError:
                last_error = f"超时（{self._timeout}s）"
                self.log(f"请求超时 ID={request_id[:8]}，attempt={attempt}", "warning")
            except Exception as e:
                last_error = str(e)
                self.log(
                    f"请求失败 ID={request_id[:8]}，attempt={attempt}: {e}", "warning"
                )

        self.log(
            f"请求已达最大重试次数 ID={request_id[:8]}，最后错误: {last_error}", "error"
        )
        return web.Response(
            status=504, text=f"请求失败（已重试{self._max_retries}次）: {last_error}"
        )

    async def _process_stream(
        self, request: web.Request, ws_message: dict, request_id: str
    ) -> web.Response:
        """处理流式请求"""
        stream_response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await stream_response.prepare(request)

        queue: asyncio.Queue = asyncio.Queue()
        self.response_queues[request_id] = queue

        try:
            if not await self._send_to_st(ws_message):
                await stream_response.write(b"data: [DONE]\n\n")
                return stream_response

            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=self._timeout)
                if not chunk or not isinstance(chunk, str):
                    continue
                chunk = chunk.strip()
                if not chunk:
                    continue
                if chunk == "[DONE]":
                    await stream_response.write(b"data: [DONE]\n\n")
                    break
                if not chunk.startswith("data: "):
                    chunk = f"data: {chunk}"
                if not chunk.endswith("\n\n"):
                    chunk = f"{chunk}\n\n"
                await stream_response.write(chunk.encode())

            self.log(f"流式请求完成 ID={request_id[:8]}")
            return stream_response

        finally:
            self.response_queues.pop(request_id, None)

    async def _process_nonstream(
        self, ws_message: dict, request_id: str
    ) -> web.Response:
        """处理非流式请求"""
        future: asyncio.Future = asyncio.Future()
        self.response_futures[request_id] = future

        try:
            if not await self._send_to_st(ws_message):
                return web.Response(status=503, text="发送到SillyTavern失败")

            response = await asyncio.wait_for(future, timeout=self._timeout)
            self.log(f"非流式请求完成 ID={request_id[:8]}")
            return web.json_response(response)
        finally:
            self.response_futures.pop(request_id, None)

    async def _send_to_st(self, ws_message: dict) -> bool:
        """发送消息到SillyTavern（WebSocket或HTTP长轮询），返回是否成功"""
        # 优先用WebSocket
        for ws in list(self.ws_clients):
            try:
                await ws.send(json.dumps(ws_message))
                self.log(f"[WS] 已发送请求到SillyTavern: ID={ws_message['id'][:8]}")
                return True
            except Exception as e:
                self.log(f"发送WebSocket消息失败: {e}", "error")

        # 降级到HTTP长轮询
        if not self._http_poll_queue.empty() or True:
            request_id = ws_message["id"]
            self._http_response_queues[request_id] = asyncio.Queue()
            await self._http_poll_queue.put(ws_message)
            self.log(f"[HTTP] 已推送请求到长轮询队列: ID={request_id[:8]}")
            return True

        self.log("没有可用的SillyTavern连接", "error")
        return False

    async def handle_st_connect(self, request: web.Request) -> web.Response:
        """ST扩展心跳/注册接口"""
        token = self.settings["websocket"].get("token", "")
        if token and request.headers.get("X-Token") != token:
            return web.Response(status=401, text="Token验证失败")
        self.log("ST扩展已通过HTTP连接")
        return web.json_response({"status": "ok"})

    async def handle_st_poll(self, request: web.Request) -> web.Response:
        """ST扩展长轮询取任务，最多等30秒"""
        token = self.settings["websocket"].get("token", "")
        if token and request.headers.get("X-Token") != token:
            return web.Response(status=401, text="Token验证失败")
        try:
            msg = await asyncio.wait_for(self._http_poll_queue.get(), timeout=30)
            self.log(f"[HTTP] ST扩展取走任务: ID={msg['id'][:8]}")
            return web.json_response(msg)
        except asyncio.TimeoutError:
            return web.Response(status=204)  # 无任务，让扩展重新轮询

    async def handle_st_response(self, request: web.Request) -> web.Response:
        """ST扩展回传响应"""
        token = self.settings["websocket"].get("token", "")
        if token and request.headers.get("X-Token") != token:
            return web.Response(status=401, text="Token验证失败")
        try:
            data = await request.json()
            request_id = data.get("id")
            content = data.get("content")
            msg_type = data.get("type", "st_response")

            # 处理非流式响应
            if msg_type == "st_response" and request_id in self.response_futures:
                future = self.response_futures[request_id]
                if not future.done():
                    future.set_result(content)

            # 处理流式响应块
            if msg_type == "st_chunk" and request_id in self.response_queues:
                await self.response_queues[request_id].put(content)

            # 处理流式结束
            if msg_type == "st_done" and request_id in self.response_queues:
                await self.response_queues[request_id].put("[DONE]")

            return web.json_response({"status": "ok"})
        except Exception as e:
            return web.Response(status=400, text=str(e))

    def get_status(self) -> Dict[str, Any]:
        """获取转发器状态"""
        return {
            "websocket_clients": len(self.ws_clients),
            "active_futures": len(self.response_futures),
            "active_queues": len(self.response_queues),
            "pending_requests": self._pending_count,
            "is_processing": self._request_lock.locked(),
            "max_retries": self._max_retries,
            "retry_delay": self._retry_delay,
            "timeout": self._timeout,
        }
