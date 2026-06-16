from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re
from typing import Annotated
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .auth import PinAuth
from .browser import BrowserManager
from .config import settings
from .url_policy import HostAccessPolicy, URLPolicyError


STATIC_DIR = Path(__file__).resolve().parent / "static"
PLAYWRIGHT_FILE_PAYLOAD_LIMIT_BYTES = 50 * 1024 * 1024
manager = BrowserManager(settings)
pin_auth = PinAuth(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="Website Agent Server", version=__version__, lifespan=lifespan)


class CreateSessionRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    lock_url: str | None = Field(default=None, max_length=4096)
    width: int = Field(default=1280, ge=240, le=4096)
    height: int = Field(default=720, ge=180, le=4096)
    is_mobile: bool = False
    device_scale_factor: float = Field(default=1.0, ge=0.5, le=4.0)


class CreateSessionResponse(BaseModel):
    session_id: str
    url: str


class CurrentSessionResponse(BaseModel):
    session_id: str | None = None
    url: str | None = None
    locked: bool = False


class ClientConfigResponse(BaseModel):
    locked: bool
    lock_url: str | None = None
    global_locked: bool = False


class ClipboardRequest(BaseModel):
    cut: bool = False


class ClipboardResponse(BaseModel):
    text: str


class CookieRecord(BaseModel):
    name: str = Field(min_length=1, max_length=4096)
    value: str = Field(default="", max_length=16384)
    domain: str = Field(default="", max_length=4096)
    path: str = Field(default="/", max_length=4096)
    expires: float | None = None
    httpOnly: bool = False
    secure: bool = False
    sameSite: str | None = None
    partitionKey: str | None = None


class CookieListResponse(BaseModel):
    cookies: list[CookieRecord]


class CookieUpdateRequest(BaseModel):
    cookies: list[CookieRecord]


