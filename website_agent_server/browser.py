from __future__ import annotations

import asyncio
import struct
import zlib
import json
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
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
    Request as PlaywrightRequest,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    WebSocketRoute,
    async_playwright,
)

from .config import Settings
from .url_policy import HostAccessPolicy, URLPolicyError


logger = logging.getLogger(__name__)


MOBILE_DEVICE_DESCRIPTOR_NAMES = ("Pixel 7", "Pixel 5", "Pixel 4", "Galaxy S9+")
MOBILE_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
)
MOBILE_CLIENT_HINT_HEADERS = {
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
}
DESKTOP_CLIENT_HINT_PLATFORM = '"Windows"'
DESKTOP_USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version} Safari/537.36"
)
MOUSE_MOVE_MAX_DURATION_SECONDS = 0.1
MOUSE_MOVE_SPEED_PIXELS_PER_SECOND = 12000.0
MOUSE_MOVE_STEP_INTERVAL_SECONDS = 0.008
MOUSE_MOVE_STEP_DISTANCE = 42.0
MOUSE_MOVE_MAX_STEPS = 16
MAX_AUDIO_CHUNK_BASE64_LENGTH = 2_000_000
FORCE_RELOAD_NO_CACHE_SECONDS = 20.0
INTERACTION_FRAME_MESSAGE_TYPES = {
    "mouse_down",
    "mouse_up",
    "tap",
    "wheel",
    "pinch",
    "key",
    "text",
    "paste",
}

NATIVE_CARET_SUPPRESSION_SCRIPT = """() => {
    const styleId = "__website_agent_native_caret_hidden";
    const listenersFlag = "__websiteAgentNativeCaretListeners";
    const editableSelector = "input, textarea, [contenteditable], [role='textbox']";
    const styleText = `
        html,
        body,
        input,
        textarea,
        [contenteditable],
        [contenteditable] *,
        [role="textbox"],
        [role="textbox"] * {
            caret-color: transparent !important;
        }
    `;

    function forceTransparentCaret(element) {
        if (!element || !element.style) {
            return;
        }
        element.style.setProperty("caret-color", "transparent", "important");
    }

    function forceEditableCaret(element) {
        if (!element || !element.closest) {
            return;
        }
        const editable = element.closest(editableSelector);
        if (!editable) {
            return;
        }
        forceTransparentCaret(editable);
        let current = element;
        while (current && current !== editable && current.style) {
            forceTransparentCaret(current);
            current = current.parentElement;
        }
    }

    function forceSelectionCaret() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || !selection.anchorNode) {
            return;
        }
        const anchor =
            selection.anchorNode.nodeType === Node.ELEMENT_NODE
                ? selection.anchorNode
                : selection.anchorNode.parentElement;
        forceEditableCaret(anchor);
    }

    function install() {
        const root = document.documentElement;
        if (!root) {
            return;
        }
        root.style.setProperty("caret-color", "transparent", "important");
        if (document.body) {
            document.body.style.setProperty("caret-color", "transparent", "important");
        }
        forceEditableCaret(document.activeElement);
        forceSelectionCaret();
        if (document.getElementById(styleId)) {
            return;
        }
        const style = document.createElement("style");
        style.id = styleId;
        style.textContent = styleText;
        (document.head || root).appendChild(style);
    }

    install();
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", install, { once: true });
    }
    if (!window[listenersFlag]) {
        window[listenersFlag] = true;
        document.addEventListener("focusin", (event) => forceEditableCaret(event.target), true);
        document.addEventListener("selectionchange", forceSelectionCaret, true);
    }
}"""
NATIVE_CARET_SUPPRESSION_INIT_SCRIPT = f"({NATIVE_CARET_SUPPRESSION_SCRIPT})()"

AUDIO_CAPTURE_SCRIPT = """() => {
    if (window.__websiteAgentAudioCaptureInstalled) {
        return;
    }
    window.__websiteAgentAudioCaptureInstalled = true;
    if (
        typeof window.__websiteAgentAudioBridge !== "function" ||
        typeof MediaRecorder === "undefined" ||
        typeof MediaStream === "undefined"
    ) {
        return;
    }
    const mimeType = [
        "audio/webm;codecs=opus",
        "audio/webm",
    ].find((candidate) => MediaRecorder.isTypeSupported(candidate));
    if (!mimeType) {
        return;
    }

    const records = new WeakMap();
    let nextStreamId = 1;
    let audioContextPatched = false;

    function post(message) {
        try {
            window.__websiteAgentAudioBridge(message);
        } catch {
            // The local bridge may have gone away during navigation.
        }
    }

    function bufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = "";
        const size = 0x8000;
        for (let offset = 0; offset < bytes.length; offset += size) {
            const chunk = bytes.subarray(offset, offset + size);
            binary += String.fromCharCode(...chunk);
        }
        return btoa(binary);
    }

    function eligible(element) {
        return (
            element instanceof HTMLMediaElement &&
            !element.paused &&
            !element.ended &&
            !element.muted &&
            element.volume > 0
        );
    }

    function ensureRecord(element, streamFactory) {
        if (!(element instanceof HTMLMediaElement)) {
            return null;
        }
        let record = records.get(element);
        if (record) {
            return record;
        }
        let stream;
        try {
            stream = streamFactory(element);
        } catch {
            return null;
        }
        if (!stream || typeof stream.getAudioTracks !== "function") {
            return null;
        }
        record = {
            stream,
            recorder: null,
            streamId: `media-${Date.now()}-${nextStreamId++}`,
            watched: false,
        };
        records.set(element, record);
        stream.addEventListener("addtrack", () => {
            window.setTimeout(() => startRecording(element), 0);
        });
        return record;
    }

    function stopRecording(element) {
        const record = records.get(element);
        if (!record || !record.recorder) {
            return;
        }
        const recorder = record.recorder;
        record.recorder = null;
        try {
            if (recorder.state !== "inactive") {
                recorder.stop();
            }
        } catch {
            post({ kind: "stop", streamId: record.streamId });
        }
    }

    function startRecording(element) {
        if (!eligible(element)) {
            stopRecording(element);
            return;
        }
        let record = records.get(element);
        if (!record) {
            if (typeof element.captureStream !== "function") {
                post({ kind: "debug", reason: "capture-stream-unavailable" });
                return;
            }
            record = ensureRecord(element, (mediaElement) => mediaElement.captureStream());
        }
        if (!record || (record.recorder && record.recorder.state !== "inactive")) {
            return;
        }
        const tracks = record.stream
            .getAudioTracks()
            .filter((track) => track.readyState === "live");
        if (tracks.length === 0) {
            post({ kind: "debug", streamId: record.streamId, reason: "no-live-audio-tracks" });
            return;
        }
        let recorder;
        try {
            recorder = new MediaRecorder(new MediaStream(tracks), { mimeType });
        } catch {
            return;
        }
        record.recorder = recorder;
        recorder.addEventListener("start", () => {
            post({ kind: "start", streamId: record.streamId, mime: mimeType });
        });
        recorder.addEventListener("stop", () => {
            post({ kind: "stop", streamId: record.streamId });
        });
        recorder.addEventListener("dataavailable", async (event) => {
            if (!event.data || event.data.size <= 0) {
                return;
            }
            try {
                const buffer = await event.data.arrayBuffer();
                post({
                    kind: "chunk",
                    streamId: record.streamId,
                    mime: recorder.mimeType || mimeType,
                    data: bufferToBase64(buffer),
                });
            } catch {
                // Dropping one audio chunk is better than breaking page playback.
            }
        });
        try {
            recorder.start(500);
        } catch {
            post({ kind: "debug", streamId: record.streamId, reason: "recorder-start-failed" });
            record.recorder = null;
        }
    }

    function watchMediaElement(element) {
        if (!(element instanceof HTMLMediaElement)) {
            return;
        }
        if (typeof element.captureStream !== "function") {
            post({ kind: "debug", reason: "capture-stream-unavailable" });
            return;
        }
        const record = ensureRecord(element, (mediaElement) => mediaElement.captureStream());
        if (!record || record.watched) {
            return;
        }
        record.watched = true;
        element.addEventListener("play", () => startRecording(element), true);
        element.addEventListener("playing", () => startRecording(element), true);
        element.addEventListener("canplay", () => startRecording(element), true);
        element.addEventListener("volumechange", () => startRecording(element), true);
        element.addEventListener("pause", () => stopRecording(element), true);
        element.addEventListener("ended", () => stopRecording(element), true);
        element.addEventListener("emptied", () => stopRecording(element), true);
        startRecording(element);
    }

    function startRecordingFromStream(element, stream) {
        if (!(element instanceof HTMLMediaElement) || !stream) {
            return;
        }
        let record = records.get(element);
        if (!record) {
            record = {
                stream,
                recorder: null,
                streamId: `media-${Date.now()}-${nextStreamId++}`,
                watched: true,
            };
            records.set(element, record);
        } else if (!record.stream || record.stream.getAudioTracks().length === 0) {
            record.stream = stream;
        }
        stream.addEventListener("addtrack", () => {
            window.setTimeout(() => startRecording(element), 0);
        });
        window.setTimeout(() => startRecording(element), 0);
    }

    function patchAudioContextClass(ContextClass) {
        if (!ContextClass || ContextClass.prototype.__websiteAgentAudioPatched) {
            return;
        }
        Object.defineProperty(ContextClass.prototype, "__websiteAgentAudioPatched", {
            value: true,
            configurable: true,
        });
        const original = ContextClass.prototype.createMediaElementSource;
        if (typeof original !== "function") {
            return;
        }
        ContextClass.prototype.createMediaElementSource = function (element) {
            const source = original.call(this, element);
            try {
                const destination = this.createMediaStreamDestination();
                source.connect(destination);
                startRecordingFromStream(element, destination.stream);
            } catch {
                // Fall back to captureStream.
            }
            return source;
        };
    }

    function patchAudioContext() {
        if (audioContextPatched) {
            return;
        }
        audioContextPatched = true;
        patchAudioContextClass(window.AudioContext);
        patchAudioContextClass(window.webkitAudioContext);
    }

    const originalPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (...args) {
        patchAudioContext();
        watchMediaElement(this);
        const result = originalPlay.apply(this, args);
        if (result && typeof result.then === "function") {
            result.then(() => startRecording(this)).catch(() => {});
        } else {
            window.setTimeout(() => startRecording(this), 0);
        }
        return result;
    };

    function scan(root) {
        if (!root || !root.querySelectorAll) {
            return;
        }
        if (root instanceof HTMLMediaElement) {
            watchMediaElement(root);
        }
        root.querySelectorAll("audio, video").forEach(watchMediaElement);
    }

    patchAudioContext();
    scan(document);
    new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            for (const node of mutation.addedNodes) {
                if (node instanceof Element) {
                    scan(node);
                }
            }
        }
    }).observe(document.documentElement, { childList: true, subtree: true });
}"""
AUDIO_CAPTURE_INIT_SCRIPT = f"({AUDIO_CAPTURE_SCRIPT})()"

