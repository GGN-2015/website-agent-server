from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
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
_SCHEME_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_HTTPS_PROBE_TIMEOUT_SECONDS = 2.0


def _has_explicit_url_scheme(value: str) -> bool:
    return bool(_SCHEME_PREFIX_RE.match(value))


def _with_default_scheme(value: str, scheme: str) -> str:
    if value.startswith("//"):
        return f"{scheme}:{value}"
    return f"{scheme}://{value}"


def normalize_target_url(raw_url: str, default_scheme: str = "https") -> str:
    value = raw_url.strip()
    if not value:
        raise URLPolicyError("URL is required.")
    if default_scheme not in {"http", "https"}:
        raise URLPolicyError("Default URL scheme must be http or https.")

    leading_scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", value)
    if leading_scheme and leading_scheme.group(1).lower() in _BLOCKED_INPUT_SCHEMES:
        raise URLPolicyError(f"{leading_scheme.group(1)} URLs are not supported.")

    if not _has_explicit_url_scheme(value):
        value = _with_default_scheme(value, default_scheme)

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise URLPolicyError("Only http and https URLs are supported.")
    if not parts.hostname:
        raise URLPolicyError("URL must include a host name.")

    path = parts.path or "/"
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


async def normalize_target_url_with_probe(raw_url: str, verify_https: bool = True) -> str:
    value = raw_url.strip()
    if not value:
        raise URLPolicyError("URL is required.")
    if _has_explicit_url_scheme(value):
        return normalize_target_url(value)

    https_url = normalize_target_url(value, default_scheme="https")
    if await _https_service_available(https_url, verify_tls=verify_https):
        return https_url
    return normalize_target_url(value, default_scheme="http")


async def _https_service_available(url: str, verify_tls: bool = True) -> bool:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise URLPolicyError("URL must include a host name.")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise URLPolicyError(str(exc)) from exc

    ssl_context = ssl.create_default_context()
    if not verify_tls:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=ssl_context,
                server_hostname=host,
            ),
            timeout=_HTTPS_PROBE_TIMEOUT_SECONDS,
        )
        return True
    except (OSError, ssl.SSLError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


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

    async def ensure_navigation_url_allowed(
        self, raw_url: str, verify_https: bool = True
    ) -> str:
        policy_url = normalize_target_url(raw_url)
        await self.ensure_request_url_allowed(policy_url)
        url = await normalize_target_url_with_probe(raw_url, verify_https=verify_https)
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
