# Website Agent Server

English | [中文](README.zh-CN.md)

Website Agent Server is a Python server-side browser proxy. The client never loads the target website directly. It connects only to this server, receives rendered browser frames, and sends mouse, keyboard, input method, clipboard, wheel, file upload, and navigation events back to the server.

## How It Works

- FastAPI serves the local control UI, HTTP API, and WebSocket endpoint.
- Playwright launches Chromium on the server.
- The target website runs inside the server-side browser context.
- The client receives JPEG screenshots of that browser viewport.
- User actions are replayed into Chromium by the server.
- IME text, paste, copy, cut, downloads, file chooser actions, and cookie management are brokered through local server endpoints.

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), enter a website URL, and operate the remote site through the rendered viewport. By default the server listens on all interfaces, so LAN clients can also connect with the server machine's LAN IP.

If a target URL is entered without an `http://` or `https://` prefix, the server first probes HTTPS. It uses HTTPS when the TLS service is available, otherwise it falls back to HTTP. The same rule applies to `--lock-url` and `/lock_url/...` paths.

You can lock only one client by putting the target URL in the server URL path:

```text
http://127.0.0.1:8000/lock_url/https/example.com/path
```

That client opens the target immediately and hides the browser option controls. Other clients that open `/` keep the normal URL picker. Query strings and fragments are preserved, for example `/lock_url/https/example.com/path?x=1#section`.

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
| `--host` | `0.0.0.0` | Server bind host. Use `127.0.0.1` to restrict access to this machine. |
| `--port` | `8000` | Server port. |
| `--headed` | disabled | Run Chromium with a visible browser window. |
| `--ignore-https-errors` | disabled | Ignore remote TLS certificate errors. |
| `--allow-private-hosts` | disabled | Allow navigation and resource requests to private, local, or reserved networks. |
| `--session-ttl-seconds` | `600` | Disconnected client session and client browser context lifetime. A client can reconnect to its cached browser session during this window. |
| `--navigation-timeout-ms` | `30000` | Navigation timeout. |
| `--frame-interval-seconds` | `0.18` | Screenshot streaming interval. |
| `--screenshot-quality` | `95` | JPEG frame quality, from 1 to 100. |
| `--min-viewport-width` | `320` | Minimum remote viewport width. |
| `--min-viewport-height` | `240` | Minimum remote viewport height. |
| `--max-viewport-width` | `1920` | Maximum remote viewport width. |
| `--max-viewport-height` | `1600` | Maximum remote viewport height. |
| `--data-dir` | `.agent-data` | Runtime downloads and temporary uploads directory. |
| `--pin` | disabled | Require this PIN before clients can access the proxy UI, API, or WebSocket. |
| `--lock-url` | disabled | Open this URL automatically and hide/disable browser option controls such as Back, Forward, Cookie, Quit, and address navigation. PIN authentication still applies when configured. |

Private and local network targets are blocked by default to reduce SSRF risk. Use `--allow-private-hosts` only when you trust the users who can access the proxy.

Because LAN access is enabled by default, prefer using a PIN:

```powershell
venv\Scripts\python.exe -m website_agent_server --pin 123456
```

If the proxy itself also needs to open LAN or localhost target URLs, enable private hosts explicitly:

```powershell
venv\Scripts\python.exe -m website_agent_server --allow-private-hosts --pin 123456
```

Each client receives exactly one server-side Playwright `BrowserContext`, keyed only by its local `session-uuid` cookie. Contexts are never shared by IP address, target host, port, URL path, or device class. If the same client opens another target URL before its UUID expires, the old page is closed and the same context is reused, including storage partitions, service workers, permissions, and other browser context state.

Mobile clients use a mobile Playwright browser profile with a narrow viewport, touch support, a mobile Chromium user agent, and mobile Client Hints so upstream responsive sites can select their mobile layout.

The local `session-uuid` cookie only identifies the Website Agent client, not the remote site. If the local page refreshes or the WebSocket drops, the server uses that cookie to reconnect the same client to its existing browser session. Disconnected browser sessions and idle client contexts are removed after `--session-ttl-seconds`, which is 10 minutes by default. When a UUID is removed, its BrowserContext, browsing history, cookies, localStorage, IndexedDB, download files, upload files, and in-memory session records are removed together.

Uvicorn's WebSocket ping keepalive is disabled because mobile browsers may suspend sockets while loading, switching apps, or sleeping. The client reconnect path and session TTL handle those drops without printing keepalive tracebacks.

## Limitations

This project proxies interaction by streaming rendered browser frames, not by rewriting HTML. That keeps remote network access on the server, but it also means the client sees a bitmap viewport rather than native DOM nodes. Browser extension APIs, local client certificates, DRM-protected media, and some system dialogs are outside the current scope.