EDITABLE_METRICS_SCRIPT = """() => {
    const textTypes = new Set([
        "email",
        "number",
        "password",
        "search",
        "tel",
        "text",
        "url",
    ]);

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function numeric(value) {
        const parsed = Number.parseFloat(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function documentSize() {
        return {
            width: Math.max(
                document.documentElement.scrollWidth,
                document.body ? document.body.scrollWidth : 0,
                window.innerWidth
            ),
            height: Math.max(
                document.documentElement.scrollHeight,
                document.body ? document.body.scrollHeight : 0,
                window.innerHeight
            ),
        };
    }

    function findEditable() {
        const active = document.activeElement;
        if (!active || active === document.body || active === document.documentElement) {
            return null;
        }
        const editable = active.closest(
            "input, textarea, [contenteditable], [role='textbox']"
        );
        if (!editable) {
            return null;
        }
        if (editable.matches("input")) {
            const type = (editable.getAttribute("type") || "text").toLowerCase();
            if (!textTypes.has(type) || editable.disabled || editable.readOnly) {
                return null;
            }
        }
        if (editable.matches("textarea") && (editable.disabled || editable.readOnly)) {
            return null;
        }
        if (
            editable.matches("[contenteditable]") &&
            editable.getAttribute("contenteditable") === "false"
        ) {
            return null;
        }
        return editable;
    }

    function copyTextStyles(source, target) {
        const style = window.getComputedStyle(source);
        const properties = [
            "boxSizing",
            "borderTopWidth",
            "borderRightWidth",
            "borderBottomWidth",
            "borderLeftWidth",
            "borderStyle",
            "direction",
            "fontFamily",
            "fontFeatureSettings",
            "fontKerning",
            "fontSize",
            "fontStretch",
            "fontStyle",
            "fontVariant",
            "fontWeight",
            "letterSpacing",
            "lineHeight",
            "paddingTop",
            "paddingRight",
            "paddingBottom",
            "paddingLeft",
            "tabSize",
            "textAlign",
            "textDecoration",
            "textIndent",
            "textTransform",
            "wordBreak",
            "wordSpacing",
        ];
        for (const property of properties) {
            target.style[property] = style[property];
        }
        return style;
    }

    function caretResult(x, y, height, fontSize, rect) {
        const caretHeight = Math.max(8, Math.min(rect.height, height || fontSize || rect.height));
        const caretWidth = Math.max(1, Math.min(3, (fontSize || caretHeight) * 0.12));
        const centerY = clamp(y, rect.top, rect.bottom);
        const top = clamp(centerY - caretHeight / 2, rect.top, Math.max(rect.top, rect.bottom - caretHeight));
        const centerX = clamp(x, rect.left, rect.right);
        return {
            focusLeft: centerX,
            focusTop: top + caretHeight / 2,
            caretX: centerX,
            caretY: top,
            caretWidth,
            caretHeight,
        };
    }

    function textControlFocusPoint(editable, rect) {
        if (typeof editable.selectionStart !== "number") {
            return null;
        }
        const value = editable.value || "";
        const selectionStart = editable.selectionStart ?? value.length;
        const selectionEnd = editable.selectionEnd ?? selectionStart;
        const caret = clamp(Math.max(selectionStart, selectionEnd), 0, value.length);
        const inputType = (editable.getAttribute("type") || "text").toLowerCase();
        const mirror = document.createElement("div");
        const style = copyTextStyles(editable, mirror);
        mirror.style.position = "absolute";
        mirror.style.visibility = "hidden";
        mirror.style.pointerEvents = "none";
        mirror.style.left = "-10000px";
        mirror.style.top = "0";
        mirror.style.overflow = "hidden";
        mirror.style.whiteSpace = editable.tagName === "TEXTAREA" ? "pre-wrap" : "pre";
        mirror.style.overflowWrap =
            editable.tagName === "TEXTAREA" ? "break-word" : "normal";
        mirror.style.width = editable.tagName === "TEXTAREA" ? `${rect.width}px` : "auto";
        mirror.style.minWidth = `${rect.width}px`;
        mirror.style.height = "auto";

        let before = value.slice(0, caret);
        if (inputType === "password") {
            before = "x".repeat(before.length);
        }
        if (editable.tagName === "TEXTAREA" && before.endsWith("\\n")) {
            before += " ";
        }
        mirror.textContent = before;
        const marker = document.createElement("span");
        marker.textContent = "\\u200b";
        mirror.appendChild(marker);
        document.body.appendChild(mirror);

        const markerRect = marker.getBoundingClientRect();
        const mirrorRect = mirror.getBoundingClientRect();
        const fontSize = numeric(style.fontSize) || rect.height;
        const lineHeight = numeric(style.lineHeight) || markerRect.height || fontSize;
        let focusLeft = rect.left + markerRect.left - mirrorRect.left - editable.scrollLeft;
        let focusTop;
        if (editable.tagName === "TEXTAREA") {
            focusTop =
                rect.top +
                markerRect.top -
                mirrorRect.top -
                editable.scrollTop +
                lineHeight / 2;
        } else {
            focusTop = rect.top + rect.height / 2;
        }
        mirror.remove();

        const leftInset = numeric(style.borderLeftWidth) + numeric(style.paddingLeft);
        const rightInset = numeric(style.borderRightWidth) + numeric(style.paddingRight);
        const minLeft = rect.left + Math.max(0, Math.min(rect.width / 2, leftInset));
        const maxLeft = rect.right - Math.max(0, Math.min(rect.width / 2, rightInset));
        const safeLeft = clamp(focusLeft, minLeft, Math.max(minLeft, maxLeft));
        return caretResult(safeLeft, focusTop, lineHeight, fontSize, rect);
    }

    function selectionFocusPoint(editable, rect) {
        const selection = window.getSelection();
        if (!selection || selection.rangeCount < 1) {
            return null;
        }
        const selectedRange = selection.getRangeAt(0);
        if (!editable.contains(selectedRange.endContainer)) {
            return null;
        }
        const range = selectedRange.cloneRange();
        range.collapse(false);
        let caretRect = null;
        const rects = range.getClientRects();
        if (rects.length > 0) {
            caretRect = rects[rects.length - 1];
        } else {
            const restoreRange = selectedRange.cloneRange();
            const marker = document.createElement("span");
            marker.textContent = "\\u200b";
            marker.style.display = "inline-block";
            marker.style.width = "1px";
            marker.style.height = "1em";
            range.insertNode(marker);
            caretRect = marker.getBoundingClientRect();
            marker.remove();
            selection.removeAllRanges();
            selection.addRange(restoreRange);
        }
        if (!caretRect) {
            return null;
        }
        return caretResult(
            caretRect.left + caretRect.width / 2,
            caretRect.top + caretRect.height / 2,
            caretRect.height,
            caretRect.height,
            rect
        );
    }

    const active = document.activeElement;
    const editable = findEditable();
    if (!editable) {
        return null;
    }
    const rect = editable.getBoundingClientRect();
    const size = documentSize();
    const editableStyle = window.getComputedStyle(editable);
    const caret =
        active.matches("input, textarea")
            ? textControlFocusPoint(active, rect)
            : selectionFocusPoint(editable, rect);
    const fallbackHeight = Math.max(8, Math.min(rect.height, 18));
    const fallback = caretResult(
        rect.left + rect.width / 2,
        rect.top + Math.min(rect.height / 2, 32),
        fallbackHeight,
        fallbackHeight,
        rect
    );
    const focus = caret || fallback;
    const visualViewport = window.visualViewport;
    return {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        editableTag: editable.tagName,
        editableType: active.matches("input") ? (active.getAttribute("type") || "text").toLowerCase() : "",
        selectionStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
        selectionEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
        valueLength: typeof active.value === "string" ? active.value.length : null,
        borderLeft: numeric(editableStyle.borderLeftWidth),
        borderTop: numeric(editableStyle.borderTopWidth),
        borderRight: numeric(editableStyle.borderRightWidth),
        borderBottom: numeric(editableStyle.borderBottomWidth),
        paddingLeft: numeric(editableStyle.paddingLeft),
        paddingTop: numeric(editableStyle.paddingTop),
        paddingRight: numeric(editableStyle.paddingRight),
        paddingBottom: numeric(editableStyle.paddingBottom),
        focusLeft: focus.focusLeft,
        focusTop: focus.focusTop,
        caretX: focus.caretX,
        caretY: focus.caretY,
        caretWidth: focus.caretWidth,
        caretHeight: focus.caretHeight,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        documentWidth: size.width,
        documentHeight: size.height,
        visualScale: visualViewport ? visualViewport.scale : 1,
        visualOffsetLeft: visualViewport ? visualViewport.offsetLeft : 0,
        visualOffsetTop: visualViewport ? visualViewport.offsetTop : 0,
    };
}"""


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return cleaned or "download"


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _float_value(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _visual_viewport_offset(
    viewport: dict[str, Any], offset_key: str, page_key: str, scroll_key: str
) -> float:
    reported_offset = _float_value(viewport.get(offset_key), 0.0) or 0.0
    page_offset = _float_value(viewport.get(page_key), None)
    scroll_offset = _float_value(viewport.get(scroll_key), None)
    if page_offset is not None and scroll_offset is not None:
        return max(0.0, page_offset - scroll_offset)
    return max(0.0, reported_offset)


def _png_rgba_rows(data: bytes) -> tuple[int, int, list[bytes]] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    position = 8
    width = 0
    height = 0
    bit_depth = 0
    color_type = 0
    idat = bytearray()
    try:
        while position + 8 <= len(data):
            length = struct.unpack(">I", data[position : position + 4])[0]
            kind = data[position + 4 : position + 8]
            chunk = data[position + 8 : position + 8 + length]
            position += 12 + length
            if kind == b"IHDR":
                width = struct.unpack(">I", chunk[:4])[0]
                height = struct.unpack(">I", chunk[4:8])[0]
                bit_depth = chunk[8]
                color_type = chunk[9]
            elif kind == b"IDAT":
                idat.extend(chunk)
            elif kind == b"IEND":
                break
        if bit_depth != 8 or color_type not in {2, 6} or width <= 0 or height <= 0:
            return None
        channels = 4 if color_type == 6 else 3
        stride = width * channels
        raw = zlib.decompress(bytes(idat))
        rows: list[bytes] = []
        previous = bytearray(stride)
        index = 0
        for _ in range(height):
            filter_type = raw[index]
            index += 1
            scanline = bytearray(raw[index : index + stride])
            index += stride
            reconstructed = bytearray(stride)
            for i, value in enumerate(scanline):
                left = reconstructed[i - channels] if i >= channels else 0
                up = previous[i]
                up_left = previous[i - channels] if i >= channels else 0
                if filter_type == 0:
                    predictor = 0
                elif filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = up
                elif filter_type == 3:
                    predictor = (left + up) // 2
                elif filter_type == 4:
                    p = left + up - up_left
                    pa = abs(p - left)
                    pb = abs(p - up)
                    pc = abs(p - up_left)
                    predictor = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                else:
                    return None
                reconstructed[i] = (value + predictor) & 0xFF
            if channels == 3:
                rgba = bytearray(width * 4)
                for x in range(width):
                    rgba[x * 4 : x * 4 + 3] = reconstructed[x * 3 : x * 3 + 3]
                    rgba[x * 4 + 3] = 255
                rows.append(bytes(rgba))
            else:
                rows.append(bytes(reconstructed))
            previous = reconstructed
        return width, height, rows
    except Exception:
        return None


@dataclass(frozen=True)
class DownloadRecord:
    filename: str
    path: Path


@dataclass
class ClientContextEntry:
    context: BrowserContext
    last_used: float
    options_signature: tuple[Any, ...]


@dataclass
class NavigationEntry:
    page: Page
    url: str


@dataclass
class PendingDialog:
    dialog: Any
    future: asyncio.Future[dict[str, Any]]


@dataclass
class SessionConnection:
    websocket: WebSocket
    outgoing: asyncio.Queue[dict[str, Any]]


def _cookie_identity(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or ""),
        str(cookie.get("path") or "/"),
    )


