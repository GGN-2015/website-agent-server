# Website Agent Server

[English](README.md) | 中文

Website Agent Server 是一个 Python 服务端浏览器代理。客户端不会直接加载目标网站；客户端只连接本服务器，接收服务端浏览器渲染后的画面，并把鼠标、键盘、输入法、剪贴板、滚轮、文件上传和导航操作发送回服务器执行。

## 工作方式

- FastAPI 提供本地控制界面、HTTP API 和 WebSocket 端点。
- Playwright 在服务器上启动 Chromium。
- 目标网站运行在服务器端的浏览器上下文中。
- 客户端只接收该浏览器视口的 JPEG 截图帧。
- 用户操作由服务器转发并在 Chromium 中重放。
- 输入法文本、粘贴、复制、剪切、下载和文件选择通过本服务器接口中转。

因为远程页面不会以 HTML 形式嵌入客户端，所以页面脚本、链接点击、图片、XHR/fetch 请求、WebSocket 连接和表单提交都由服务器端浏览器完成。

## 安装

在仓库根目录创建或复用 `venv`：

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m playwright install chromium
```

## 运行

```powershell
venv\Scripts\python.exe -m website_agent_server
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，输入目标网站 URL，然后在渲染视口中操作远程网站。

## 命令行配置

```powershell
venv\Scripts\python.exe -m website_agent_server --port 8080 --headed
```

要求用户先输入 PIN 才能使用代理：

```powershell
venv\Scripts\python.exe -m website_agent_server --pin 123456
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 服务监听地址。 |
| `--allow-lan` | 禁用 | 允许局域网客户端访问；如果没有设置 `--host`，会监听 `0.0.0.0`。 |
| `--port` | `8000` | 服务端口。 |
| `--headed` | 禁用 | 使用可见浏览器窗口运行 Chromium。 |
| `--ignore-https-errors` | 禁用 | 忽略远程 TLS 证书错误。 |
| `--allow-private-hosts` | 禁用 | 允许访问私有、本地或保留网段。 |
| `--session-ttl-seconds` | `900` | 空闲会话保留时间。 |
| `--navigation-timeout-ms` | `30000` | 导航超时时间。 |
| `--frame-interval-seconds` | `0.18` | 截图帧推送间隔。 |
| `--screenshot-quality` | `72` | JPEG 画面质量，范围 1 到 100。 |
| `--min-viewport-width` | `320` | 最小远程视口宽度。 |
| `--min-viewport-height` | `240` | 最小远程视口高度。 |
| `--max-viewport-width` | `1920` | 最大远程视口宽度。 |
| `--max-viewport-height` | `1600` | 最大远程视口高度。 |
| `--data-dir` | `.agent-data` | 运行时下载和临时上传目录。 |
| `--pin` | 禁用 | 要求用户提供该 PIN 后才能访问代理 UI、API 和 WebSocket。 |

默认会阻止私有和本地网络目标，以降低 SSRF 风险。只有在你信任所有能访问该代理的用户时，才建议使用 `--allow-private-hosts`。

如果要暴露给局域网，建议同时设置 PIN：

```powershell
venv\Scripts\python.exe -m website_agent_server --allow-lan --pin 123456
```

## 限制

本项目通过流式传输服务端浏览器画面来代理交互，而不是重写 HTML。这样可以确保远程网络访问发生在服务器端，但客户端看到的是位图视口，不是原生 DOM。浏览器扩展 API、本地客户端证书、受 DRM 保护的媒体，以及部分系统级对话框不在当前范围内。
