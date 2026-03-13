"""
ChatBridge Headless - 主入口
整合 WebSocket转发器 + Playwright无头浏览器 + 管理UI
"""

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def load_settings() -> dict:
    """加载配置文件"""
    settings_path = os.path.join(os.path.dirname(__file__), "settings.json")

    if not os.path.exists(settings_path):
        template_path = os.path.join(
            os.path.dirname(__file__), "settings.json.template"
        )
        if os.path.exists(template_path):
            import shutil

            shutil.copy(template_path, settings_path)
            logger.warning("settings.json 不存在，已从模板创建，请修改配置后重启")
        else:
            logger.error("找不到 settings.json 和 settings.json.template")
            sys.exit(1)

    with open(settings_path, "r", encoding="utf-8") as f:
        return json.load(f)


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