class BrowserSession:
    def __init__(
        self,
        manager: "BrowserManager",
        session_id: str,
        client_uuid: str,
        lock_url: str | None,
    ) -> None:
        self.manager = manager
        self.id = session_id
        self.client_uuid = client_uuid
        self.settings = manager.settings
        self.lock_url = lock_url
        self.policy = HostAccessPolicy(self.settings.allow_private_hosts)
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.initial_url = ""
        self.viewport_width = 1280
        self.viewport_height = 720
        self.last_activity = time.monotonic()
        self.downloads: dict[str, DownloadRecord] = {}
        self.file_choosers: dict[str, FileChooser] = {}
        self.pending_dialogs: dict[str, PendingDialog] = {}
        self._pending_reliable_messages: list[dict[str, Any]] = []
        self._history: list[NavigationEntry] = []
        self._initial_navigation_task: asyncio.Task[None] | None = None
        self._history_index = -1
        self._history_replaying = False
        self._is_mobile = False
        self._device_scale_factor = 1.0
        self._page_scale_factor = 1.0
        self._mobile_focus_zoom = 1.0
        self._mobile_focus_clip: dict[str, float] | None = None
        self._calibrate_caret_until = 0.0
        self._mouse_position: tuple[float, float] = (0.0, 0.0)
        self._pending_document_navigation_url = ""
        self._force_reload_until = 0.0
        self._media_state_checked_at = 0.0
        self._media_playing = False
        self._action_lock = asyncio.Lock()
        self._frame_capture_lock = asyncio.Lock()
        self._frame_loop_task: asyncio.Task[None] | None = None
        self._requested_frame_task: asyncio.Task[None] | None = None
        self._connections: dict[str, SessionConnection] = {}
        self._audio_connections: dict[str, SessionConnection] = {}
        self._closed = False
        self._connected = False
        self.disconnected_at = time.monotonic()

    @property
    def is_locked(self) -> bool:
        return self.lock_url is not None

    async def start(
        self,
        raw_url: str,
        width: int,
        height: int,
        is_mobile: bool,
        device_scale_factor: float,
    ) -> None:
        self._is_mobile = is_mobile
        self._device_scale_factor = max(0.5, min(4.0, device_scale_factor))
        initial_url = await self.policy.ensure_navigation_url_allowed(
            raw_url,
            verify_https=not self.settings.ignore_https_errors,
        )
        self.initial_url = initial_url
        self.viewport_width = _clamp(
            width, self.settings.min_viewport_width, self.settings.max_viewport_width
        )
        self.viewport_height = _clamp(
            height, self.settings.min_viewport_height, self.settings.max_viewport_height
        )
        context_options: dict[str, Any] = {
            "accept_downloads": True,
            "ignore_https_errors": self.settings.ignore_https_errors,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "device_scale_factor": self._device_scale_factor,
            "locale": self.settings.locale,
            "timezone_id": self.settings.timezone_id,
            "extra_http_headers": self.manager.desktop_extra_http_headers(),
            "user_agent": self.manager.desktop_user_agent(),
        }
        if self._is_mobile:
            context_options.update(
                self.manager.mobile_context_options(self.viewport_width, self.viewport_height)
            )
        context = await self.manager.acquire_context(self.client_uuid, context_options)
        self.context = context
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = await self.context.new_page()
        await self._prepare_page(page)
        self._initial_navigation_task = asyncio.create_task(
            self._run_initial_navigation(initial_url)
        )

    async def _run_initial_navigation(self, initial_url: str) -> None:
        try:
            await self.navigate(initial_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue_message(
                {"type": "error", "message": f"Could not open the target site: {exc}"},
                reliable=True,
            )
        finally:
            if self._initial_navigation_task is asyncio.current_task():
                self._initial_navigation_task = None

    async def close(self) -> None:
        self._closed = True
        context = self.context
        page = self.page
        connections = list(self._connections.values())
        audio_connections = list(self._audio_connections.values())
        self.context = None
        self.page = None
        self._connections.clear()
        self._audio_connections.clear()
        self._connected = False
        initial_navigation_task = self._initial_navigation_task
        self._initial_navigation_task = None
        frame_loop_task = self._frame_loop_task
        self._frame_loop_task = None
        requested_frame_task = self._requested_frame_task
        self._requested_frame_task = None
        if initial_navigation_task is not None:
            initial_navigation_task.cancel()
            await asyncio.gather(initial_navigation_task, return_exceptions=True)
        if frame_loop_task is not None:
            frame_loop_task.cancel()
            await asyncio.gather(frame_loop_task, return_exceptions=True)
        if requested_frame_task is not None:
            requested_frame_task.cancel()
        for token, pending_dialog in list(self.pending_dialogs.items()):
            if not pending_dialog.future.done():
                pending_dialog.future.set_result({"accepted": False, "value": ""})
            self.pending_dialogs.pop(token, None)
        for connection in connections:
            await _ignore_shutdown_disconnect(connection.websocket.close(code=1001))
        for connection in audio_connections:
            await _ignore_shutdown_disconnect(connection.websocket.close(code=1001))
        pages_to_close = self._session_pages(page)
        for session_page in pages_to_close:
            await _ignore_shutdown_disconnect(session_page.close())
        if context is not None:
            self.manager.touch_context(self.client_uuid)
        self._history.clear()
        self._history_index = -1
        self._history_replaying = False
        self._pending_reliable_messages.clear()

    async def connect(self, client_uuid: str, websocket: WebSocket) -> None:
        await websocket.accept()
        outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        previous_connection = self._connections.get(client_uuid)
        if previous_connection is not None:
            await _ignore_shutdown_disconnect(
                previous_connection.websocket.close(code=1001, reason="Reconnected")
            )
        self.last_activity = time.monotonic()
        self.disconnected_at = 0.0
        self._connected = True
        self._connections[client_uuid] = SessionConnection(
            websocket=websocket,
            outgoing=outgoing,
        )
        await self._queue_message(self._status_message("connected"))
        await self._queue_pending_reliable_messages()
        await self._queue_pending_dialogs()
        self._ensure_frame_loop()

        tasks = {
            asyncio.create_task(self._send_loop(websocket, outgoing)),
            asyncio.create_task(self._receive_loop(websocket, incoming)),
            asyncio.create_task(self._message_loop(incoming)),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect) and not _is_websocket_disconnect(exc):
                    raise exc
            for task in pending:
                task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            current = self._connections.get(client_uuid)
            if current is not None and current.websocket is websocket:
                self._connections.pop(client_uuid, None)
            if not self._connections:
                self._connected = False
                self.disconnected_at = time.monotonic()
                self._cancel_frame_loop_if_idle()

    @property
    def is_connected(self) -> bool:
        return bool(self._connections)

    async def connect_audio(self, client_uuid: str, websocket: WebSocket) -> None:
        await websocket.accept()
        outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=48)
        previous_connection = self._audio_connections.get(client_uuid)
        if previous_connection is not None:
            await _ignore_shutdown_disconnect(
                previous_connection.websocket.close(code=1001, reason="Reconnected")
            )
        self.last_activity = time.monotonic()
        self._audio_connections[client_uuid] = SessionConnection(
            websocket=websocket,
            outgoing=outgoing,
        )
        send_task = asyncio.create_task(self._send_loop(websocket, outgoing))
        receive_task = asyncio.create_task(self._audio_receive_loop(websocket))
        tasks = {send_task, receive_task}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect) and not _is_websocket_disconnect(exc):
                    raise exc
            for task in pending:
                task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            current = self._audio_connections.get(client_uuid)
            if current is not None and current.websocket is websocket:
                self._audio_connections.pop(client_uuid, None)

    async def disconnect_client(self, client_uuid: str) -> None:
        connection = self._connections.pop(client_uuid, None)
        audio_connection = self._audio_connections.pop(client_uuid, None)
        if connection is not None:
            await _ignore_shutdown_disconnect(connection.websocket.close(code=1001))
        if audio_connection is not None:
            await _ignore_shutdown_disconnect(audio_connection.websocket.close(code=1001))
        if not self._connections:
            self._connected = False
            self.disconnected_at = time.monotonic()
            self._cancel_frame_loop_if_idle()

    def connected_client_count(self) -> int:
        return len(self._connections)

    async def navigate(self, raw_url: str) -> None:
        page = self._require_page()
        url = await self.policy.ensure_navigation_url_allowed(
            raw_url,
            verify_https=not self.settings.ignore_https_errors,
        )
        self._clear_mobile_focus_zoom()
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

    async def upload_file_chooser(
        self, token: str, files: list[Path] | list[dict[str, object]]
    ) -> None:
        chooser = self.file_choosers.pop(token, None)
        if chooser is None:
            raise KeyError("File chooser is no longer available.")
        if files and isinstance(files[0], Path):
            await chooser.set_files([str(path) for path in files])
        else:
            await chooser.set_files(files)
        await self._queue_message({"type": "status", "state": "files-selected"})

    def get_download(self, token: str) -> DownloadRecord | None:
        return self.downloads.get(token)

    async def list_cookies(self) -> list[dict[str, Any]]:
        context = self._require_context()
        page = self._require_page()
        self.last_activity = time.monotonic()
        async with self._action_lock:
            return await context.cookies([page.url])

    async def replace_cookies(self, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        context = self._require_context()
        page = self._require_page()
        self.last_activity = time.monotonic()
        async with self._action_lock:
            page_url = page.url
            existing = await context.cookies([page_url])
            normalized = [self._normalize_cookie(cookie, page_url) for cookie in cookies]
            if normalized:
                await context.add_cookies(normalized)
            next_keys = {_cookie_identity(cookie) for cookie in normalized}
            for cookie in existing:
                if _cookie_identity(cookie) not in next_keys:
                    await context.clear_cookies(
                        name=str(cookie.get("name") or ""),
                        domain=str(cookie.get("domain") or ""),
                        path=str(cookie.get("path") or "/"),
                    )
            return await context.cookies([page_url])

    async def handle_message(self, payload: dict[str, Any]) -> None:
        self.last_activity = time.monotonic()
        message_type = payload.get("type")
        if message_type == "resize":
            await self._resize(int(payload.get("width", self.viewport_width)), int(payload.get("height", self.viewport_height)))
        elif message_type == "dialog_response":
            await self._handle_dialog_response(payload)
        elif message_type == "navigate":
            if self.is_locked:
                await self._queue_message(
                    {"type": "warning", "message": "Options are locked by the server."}
                )
                return
            await self.navigate(str(payload.get("url", "")))
        elif message_type == "reload":
            await self._reload()
        elif message_type == "back":
            if self.is_locked:
                await self._queue_message(
                    {"type": "warning", "message": "Options are locked by the server."}
                )
                return
            await self._go_back()
        elif message_type == "forward":
            if self.is_locked:
                await self._queue_message(
                    {"type": "warning", "message": "Options are locked by the server."}
                )
                return
            await self._go_forward()
        elif message_type == "mouse_move":
            await self._mouse_move(payload)
        elif message_type == "mouse_down":
            await self._mouse_button(payload, down=True)
        elif message_type == "mouse_up":
            await self._mouse_button(payload, down=False)
        elif message_type == "tap":
            await self._tap(payload)
        elif message_type == "wheel":
            await self._wheel(payload)
        elif message_type == "pinch":
            await self._pinch(payload)
        elif message_type == "probe_editable":
            await self._probe_editable(payload)
        elif message_type == "key":
            await self._press_key(payload)
        elif message_type == "text":
            await self._insert_text(
                str(payload.get("text", "")),
                focus_view=bool(payload.get("focus_view")),
            )
        elif message_type == "paste":
            await self._paste_text(str(payload.get("text", "")))
        elif message_type == "copy":
            await self.copy_selection(cut=False)
        elif message_type == "cut":
            await self.copy_selection(cut=True)
        if message_type in INTERACTION_FRAME_MESSAGE_TYPES:
            self._request_frame_soon()

    async def _prepare_page(self, page: Page) -> None:
        await page.set_viewport_size({"width": self.viewport_width, "height": self.viewport_height})
        await page.add_init_script(NATIVE_CARET_SUPPRESSION_INIT_SCRIPT)
        await page.add_init_script(AUDIO_CAPTURE_INIT_SCRIPT)
        await page.route("**/*", self._guard_route)
        await page.route_web_socket("**/*", self._guard_websocket)
        await self._attach_page(page)
        await self._suppress_native_caret(page)
        await self._install_audio_capture(page)

    async def _attach_page(self, page: Page) -> None:
        self.page = page
        page.on("popup", lambda popup: asyncio.create_task(self._on_popup(popup)))
        page.on("download", lambda download: asyncio.create_task(self._on_download(download)))
        page.on("filechooser", lambda chooser: asyncio.create_task(self._on_filechooser(chooser)))
        page.on("dialog", lambda dialog: asyncio.create_task(self._on_dialog(dialog)))
        page.on("request", lambda request: asyncio.create_task(self._on_request(request)))
        page.on("requestfailed", lambda request: asyncio.create_task(self._on_request_failed(request)))
        page.on("framenavigated", lambda frame: asyncio.create_task(self._on_frame_navigated(frame)))

    async def _guard_route(self, route: Route) -> None:
        url = route.request.url
        try:
            await self.policy.ensure_request_url_allowed(url)
            headers = self.manager.compatible_request_headers(
                route.request.headers,
                is_mobile=self._is_mobile,
            )
            if time.monotonic() < self._force_reload_until:
                headers["cache-control"] = "no-cache"
                headers["pragma"] = "no-cache"
                headers["expires"] = "0"
            await route.continue_(headers=headers)
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

    async def _suppress_native_caret(self, page: Page) -> None:
        try:
            await page.evaluate(NATIVE_CARET_SUPPRESSION_SCRIPT)
        except Exception:
            return

    async def _install_audio_capture(self, page: Page) -> None:
        try:
            await page.evaluate(AUDIO_CAPTURE_SCRIPT)
        except Exception:
            return

    async def handle_audio_bridge_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        kind = str(message.get("kind") or "")
        if kind == "debug":
            reason = str(message.get("reason") or "unknown")
            await self._queue_message({"type": "warning", "message": f"Audio capture: {reason}"})
            return
        if kind not in {"start", "chunk", "stop"}:
            return
        payload: dict[str, Any] = {
            "type": "audio",
            "kind": kind,
            "streamId": str(message.get("streamId") or "media"),
        }
        mime = message.get("mime")
        if isinstance(mime, str) and mime:
            payload["mime"] = mime
        data = message.get("data")
        if kind == "chunk":
            if not isinstance(data, str) or len(data) > MAX_AUDIO_CHUNK_BASE64_LENGTH:
                return
            payload["data"] = data
        await self._queue_audio(payload)

    async def _on_popup(self, popup: Page) -> None:
        await self._prepare_page(popup)
        self._record_navigation(popup, popup.url)
        await self._queue_message({"type": "status", "state": "popup", "url": popup.url})

    async def _on_download(self, download: Download) -> None:
        token = secrets.token_urlsafe(18)
        filename = _safe_filename(download.suggested_filename or "download")
        download_dir = self.manager.client_downloads_dir(self.client_uuid) / self.id
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
        token = secrets.token_urlsafe(18)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending_dialogs[token] = PendingDialog(dialog=dialog, future=future)
        await self._queue_message(
            {
                "type": "dialog",
                "token": token,
                "dialogType": dialog_type,
                "message": message,
                "defaultValue": default_value,
            },
            reliable=True,
        )
        try:
            response = await future
            accepted = bool(response.get("accepted", True))
            value = str(response.get("value", default_value))
            if accepted:
                await dialog.accept(value)
            else:
                await dialog.dismiss()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Dialog response failed for session %s: %s", self.id, exc)
            try:
                await dialog.dismiss()
            except Exception:
                pass
        finally:
            self.pending_dialogs.pop(token, None)

    async def _handle_dialog_response(self, payload: dict[str, Any]) -> None:
        token = str(payload.get("token") or "")
        pending_dialog = self.pending_dialogs.get(token)
        if pending_dialog is None or pending_dialog.future.done():
            return
        pending_dialog.future.set_result(
            {
                "accepted": bool(payload.get("accepted", False)),
                "value": str(payload.get("value", "")),
            }
        )

    async def _queue_pending_dialogs(self) -> None:
        for token, pending_dialog in list(self.pending_dialogs.items()):
            if pending_dialog.future.done():
                continue
            dialog = pending_dialog.dialog
            await self._queue_message(
                {
                    "type": "dialog",
                    "token": token,
                    "dialogType": dialog.type,
                    "message": dialog.message,
                    "defaultValue": getattr(dialog, "default_value", "") or "",
                },
                reliable=True,
            )

    async def _queue_pending_reliable_messages(self) -> None:
        messages = self._pending_reliable_messages
        self._pending_reliable_messages = []
        for message in messages:
            await self._queue_message(message, reliable=True)

    async def _on_request(self, request: PlaywrightRequest) -> None:
        page = self.page
        if page is None or request.frame != page.main_frame:
            return
        if request.resource_type != "document":
            return
        url = request.url
        if not url or url == "about:blank" or url == self._pending_document_navigation_url:
            return
        self._pending_document_navigation_url = url
        self._clear_mobile_focus_zoom()
        await self._queue_message({"type": "status", "state": "loading", "url": url})

    async def _on_request_failed(self, request: PlaywrightRequest) -> None:
        page = self.page
        if page is None or request.frame != page.main_frame:
            return
        if request.resource_type != "document":
            return
        if request.url != self._pending_document_navigation_url:
            return
        self._pending_document_navigation_url = ""
        await self._queue_message(self._status_message("ready"))

    async def _on_frame_navigated(self, frame: Any) -> None:
        page = self.page
        if page is not None and frame == page.main_frame:
            self._pending_document_navigation_url = ""
            self._clear_mobile_focus_zoom()
            self._record_navigation(page, page.url)
            await self._queue_message(self._status_message("ready"))

    def _record_navigation(self, page: Page, url: str) -> None:
        if not url or url == "about:blank":
            return

        if self._history_replaying:
            if 0 <= self._history_index < len(self._history):
                current = self._history[self._history_index]
                if current.page is page:
                    current.url = url
            return

        if self._history_index >= 0 and self._history_index < len(self._history):
            current = self._history[self._history_index]
            if current.page is page and current.url == url:
                return

        if self._history_index < len(self._history) - 1:
            self._history = self._history[: self._history_index + 1]

        self._history.append(NavigationEntry(page=page, url=url))
        self._history_index = len(self._history) - 1
        self._prune_closed_history()

    def _session_pages(self, current_page: Page | None = None) -> list[Page]:
        pages: list[Page] = []
        seen: set[Page] = set()
        for page in [current_page, *[entry.page for entry in self._history]]:
            if page is None or page in seen or page.is_closed():
                continue
            pages.append(page)
            seen.add(page)
        return pages

    def _prune_closed_history(self) -> None:
        if not self._history:
            self._history_index = -1
            return
        active_entry = (
            self._history[self._history_index]
            if 0 <= self._history_index < len(self._history)
            else None
        )
        self._history = [entry for entry in self._history if not entry.page.is_closed()]
        if not self._history:
            self._history_index = -1
            return
        if active_entry is not None and not active_entry.page.is_closed():
            for index, entry in enumerate(self._history):
                if entry is active_entry:
                    self._history_index = index
                    return
        self._history_index = min(max(self._history_index, 0), len(self._history) - 1)

    async def _send_loop(
        self, websocket: WebSocket, outgoing: asyncio.Queue[dict[str, Any]]
    ) -> None:
        while True:
            message = await outgoing.get()
            if message.get("type") == "frame":
                image = message.get("image", b"")
                if isinstance(image, bytes):
                    header = {key: value for key, value in message.items() if key != "image"}
                    await websocket.send_text(json.dumps(header, separators=(",", ":")))
                    await websocket.send_bytes(image)
                    continue
            await websocket.send_text(json.dumps(message, separators=(",", ":")))

    async def _audio_receive_loop(self, websocket: WebSocket) -> None:
        while True:
            try:
                await websocket.receive_text()
            except RuntimeError as exc:
                if _is_websocket_disconnect(exc):
                    raise WebSocketDisconnect() from exc
                raise

    async def _receive_loop(
        self, websocket: WebSocket, incoming: asyncio.Queue[dict[str, Any]]
    ) -> None:
        while True:
            try:
                text = await websocket.receive_text()
            except RuntimeError as exc:
                if _is_websocket_disconnect(exc):
                    raise WebSocketDisconnect() from exc
                raise
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                if payload.get("type") == "dialog_response":
                    await self._handle_dialog_response(payload)
                else:
                    self._queue_incoming_message(incoming, payload)

    async def _message_loop(self, incoming: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            payload = await incoming.get()
            try:
                await self.handle_message(payload)
            except URLPolicyError as exc:
                await self._queue_message({"type": "error", "message": str(exc)})
            except Exception as exc:
                await self._queue_message(
                    {"type": "warning", "message": f"Input event ignored: {exc}"}
                )

    def _queue_incoming_message(
        self, incoming: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
    ) -> None:
        try:
            incoming.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass
        self._drop_queued_messages(
            incoming,
            lambda queued: queued.get("type") in {"mouse_move", "wheel", "probe_editable"},
            limit=16,
        )
        if incoming.full():
            try:
                incoming.get_nowait()
            except asyncio.QueueEmpty:
                return
        incoming.put_nowait(payload)

    async def _frame_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(await self._current_frame_interval())
            if not self._connections:
                return
            await self._send_current_frame()

    def _ensure_frame_loop(self) -> None:
        if self._closed:
            return
        if self._frame_loop_task is not None and not self._frame_loop_task.done():
            return
        self._frame_loop_task = asyncio.create_task(self._frame_loop())

    def _cancel_frame_loop_if_idle(self) -> None:
        if self._connections:
            return
        task = self._frame_loop_task
        self._frame_loop_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _current_frame_interval(self) -> float:
        if await self._is_media_playing():
            return max(self.settings.frame_interval_seconds, self.settings.media_frame_interval_seconds)
        return self.settings.frame_interval_seconds

    async def _is_media_playing(self) -> bool:
        now = time.monotonic()
        if now - self._media_state_checked_at < 0.75:
            return self._media_playing
        self._media_state_checked_at = now
        page = self.page
        if page is None or page.is_closed():
            self._media_playing = False
            return False
        try:
            result = await page.evaluate(
                """() => Array.from(document.querySelectorAll("video, audio")).some((element) => (
                    element instanceof HTMLMediaElement &&
                    !element.paused &&
                    !element.ended &&
                    element.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
                ))"""
            )
        except Exception:
            self._media_playing = False
            return False
        self._media_playing = bool(result)
        return self._media_playing

    async def _send_current_frame(self) -> None:
        if self._frame_capture_lock.locked():
            return
        started_at = time.monotonic()
        async with self._frame_capture_lock:
            frame = await self._capture_frame()
        if frame is not None:
            await self._queue_frame(frame)
        elapsed = time.monotonic() - started_at
        if elapsed > 0.75:
            logger.debug(
                "Slow frame capture for session %s: %.3fs, media_playing=%s, viewport=%sx%s",
                self.id,
                elapsed,
                self._media_playing,
                self.viewport_width,
                self.viewport_height,
            )

    def _request_frame_soon(self) -> None:
        if self._closed:
            return
        if self._requested_frame_task is not None and not self._requested_frame_task.done():
            return
        self._requested_frame_task = asyncio.create_task(self._send_delayed_frame())

    async def _send_delayed_frame(self) -> None:
        try:
            await asyncio.sleep(0.03)
            await self._send_current_frame()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _capture_frame(self) -> dict[str, Any] | None:
        page = self.page
        if page is None or page.is_closed():
            return None
        try:
            caret = await self._editable_caret(page)
            await self._suppress_native_caret(page)
            clip = self._mobile_focus_clip
            focus_zoom_frame = clip is not None
            lossless_frame = self.settings.screenshot_quality >= 100
            media_playing = await self._is_media_playing()
            screenshot_quality = (
                self.settings.screenshot_quality
                if lossless_frame or not media_playing
                else min(self.settings.screenshot_quality, self.settings.media_screenshot_quality)
            )
            screenshot_type = "png" if focus_zoom_frame or lossless_frame else "jpeg"
            screenshot_options: dict[str, Any] = {
                "type": screenshot_type,
                "scale": "device" if focus_zoom_frame else "css",
                "full_page": False,
                "timeout": 8000,
            }
            if screenshot_type == "jpeg":
                screenshot_options["quality"] = screenshot_quality
            if clip is not None:
                screenshot_options["clip"] = clip
            try:
                image = await page.screenshot(**screenshot_options)
            except Exception:
                if clip is None:
                    raise
                self._clear_mobile_focus_zoom()
                focus_zoom_frame = False
                screenshot_type = "png" if lossless_frame else "jpeg"
                screenshot_options = {
                    "type": screenshot_type,
                    "scale": "css",
                    "full_page": False,
                    "timeout": 8000,
                }
                if screenshot_type == "jpeg":
                    screenshot_options["quality"] = screenshot_quality
                image = await page.screenshot(**screenshot_options)
            title = await page.title()
            url = page.url
        except Exception as exc:
            await self._queue_message({"type": "warning", "message": f"Frame capture failed: {exc}"})
            return None

        return {
            "type": "frame",
            "mime": "image/png" if screenshot_type == "png" else "image/jpeg",
            "image": image,
            "width": self.viewport_width,
            "height": self.viewport_height,
            "url": url,
            "title": title,
            "caret": caret,
        }

    async def _resize(self, width: int, height: int) -> None:
        self._clear_mobile_focus_zoom()
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
        self._clear_mobile_focus_zoom()
        page = self._require_page()
        self.last_activity = time.monotonic()
        self._force_reload_until = time.monotonic() + FORCE_RELOAD_NO_CACHE_SECONDS
        await self._queue_message({"type": "status", "state": "loading", "url": page.url})
        async with self._action_lock:
            client = None
            try:
                client = await page.context.new_cdp_session(page)
                await client.send("Network.enable")
                await client.send("Network.setCacheDisabled", {"cacheDisabled": True})
                await client.send("Network.setBypassServiceWorker", {"bypass": True})
            except Exception:
                client = None

            try:
                if client is not None:
                    await client.send("Page.reload", {"ignoreCache": True})
                    await page.wait_for_load_state(
                        "load",
                        timeout=self.settings.navigation_timeout_ms,
                    )
                else:
                    await page.reload(
                        wait_until="load",
                        timeout=self.settings.navigation_timeout_ms,
                    )
            except PlaywrightTimeoutError:
                await self._queue_message(
                    {
                        "type": "warning",
                        "message": "Reload timed out; showing the current browser state.",
                    }
                )
            finally:
                if client is not None:
                    await _ignore_shutdown_disconnect(
                        client.send("Network.setCacheDisabled", {"cacheDisabled": False})
                    )
                    await _ignore_shutdown_disconnect(
                        client.send("Network.setBypassServiceWorker", {"bypass": False})
                    )
                    await _ignore_shutdown_disconnect(client.detach())
        await self._queue_message(self._status_message("ready"))
        await self._send_current_frame()

    async def _go_back(self) -> None:
        self._clear_mobile_focus_zoom()
        if await self._activate_history_delta(-1):
            return
        page = self._require_page()
        await self._queue_message({"type": "status", "state": "loading", "url": page.url})
        async with self._action_lock:
            try:
                await page.go_back(
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

    async def _go_forward(self) -> None:
        self._clear_mobile_focus_zoom()
        if await self._activate_history_delta(1):
            return
        page = self._require_page()
        await self._queue_message({"type": "status", "state": "loading", "url": page.url})
        async with self._action_lock:
            try:
                await page.go_forward(
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

    async def _activate_history_delta(self, delta: int) -> bool:
        self._prune_closed_history()
        target_index = self._history_index + delta
        if target_index < 0 or target_index >= len(self._history):
            await self._queue_message({"type": "warning", "message": "No history entry available."})
            return True

        entry = self._history[target_index]
        self._history_index = target_index
        if entry.page.is_closed():
            self._prune_closed_history()
            return True

        self.page = entry.page
        await self._queue_message({"type": "status", "state": "loading", "url": entry.url})
        async with self._action_lock:
            if entry.page.url != entry.url:
                try:
                    self._history_replaying = True
                    await entry.page.goto(
                        entry.url,
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
                finally:
                    self._history_replaying = False
        await self._queue_message(self._status_message("ready"))
        return True

    async def _move_mouse_smoothly(self, page: Page, x: float, y: float) -> None:
        start_x, start_y = self._mouse_position
        distance = ((x - start_x) ** 2 + (y - start_y) ** 2) ** 0.5
        if distance < 0.5:
            self._mouse_position = (x, y)
            return

        duration = min(
            MOUSE_MOVE_MAX_DURATION_SECONDS,
            max(0.0, distance / MOUSE_MOVE_SPEED_PIXELS_PER_SECOND),
        )
        steps_by_distance = int(distance / MOUSE_MOVE_STEP_DISTANCE) + 1
        steps_by_time = int(duration / MOUSE_MOVE_STEP_INTERVAL_SECONDS) + 1
        steps = max(2, min(MOUSE_MOVE_MAX_STEPS, steps_by_distance, steps_by_time))
        delay = duration / steps if duration > 0 else 0

        for index in range(1, steps + 1):
            progress = index / steps
            # Smoothstep avoids a harsh start/stop without making the path slow.
            eased = progress * progress * (3 - 2 * progress)
            next_x = start_x + (x - start_x) * eased
            next_y = start_y + (y - start_y) * eased
            await page.mouse.move(next_x, next_y)
            if delay > 0 and index < steps:
                await asyncio.sleep(delay)
        self._mouse_position = (x, y)

    async def _move_mouse_directly(self, page: Page, x: float, y: float) -> None:
        await page.mouse.move(x, y)
        self._mouse_position = (x, y)

    async def _mouse_move(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        async with self._action_lock:
            x, y = await self._input_point(page, payload)
            await self._move_mouse_smoothly(page, x, y)
            if not payload.get("focus_view_keep"):
                self._clear_mobile_focus_zoom()

    async def _mouse_button(self, payload: dict[str, Any], down: bool) -> None:
        page = self._require_page()
        button = str(payload.get("button", "left"))
        if button not in {"left", "middle", "right"}:
            button = "left"
        async with self._action_lock:
            x, y = await self._input_point(page, payload)
            await self._move_mouse_directly(page, x, y)
            if down:
                await page.mouse.down(button=button)
            else:
                await page.mouse.up(button=button)
            self._clear_mobile_focus_zoom()

    async def _wheel(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        delta_x = float(payload.get("deltaX", 0))
        delta_y = float(payload.get("deltaY", 0))
        async with self._action_lock:
            if "x" in payload and "y" in payload:
                x, y = await self._input_point(page, payload)
                await self._move_mouse_smoothly(page, x, y)
            await page.mouse.wheel(delta_x, delta_y)
            self._clear_mobile_focus_zoom()

    async def _tap(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        async with self._action_lock:
            x, y = await self._input_point(page, payload)
            if self._is_mobile:
                await page.touchscreen.tap(x, y)
            else:
                await self._move_mouse_directly(page, x, y)
                await page.mouse.down()
                await page.mouse.up()

    async def _pinch(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        scale = max(0.2, min(5.0, float(payload.get("scale", 1.0))))
        if abs(scale - 1.0) < 0.01:
            return
        async with self._action_lock:
            next_page_scale_factor = max(0.5, min(5.0, self._page_scale_factor * scale))
            try:
                client = await page.context.new_cdp_session(page)
                try:
                    await client.send(
                        "Emulation.setPageScaleFactor",
                        {"pageScaleFactor": next_page_scale_factor},
                    )
                    self._page_scale_factor = next_page_scale_factor
                finally:
                    await client.detach()
            except Exception:
                x, y = await self._input_point(page, payload)
                await self._move_mouse_smoothly(page, x, y)
                await page.keyboard.down("Control")
                try:
                    await page.mouse.wheel(0, -360 * (scale - 1.0))
                finally:
                    await page.keyboard.up("Control")
            self._clear_mobile_focus_zoom()

    async def _probe_editable(self, payload: dict[str, Any]) -> None:
        page = self._require_page()
        async with self._action_lock:
            x, y = await self._input_point(page, payload, dom_coordinates=True)
            editable = await page.evaluate(
                """({ x, y }) => {
                    let element = document.elementFromPoint(x, y);
                    while (element && element.shadowRoot) {
                        const nested = element.shadowRoot.elementFromPoint(x, y);
                        if (!nested || nested === element) {
                            break;
                        }
                        element = nested;
                    }
                    if (!element) {
                        return false;
                    }
                    const editable = element.closest(
                        "input, textarea, [contenteditable], [role='textbox']"
                    );
                    if (!editable) {
                        return false;
                    }
                    if (editable.matches("input")) {
                        const type = (editable.getAttribute("type") || "text").toLowerCase();
                        const textTypes = new Set([
                            "email",
                            "number",
                            "password",
                            "search",
                            "tel",
                            "text",
                            "url",
                        ]);
                        return textTypes.has(type) && !editable.disabled && !editable.readOnly;
                    }
                    if (editable.matches("textarea")) {
                        return !editable.disabled && !editable.readOnly;
                    }
                    if (editable.matches("[contenteditable]")) {
                        return editable.getAttribute("contenteditable") !== "false";
                    }
                    return true;
                }""",
                {"x": x, "y": y},
            )
            self._clear_mobile_focus_zoom()
        await self._queue_message({"type": "editable", "editable": bool(editable)})

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
        await self._send_current_frame()

    async def _insert_text(self, text: str, *, focus_view: bool = False) -> None:
        if not text:
            return
        page = self._require_page()
        async with self._action_lock:
            await page.keyboard.insert_text(text)
            self._calibrate_caret_until = time.monotonic() + 1.0
            if focus_view and self._is_mobile:
                await self._focus_mobile_input_view(page)
        await self._send_current_frame()

    async def _editable_caret(self, page: Page) -> dict[str, float] | None:
        try:
            metrics = await page.evaluate(EDITABLE_METRICS_SCRIPT)
        except Exception:
            return None
        if not isinstance(metrics, dict):
            return None

        def metric_number(key: str, default: float) -> float:
            value = _float_value(metrics.get(key), None)
            return default if value is None else value

        caret_x = metric_number("caretX", metric_number("focusLeft", 0.0))
        caret_y = metric_number("caretY", metric_number("focusTop", 0.0))
        caret_width = max(1.0, metric_number("caretWidth", 2.0))
        caret_height = max(8.0, metric_number("caretHeight", 18.0))
        if time.monotonic() <= self._calibrate_caret_until:
            corrected_caret_x = await self._calibrate_text_input_caret_x(page, metrics, caret_x)
            if corrected_caret_x is not None:
                caret_x = corrected_caret_x
        visual_scale = metric_number("visualScale", 1.0)
        if visual_scale <= 0:
            visual_scale = 1.0
        visual_offset_left = metric_number("visualOffsetLeft", 0.0)
        visual_offset_top = metric_number("visualOffsetTop", 0.0)

        x = (caret_x - visual_offset_left) * visual_scale
        y = (caret_y - visual_offset_top) * visual_scale
        width = caret_width * visual_scale
        height = caret_height * visual_scale

        focus_clip = self._mobile_focus_clip
        if focus_clip is not None and self._mobile_focus_zoom > 0:
            clip_x = _float_value(focus_clip.get("x"), 0.0) or 0.0
            clip_y = _float_value(focus_clip.get("y"), 0.0) or 0.0
            x = (x - clip_x) * self._mobile_focus_zoom
            y = (y - clip_y) * self._mobile_focus_zoom
            width *= self._mobile_focus_zoom
            height *= self._mobile_focus_zoom

        frame_width = float(self.viewport_width)
        frame_height = float(self.viewport_height)
        if x + width < 0 or y + height < 0 or x > frame_width or y > frame_height:
            return None
        return {
            "x": max(0.0, min(frame_width, x)),
            "y": max(0.0, min(frame_height, y)),
            "width": max(1.0, min(frame_width, width)),
            "height": max(8.0, min(frame_height, height)),
        }

    async def _calibrate_text_input_caret_x(
        self, page: Page, metrics: dict[str, Any], caret_x: float
    ) -> float | None:
        if str(metrics.get("editableTag") or "").upper() != "INPUT":
            return None
        if str(metrics.get("editableType") or "text").lower() == "password":
            return None
        selection_start = _float_value(metrics.get("selectionStart"), None)
        selection_end = _float_value(metrics.get("selectionEnd"), None)
        value_length = _float_value(metrics.get("valueLength"), None)
        if (
            selection_start is None
            or selection_end is None
            or value_length is None
            or int(selection_start) != int(selection_end)
            or int(selection_end) != int(value_length)
            or value_length <= 0
        ):
            return None

        left = _float_value(metrics.get("left"), None)
        top = _float_value(metrics.get("top"), None)
        width = _float_value(metrics.get("width"), None)
        height = _float_value(metrics.get("height"), None)
        if left is None or top is None or width is None or height is None:
            return None
        if width < 12 or height < 12:
            return None

        clip_x = max(0.0, left)
        clip_y = max(0.0, top)
        clip_width = max(1.0, min(width, float(self.viewport_width) - clip_x))
        clip_height = max(1.0, min(height, float(self.viewport_height) - clip_y))
        try:
            image = await page.screenshot(
                type="png",
                scale="css",
                full_page=False,
                clip={"x": clip_x, "y": clip_y, "width": clip_width, "height": clip_height},
                timeout=3000,
            )
        except Exception:
            return None
        decoded = _png_rgba_rows(image)
        if decoded is None:
            return None
        image_width, image_height, rows = decoded
        if image_width < 4 or image_height < 4:
            return None

        border_left = max(0, int(round(_float_value(metrics.get("borderLeft"), 0.0) or 0.0)))
        border_top = max(0, int(round(_float_value(metrics.get("borderTop"), 0.0) or 0.0)))
        border_right = max(0, int(round(_float_value(metrics.get("borderRight"), 0.0) or 0.0)))
        border_bottom = max(0, int(round(_float_value(metrics.get("borderBottom"), 0.0) or 0.0)))
        padding_left = max(0, int(round(_float_value(metrics.get("paddingLeft"), 0.0) or 0.0)))
        padding_top = max(0, int(round(_float_value(metrics.get("paddingTop"), 0.0) or 0.0)))
        padding_right = max(0, int(round(_float_value(metrics.get("paddingRight"), 0.0) or 0.0)))
        padding_bottom = max(0, int(round(_float_value(metrics.get("paddingBottom"), 0.0) or 0.0)))
        content_left = min(image_width - 1, border_left + padding_left)
        content_right = max(content_left + 1, image_width - border_right - padding_right)
        content_top = min(image_height - 1, border_top + padding_top)
        content_bottom = max(content_top + 1, image_height - border_bottom - padding_bottom)

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            row = rows[y]
            index = x * 4
            return row[index], row[index + 1], row[index + 2]

        samples = [
            pixel(content_left, content_top),
            pixel(max(content_left, content_right - 1), content_top),
            pixel(content_left, max(content_top, content_bottom - 1)),
            pixel(max(content_left, content_right - 1), max(content_top, content_bottom - 1)),
        ]
        background = tuple(sum(sample[channel] for sample in samples) / len(samples) for channel in range(3))

        def distance_from_background(x: int, y: int) -> float:
            red, green, blue = pixel(x, y)
            return (
                abs(red - background[0])
                + abs(green - background[1])
                + abs(blue - background[2])
            )

        threshold = 72.0
        min_y = max(content_top, int(image_height * 0.18))
        max_y = min(content_bottom, int(image_height * 0.82))
        rightmost = -1
        for y in range(min_y, max_y):
            for x in range(content_left, content_right):
                if distance_from_background(x, y) >= threshold:
                    rightmost = max(rightmost, x)

        if rightmost < 0:
            return None
        corrected = clip_x + min(float(image_width), float(rightmost + 1))
        if abs(corrected - caret_x) > max(36.0, width * 0.6):
            return None
        return corrected

    async def _focus_mobile_input_view(self, page: Page) -> None:
        try:
            metrics = await page.evaluate(EDITABLE_METRICS_SCRIPT)
            if not isinstance(metrics, dict):
                return

            def metric_number(source: dict[str, Any], key: str, default: float) -> float:
                value = _float_value(source.get(key), None)
                return default if value is None else value

            zoom = 2.0
            clip_width = max(1.0, float(self.viewport_width) / zoom)
            clip_height = max(1.0, float(self.viewport_height) / zoom)
            element_left = metric_number(metrics, "left", 0.0)
            element_top = metric_number(metrics, "top", 0.0)
            element_width = metric_number(metrics, "width", 0.0)
            element_height = metric_number(metrics, "height", 0.0)
            focus_left = metric_number(metrics, "focusLeft", element_left + element_width / 2)
            focus_top = metric_number(metrics, "focusTop", element_top + element_height / 2)
            scroll_x = metric_number(metrics, "scrollX", 0.0)
            scroll_y = metric_number(metrics, "scrollY", 0.0)
            inner_width = metric_number(metrics, "innerWidth", float(self.viewport_width))
            inner_height = metric_number(metrics, "innerHeight", float(self.viewport_height))
            inner_width = inner_width if inner_width > 0 else float(self.viewport_width)
            inner_height = inner_height if inner_height > 0 else float(self.viewport_height)
            document_width = metric_number(metrics, "documentWidth", inner_width)
            document_height = metric_number(metrics, "documentHeight", inner_height)
            focus_page_x = scroll_x + focus_left
            focus_page_y = scroll_y + focus_top
            target_x_in_viewport = clip_width / 2
            target_y_in_viewport = clip_height * 0.42
            max_scroll_x = max(0.0, document_width - inner_width)
            max_scroll_y = max(0.0, document_height - inner_height)
            scroll_target_x = max(
                0.0, min(max_scroll_x, focus_page_x - target_x_in_viewport)
            )
            scroll_target_y = max(
                0.0, min(max_scroll_y, focus_page_y - target_y_in_viewport)
            )
            await page.evaluate(
                """({ x, y }) => {
                    window.scrollTo({ left: x, top: y, behavior: "instant" });
                }""",
                {"x": scroll_target_x, "y": scroll_target_y},
            )
            await page.wait_for_timeout(50)
            scrolled_metrics = await page.evaluate(EDITABLE_METRICS_SCRIPT)
            if isinstance(scrolled_metrics, dict):
                metrics = scrolled_metrics
                element_left = metric_number(metrics, "left", element_left)
                element_top = metric_number(metrics, "top", element_top)
                element_width = metric_number(metrics, "width", element_width)
                element_height = metric_number(metrics, "height", element_height)
                focus_left = metric_number(metrics, "focusLeft", element_left + element_width / 2)
                focus_top = metric_number(metrics, "focusTop", element_top + element_height / 2)
                inner_width = metric_number(metrics, "innerWidth", inner_width)
                inner_height = metric_number(metrics, "innerHeight", inner_height)
            target_x_in_clip = clip_width / 2
            target_y_in_clip = clip_height * 0.42
            crop_width = min(float(self.viewport_width), inner_width)
            crop_height = min(float(self.viewport_height), inner_height)
            max_clip_x = max(0.0, crop_width - clip_width)
            max_clip_y = max(0.0, crop_height - clip_height)
            clip_x = max(
                0.0, min(max_clip_x, focus_left - target_x_in_clip)
            )
            clip_y = max(
                0.0, min(max_clip_y, focus_top - target_y_in_clip)
            )
            self._mobile_focus_zoom = zoom
            self._mobile_focus_clip = {
                "x": clip_x,
                "y": clip_y,
                "width": clip_width,
                "height": clip_height,
            }
            await page.wait_for_timeout(80)
        except Exception:
            return

    def _clear_mobile_focus_zoom(self) -> None:
        self._mobile_focus_zoom = 1.0
        self._mobile_focus_clip = None

    async def _paste_text(self, text: str) -> None:
        if not text:
            return
        page = self._require_page()
        async with self._action_lock:
            if await self._write_clipboard_text(page, text):
                await page.keyboard.press("Control+V")
            elif not await self._dispatch_synthetic_paste(page, text):
                await page.keyboard.insert_text(text)
        await self._send_current_frame()

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

    async def _input_point(
        self, page: Page, payload: dict[str, Any], *, dom_coordinates: bool = False
    ) -> tuple[float, float]:
        x, y = self._point(payload)
        focus_clip = self._mobile_focus_clip
        if focus_clip is not None and self._mobile_focus_zoom > 0:
            x = (_float_value(focus_clip.get("x"), 0.0) or 0.0) + x / self._mobile_focus_zoom
            y = (_float_value(focus_clip.get("y"), 0.0) or 0.0) + y / self._mobile_focus_zoom
        if not self._is_mobile:
            return x, y
        try:
            viewport = await page.evaluate(
                """() => ({
                    scale: window.visualViewport ? window.visualViewport.scale : 1,
                    offsetLeft: window.visualViewport ? window.visualViewport.offsetLeft : 0,
                    offsetTop: window.visualViewport ? window.visualViewport.offsetTop : 0,
                    pageLeft: window.visualViewport ? window.visualViewport.pageLeft : window.scrollX,
                    pageTop: window.visualViewport ? window.visualViewport.pageTop : window.scrollY,
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    width: window.visualViewport ? window.visualViewport.width : window.innerWidth,
                    height: window.visualViewport ? window.visualViewport.height : window.innerHeight,
                })"""
            )
        except Exception:
            return x, y

        scale = _float_value(viewport.get("scale"), 1.0) or 1.0
        if scale <= 0:
            return x, y
        offset_left = _visual_viewport_offset(viewport, "offsetLeft", "pageLeft", "scrollX")
        offset_top = _visual_viewport_offset(viewport, "offsetTop", "pageTop", "scrollY")
        visible_width = _float_value(viewport.get("width"), float(self.viewport_width))
        visible_height = _float_value(viewport.get("height"), float(self.viewport_height))
        visible_width = visible_width if visible_width and visible_width > 0 else float(self.viewport_width)
        visible_height = (
            visible_height if visible_height and visible_height > 0 else float(self.viewport_height)
        )
        css_x = x / scale
        css_y = y / scale
        if dom_coordinates:
            css_x += offset_left
            css_y += offset_top
            css_x = max(offset_left, min(offset_left + visible_width, css_x))
            css_y = max(offset_top, min(offset_top + visible_height, css_y))
        else:
            css_x = max(0.0, min(visible_width, css_x))
            css_y = max(0.0, min(visible_height, css_y))
        return css_x, css_y

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

    async def _queue_message(self, message: dict[str, Any], *, reliable: bool = False) -> None:
        if not self._connections:
            if reliable and message.get("type") != "dialog":
                self._pending_reliable_messages.append(message)
                self._pending_reliable_messages = self._pending_reliable_messages[-32:]
            return
        for connection in list(self._connections.values()):
            await self._queue_to_connection(connection.outgoing, message, reliable=reliable)

    async def _queue_to_connection(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        message: dict[str, Any],
        *,
        reliable: bool = False,
    ) -> None:
        if reliable:
            await queue.put(message)
            return
        message_type = message.get("type")
        try:
            queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass

        if message_type == "audio" and message.get("kind") == "chunk":
            self._drop_queued_messages(
                queue,
                lambda queued: queued.get("type") == "audio" and queued.get("kind") == "chunk",
                limit=1,
            )
            if not queue.full():
                queue.put_nowait(message)
            return

        self._drop_queued_messages(queue, lambda queued: queued.get("type") == "frame")
        if queue.full():
            self._drop_queued_messages(
                queue,
                lambda queued: queued.get("type") == "audio" and queued.get("kind") == "chunk",
                limit=1,
            )
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
        queue.put_nowait(message)

    async def _queue_audio(self, message: dict[str, Any]) -> None:
        if not self._audio_connections:
            return
        for connection in list(self._audio_connections.values()):
            await self._queue_audio_to_connection(connection.outgoing, message)

    async def _queue_audio_to_connection(
        self, queue: asyncio.Queue[dict[str, Any]], message: dict[str, Any]
    ) -> None:
        try:
            queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass

        if message.get("kind") == "chunk":
            self._drop_queued_messages(
                queue,
                lambda queued: queued.get("type") == "audio" and queued.get("kind") == "chunk",
                limit=4,
            )
            if queue.full():
                return
            queue.put_nowait(message)
            return

        self._drop_queued_messages(
            queue,
            lambda queued: queued.get("type") == "audio" and queued.get("kind") == "chunk",
            limit=1,
        )
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
        queue.put_nowait(message)

    async def _queue_frame(self, message: dict[str, Any]) -> None:
        if not self._connections:
            return
        for connection in list(self._connections.values()):
            queue = connection.outgoing
            self._drop_queued_messages(queue, lambda queued: queued.get("type") == "frame")
            if queue.full():
                self._drop_queued_messages(
                    queue,
                    lambda queued: queued.get("type") == "audio" and queued.get("kind") == "chunk",
                    limit=1,
                )
            if queue.full():
                continue
            await queue.put(message)

    @staticmethod
    def _drop_queued_messages(
        queue: asyncio.Queue[dict[str, Any]],
        predicate: Any,
        *,
        limit: int | None = None,
    ) -> int:
        kept: list[dict[str, Any]] = []
        dropped = 0
        while True:
            try:
                message = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (limit is None or dropped < limit) and predicate(message):
                dropped += 1
                continue
            kept.append(message)
        for message in kept:
            queue.put_nowait(message)
        return dropped

    def _require_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            raise RuntimeError("Browser page is not available.")
        return self.page

    def _require_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("Browser context is not available.")
        return self.context

    def _normalize_cookie(self, cookie: dict[str, Any], page_url: str) -> dict[str, Any]:
        name = str(cookie.get("name") or "").strip()
        if not name:
            raise ValueError("Cookie name is required.")
        path = str(cookie.get("path") or "/").strip() or "/"
        domain = str(cookie.get("domain") or "").strip()
        if not domain:
            host = urlsplit(page_url).hostname
            if not host:
                raise ValueError("Cookie domain is required.")
            domain = host

        normalized: dict[str, Any] = {
            "name": name,
            "value": str(cookie.get("value") or ""),
            "domain": domain,
            "path": path,
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", False)),
        }
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            normalized["expires"] = float(expires)
        same_site = cookie.get("sameSite")
        if same_site in {"Lax", "None", "Strict"}:
            normalized["sameSite"] = same_site
        partition_key = cookie.get("partitionKey")
        if isinstance(partition_key, str) and partition_key:
            normalized["partitionKey"] = partition_key
        return normalized


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._client_sessions: dict[str, str] = {}
        self._session_clients: dict[str, set[str]] = {}
        self._client_contexts: dict[str, ClientContextEntry] = {}
        self._session_start_tasks: set[asyncio.Task[Any]] = set()
        self._client_contexts_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._shared_context_policy = HostAccessPolicy(self.settings.allow_private_hosts)
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless,
            args=[
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                f"--lang={self.settings.locale}",
            ],
        )
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        self._stopping = True
        await self._cancel_session_start_tasks()
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

        for session_id in list(self._sessions):
            await self.close_session(session_id)

        await self._close_all_client_contexts()

        browser = self._browser
        self._browser = None
        if browser is not None:
            await _ignore_shutdown_disconnect(browser.close())

        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            await _ignore_shutdown_disconnect(playwright.stop())

    @property
    def is_stopping(self) -> bool:
        return self._stopping

    def request_stop(self) -> None:
        self._stopping = True
        for task in list(self._session_start_tasks):
            if not task.done():
                task.cancel()

    async def _cancel_session_start_tasks(self) -> None:
        tasks = [task for task in self._session_start_tasks if not task.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get_browser(self) -> Browser:
        if self._stopping:
            raise RuntimeError("Browser manager is shutting down.")
        if self._browser is None:
            await self.start()
        if self._browser is None:
            raise RuntimeError("Browser failed to start.")
        return self._browser

    def browser_version(self) -> str:
        if self._browser is None:
            return "120.0.0.0"
        return self._browser.version

    def browser_major_version(self) -> str:
        version = self.browser_version()
        major = version.split(".", 1)[0]
        return major if major.isdigit() else "120"

    def chrome_brands_header(self) -> str:
        major = self.browser_major_version()
        return (
            f'"Chromium";v="{major}", '
            f'"Google Chrome";v="{major}", '
            '"Not=A?Brand";v="99"'
        )

    def desktop_user_agent(self) -> str:
        if self.settings.user_agent:
            return self.settings.user_agent
        return DESKTOP_USER_AGENT_TEMPLATE.format(version=self.browser_version())

    def desktop_extra_http_headers(self) -> dict[str, str]:
        return {
            "Accept-Language": self.settings.accept_language,
            "sec-ch-ua": self.chrome_brands_header(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": DESKTOP_CLIENT_HINT_PLATFORM,
        }

    def mobile_extra_http_headers(self) -> dict[str, str]:
        return {
            "Accept-Language": self.settings.accept_language,
            "sec-ch-ua": self.chrome_brands_header(),
            **MOBILE_CLIENT_HINT_HEADERS,
        }

    def compatible_request_headers(
        self, original_headers: dict[str, str], *, is_mobile: bool = False
    ) -> dict[str, str]:
        headers = dict(original_headers)
        compatible_headers = (
            self.mobile_extra_http_headers() if is_mobile else self.desktop_extra_http_headers()
        )
        if not is_mobile:
            headers["user-agent"] = self.desktop_user_agent()
        for key, value in compatible_headers.items():
            headers[key.lower()] = value
        return headers

    def mobile_context_options(self, width: int, height: int) -> dict[str, Any]:
        options: dict[str, Any] = {
            "is_mobile": True,
            "has_touch": True,
            "screen": {"width": width, "height": height},
            "extra_http_headers": self.mobile_extra_http_headers(),
            "user_agent": MOBILE_FALLBACK_USER_AGENT,
        }
        if self._playwright is None:
            return options

        for name in MOBILE_DEVICE_DESCRIPTOR_NAMES:
            descriptor = self._playwright.devices.get(name)
            if not descriptor:
                continue
            user_agent = descriptor.get("user_agent")
            if user_agent:
                options["user_agent"] = user_agent
            return options
        return options

    async def acquire_context(
        self, client_uuid: str, context_options: dict[str, Any]
    ) -> BrowserContext:
        options_signature = self._context_options_signature(context_options)
        stale_context: BrowserContext | None = None
        async with self._client_contexts_lock:
            cached = self._client_contexts.get(client_uuid)
            if cached is not None:
                if cached.options_signature == options_signature:
                    cached.last_used = time.monotonic()
                    return cached.context
                self._client_contexts.pop(client_uuid, None)
                stale_context = cached.context

            browser = await self.get_browser()
            context = await browser.new_context(**context_options)
            await context.expose_binding(
                "__websiteAgentAudioBridge",
                lambda source, message, client_uuid=client_uuid: asyncio.create_task(
                    self._handle_audio_bridge(client_uuid, message)
                ),
            )
            await context.route("**/*", self._guard_shared_context_route)
            await context.route_web_socket("**/*", self._guard_shared_context_websocket)
            self._client_contexts[client_uuid] = ClientContextEntry(
                context=context,
                last_used=time.monotonic(),
                options_signature=options_signature,
            )
        if stale_context is not None:
            await _ignore_shutdown_disconnect(stale_context.close())
        return context

    def _context_options_signature(self, options: dict[str, Any]) -> tuple[Any, ...]:
        viewport = options.get("viewport")
        screen = options.get("screen")
        headers = options.get("extra_http_headers")
        return (
            bool(options.get("is_mobile", False)),
            bool(options.get("has_touch", False)),
            options.get("user_agent"),
            options.get("locale"),
            options.get("timezone_id"),
            options.get("device_scale_factor"),
            tuple(sorted(viewport.items())) if isinstance(viewport, dict) else viewport,
            tuple(sorted(screen.items())) if isinstance(screen, dict) else screen,
            tuple(sorted(headers.items())) if isinstance(headers, dict) else headers,
        )

    def touch_context(self, client_uuid: str) -> None:
        cached = self._client_contexts.get(client_uuid)
        if cached is not None:
            cached.last_used = time.monotonic()

    async def create_session(
        self,
        raw_url: str,
        width: int,
        height: int,
        client_uuid: str,
        lock_url: str | None = None,
        is_mobile: bool = False,
        device_scale_factor: float = 1.0,
    ) -> BrowserSession:
        if self._stopping:
            raise RuntimeError("Browser manager is shutting down.")
        previous_session_id = self._client_sessions.get(client_uuid)
        if previous_session_id is not None:
            await self.close_session(previous_session_id, close_context=False)
        session_id = uuid4().hex
        session = BrowserSession(self, session_id, client_uuid, lock_url)
        self._sessions[session_id] = session
        self._client_sessions[client_uuid] = session_id
        self._session_clients[session_id] = {client_uuid}
        start_task: asyncio.Task[None] | None = None
        try:
            start_task = asyncio.create_task(
                session.start(raw_url, width, height, is_mobile, device_scale_factor)
            )
            self._session_start_tasks.add(start_task)
            await start_task
            if self._stopping:
                raise RuntimeError("Browser manager is shutting down.")
        except asyncio.CancelledError:
            self._sessions.pop(session_id, None)
            self._session_clients.pop(session_id, None)
            if self._client_sessions.get(client_uuid) == session_id:
                self._client_sessions.pop(client_uuid, None)
            await session.close()
            await self.close_client_context(client_uuid)
            raise
        except Exception:
            self._sessions.pop(session_id, None)
            self._session_clients.pop(session_id, None)
            if self._client_sessions.get(client_uuid) == session_id:
                self._client_sessions.pop(client_uuid, None)
            await session.close()
            await self.close_client_context(client_uuid)
            raise
        finally:
            if start_task is not None:
                self._session_start_tasks.discard(start_task)
        return session

    async def _guard_shared_context_route(self, route: Route) -> None:
        try:
            await self._shared_context_policy.ensure_request_url_allowed(route.request.url)
            await route.continue_(
                headers=self.compatible_request_headers(route.request.headers)
            )
        except URLPolicyError:
            await route.abort()

    async def _guard_shared_context_websocket(self, websocket: WebSocketRoute) -> None:
        try:
            await self._shared_context_policy.ensure_request_url_allowed(websocket.url)
            websocket.connect_to_server()
        except URLPolicyError as exc:
            await websocket.close(code=1008, reason=str(exc)[:120])

    async def _handle_audio_bridge(self, client_uuid: str, message: Any) -> None:
        session = self.get_session_for_client(client_uuid)
        if session is None:
            return
        await session.handle_audio_bridge_message(message)

    def get_session(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    def client_can_access_session(self, client_uuid: str, session_id: str) -> bool:
        return client_uuid in self._session_clients.get(session_id, set())

    def get_session_for_client(self, client_uuid: str) -> BrowserSession | None:
        session_id = self._client_sessions.get(client_uuid)
        if session_id is None:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            self._client_sessions.pop(client_uuid, None)
        return session

    async def join_session(self, session_id: str, client_uuid: str) -> BrowserSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        previous_session_id = self._client_sessions.get(client_uuid)
        if previous_session_id and previous_session_id != session_id:
            await self.leave_session(previous_session_id, client_uuid)
        self._client_sessions[client_uuid] = session_id
        self._session_clients.setdefault(session_id, set()).add(client_uuid)
        session.last_activity = time.monotonic()
        return session

    async def leave_session(self, session_id: str, client_uuid: str) -> None:
        session = self._sessions.get(session_id)
        if self._client_sessions.get(client_uuid) == session_id:
            self._client_sessions.pop(client_uuid, None)
        clients = self._session_clients.get(session_id)
        if clients is not None:
            clients.discard(client_uuid)
            if not clients:
                self._session_clients.pop(session_id, None)
        if session is not None:
            await session.disconnect_client(client_uuid)
            if client_uuid == session.client_uuid and not self._session_clients.get(session_id):
                await self.close_session(session_id)

    async def close_session(self, session_id: str, *, close_context: bool = True) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            client_uuid = session.client_uuid
            old_context_active = any(
                other_session_id != session_id
                and other_session is not session
                and other_session.client_uuid == client_uuid
                for other_session_id, other_session in self._sessions.items()
            )
            clients = self._session_clients.pop(session_id, set())
            clients.add(session.client_uuid)
            for mapped_client_uuid in clients:
                if self._client_sessions.get(mapped_client_uuid) == session_id:
                    self._client_sessions.pop(mapped_client_uuid, None)
            await session.close()
            if close_context and client_uuid not in self._client_sessions and not old_context_active:
                await self.close_client_context(client_uuid)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if (
                    not session.is_connected
                    and session.disconnected_at > 0
                    and now - session.disconnected_at > self.settings.session_ttl_seconds
                )
            ]
            for session_id in expired:
                await self.close_session(session_id)
            await self._cleanup_idle_client_contexts(now)

    async def _cleanup_idle_client_contexts(self, now: float) -> None:
        ttl = self.settings.session_ttl_seconds
        async with self._client_contexts_lock:
            expired = [
                client_uuid
                for client_uuid, entry in self._client_contexts.items()
                if (
                    client_uuid not in self._client_sessions
                    and not self._has_session_for_context_owner(client_uuid)
                    and now - entry.last_used > ttl
                )
            ]
        for client_uuid in expired:
            await self.close_client_context(client_uuid)

    def _has_session_for_context_owner(self, client_uuid: str) -> bool:
        return any(session.client_uuid == client_uuid for session in self._sessions.values())

    async def close_client_context(self, client_uuid: str) -> None:
        async with self._client_contexts_lock:
            entry = self._client_contexts.pop(client_uuid, None)
        if entry is not None:
            await _ignore_shutdown_disconnect(entry.context.close())
        await self.delete_client_files(client_uuid)

    async def _close_all_client_contexts(self) -> None:
        async with self._client_contexts_lock:
            items = list(self._client_contexts.items())
            self._client_contexts.clear()
        for client_uuid, entry in items:
            await _ignore_shutdown_disconnect(entry.context.close())
            await self.delete_client_files(client_uuid)

    def client_downloads_dir(self, client_uuid: str) -> Path:
        return self.settings.downloads_dir / client_uuid

    def client_uploads_dir(self, client_uuid: str) -> Path:
        return self.settings.uploads_dir / client_uuid

    async def delete_client_files(self, client_uuid: str) -> None:
        roots = [self.client_downloads_dir(client_uuid), self.client_uploads_dir(client_uuid)]
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            allowed_roots = [
                self.settings.downloads_dir.resolve(),
                self.settings.uploads_dir.resolve(),
            ]
            if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
                logger.warning("Refusing to delete unexpected client data path: %s", resolved)
                continue
            await asyncio.to_thread(shutil.rmtree, resolved, ignore_errors=True)


async def _ignore_shutdown_disconnect(awaitable: Any) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not _is_shutdown_disconnect(exc) and not _is_websocket_disconnect(exc):
            raise


def _is_shutdown_disconnect(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "connection closed" in message
        or "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "playwright connection closed" in message
    )


def _is_websocket_disconnect(exc: Exception) -> bool:
    if isinstance(exc, WebSocketDisconnect):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).lower()
    return (
        "websocket is not connected" in message
        or "need to call \"accept\" first" in message
        or "cannot call \"send\" once a close message has been sent" in message
        or ("unexpected asgi message" in message and "websocket" in message)
    )
