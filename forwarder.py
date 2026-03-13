"""
ChatBridge Forwarder - 处理WebSocket和API转发
"""

import json
import asyncio
import websockets
import aiohttp
import logging
import os
import urllib.parse
from collections import deque
import uuid
from aiohttp import web
from typing import List, Dict, Any, Optional, Set

logger = logging.getLogger(__name__)


class APIKeyRotator:
    def __init__(self, api_keys: List[str]):
        self.api_keys = deque(api_keys)

    def get_next_key(self) -> str:
        if not self.api_keys:
            return ""
        current_key = self.api_keys[0]
        self.api_keys.rotate(-1)
        return current_key


class ChatBridgeForwarder:
    def __init__(
        self, settings: Dict[str, Any], log_queue: Optional[asyncio.Queue] = None
    ):
        self.settings = settings
        self.ws_clients: Set[Any] = set()
        self.key_rotator = APIKeyRotator(self.settings["llm_api"]["api_keys"])
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

        # 推送到管理UI日志队列
        try:
            if self.log_queue:
                self.log_queue.put_nowait(
                    {
                        "source": "forwarder",
                        "level": level,
                        "message": message,
                        "timestamp": asyncio.get_event_loop().time(),
                    }
                )
        except:
            pass

    async def start_websocket_server(self):
        """启动WebSocket服务器"""
        host = self.settings["websocket"]["host"]
        port = self.settings["websocket"]["port"]

        server = websockets.serve(self.handle_websocket, host, port)

        self.log(f"WebSocket服务器启动在 ws://{host}:{port}")
        return server

    async def handle_websocket(self, websocket):
        """处理WebSocket连接"""
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
        else:
            self.log("Token未设置，跳过验证")

        self.ws_clients.add(websocket)
        self.log(f"WebSocket客户端已连接，当前连接数: {len(self.ws_clients)}")

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    self.log(
                        f"收到WebSocket消息: {json.dumps(data, ensure_ascii=False)[:200]}..."
                    )

                    # 处理ST的响应
                    if data.get("type") == "st_response":
                        request_id = data.get("id")
                        if request_id in self.response_futures:
                            future = self.response_futures[request_id]
                            if not future.done():
                                future.set_result(data.get("content"))

                except json.JSONDecodeError:
                    self.log("无效的WebSocket消息格式", "error")
        finally:
            self.ws_clients.remove(websocket)
            self.log(f"WebSocket客户端已断开，剩余连接数: {len(self.ws_clients)}")

    async def start_st_api_server(self) -> web.AppRunner:
        """启动ST API服务器"""
        app = web.Application()

        # 路由设置
        app.router.add_get("/models", self.handle_models)
        app.router.add_get("/v1/models", self.handle_models)
        app.router.add_post("/chat/completions", self.handle_chat_completions)
        app.router.add_post("/v1/chat/completions", self.handle_chat_completions)

        host = self.settings["st_api"]["host"]
        port = self.settings["st_api"]["port"]

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        self.log(f"ST API服务器启动在 http://{host}:{port}")
        return runner

    async def start_user_api_server(self) -> web.AppRunner:
        """启动用户API服务器"""
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.handle_user_api)

        host = self.settings["user_api"]["host"]
        port = self.settings["user_api"]["port"]

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        self.log(f"用户API服务器启动在 http://{host}:{port}")
        return runner

    async def handle_user_api(self, request: web.Request) -> web.Response:
        """处理来自用户的API请求"""
        # API密钥验证
        if (
            request.headers.get("Authorization")
            != f"Bearer {self.settings['user_api']['api_key']}"
        ):
            return web.Response(status=401, text="无效的API密钥")

        try:
            request_data = await request.json()
            request_id = str(uuid.uuid4())
            is_stream = request_data.get("stream", False)
            self.log(f"用户API请求 ID={request_id}, stream={is_stream}")

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

                # 创建事件队列
                queue = asyncio.Queue()
                self.response_queues[request_id] = queue

                try:
                    # 发送WebSocket消息到SillyTavern
                    ws_message = {
                        "type": "user_request",
                        "id": request_id,
                        "content": request_data,
                    }

                    if not self.ws_clients:
                        self.log("没有WebSocket客户端连接", "warning")
                        return web.Response(
                            status=503, text="No WebSocket clients connected"
                        )

                    # 发送到第一个可用的WebSocket客户端
                    sent = False
                    for ws in self.ws_clients:
                        try:
                            await ws.send(json.dumps(ws_message))
                            self.log(f"已发送请求到WebSocket: ID={request_id}")
                            sent = True
                            break
                        except Exception as e:
                            self.log(f"发送WebSocket消息失败: {e}", "error")
                            continue

                    if not sent:
                        return web.Response(
                            status=503, text="Failed to send to WebSocket"
                        )

                    # 等待并转发响应块
                    while True:
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=60.0)

                            if chunk and isinstance(chunk, str):
                                chunk = chunk.strip()
                                if not chunk:
                                    continue

                                if chunk == "[DONE]":
                                    await stream_response.write(b"data: [DONE]\n\n")
                                    self.log(f"发送流式响应结束标记: ID={request_id}")
                                    break

                                # 确保响应格式正确
                                if not chunk.startswith("data: "):
                                    chunk = f"data: {chunk}"
                                if not chunk.endswith("\n\n"):
                                    chunk = f"{chunk}\n\n"

                                await stream_response.write(chunk.encode())

                        except asyncio.TimeoutError:
                            self.log(f"等待响应块超时: ID={request_id}", "warning")
                            await stream_response.write(b"data: [DONE]\n\n")
                            break

                    return stream_response

                finally:
                    # 清理队列
                    self.response_queues.pop(request_id, None)

            else:
                # 非流式请求
                future = asyncio.Future()
                self.response_futures[request_id] = future

                # 发送WebSocket消息
                ws_message = {
                    "type": "user_request",
                    "id": request_id,
                    "content": request_data,
                }

                if not self.ws_clients:
                    return web.Response(
                        status=503, text="No WebSocket clients connected"
                    )

                sent = False
                for ws in self.ws_clients:
                    try:
                        await ws.send(json.dumps(ws_message))
                        self.log(f"已发送请求到WebSocket: ID={request_id}")
                        sent = True
                        break
                    except Exception as e:
                        self.log(f"发送WebSocket消息失败: {e}", "error")
                        continue

                if not sent:
                    return web.Response(status=503, text="Failed to send to WebSocket")

                try:
                    # 等待响应
                    response = await asyncio.wait_for(future, timeout=60.0)
                    return web.json_response(response)
                except asyncio.TimeoutError:
                    self.log(f"等待响应超时: ID={request_id}", "warning")
                    return web.Response(status=504, text="Request timeout")
                finally:
                    self.response_futures.pop(request_id, None)

        except Exception as e:
            self.log(f"处理用户API请求失败: {str(e)}", "error")
            return web.Response(status=500, text=f"Internal Server Error: {str(e)}")

    async def handle_models(self, request: web.Request) -> web.Response:
        """处理模型列表请求"""
        self.log(f"收到models请求: {request.path}")
        api_key = self.key_rotator.get_next_key()

        if not api_key:
            return web.Response(status=500, text="No API keys configured")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                target_url = f"{self.settings['llm_api']['base_url']}/models"
                self.log(f"转发请求到: {target_url}")

                async with session.get(target_url, headers=headers) as response:
                    response_data = await response.json()
                    self.log(f"模型列表响应状态: {response.status}")
                    return web.json_response(response_data, status=response.status)

        except Exception as e:
            self.log(f"获取模型列表失败: {str(e)}", "error")
            return web.Response(status=500, text=str(e))

    async def handle_chat_completions(self, request: web.Request) -> web.Response:
        """处理聊天完成请求"""
        try:
            request_data = await request.json()
            is_stream = request_data.get("stream", False)
            self.log(
                f"收到chat completion请求: PATH={request.path}, STREAM={is_stream}"
            )

            api_key = self.key_rotator.get_next_key()
            if not api_key:
                return web.Response(status=500, text="No API keys configured")

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            target_url = f"{self.settings['llm_api']['base_url']}/chat/completions"

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    target_url, json=request_data, headers=headers
                ) as llm_response:
                    if llm_response.headers.get("content-type") == "text/event-stream":
                        self.log("处理流式响应")

                        # 创建流式响应
                        st_response = web.StreamResponse(
                            status=llm_response.status,
                            headers={"Content-Type": "text/event-stream"},
                        )
                        await st_response.prepare(request)

                        # 找到活跃的用户请求队列
                        active_queues = {
                            rid: queue for rid, queue in self.response_queues.items()
                        }

                        async for chunk in llm_response.content:
                            if chunk:
                                chunk_str = chunk.decode()
                                self.log(f"收到数据块: {chunk_str[:100]}...", "debug")

                                # 发送到ST
                                await st_response.write(chunk)

                                # 转发到用户队列
                                if active_queues:
                                    for queue_id, queue in active_queues.items():
                                        try:
                                            await queue.put(chunk_str)
                                            self.log(
                                                f"转发数据块到用户队列 {queue_id}",
                                                "debug",
                                            )
                                        except Exception as e:
                                            self.log(
                                                f"转发到用户队列失败 {queue_id}: {e}",
                                                "error",
                                            )

                        # 发送结束标记
                        if active_queues:
                            for queue_id, queue in active_queues.items():
                                try:
                                    await queue.put("[DONE]")
                                    self.log(f"发送结束标记到用户队列 {queue_id}")
                                except Exception as e:
                                    self.log(
                                        f"发送结束标记失败 {queue_id}: {e}", "error"
                                    )

                        return st_response

                    else:
                        self.log("处理非流式响应")
                        response_data = await llm_response.json()
                        self.log(f"收到LLM响应状态: {llm_response.status}")

                        # 转发到所有等待的用户请求
                        futures_updated = False
                        for request_id, future in list(self.response_futures.items()):
                            try:
                                if (
                                    isinstance(future, asyncio.Future)
                                    and not future.done()
                                ):
                                    future.set_result(response_data)
                                    self.log(f"成功设置用户请求结果: ID={request_id}")
                                    futures_updated = True
                            except Exception as e:
                                self.log(
                                    f"设置用户请求结果失败 {request_id}: {e}", "error"
                                )

                        if not futures_updated:
                            self.log("没有成功更新任何用户请求的结果", "warning")

                        return web.json_response(
                            response_data, status=llm_response.status
                        )

        except Exception as e:
            error_msg = f"处理聊天完成请求失败: {str(e)}"
            self.log(error_msg, "error")
            return web.Response(status=500, text=error_msg)

    def get_status(self) -> Dict[str, Any]:
        """获取转发器状态"""
        return {
            "websocket_clients": len(self.ws_clients),
            "active_futures": len(self.response_futures),
            "active_queues": len(self.response_queues),
        }
