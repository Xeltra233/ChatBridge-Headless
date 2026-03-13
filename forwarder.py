"""
ChatBridge Forwarder - 处理WebSocket和API转发

架构：
  NapCat → 用户API(8003) → WebSocket(8001) → SillyTavern扩展 → SillyTavern自己调LLM
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
        """启动用户API服务器（NapCat调用此处）"""
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.handle_user_api)
        app.router.add_get("/v1/models", self.handle_models_stub)
        app.router.add_get("/models", self.handle_models_stub)

        host = self.settings["user_api"]["host"]
        port = self.settings["user_api"]["port"]

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        self.log(f"用户API服务器启动在 http://{host}:{port}")
        return runner

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
        """处理来自NapCat的API请求，转发给SillyTavern"""
        # API密钥验证
        expected_key = self.settings["user_api"]["api_key"]
        if (
            expected_key
            and request.headers.get("Authorization") != f"Bearer {expected_key}"
        ):
            return web.Response(status=401, text="无效的API密钥")

        try:
            request_data = await request.json()
            request_id = str(uuid.uuid4())
            is_stream = request_data.get("stream", False)
            self.log(f"用户API请求 ID={request_id}, stream={is_stream}")

            if not self.ws_clients:
                self.log("没有WebSocket客户端连接", "warning")
                return web.Response(
                    status=503, text="SillyTavern未连接，请检查扩展是否已连接"
                )

            ws_message = {
                "type": "user_request",
                "id": request_id,
                "content": request_data,
            }

            if is_stream:
                # 流式响应
                stream_response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
                await stream_response.prepare(request)

                queue = asyncio.Queue()
                self.response_queues[request_id] = queue

                try:
                    sent = False
                    for ws in self.ws_clients:
                        try:
                            await ws.send(json.dumps(ws_message))
                            self.log(f"已发送请求到SillyTavern: ID={request_id}")
                            sent = True
                            break
                        except Exception as e:
                            self.log(f"发送WebSocket消息失败: {e}", "error")

                    if not sent:
                        return web.Response(status=503, text="发送到SillyTavern失败")

                    while True:
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=120.0)
                            if chunk and isinstance(chunk, str):
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
                        except asyncio.TimeoutError:
                            self.log(f"等待响应超时: ID={request_id}", "warning")
                            await stream_response.write(b"data: [DONE]\n\n")
                            break

                    return stream_response

                finally:
                    self.response_queues.pop(request_id, None)

            else:
                # 非流式响应
                future = asyncio.Future()
                self.response_futures[request_id] = future

                sent = False
                for ws in self.ws_clients:
                    try:
                        await ws.send(json.dumps(ws_message))
                        self.log(f"已发送请求到SillyTavern: ID={request_id}")
                        sent = True
                        break
                    except Exception as e:
                        self.log(f"发送WebSocket消息失败: {e}", "error")

                if not sent:
                    return web.Response(status=503, text="发送到SillyTavern失败")

                try:
                    response = await asyncio.wait_for(future, timeout=120.0)
                    return web.json_response(response)
                except asyncio.TimeoutError:
                    self.log(f"等待响应超时: ID={request_id}", "warning")
                    return web.Response(status=504, text="Request timeout")
                finally:
                    self.response_futures.pop(request_id, None)

        except Exception as e:
            self.log(f"处理用户API请求失败: {str(e)}", "error")
            return web.Response(status=500, text=f"Internal Server Error: {str(e)}")

    def get_status(self) -> Dict[str, Any]:
        """获取转发器状态"""
        return {
            "websocket_clients": len(self.ws_clients),
            "active_futures": len(self.response_futures),
            "active_queues": len(self.response_queues),
        }