def _auth_response() -> FileResponse:
    response = FileResponse(STATIC_DIR / "auth.html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "connect-src 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


def _app_response(request: Request) -> FileResponse:
    host = request.url.hostname or settings.host
    if request.url.port is not None:
        host = f"{host}:{request.url.port}"
    client_uuid = _client_session_uuid(request)
    response = FileResponse(STATIC_DIR / "index.html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        f"connect-src 'self' ws://{host} wss://{host}; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    _set_client_session_cookie(response, client_uuid)
    return response


def _request_path_with_query(request: Request) -> str:
    path = request.url.path or "/"
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return path


def _client_session_uuid(request: Request) -> str:
    value = request.cookies.get(settings.client_session_cookie_name, "")
    if len(value) == 32:
        try:
            int(value, 16)
        except ValueError:
            pass
        else:
            return value
    return uuid4().hex


def _set_client_session_cookie(response: Response, client_uuid: str) -> None:
    response.set_cookie(
        settings.client_session_cookie_name,
        client_uuid,
        max_age=settings.client_session_cookie_max_age,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _upload_display_filename(filename: str | None) -> str:
    name = Path(filename or "upload.bin").name
    return name or "upload.bin"


def _safe_upload_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return cleaned or "upload.bin"


def _require_owned_session(request: Request, session_id: str):
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if request.cookies.get(settings.client_session_cookie_name, "") != session.client_uuid:
        raise HTTPException(status_code=403, detail="Session does not belong to this client.")
    return session


def _safe_next_path(next_url: str | None) -> str:
    if (
        not next_url
        or not next_url.startswith("/")
        or next_url.startswith("//")
        or "\r" in next_url
        or "\n" in next_url
    ):
        return "/"
    return next_url


def _auth_redirect_for(request: Request) -> RedirectResponse:
    next_url = quote(_request_path_with_query(request), safe="")
    return RedirectResponse(f"/auth?next_url={next_url}", status_code=303)


def _append_query_to_url(url: str, query: str) -> str:
    if not query:
        return url
    base, separator, fragment = url.partition("#")
    query_separator = "&" if "?" in base else "?"
    result = f"{base}{query_separator}{query}"
    if separator:
        result = f"{result}{separator}{fragment}"
    return result


async def _lock_url_from_agent_path(path: str, query: str = "") -> str | None:
    prefix = "/lock_url/"
    if not path.startswith(prefix):
        return None
    raw_path = path[len(prefix) :]
    if not raw_path:
        raise URLPolicyError("Locked URL path is empty.")

    path_parts = raw_path.split("/")
    try:
        scheme = unquote(path_parts[0] or "").lower()
        if scheme in {"http", "https"} and len(path_parts) > 1:
            rest = unquote("/".join(path_parts[1:]))
            if not rest:
                raise URLPolicyError("Locked URL path must include a host name.")
            target_url = f"{scheme}://{rest}"
        else:
            target_url = unquote(raw_path)
    except UnicodeDecodeError as exc:
        raise URLPolicyError("Locked URL path is malformed.") from exc

    lock_url = _append_query_to_url(target_url, query)
    policy = HostAccessPolicy(settings.allow_private_hosts)
    return await policy.ensure_navigation_url_allowed(
        lock_url,
        verify_https=not settings.ignore_https_errors,
    )


async def _lock_url_from_request_referrer(request: Request) -> str | None:
    referrer = request.headers.get("referer")
    if not referrer:
        return None
    parts = urlsplit(referrer)
    request_host = request.headers.get("host", "")
    if parts.netloc and request_host and parts.netloc != request_host:
        return None
    return await _lock_url_from_agent_path(parts.path, parts.query)


def _same_url_without_fragment(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme,
        left_parts.netloc,
        left_parts.path,
        left_parts.query,
    ) == (
        right_parts.scheme,
        right_parts.netloc,
        right_parts.path,
        right_parts.query,
    )


@app.get("/")
async def index(request: Request) -> FileResponse:
    if not pin_auth.is_request_allowed(request):
        return _auth_response()
    return _app_response(request)


@app.get("/lock_url/{target:path}")
async def locked_url_index(request: Request, target: str) -> Response:
    if not pin_auth.is_request_allowed(request):
        return _auth_redirect_for(request)
    return _app_response(request)


@app.get("/static/{filename}")
async def static_file(request: Request, filename: str) -> FileResponse:
    pin_auth.require_request(request)
    if filename not in {"app.js", "styles.css"}:
        raise HTTPException(status_code=404, detail="Static file not found.")
    return FileResponse(STATIC_DIR / filename)


@app.get("/auth-static/{filename}")
async def auth_static(filename: str) -> FileResponse:
    if filename not in {"auth.css", "auth.js"}:
        raise HTTPException(status_code=404, detail="Static file not found.")
    return FileResponse(STATIC_DIR / filename)


@app.get("/auth")
async def auth_page() -> FileResponse:
    return _auth_response()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=ClientConfigResponse)
async def client_config(request: Request) -> ClientConfigResponse:
    pin_auth.require_request(request)
    try:
        request_lock_url = await _lock_url_from_request_referrer(request)
    except URLPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lock_url = settings.lock_url or request_lock_url
    return ClientConfigResponse(
        locked=lock_url is not None,
        lock_url=lock_url,
        global_locked=settings.lock_url is not None,
    )


@app.get("/api/sessions/current", response_model=CurrentSessionResponse)
async def current_session(request: Request, response: Response) -> CurrentSessionResponse:
    pin_auth.require_request(request)
    client_uuid = _client_session_uuid(request)
    _set_client_session_cookie(response, client_uuid)
    session = manager.get_session_for_client(client_uuid)
    if session is None:
        return CurrentSessionResponse()
    page = session.page
    return CurrentSessionResponse(
        session_id=session.id,
        url=page.url if page is not None and not page.is_closed() else "",
        locked=session.is_locked,
    )


@app.post("/auth")
async def authenticate(
    pin: Annotated[str, Form()],
    next_url: Annotated[str, Form()] = "/",
) -> RedirectResponse:
    redirect_url = _safe_next_path(next_url)
    if not pin_auth.verify_pin(pin):
        encoded_next_url = quote(redirect_url, safe="")
        return RedirectResponse(f"/auth?error=1&next_url={encoded_next_url}", status_code=303)
    response = RedirectResponse(redirect_url, status_code=303)
    pin_auth.set_cookie(response)
    return response


@app.post("/auth/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    pin_auth.clear_cookie(response)
    return response


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session(
    request: Request, response: Response, payload: CreateSessionRequest
) -> CreateSessionResponse:
    pin_auth.require_request(request)
    client_uuid = _client_session_uuid(request)
    _set_client_session_cookie(response, client_uuid)
    try:
        request_lock_url = await _lock_url_from_request_referrer(request)
        payload_lock_url = None
        if payload.lock_url:
            lock_url_policy = HostAccessPolicy(settings.allow_private_hosts)
            payload_lock_url = await lock_url_policy.ensure_navigation_url_allowed(
                payload.lock_url,
                verify_https=not settings.ignore_https_errors,
            )
        lock_url = settings.lock_url
        if lock_url is None and request_lock_url is not None:
            if payload_lock_url is not None:
                if not _same_url_without_fragment(payload_lock_url, request_lock_url):
                    raise URLPolicyError("Locked URL does not match the client path.")
                lock_url = payload_lock_url
            else:
                lock_url = request_lock_url
        elif lock_url is None:
            lock_url = payload_lock_url
        target_url = lock_url or payload.url
        session = await manager.create_session(
            target_url,
            payload.width,
            payload.height,
            client_uuid,
            lock_url=lock_url,
            is_mobile=payload.is_mobile,
            device_scale_factor=payload.device_scale_factor,
        )
    except URLPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open the target site: {exc}") from exc
    page = session.page
    return CreateSessionResponse(session_id=session.id, url=page.url if page is not None else "")


@app.post("/api/sessions/{session_id}/close")
async def close_session(request: Request, session_id: str) -> dict[str, str]:
    pin_auth.require_request(request)
    _require_owned_session(request, session_id)
    await manager.close_session(session_id)
    return {"status": "closed"}


@app.post("/api/sessions/{session_id}/clipboard", response_model=ClipboardResponse)
async def read_clipboard_selection(
    request: Request, session_id: str, payload: ClipboardRequest
) -> ClipboardResponse:
    pin_auth.require_request(request)
    session = _require_owned_session(request, session_id)
    text = await session.copy_selection(cut=payload.cut)
    return ClipboardResponse(text=text)


@app.get("/api/sessions/{session_id}/cookies", response_model=CookieListResponse)
async def list_cookies(request: Request, session_id: str) -> CookieListResponse:
    pin_auth.require_request(request)
    session = _require_owned_session(request, session_id)
    if session.is_locked:
        raise HTTPException(status_code=403, detail="Cookie management is locked.")
    cookies = await session.list_cookies()
    return CookieListResponse(cookies=[CookieRecord(**cookie) for cookie in cookies])


@app.put("/api/sessions/{session_id}/cookies", response_model=CookieListResponse)
async def update_cookies(
    request: Request, session_id: str, payload: CookieUpdateRequest
) -> CookieListResponse:
    pin_auth.require_request(request)
    session = _require_owned_session(request, session_id)
    if session.is_locked:
        raise HTTPException(status_code=403, detail="Cookie management is locked.")
    try:
        cookies = await session.replace_cookies([cookie.model_dump() for cookie in payload.cookies])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CookieListResponse(cookies=[CookieRecord(**cookie) for cookie in cookies])


@app.websocket("/ws/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    if not pin_auth.is_websocket_allowed(websocket):
        await websocket.close(code=4401)
        return
    session = manager.get_session(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    client_uuid = websocket.cookies.get(settings.client_session_cookie_name, "")
    if client_uuid != session.client_uuid:
        await websocket.close(code=4403)
        return
    await session.connect(websocket)


@app.websocket("/ws/{session_id}/audio")
async def session_audio_socket(websocket: WebSocket, session_id: str) -> None:
    if not pin_auth.is_websocket_allowed(websocket):
        await websocket.close(code=4401)
        return
    session = manager.get_session(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    client_uuid = websocket.cookies.get(settings.client_session_cookie_name, "")
    if client_uuid != session.client_uuid:
        await websocket.close(code=4403)
        return
    await session.connect_audio(websocket)


@app.get("/api/sessions/{session_id}/downloads/{token}")
async def download_file(request: Request, session_id: str, token: str) -> FileResponse:
    pin_auth.require_request(request)
    session = _require_owned_session(request, session_id)
    record = session.get_download(token)
    if record is None or not record.path.exists():
        raise HTTPException(status_code=404, detail="Download not found.")
    return FileResponse(record.path, filename=record.filename, media_type="application/octet-stream")


@app.post("/api/sessions/{session_id}/file-chooser/{token}")
async def choose_files(
    request: Request,
    session_id: str,
    token: str,
    files: Annotated[list[UploadFile], File()],
) -> dict[str, str]:
    pin_auth.require_request(request)
    session = _require_owned_session(request, session_id)
    upload_dir = manager.client_uploads_dir(session.client_uuid) / session_id / token
    upload_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    payloads: list[dict[str, object]] = []
    total_payload_size = 0
    for upload in files:
        display_name = _upload_display_filename(upload.filename)
        safe_name = _safe_upload_filename(display_name)
        path = upload_dir / f"{len(paths):04d}-{safe_name}"
        content = await upload.read()
        content_type = upload.content_type or "application/octet-stream"
        path.write_bytes(content)
        paths.append(path)
        total_payload_size += len(content)
        payloads.append(
            {
                "name": display_name,
                "mimeType": content_type,
                "buffer": content,
            }
        )
    if not files:
        selection: list[dict[str, object]] | list[Path] = []
    elif total_payload_size <= PLAYWRIGHT_FILE_PAYLOAD_LIMIT_BYTES:
        selection = payloads
    else:
        selection = paths
    try:
        await session.upload_file_chooser(token, selection)
    except KeyError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    return {"status": "selected"}
