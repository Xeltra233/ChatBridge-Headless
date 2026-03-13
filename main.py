"""
ChatBridge Headless - 主入口
整合 WebSocket转发器 + Playwright无头浏览器 + 管理UI

环境变量配置（优先级高于settings.json）：
  CB_WS_PORT          WebSocket端口（默认8001）
  CB_WS_TOKEN         WebSocket Token
  CB_ST_API_PORT      ST API端口（默认8002）
  CB_ST_API_KEY       ST API密钥
  CB_USER_API_PORT    用户API端口（默认8003）
  CB_USER_API_KEY     用户API密钥
  CB_LLM_BASE_URL     LLM API地址
  CB_LLM_API_KEYS     LLM API密钥，多个用逗号分隔
  CB_ST_URL           SillyTavern地址
  CB_BASIC_AUTH_USER  Basic Auth用户名
  CB_BASIC_AUTH_PASS  Basic Auth密码
  CB_ST_USER          ST用户名（默认QQbot）
  CB_ST_PASS          ST用户密码
  CB_ADMIN_PORT       管理UI端口（默认8080）
  CB_ADMIN_PASSWORD   管理UI密码
"""

import asyncio
import json
import logging
import os
import shutil
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def load_settings() -> dict:
    """加载配置文件，环境变量优先"""
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
    template_path = os.path.join(os.path.dirname(__file__), "settings.json.template")

    # 如果settings.json不存在，从模板创建
    if not os.path.exists(settings_path):
        if os.path.exists(template_path):
            shutil.copy(template_path, settings_path)
            logger.info("已从模板创建 settings.json")
        else:
            logger.error("找不到 settings.json 和 settings.json.template")
            sys.exit(1)

    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)

    # 用环境变量覆盖配置
    _apply_env(settings)

    return settings


def _apply_env(settings: dict):
    """将环境变量覆盖到settings中"""
    env = os.environ

    # WebSocket
    if v := env.get("CB_WS_PORT"):
        settings["websocket"]["port"] = int(v)
    if v := env.get("CB_WS_TOKEN"):
        settings["websocket"]["token"] = v

    # ST API
    if v := env.get("CB_ST_API_PORT"):
        settings["st_api"]["port"] = int(v)
    if v := env.get("CB_ST_API_KEY"):
        settings["st_api"]["api_key"] = v

    # 用户API
    if v := env.get("CB_USER_API_PORT"):
        settings["user_api"]["port"] = int(v)
    if v := env.get("CB_USER_API_KEY"):
        settings["user_api"]["api_key"] = v

    # LLM API
    if v := env.get("CB_LLM_BASE_URL"):
        settings["llm_api"]["base_url"] = v
    if v := env.get("CB_LLM_API_KEYS"):
        settings["llm_api"]["api_keys"] = [k.strip() for k in v.split(",") if k.strip()]

    # 浏览器
    if v := env.get("CB_ST_URL"):
        settings["browser"]["st_url"] = v
    if v := env.get("CB_BASIC_AUTH_USER"):
        settings["browser"]["basic_auth_user"] = v
    if v := env.get("CB_BASIC_AUTH_PASS"):
        settings["browser"]["basic_auth_pass"] = v
    if v := env.get("CB_ST_USER"):
        settings["browser"]["st_user"] = v
    if v := env.get("CB_ST_PASS"):
        settings["browser"]["st_pass"] = v

    # 管理UI
    if v := env.get("CB_ADMIN_PORT"):
        settings["admin_ui"]["port"] = int(v)
    if v := env.get("CB_ADMIN_PASSWORD"):
        settings["admin_ui"]["password"] = v

    # 打印生效的环境变量（隐藏敏感值）
    applied = [k for k in env if k.startswith("CB_")]
    if applied:
        safe = []
        sensitive = {
            "CB_WS_TOKEN",
            "CB_ST_API_KEY",
            "CB_USER_API_KEY",
            "CB_LLM_API_KEYS",
            "CB_BASIC_AUTH_PASS",
            "CB_ST_PASS",
            "CB_ADMIN_PASSWORD",
        }
        for k in applied:
            safe.append(f"{k}={'***' if k in sensitive else env[k]}")
        logger.info(f"已应用环境变量: {', '.join(safe)}")


async def main():
    settings = load_settings()

    # 共享日志队列
    log_queue = asyncio.Queue()

    # 初始化各模块
    from forwarder import ChatBridgeForwarder
    from browser_manager import BrowserManager
    from admin.server import AdminServer

    forwarder = ChatBridgeForwarder(settings, log_queue)
    browser = BrowserManager(settings, log_queue)
    admin = AdminServer(settings, log_queue, forwarder, browser)

    logger.info("正在启动 ChatBridge Headless...")

    # 启动所有服务
    ws_server = await forwarder.start_websocket_server()
    st_runner = await forwarder.start_st_api_server()
    user_runner = await forwarder.start_user_api_server()
    admin_runner = await admin.start()

    logger.info("所有服务已启动")
    logger.info(f"  WebSocket:  ws://0.0.0.0:{settings['websocket']['port']}")
    logger.info(f"  ST API:     http://0.0.0.0:{settings['st_api']['port']}")
    logger.info(f"  用户 API:   http://0.0.0.0:{settings['user_api']['port']}")
    logger.info(f"  管理 UI:    http://0.0.0.0:{settings['admin_ui']['port']}")

    # 启动浏览器（异步，不阻塞主流程）
    asyncio.create_task(start_browser(browser))

    # 保持运行
    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("正在关闭服务...")
        await browser.stop()
        await st_runner.cleanup()
        await user_runner.cleanup()
        await admin_runner.cleanup()


async def start_browser(browser):
    """启动浏览器，失败后自动重试"""
    retry_delay = 10
    while True:
        try:
            await browser.start()
            break
        except Exception as e:
            logger.error(f"浏览器启动失败: {e}，{retry_delay}秒后重试...")
            await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    asyncio.run(main())
