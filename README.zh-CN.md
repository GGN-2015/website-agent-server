# Website Agent Server

[English](README.md) | 中文

Website Agent Server 是一个 Python 服务端浏览器代理。客户端不会直接加载目标网站；客户端只连接本服务器，接收服务端浏览器渲染后的画面，并把鼠标、键盘、输入法、剪贴板、滚轮、文件上传和导航操作发送回服务器执行。

## 工作方式

- FastAPI 提供本地控制界面、HTTP API 和 WebSocket 端点。
- Playwright 在服务器上启动 Chromium。
- 目标网站运行在服务器端的浏览器上下文中。
- 客户端默认接收该浏览器视口的 JPEG 截图帧；当使用 `--screenshot-quality 100` 时接收 PNG 无损帧。
- 用户操作由服务器转发并在 Chromium 中重放。
- 输入法文本、粘贴、复制、剪切、下载、文件选择和 Cookie 管理通过本服务器接口中转。

因为远程页面不会以 HTML 形式嵌入客户端，所以页面脚本、链接点击、图片、XHR/fetch 请求、WebSocket 连接和表单提交都由服务器端浏览器完成。

## 安装

在仓库根目录创建或复用 `venv`：

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果 Playwright 找不到 Chromium，服务端会在首次启动时自动下载。若希望提前下载，可以手动运行 `venv\Scripts\python.exe -m playwright install chromium`。在 Linux 上，如果 Chromium 已经下载成功但仍因为缺少系统依赖无法启动，请用相应权限运行 `python -m playwright install-deps chromium`。

## 运行

```powershell
venv\Scripts\python.exe -m website_agent_server
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，输入目标网站 URL，然后在渲染视口中操作远程网站。服务默认监听所有网卡，所以局域网客户端也可以用服务器机器的局域网 IP 连接。

如果目标 URL 没有填写 `http://` 或 `https://` 前缀，服务端会先探测 HTTPS；HTTPS 服务可用时使用 HTTPS，否则回退到 HTTP。`--lock-url` 和 `/lock_url/...` 路径也遵循同样规则。

也可以只锁定某一个客户端，把目标 URL 放到服务器 URL 路径里：

```text
http://127.0.0.1:8000/lock_url/https/example.com/path
```

这个客户端会立即打开目标网站，并隐藏浏览器选项控件。其他访问 `/` 的客户端仍然保持普通 URL 输入模式。查询参数和片段会保留，例如 `/lock_url/https/example.com/path?x=1#section`。

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
| `--host` | `0.0.0.0` | 服务监听地址。如果只想允许本机访问，使用 `127.0.0.1`。 |
| `--port` | `8000` | 服务端口。 |
| `--headed` | 禁用 | 使用可见浏览器窗口运行 Chromium。 |
| `--ignore-https-errors` | 禁用 | 忽略远程 TLS 证书错误。 |
| `--allow-private-hosts` | 禁用 | 允许访问私有、本地或保留网段。 |
| `--locale` | `zh-CN` | 暴露给远程网站的浏览器语言区域。 |
| `--timezone-id` | `Asia/Shanghai` | 暴露给远程网站的浏览器时区。 |
| `--accept-language` | `zh-CN,zh;q=0.9,en;q=0.8` | 浏览器上下文发送的 `Accept-Language` 请求头。 |
| `--user-agent` | 自动 | 桌面端浏览器 User-Agent。默认会根据内置 Chromium 版本生成普通 Chrome UA，而不是暴露 `HeadlessChrome`。 |
| `--session-ttl-seconds` | `600` | 客户端断线后的会话和客户端浏览器上下文保留时间。客户端可在该时间内重连到缓存的浏览器会话。 |
| `--shutdown-timeout-seconds` | `3.0` | Ctrl+C 后等待活动 HTTP/WebSocket 连接优雅关闭的最长时间。 |
| `--navigation-timeout-ms` | `30000` | 导航超时时间。 |
| `--frame-interval-seconds` | `0.18` | 截图帧推送间隔。 |
| `--screenshot-quality` | `95` | 画面质量，范围 1 到 100。低于 100 使用 JPEG；100 使用 PNG。 |
| `--min-viewport-width` | `320` | 最小远程视口宽度。 |
| `--min-viewport-height` | `240` | 最小远程视口高度。 |
| `--max-viewport-width` | `1920` | 最大远程视口宽度。 |
| `--max-viewport-height` | `1600` | 最大远程视口高度。 |
| `--data-dir` | `.agent-data` | 运行时下载和临时上传目录。 |
| `--pin` | 禁用 | 要求用户提供该 PIN 后才能访问代理 UI、API 和 WebSocket。 |
| `--lock-url` | 禁用 | 自动打开该 URL，并隐藏/禁用 Back、Forward、Cookie、Quit 和地址导航等浏览器选项控件。如果配置了 PIN，认证仍然生效。 |

默认会阻止私有和本地网络目标，以降低 SSRF 风险。只有在你信任所有能访问该代理的用户时，才建议使用 `--allow-private-hosts`。

因为默认允许局域网访问，建议同时设置 PIN：

```powershell
venv\Scripts\python.exe -m website_agent_server --pin 123456
```

如果代理本身也需要打开局域网或本机目标 URL，需要显式允许私有目标：

```powershell
venv\Scripts\python.exe -m website_agent_server --allow-private-hosts --pin 123456
```

每个客户端只会得到一个服务端 Playwright `BrowserContext`，它只由本地 `session-uuid` Cookie 决定。上下文不会再按 IP 地址、目标 host、端口、URL 路径或设备类型共享。同一个客户端在 UUID 过期前打开另一个目标 URL 时，旧页面会关闭，但会复用同一个上下文，包括存储分区、Service Worker、权限和其他浏览器上下文状态。

桌面端客户端默认会使用普通 Chrome 风格的 User-Agent、语言区域、时区、`Accept-Language` 和 Client Hints。这样可以改善某些网站因为明显的 headless 浏览器元数据而直接拒绝访问的问题，但不会绕过账号校验、限流、验证码或其他网站访问控制。

手机端客户端会使用移动端 Playwright 浏览器配置，包括窄视口、触控能力、移动版 Chromium User-Agent、语言请求头和移动端 Client Hints，方便上游响应式网站选择手机页面。

本地 `session-uuid` Cookie 只用于识别 Website Agent 客户端，不是远程网站 Cookie。如果本地页面刷新或 WebSocket 掉线，服务端会用这个 Cookie 把同一个客户端重新连接到已有浏览器会话。断线浏览器会话和空闲客户端上下文会在 `--session-ttl-seconds` 秒后删除，默认是 10 分钟。某个 UUID 被删除时，它对应的 BrowserContext、浏览历史、Cookie、localStorage、IndexedDB、下载文件、上传文件和内存会话记录会一起删除。

服务端关闭了 Uvicorn 的 WebSocket ping keepalive，因为手机浏览器在加载、切后台或休眠时可能挂起 socket。客户端重连逻辑和会话 TTL 会处理这类断线，同时避免 keepalive traceback 刷屏。

## 限制

本项目通过流式传输服务端浏览器画面来代理交互，而不是重写 HTML。这样可以确保远程网络访问发生在服务器端，但客户端看到的是位图视口，不是原生 DOM。浏览器扩展 API、本地客户端证书、受 DRM 保护的媒体，以及部分系统级对话框不在当前范围内。
