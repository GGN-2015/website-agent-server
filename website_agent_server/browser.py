from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    FileChooser,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    WebSocketRoute,
    async_playwright,
)

from .config import Settings
from .url_policy import HostAccessPolicy, URLPolicyError


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return cleaned or "download"


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class DownloadRecord:
    filename: str
    path: Path


class BrowserSession:
    def __init__(self, manager: "BrowserManager", session_id: str) -> None:
        self.manager = manager
        self.id = session_id
        self.settings = manager.settings
        self.policy = HostAccessPolicy(self.settings.allow_private_hosts)
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.viewport_width = 1280
        self.viewport_height = 720
        self.last_activity = time.monotonic()
        self.downloads: dict[str, DownloadRecord] = {}
        self.file_choosers: dict[str, FileChooser] = {}
        self._action_lock = asyncio.Lock()
        self._outgoing: asyncio.Queue[dict[str, Any]] | None = None
        self._closed = False

    async def start(self, raw_url: str, width: int, height: int) -> None:
        self.viewport_width = _clamp(
            width, self.settings.min_viewport_width, self.settings.max_viewport_width
        )
        self.viewport_height = _clamp(
            height, self.settings.min_viewport_height, self.settings.max_viewport_height
        )
        browser = await self.manager.get_browser()
        self.context = await browser.new_context(
            accept_downloads=True,
            ignore_https_errors=self.settings.ignore_https_errors,
            viewport={"width": self.viewport_width, "height": self.viewport_height},
        )
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        await self.context.route("**/*", self._guard_route)
        await self.context.route_web_socket("**/*", self._guard_websocket)
        page = await self.context.new_page()
        await self._attach_page(page)
        await self.navigate(raw_url)

    async def close(self) -> None:
        self._closed = True
        context = self.context
        self.context = None
        self.page = None
        if context is not None:
            await _ignore_shutdown_disconnect(context.close())

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        self._outgoing = outgoing
        await self._queue_message(self._status_message("connected"))

        tasks = {
            asyncio.create_task(self._send_loop(websocket, outgoing)),
            asyncio.create_task(self._receive_loop(websocket)),
            asyncio.create_task(self._frame_loop()),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
            for task in pending:
                task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            if self._outgoing is outgoing:
                self._outgoing = None

    async def navigate(self, raw_url: str) -> None:
        page = self._require_page()
        url = await self.policy.ensure_navigation_url_allowed(raw_url)
        self.last_activity = time.monotonic()
        await self._queue_message({"type": "status", "state": "loading", "url": url})
        async with self._action_lock:
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                await self._queue_message(
                    {
                        "type": "warning",
                        "message": "Navigation timed out; showing the current browser state.",
                    }
                )
        await self._queue_message(self._status_message("ready"))

    async def upload_file_chooser(self, token: str, paths: list[Path]) -> None:
        chooser = self.file_choosers.pop(token, None)
        if chooser is None:
            raise KeyError("File chooser is no longer available.")
        await chooser.set_files([str(path) for path in paths])
        await self._queue_message({"type": "status", "state": "files-selected"})

    def get_download(self, token: str) -> DownloadRecord | None:
        return self.downloads.get(token)

    async def handle_message(self, payload: dict[str, Any]) -> None:
        self.last_activity = time.monotonic()
        message_type = payload.get("type")
        if message_type == "resize":
            await self._resize(int(payload.get("width", self.viewport_width)), int(payload.get("height", self.viewport_height)))
        elif message_type == "navigate":
            await self.navigate(str(payload.get("url", "")))
        elif message_type == "reload":
            await self._reload()
        elif message_type == "back":
            await self._go_back()
        elif message_type == "forward":
            await self._go_forward()
        elif message_type == "mouse_move":
            await self._mouse_move(payload)
        elif message_type == "mouse_down":
            await self._mouse_button(payload, down=True)
        elif message_type == "mouse_up":
            await self._mouse_button(payload, down=False)
        elif message_type == "wheel":
            await self._wheel(payload)
        elif message_type == "key":
            await self._press_key(payload)
        elif message_type == "text":
            await self._insert_text(str(payload.get("text", "")))
        elif message_type == "paste":
            await self._paste_text(str(payload.get("text", "")))
        elif message_type == "copy":
            await self.copy_selection(cut=False)
        elif message_type == "cut":
            await self.copy_selection(cut=True)

    async def _attach_page(self, page: Page) -> None:
        self.page = page
        page.on("popup", lambda popup: asyncio.create_task(self._on_popup(popup)))
        page.on("download", lambda download: asyncio.create_task(self._on_download(download)))
        page.on("filechooser", lambda chooser: asyncio.create_task(self._on_filechooser(chooser)))
        page.on("dialog", lambda dialog: asyncio.create_task(self._on_dialog(dialog)))
        page.on("framenavigated", lambda frame: asyncio.create_task(self._on_frame_navigated(frame)))

    async def _guard_route(self, route: Route) -> None:
        url = route.request.url
        try:
            await self.policy.ensure_request_url_allowed(url)
            await route.continue_()
        except URLPolicyError as exc:
            await route.abort()
            await self._queue_message({"type": "blocked", "url": url, "reason": str(exc)})

    async def _guard_websocket(self, websocket: WebSocketRoute) -> None:
        url = websocket.url
        try:
            await self.policy.ensure_request_url_allowed(url)
            websocket.connect_to_server()
        except URLPolicyError as exc:
            await websocket.close(code=1008, reason=str(exc)[:120])
            await self._queue_message({"type": "blocked", "url": url, "reason": str(exc)})

    async def _on_popup(self, popup: Page) -> None:
        await self._attach_page(popup)
        await popup.set_viewport_size({"width": self.viewport_width, "height": self.viewport_height})
        await self._queue_message({"type": "status", "state": "popup", "url": popup.url})

    async def _on_download(self, download: Download) -> None:
        token = secrets.token_urlsafe(18)
        filename = _safe_filename(download.suggested_filename or "download")
        download_dir = self.settings.downloads_dir / self.id
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / f"{token}-{filename}"
        await download.save_as(str(path))
        self.downloads[token] = DownloadRecord(filename=filename, path=path)
        await self._queue_message(
            {
                "type": "download",
                "filename": filename,
                "url": f"/api/sessions/{self.id}/downloads/{token}",
            }
        )

    async def _on_filechooser(self, chooser: FileChooser) -> None:
        token = secrets.token_urlsafe(18)
        self.file_choosers[token] = chooser
        await self._queue_message(
            {"type": "filechooser", "token": token, "multiple": chooser.is_multiple()}
        )

    async def _on_dialog(self, dialog: Any) -> None:
        message = dialog.message
        dialog_type = dialog.type
        default_value = getattr(dialog, "default_value", "") or ""
        await dialog.accept(default_value)
        await self._queue_message({"type": "dialog", "dialogType": dialog_type, "message": message})

    async def _on_frame_navigated(self, frame: Any) -> None:
        page = self.page
        if page is not None and frame == page.main_frame:
            await self._queue_message(self._status_message("ready"))

    async def _send_loop(
        self, websocket: WebSocket, outgoing: asyncio.Queue[dict[str, Any]]
    ) -> None:
        while True:
            message = await outgoing.get()
            await websocket.send_text(json.dumps(message, separators=(",", ":")))

    async def _receive_loop(self, websocket: WebSocket) -> None:
        while True:
            text = await websocket.receive_text()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                try:
                    await self.handle_message(payload)
                except URLPolicyError as exc:
                    await self._queue_message({"type": "error", "message": str(exc)})
                except Exception as exc:
                    await self._queue_message(
                        {"type": "warning", "message": f"Input event ignored: {exc}"}
                    )

    async def _frame_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.settings.frame_interval_seconds)
            frame = await self._capture_frame()
            if frame is not None:
                await self._queue_frame(frame)

    async def _capture_frame(self) -> dict[str, Any] | None:
        page = self.page
        if page is None or page.is_closed():
            return None
        try:
            async with self._action_lock:
                image = await page.screenshot(
                    type="jpeg",
                    quality=self.settings.screenshot_quality,
                    full_page=False,
                    timeout=8000,
                )
                title = await page.title()
                url = page.url
        except Exception as exc:
            await self._queue_message({"type": "warning", "message": f"Frame capture failed: {exc}"})
            return None

        return {
            "type": "frame",
            "mime": "image/jpeg",
            "image": base64.b64encode(image).decode("ascii"),
            "width": self.viewport_width,
            "height": self.viewport_height,
            "url": url,
            "title": title,
        }

    async def _resize(self, width: int, height: int) -> None:
        self.viewport_width = _clamp(
            width, self.settings.min_viewport_width, self.settings.max_viewport_width
        )
        self.viewport_height = _clamp(
            height, self.settings.min_viewport_height, self.settings.max_viewport_height
        )
        page = self._require_page()
        async with self._action_lock:
            await page.set_viewport_size({"width": self.viewport_width, "height": self.viewport_height})

    async def _reload(self) -> None:
        page = self._require_page()
        async with self._action_lock:
            await page.reload(wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms)

    async def _go_back(self) -> None:
        page = self._require_page()
        async with self._action_lock:
            await page.go_back(wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms)

    async def _go_forward(self) -> None:
        page = self._require_page()
        async with self._action_lock:
            await page.go_forward(wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms)

    async def _mouse_move(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        x, y = self._point(payload)
        async with self._action_lock:
            await page.mouse.move(x, y)

    async def _mouse_button(self, payload: dict[str, Any], down: bool) -> None:
        page = self._require_page()
        x, y = self._point(payload)
        button = str(payload.get("button", "left"))
        if button not in {"left", "middle", "right"}:
            button = "left"
        async with self._action_lock:
            await page.mouse.move(x, y)
            if down:
                await page.mouse.down(button=button)
            else:
                await page.mouse.up(button=button)

    async def _wheel(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        delta_x = float(payload.get("deltaX", 0))
        delta_y = float(payload.get("deltaY", 0))
        async with self._action_lock:
            await page.mouse.wheel(delta_x, delta_y)

    async def _press_key(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        key = str(payload.get("key", ""))
        if not key:
            return
        playwright_key = self._playwright_key(key, payload)
        if playwright_key is None:
            return
        async with self._action_lock:
            await page.keyboard.press(playwright_key)

    async def _insert_text(self, text: str) -> None:
        if not text:
            return
        page = self._require_page()
        async with self._action_lock:
            await page.keyboard.insert_text(text)

    async def _paste_text(self, text: str) -> None:
        if not text:
            return
        page = self._require_page()
        async with self._action_lock:
            if await self._write_clipboard_text(page, text):
                await page.keyboard.press("Control+V")
                return
            pasted = await self._dispatch_synthetic_paste(page, text)
            if not pasted:
                await page.keyboard.insert_text(text)

    async def _write_clipboard_text(self, page: Page, text: str) -> bool:
        try:
            await page.evaluate(
                """async (value) => {
                    if (!navigator.clipboard || !navigator.clipboard.writeText) {
                        return false;
                    }
                    await navigator.clipboard.writeText(value);
                    return true;
                }""",
                text,
            )
        except Exception:
            return False
        return True

    async def _dispatch_synthetic_paste(self, page: Page, text: str) -> bool:
        try:
            result = await page.evaluate(
                """(value) => {
                    const active = document.activeElement;
                    const target = active && active !== document.body ? active : document.body;
                    let event;
                    try {
                        const data = new DataTransfer();
                        data.setData("text/plain", value);
                        event = new ClipboardEvent("paste", {
                            bubbles: true,
                            cancelable: true,
                            clipboardData: data,
                        });
                    } catch {
                        event = new Event("paste", { bubbles: true, cancelable: true });
                    }

                    const accepted = target.dispatchEvent(event);
                    if (!accepted) {
                        return true;
                    }

                    if (!active) {
                        return false;
                    }

                    const tagName = active.tagName;
                    const editableInput =
                        tagName === "TEXTAREA" ||
                        (tagName === "INPUT" &&
                            ![
                                "button",
                                "checkbox",
                                "color",
                                "file",
                                "hidden",
                                "image",
                                "radio",
                                "range",
                                "reset",
                                "submit",
                            ].includes(active.type));

                    if (editableInput) {
                        const start = active.selectionStart ?? active.value.length;
                        const end = active.selectionEnd ?? active.value.length;
                        active.setRangeText(value, start, end, "end");
                        active.dispatchEvent(new InputEvent("input", {
                            bubbles: true,
                            inputType: "insertFromPaste",
                            data: value,
                        }));
                        return true;
                    }

                    if (active.isContentEditable) {
                        document.execCommand("insertText", false, value);
                        return true;
                    }

                    return false;
                }""",
                text,
            )
        except Exception:
            return False
        return bool(result)

    async def copy_selection(self, cut: bool) -> str:
        page = self._require_page()
        async with self._action_lock:
            fallback = await self._selected_text(page)
            await page.keyboard.press("Control+X" if cut else "Control+C")
            await asyncio.sleep(0.05)
            text = await self._clipboard_text(page)
        result = text or fallback
        await self._queue_message({"type": "clipboard", "text": result})
        return result

    async def _clipboard_text(self, page: Page) -> str:
        try:
            result = await page.evaluate(
                "() => navigator.clipboard ? navigator.clipboard.readText() : ''"
            )
        except Exception:
            return ""
        return result if isinstance(result, str) else ""

    async def _selected_text(self, page: Page) -> str:
        try:
            result = await page.evaluate(
                """() => {
                    const active = document.activeElement;
                    if (
                        active &&
                        (active.tagName === "TEXTAREA" ||
                            (active.tagName === "INPUT" &&
                                !["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"].includes(active.type)))
                    ) {
                        const start = active.selectionStart ?? 0;
                        const end = active.selectionEnd ?? 0;
                        return active.value.slice(start, end);
                    }
                    const selection = window.getSelection();
                    return selection ? selection.toString() : "";
                }"""
            )
        except Exception:
            return ""
        return result if isinstance(result, str) else ""

    def _point(self, payload: dict[str, Any]) -> tuple[float, float]:
        x = float(payload.get("x", 0))
        y = float(payload.get("y", 0))
        x = max(0.0, min(float(self.viewport_width), x))
        y = max(0.0, min(float(self.viewport_height), y))
        return x, y

    def _playwright_key(self, key: str, payload: dict[str, Any]) -> str | None:
        ignored_keys = {
            "Process",
            "Unidentified",
            "Dead",
            "Compose",
            "Convert",
            "NonConvert",
            "KanaMode",
            "HangulMode",
            "JunjaMode",
            "FinalMode",
            "HanjaMode",
            "KanjiMode",
            "ModeChange",
        }
        if key in ignored_keys:
            return None
        key_map = {
            " ": "Space",
            "ArrowUp": "ArrowUp",
            "ArrowDown": "ArrowDown",
            "ArrowLeft": "ArrowLeft",
            "ArrowRight": "ArrowRight",
            "Escape": "Escape",
            "Enter": "Enter",
            "Tab": "Tab",
            "Backspace": "Backspace",
            "Delete": "Delete",
            "Home": "Home",
            "End": "End",
            "PageUp": "PageUp",
            "PageDown": "PageDown",
        }
        normalized = key_map.get(key, key)
        if len(normalized) > 1 and normalized not in set(key_map.values()) and not re.fullmatch(
            r"F(?:[1-9]|1[0-9]|2[0-4])", normalized
        ):
            return None
        modifiers: list[str] = []
        if payload.get("ctrlKey") and normalized not in {"Control", "ControlLeft", "ControlRight"}:
            modifiers.append("Control")
        if payload.get("altKey") and normalized not in {"Alt", "AltLeft", "AltRight"}:
            modifiers.append("Alt")
        if payload.get("shiftKey") and normalized not in {"Shift", "ShiftLeft", "ShiftRight"}:
            modifiers.append("Shift")
        if payload.get("metaKey") and normalized not in {"Meta", "MetaLeft", "MetaRight"}:
            modifiers.append("Meta")
        if modifiers and len(normalized) == 1:
            normalized = normalized.upper()
        return "+".join([*modifiers, normalized])

    def _status_message(self, state: str) -> dict[str, Any]:
        page = self.page
        return {
            "type": "status",
            "state": state,
            "url": page.url if page is not None and not page.is_closed() else "",
            "width": self.viewport_width,
            "height": self.viewport_height,
        }

    async def _queue_message(self, message: dict[str, Any]) -> None:
        queue = self._outgoing
        if queue is None:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(message)

    async def _queue_frame(self, message: dict[str, Any]) -> None:
        queue = self._outgoing
        if queue is None:
            return
        if queue.full():
            return
        await queue.put(message)

    def _require_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            raise RuntimeError("Browser page is not available.")
        return self.page


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless,
            args=["--disable-dev-shm-usage"],
        )
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        self._stopping = True
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

        for session_id in list(self._sessions):
            await self.close_session(session_id)

        browser = self._browser
        self._browser = None
        if browser is not None:
            await _ignore_shutdown_disconnect(browser.close())

        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            await _ignore_shutdown_disconnect(playwright.stop())

    async def get_browser(self) -> Browser:
        if self._stopping:
            raise RuntimeError("Browser manager is shutting down.")
        if self._browser is None:
            await self.start()
        if self._browser is None:
            raise RuntimeError("Browser failed to start.")
        return self._browser

    async def create_session(self, raw_url: str, width: int, height: int) -> BrowserSession:
        if self._stopping:
            raise RuntimeError("Browser manager is shutting down.")
        session_id = uuid4().hex
        session = BrowserSession(self, session_id)
        self._sessions[session_id] = session
        try:
            await session.start(raw_url, width, height)
        except Exception:
            self._sessions.pop(session_id, None)
            await session.close()
            raise
        return session

    def get_session(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_activity > self.settings.session_ttl_seconds
            ]
            for session_id in expired:
                await self.close_session(session_id)


async def _ignore_shutdown_disconnect(awaitable: Any) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not _is_shutdown_disconnect(exc):
            raise


def _is_shutdown_disconnect(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "connection closed" in message
        or "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "playwright connection closed" in message
    )
