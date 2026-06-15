# Website Agent Server

English | [中文](README.zh-CN.md)

Website Agent Server is a Python server-side browser proxy. The client never loads the target website directly. It connects only to this server, receives rendered browser frames, and sends mouse, keyboard, input method, clipboard, wheel, file upload, and navigation events back to the server.

## How It Works

- FastAPI serves the local control UI, HTTP API, and WebSocket endpoint.
- Playwright launches Chromium on the server.
- The target website runs inside the server-side browser context.
- The client receives JPEG screenshots of that browser viewport.
- User actions are replayed into Chromium by the server.
- IME text, paste, copy, cut, downloads, and file chooser actions are brokered through local server endpoints.

Because the remote page is never embedded as HTML in the client, page scripts, link clicks, images, XHR/fetch calls, WebSocket connections, and form submissions are performed by the server-side browser.

## Setup

Create or reuse the repository-root virtual environment:

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m playwright install chromium
```

## Run

```powershell
venv\Scripts\python.exe -m website_agent_server
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), enter a website URL, and operate the remote site through the rendered viewport.

## Command-Line Configuration

```powershell
venv\Scripts\python.exe -m website_agent_server --port 8080 --headed
```

Require a PIN before clients can use the proxy:

```powershell
venv\Scripts\python.exe -m website_agent_server --pin 123456
```

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Server bind host. |
| `--allow-lan` | disabled | Allow LAN clients by binding to `0.0.0.0` unless `--host` is set. |
| `--port` | `8000` | Server port. |
| `--headed` | disabled | Run Chromium with a visible browser window. |
| `--ignore-https-errors` | disabled | Ignore remote TLS certificate errors. |
| `--allow-private-hosts` | disabled | Allow navigation and resource requests to private, local, or reserved networks. |
| `--session-ttl-seconds` | `900` | Idle session lifetime. |
| `--navigation-timeout-ms` | `30000` | Navigation timeout. |
| `--frame-interval-seconds` | `0.18` | Screenshot streaming interval. |
| `--screenshot-quality` | `72` | JPEG frame quality, from 1 to 100. |
| `--min-viewport-width` | `320` | Minimum remote viewport width. |
| `--min-viewport-height` | `240` | Minimum remote viewport height. |
| `--max-viewport-width` | `1920` | Maximum remote viewport width. |
| `--max-viewport-height` | `1600` | Maximum remote viewport height. |
| `--data-dir` | `.agent-data` | Runtime downloads and temporary uploads directory. |
| `--pin` | disabled | Require this PIN before clients can access the proxy UI, API, or WebSocket. |

Private and local network targets are blocked by default to reduce SSRF risk. Use `--allow-private-hosts` only when you trust the users who can access the proxy.

When exposing the server to a LAN, prefer using a PIN:

```powershell
venv\Scripts\python.exe -m website_agent_server --allow-lan --pin 123456
```

## Limitations

This project proxies interaction by streaming rendered browser frames, not by rewriting HTML. That keeps remote network access on the server, but it also means the client sees a bitmap viewport rather than native DOM nodes. Browser extension APIs, local client certificates, DRM-protected media, and some system dialogs are outside the current scope.
