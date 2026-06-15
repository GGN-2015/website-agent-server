from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import HTTPException, Request, WebSocket, status
from starlette.responses import Response

from .config import Settings


class PinAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._secret = secrets.token_urlsafe(32)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.pin)

    def verify_pin(self, pin: str) -> bool:
        expected = self.settings.pin or ""
        return hmac.compare_digest(pin, expected)

    def token(self) -> str:
        if not self.settings.pin:
            return ""
        digest = hmac.new(
            self._secret.encode("utf-8"),
            self.settings.pin.encode("utf-8"),
            sha256,
        ).hexdigest()
        return digest

    def is_request_allowed(self, request: Request) -> bool:
        if not self.enabled:
            return True
        token = request.cookies.get(self.settings.auth_cookie_name, "")
        return hmac.compare_digest(token, self.token())

    def require_request(self, request: Request) -> None:
        if not self.is_request_allowed(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="PIN required.",
            )

    def is_websocket_allowed(self, websocket: WebSocket) -> bool:
        if not self.enabled:
            return True
        token = websocket.cookies.get(self.settings.auth_cookie_name, "")
        return hmac.compare_digest(token, self.token())

    def set_cookie(self, response: Response) -> None:
        response.set_cookie(
            self.settings.auth_cookie_name,
            self.token(),
            max_age=self.settings.auth_cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.settings.auth_cookie_name, path="/")
