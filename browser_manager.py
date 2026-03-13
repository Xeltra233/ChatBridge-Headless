"""
浏览器管理器 - 使用Playwright控制无头Chromium，自动登录SillyTavern
"""

import asyncio
import json
import logging
import os
import base64
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
except ImportError:
    print("请安装Playwright: pip install playwright && playwright install chromium")
    raise

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(
        self, settings: Dict[str, Any], log_queue: Optional[asyncio.Queue] = None
    ):
        self.settings = settings
        self.browser_config = settings["browser"]
        self.log_queue = log_queue or asyncio.Queue()

        # 浏览器状态
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_running = False
        self.last_screenshot: Optional[bytes] = None
        self.last_error: Optional[str] = None

        # 登录凭据
        self.basic_auth_user = self.browser_config.get("basic_auth_user", "")
        self.basic_auth_pass = self.browser_config.get("basic_auth_pass", "")
        self.st_user = self.browser_config.get("st_user", "QQbot")
        self.st_pass = self.browser_config.get("st_pass", "")
        self.cookie_file = self.browser_config.get("cookie_file", "cookies.json")
        self.st_url = self.browser_config.get(
            "st_url", "http://sillytavern-conel.zeabur.internal:8000"
        )

    def log(self, message: str, level: str = "info"):
        """记录日志并推送到管理UI"""
        log_msg = f"[Browser] {message}"
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
                        "source": "browser",
                        "level": level,
                        "message": message,
                        "timestamp": asyncio.get_event_loop().time(),
                    }
                )
        except:
            pass

    async def start(self):
        """启动浏览器"""
        if self.is_running:
            self.log("浏览器已经在运行", "warning")
            return

        try:
            self.log("正在启动Playwright浏览器...")
            playwright = await async_playwright().start()

            # 创建浏览器上下文
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            # 加载已有cookies
            context_options = {}
            if os.path.exists(self.cookie_file):
                try:
                    with open(self.cookie_file, "r") as f:
                        cookies = json.load(f)
                        context_options["storage_state"] = {"cookies": cookies}
                    self.log(f"已加载cookies文件: {self.cookie_file}")
                except Exception as e:
                    self.log(f"加载cookies文件失败: {e}", "warning")

            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()

            # 设置页面超时
            self.page.set_default_timeout(30000)  # 30秒

            self.is_running = True
            self.last_error = None
            self.log("浏览器启动成功")

            # 开始连接SillyTavern
            await self.connect_to_sillytavern()

        except Exception as e:
            error_msg = f"启动浏览器失败: {str(e)}"
            self.log(error_msg, "error")
            self.last_error = error_msg
            self.is_running = False
            raise

    async def connect_to_sillytavern(self):
        """连接并登录SillyTavern"""
        if not self.is_running or not self.page:
            self.log("浏览器未运行", "error")
            return False

        try:
            self.log(f"正在连接SillyTavern: {self.st_url}")

            # 监听HTTP基本认证请求
            self.page.on("request", self._handle_request)

            # 导航到SillyTavern
            response = await self.page.goto(self.st_url, wait_until="networkidle")

            if response and response.status >= 400:
                self.log(f"SillyTavern返回错误状态: {response.status}", "warning")

            # 等待页面加载完成
            await self.page.wait_for_load_state("networkidle")

            # 检查当前页面状态
            current_url = self.page.url
            self.log(f"当前URL: {current_url}")

            # 检查是否在登录页面
            is_login_page = await self._check_login_page()

            if is_login_page:
                self.log("检测到登录页面，尝试自动登录...")
                success = await self._perform_login()
                if not success:
                    self.log("自动登录失败", "error")
                    return False

            # 检查是否成功进入主界面
            is_main_page = await self._check_main_page()

            if is_main_page:
                self.log("成功进入SillyTavern主界面")

                # 保存cookies
                await self._save_cookies()

                # 开始定期监控
                asyncio.create_task(self._monitor_browser())

                return True
            else:
                self.log("无法确定当前页面状态", "warning")
                return False

        except Exception as e:
            error_msg = f"连接SillyTavern失败: {str(e)}"
            self.log(error_msg, "error")
            self.last_error = error_msg
            return False

    async def _handle_request(self, request):
        """处理HTTP请求，用于基本认证"""
        try:
            # 检查是否需要基本认证
            if (
                request.url == self.st_url
                and self.basic_auth_user
                and self.basic_auth_pass
            ):
                headers = request.headers
                auth_header = headers.get("authorization")

                # 如果没有认证头，添加基本认证
                if not auth_header and self.basic_auth_user and self.basic_auth_pass:
                    auth_str = f"{self.basic_auth_user}:{self.basic_auth_pass}"
                    auth_b64 = base64.b64encode(auth_str.encode()).decode()

                    new_headers = dict(headers)
                    new_headers["Authorization"] = f"Basic {auth_b64}"

                    await request.continue_(headers=new_headers)
                    self.log("已添加基本认证头")
                    return
        except Exception as e:
            self.log(f"处理请求失败: {e}", "debug")

        # 默认继续请求
        try:
            await request.continue_()
        except:
            pass

    async def _check_login_page(self) -> bool:
        """检查当前是否在登录页面"""
        try:
            # 检查是否有用户选择或密码输入元素
            user_selectors = [
                'text="Select User"',
                'text="选择用户"',
                'input[type="password"]',
                'button:has-text("Login")',
                'button:has-text("登录")',
            ]

            for selector in user_selectors:
                try:
                    element = await self.page.wait_for_selector(
                        selector, timeout=5000, state="attached"
                    )
                    if element:
                        return True
                except:
                    continue

            return False

        except Exception as e:
            self.log(f"检查登录页面失败: {e}", "debug")
            return False

    async def _check_main_page(self) -> bool:
        """检查是否在SillyTavern主界面"""
        try:
            # 检查SillyTavern主界面特征元素
            main_selectors = [
                "#send_but",
                "#character_list",
                ".mes_text",
                "textarea",
                'div[data-app="chat"]',
            ]

            for selector in main_selectors:
                try:
                    element = await self.page.wait_for_selector(
                        selector, timeout=5000, state="attached"
                    )
                    if element:
                        return True
                except:
                    continue

            # 检查URL是否包含chat
            current_url = self.page.url.lower()
            if "chat" in current_url or "#" in current_url:
                return True

            return False

        except Exception as e:
            self.log(f"检查主页面失败: {e}", "debug")
            return False

    async def _perform_login(self) -> bool:
        """执行SillyTavern登录"""
        try:
            # 第一步：选择用户QQbot
            user_selectors = [
                f'text="{self.st_user}"',
                f'button:has-text("{self.st_user}")',
                f'div:has-text("{self.st_user}")',
                f'[data-user="{self.st_user}"]',
            ]

            user_element = None
            for selector in user_selectors:
                try:
                    user_element = await self.page.wait_for_selector(
                        selector, timeout=5000
                    )
                    if user_element:
                        await user_element.click()
                        self.log(f"已选择用户: {self.st_user}")
                        break
                except:
                    continue

            if not user_element:
                self.log(f"未找到用户: {self.st_user}", "warning")
                # 尝试点击第一个用户
                try:
                    first_user = await self.page.wait_for_selector(
                        'button, div[role="button"]', timeout=5000
                    )
                    if first_user:
                        await first_user.click()
                        self.log("点击了第一个用户")
                except:
                    pass

            # 等待密码输入框出现
            await asyncio.sleep(1)

            # 第二步：输入密码
            password_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="password" i]',
                'input[placeholder*="密码" i]',
            ]

            password_input = None
            for selector in password_selectors:
                try:
                    password_input = await self.page.wait_for_selector(
                        selector, timeout=5000
                    )
                    if password_input:
                        break
                except:
                    continue

            if password_input:
                await password_input.fill(self.st_pass)
                self.log("已输入密码")
            else:
                self.log("未找到密码输入框", "warning")

            # 第三步：点击登录按钮
            login_selectors = [
                'button:has-text("Login")',
                'button:has-text("登录")',
                'input[type="submit"]',
                'button[type="submit"]',
            ]

            login_button = None
            for selector in login_selectors:
                try:
                    login_button = await self.page.wait_for_selector(
                        selector, timeout=5000
                    )
                    if login_button:
                        break
                except:
                    continue

            if login_button:
                await login_button.click()
                self.log("已点击登录按钮")

                # 等待登录完成
                await asyncio.sleep(2)
                await self.page.wait_for_load_state("networkidle")

                return True
            else:
                self.log("未找到登录按钮", "warning")
                return False

        except Exception as e:
            self.log(f"执行登录失败: {str(e)}", "error")
            return False

    async def _save_cookies(self):
        """保存cookies到文件"""
        try:
            if self.context:
                cookies = await self.context.cookies()

                # 过滤和清理cookies
                cleaned_cookies = []
                for cookie in cookies:
                    # 移除不必要的字段
                    cleaned_cookie = {
                        "name": cookie.get("name"),
                        "value": cookie.get("value"),
                        "domain": cookie.get("domain"),
                        "path": cookie.get("path"),
                        "expires": cookie.get("expires"),
                        "httpOnly": cookie.get("httpOnly", False),
                        "secure": cookie.get("secure", False),
                        "sameSite": cookie.get("sameSite"),
                    }
                    cleaned_cookies.append(cleaned_cookie)

                with open(self.cookie_file, "w") as f:
                    json.dump(cleaned_cookies, f, indent=2)

                self.log(f"已保存cookies到: {self.cookie_file}")

        except Exception as e:
            self.log(f"保存cookies失败: {e}", "warning")

    async def _monitor_browser(self):
        """监控浏览器状态"""
        while self.is_running and self.page:
            try:
                # 更新截图
                await self._update_screenshot()

                # 检查页面是否仍然响应
                try:
                    await self.page.evaluate("1")
                except:
                    self.log("页面无响应，尝试重新连接", "warning")
                    await self._reconnect()

                # 检查是否被踢出登录
                if await self._check_login_page():
                    self.log("检测到登录页面，尝试重新登录", "warning")
                    await self._perform_login()

                await asyncio.sleep(5)  # 5秒检查一次

            except Exception as e:
                self.log(f"浏览器监控出错: {e}", "error")
                await asyncio.sleep(10)

    async def _update_screenshot(self):
        """更新页面截图"""
        try:
            if self.page:
                # 获取页面截图（缩小尺寸以节省内存）
                screenshot = await self.page.screenshot(
                    type="jpeg", quality=70, scale="device"
                )
                self.last_screenshot = screenshot

        except Exception as e:
            self.log(f"更新截图失败: {e}", "debug")
            self.last_screenshot = None

    async def _reconnect(self):
        """重新连接浏览器"""
        try:
            self.log("正在重新连接浏览器...")

            if self.page:
                try:
                    await self.page.close()
                except:
                    pass

            if self.context:
                try:
                    await self.context.close()
                except:
                    pass

            # 重新启动
            await self.start()

        except Exception as e:
            error_msg = f"重新连接失败: {str(e)}"
            self.log(error_msg, "error")
            self.last_error = error_msg

    async def stop(self):
        """停止浏览器"""
        if not self.is_running:
            return

        self.is_running = False
        self.log("正在停止浏览器...")

        try:
            if self.page:
                await self.page.close()

            if self.context:
                await self.context.close()

            if self.browser:
                await self.browser.close()

            self.page = None
            self.context = None
            self.browser = None

            self.log("浏览器已停止")

        except Exception as e:
            self.log(f"停止浏览器时出错: {e}", "error")

    async def restart(self):
        """重启浏览器"""
        self.log("正在重启浏览器...")
        await self.stop()
        await asyncio.sleep(1)
        await self.start()

    async def get_screenshot_base64(self) -> Optional[str]:
        """获取截图Base64编码"""
        if self.last_screenshot:
            return base64.b64encode(self.last_screenshot).decode("utf-8")
        return None

    def get_status(self) -> Dict[str, Any]:
        """获取浏览器状态"""
        return {
            "is_running": self.is_running,
            "has_screenshot": self.last_screenshot is not None,
            "last_error": self.last_error,
            "st_url": self.st_url,
            "st_user": self.st_user,
        }
