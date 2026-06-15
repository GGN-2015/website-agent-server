from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class URLPolicyError(ValueError):
    """Raised when a requested URL is not allowed by the server policy."""


_BLOCKED_INPUT_SCHEMES = {
    "about",
    "blob",
    "chrome",
    "data",
    "file",
    "ftp",
    "javascript",
    "mailto",
    "view-source",
}
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


def normalize_target_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise URLPolicyError("URL is required.")

    leading_scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", value)
    if leading_scheme and leading_scheme.group(1).lower() in _BLOCKED_INPUT_SCHEMES:
        raise URLPolicyError(f"{leading_scheme.group(1)} URLs are not supported.")

    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise URLPolicyError("Only http and https URLs are supported.")
    if not parts.hostname:
        raise URLPolicyError("URL must include a host name.")

    path = parts.path or "/"
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global


async def _resolve_host(hostname: str, port: int | None) -> set[str]:
    def resolve() -> set[str]:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return {info[4][0] for info in infos}

    return await asyncio.to_thread(resolve)


@dataclass
class HostAccessPolicy:
    allow_private_hosts: bool = False
    _host_cache: dict[str, bool] = field(default_factory=dict)

    async def ensure_navigation_url_allowed(self, raw_url: str) -> str:
        url = normalize_target_url(raw_url)
        await self.ensure_request_url_allowed(url)
        return url

    async def ensure_request_url_allowed(self, url: str) -> None:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme in {"about", "blob", "data"}:
            return
        if scheme not in {"http", "https", "ws", "wss"}:
            raise URLPolicyError(f"Blocked unsupported request scheme: {scheme or 'unknown'}.")
        if not parts.hostname:
            raise URLPolicyError("Blocked request without a host name.")
        if self.allow_private_hosts:
            return

        host = parts.hostname.rstrip(".").lower()
        cache_key = f"{host}:{parts.port or ''}"
        cached = self._host_cache.get(cache_key)
        if cached is None:
            cached = await self._is_public_host(host, parts.port)
            self._host_cache[cache_key] = cached
        if not cached:
            raise URLPolicyError("Blocked private, local, or reserved network target.")

    async def _is_public_host(self, hostname: str, port: int | None) -> bool:
        if hostname in _LOCAL_HOSTNAMES or hostname.endswith(".localhost"):
            return False

        try:
            return _is_public_ip(hostname)
        except ValueError:
            pass

        try:
            addresses = await _resolve_host(hostname, port)
        except socket.gaierror as exc:
            raise URLPolicyError(f"Could not resolve host: {hostname}.") from exc

        if not addresses:
            return False
        return all(_is_public_ip(address) for address in addresses)
