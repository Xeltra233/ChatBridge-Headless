"""
ChatBridge Headless - 主入口
整合 WebSocket转发器 + Playwright无头浏览器 + 管理UI

架构：NapCat → 用户API(8003) → WebSocket(8001) → SillyTavern扩展 → SillyTavern自己调LLM

环境变量（仅此一个）：
  CB_ADMIN_PASSWORD   管理UI登录密码（必填，其余配置在管理UI中设置）
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


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_settings() -> dict:
    """加载配置文件，仅管理UI密码从环境变量读取"""
    os.makedirs(DATA_DIR, exist_ok=True)
    settings_path = os.path.join(DATA_DIR, "settings.json")
    template_path = os.path.join(os.path.dirname(__file__), "settings.json.template")

    # 如果settings.json不存在，从模板创建
    if not os.path.exists(settings_path):
        if os.path.exists(template_path):
            shutil.copy(template_path, settings_path)
            logger.info("已从模板创建 settings.json，请在管理UI中完成配置")
        else:
            logger.error("找不到 settings.json 和 settings.json.template")
            sys.exit(1)

    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)

    # 仅管理UI密码从环境变量读取（覆盖settings.json中的值）
    if password := os.environ.get("CB_ADMIN_PASSWORD"):
        settings["admin_ui"]["password"] = password
        logger.info("已从环境变量读取管理UI密码")
    elif not settings["admin_ui"].get("password"):
        logger.warning(
            "管理UI密码未设置，请设置环境变量 CB_ADMIN_PASSWORD 或在settings.json中配置"
        )

    return settings


async def main():
    settings = load_settings()

    # 共享日志队列
    log_queue = asyncio.Queue()

    from forwarder import ChatBridgeForwarder
    from browser_manager import BrowserManager
    from admin.server import AdminServer

    forwarder = ChatBridgeForwarder(settings, log_queue)
    browser = BrowserManager(settings, log_queue)
    admin = AdminServer(settings, log_queue, forwarder, browser)

    logger.info("正在启动 ChatBridge Headless...")

    ws_server = await forwarder.start_websocket_server()
    user_runner = await forwarder.start_user_api_server()
    admin_runner = await admin.start()

    logger.info("所有服务已启动")
    logger.info(f"  WebSocket:  ws://0.0.0.0:{settings['websocket']['port']}")
    logger.info(f"  用户 API:   http://0.0.0.0:{settings['user_api']['port']}")
    logger.info(f"  管理 UI:    http://0.0.0.0:{settings['admin_ui']['port']}")

    # 启动浏览器（异步，不阻塞主流程）
    asyncio.create_task(start_browser(browser))

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("正在关闭服务...")
        await browser.stop()
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
