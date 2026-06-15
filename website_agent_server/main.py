from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import PinAuth
from .browser import BrowserManager
from .config import PROJECT_ROOT, settings
from .url_policy import URLPolicyError


STATIC_DIR = PROJECT_ROOT / "static"
manager = BrowserManager(settings)
pin_auth = PinAuth(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="Website Agent Server", version="0.1.0", lifespan=lifespan)


class CreateSessionRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    width: int = Field(default=1280, ge=240, le=4096)
    height: int = Field(default=720, ge=180, le=4096)


class CreateSessionResponse(BaseModel):
    session_id: str
    url: str


class ClipboardRequest(BaseModel):
    cut: bool = False


class ClipboardResponse(BaseModel):
    text: str


@app.get("/")
async def index(request: Request) -> FileResponse:
    if not pin_auth.is_request_allowed(request):
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
    host = request.url.hostname or settings.host
    if request.url.port is not None:
        host = f"{host}:{request.url.port}"
    response = FileResponse(STATIC_DIR / "index.html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        f"connect-src 'self' ws://{host} wss://{host}; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth")
async def authenticate(pin: Annotated[str, Form()]) -> RedirectResponse:
    if not pin_auth.verify_pin(pin):
        return RedirectResponse("/?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    pin_auth.set_cookie(response)
    return response


@app.post("/auth/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    pin_auth.clear_cookie(response)
    return response


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session(
    request: Request, payload: CreateSessionRequest
) -> CreateSessionResponse:
    pin_auth.require_request(request)
    try:
        session = await manager.create_session(payload.url, payload.width, payload.height)
    except URLPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open the target site: {exc}") from exc
    page = session.page
    return CreateSessionResponse(session_id=session.id, url=page.url if page is not None else "")


@app.post("/api/sessions/{session_id}/close")
async def close_session(request: Request, session_id: str) -> dict[str, str]:
    pin_auth.require_request(request)
    await manager.close_session(session_id)
    return {"status": "closed"}


@app.post("/api/sessions/{session_id}/clipboard", response_model=ClipboardResponse)
async def read_clipboard_selection(
    request: Request, session_id: str, payload: ClipboardRequest
) -> ClipboardResponse:
    pin_auth.require_request(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    text = await session.copy_selection(cut=payload.cut)
    return ClipboardResponse(text=text)


@app.websocket("/ws/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    if not pin_auth.is_websocket_allowed(websocket):
        await websocket.close(code=4401)
        return
    session = manager.get_session(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    await session.connect(websocket)


@app.get("/api/sessions/{session_id}/downloads/{token}")
async def download_file(request: Request, session_id: str, token: str) -> FileResponse:
    pin_auth.require_request(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
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
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    upload_dir = settings.uploads_dir / session_id / token
    upload_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for upload in files:
        filename = Path(upload.filename or "upload.bin").name
        path = upload_dir / filename
        content = await upload.read()
        path.write_bytes(content)
        paths.append(path)
    try:
        await session.upload_file_chooser(token, paths)
    except KeyError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    return {"status": "selected"}
