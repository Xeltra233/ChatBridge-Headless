# ChatBridge-Headless 部署教程

## 架构说明

```
NapCat → ChatBridge-Headless(8003) → WebSocket(8001) → SillyTavern扩展 → SillyTavern调LLM
```

---

## 前提条件

- Zeabur 同一个专案内已有 SillyTavern 服务
- SillyTavern 已安装 ChatBridge 扩展（SillyTavern-Extension-ChatBridge）

---

## 第一步：部署 ChatBridge-Headless

1. 在 Zeabur 专案内新建服务，选择「从 GitHub 部署」
2. 选择仓库 `ChatBridge-Headless`
3. Zeabur 会自动用 Dockerfile 构建

---

## 第二步：添加环境变量

在 Zeabur 控制台 → 该服务 → 「变量」分页，添加：

| 变量名 | 值 |
|--------|-----|
| `CB_ADMIN_PASSWORD` | 你的管理UI登录密码（随便设） |

**只需要这一个环境变量，其余配置在管理UI里填。**

---

## 第三步：挂载数据目录

在 Zeabur 控制台 → 该服务 → 「存储」分页，添加挂载：

| 挂载路径 |
|----------|
| `/app/data` |

这样 `settings.json` 和 `cookies.json` 重启后不会丢失。

---

## 第四步：暴露端口，绑定域名

在 Zeabur 控制台 → 该服务 → 「网络」分页：

- 添加端口 `8080`，绑定一个公网域名（用于访问管理UI）
- 其余端口（8001、8003）**不需要**对外暴露，走内部网络即可

---

## 第五步：登录管理UI，填写配置

访问你绑定的域名，用 `CB_ADMIN_PASSWORD` 设置的密码登录。

进入「设置」页，将以下字段填写完整后保存：

```json
{
    "websocket": {
        "host": "0.0.0.0",
        "port": 8001,
        "token": "填一个随机字符串，ST扩展连接时需要"
    },
    "user_api": {
        "host": "0.0.0.0",
        "port": 8003,
        "api_key": "填一个随机字符串，NapCat调用时需要"
    },
    "retry": {
        "max_retries": 3,
        "retry_delay": 5,
        "timeout": 120
    },
    "browser": {
        "st_url": "http://sillytavern-conel.zeabur.internal:8000",
        "basic_auth_user": "SillyTavern的Basic Auth用户名",
        "basic_auth_pass": "SillyTavern的Basic Auth密码",
        "st_user": "QQbot",
        "st_pass": "QQbot用户的登录密码",
        "cookie_file": "cookies.json"
    },
    "admin_ui": {
        "host": "0.0.0.0",
        "port": 8080,
        "password": ""
    }
}
```

> `host` 字段全部保持 `0.0.0.0`，不要修改。
> `admin_ui.password` 留空即可，密码由环境变量 `CB_ADMIN_PASSWORD` 控制。

保存后在 Zeabur 重启服务。

---

## 第六步：配置 SillyTavern 扩展

在 SillyTavern 界面，打开 ChatBridge 扩展设置：

| 字段 | 填写内容 |
|------|----------|
| 服务器地址 | `chatbridge-headless.zeabur.internal`（ChatBridge服务的内部域名） |
| 端口 | `8001` |
| Token | 与第五步 `websocket.token` 一致 |

点击「连接」，状态变绿即成功。

> 内部域名格式：Zeabur 控制台 → ChatBridge-Headless服务 → 「网络」分页 → 内部域名

---

## 第七步：配置 NapCat

NapCat 的 LLM API 地址填写：

```
http://chatbridge-headless.zeabur.internal:8003/v1
```

API Key 填写第五步 `user_api.api_key` 的值。

---

## 验证是否正常

1. 管理UI「概览」页：
   - 浏览器状态显示「运行中」
   - WebSocket 连接数显示 `1`（ST扩展已连接）

2. 在 NapCat 发送一条消息，管理UI「日志」页应该能看到请求流转的日志

---

## 常见问题

**浏览器状态一直「已停止」**
→ 检查 `browser.basic_auth_user/pass` 和 `st_pass` 是否填写正确
→ 查看日志页的具体错误信息

**WebSocket 连接数为 0**
→ 检查 SillyTavern 扩展里填的地址和 Token 是否正确
→ 确认 ChatBridge-Headless 和 SillyTavern 在同一个 Zeabur 专案内

**NapCat 调用返回 503**
→ WebSocket 未连接，先解决上一个问题

**管理UI打不开**
→ 确认 Zeabur 网络分页已暴露 8080 端口并绑定了域名
