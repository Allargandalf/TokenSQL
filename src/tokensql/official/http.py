"""Hardened GET-only transport for official pricing evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .base import FetchResult


Resolver = Callable[[str, int], Iterable[str]]
Clock = Callable[[], datetime]

_DEFAULT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "text/html",
        "text/plain",
    }
)
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class OfficialHttpClient:
    """Fetch official evidence while enforcing provenance and SSRF boundaries."""

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str],
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
        timeout: float = 10.0,
        max_bytes: int = 2_000_000,
        retries: int = 2,
        max_redirects: int = 3,
        allowed_content_types: Iterable[str] = _DEFAULT_CONTENT_TYPES,
        clock: Clock | None = None,
    ) -> None:
        domains = frozenset(_normalize_domain(item) for item in allowed_domains)
        if not domains:
            raise ValueError("at least one official domain must be allowlisted")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if retries < 0 or max_redirects < 0:
            raise ValueError("retry and redirect limits must be non-negative")

        self._allowed_domains = domains
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._resolver = resolver or _resolve_addresses
        self._timeout = httpx.Timeout(timeout)
        self._max_bytes = max_bytes
        self._retries = retries
        self._max_redirects = max_redirects
        self._content_types = frozenset(
            item.strip().lower() for item in allowed_content_types
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OfficialHttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def fetch(self, url: str) -> FetchResult:
        fetched_at = self._clock()
        requested_url = url
        current_url = url
        attempts = 0
        redirects = 0
        origin_host: str | None = None

        try:
            origin_host = self._validate_url(current_url)
        except Exception as exc:
            return _pending(
                requested_url,
                current_url,
                fetched_at,
                attempts,
                f"unsafe source URL: {exc}",
            )

        while True:
            response: httpx.Response | None = None
            failure: Exception | None = None
            for attempt in range(self._retries + 1):
                attempts += 1
                try:
                    request = self._client.build_request(
                        "GET",
                        current_url,
                        headers={
                            "Accept": "application/json,text/html,text/plain",
                            "User-Agent": "TokenSQL-official-price/1",
                        },
                        timeout=self._timeout,
                    )
                    response = self._client.send(
                        request,
                        stream=True,
                        follow_redirects=False,
                    )
                    if response.status_code not in _RETRY_STATUS:
                        failure = None
                        break
                    response.close()
                    response = None
                    failure = RuntimeError(f"retryable HTTP status {request.url}")
                except httpx.RequestError as exc:
                    failure = exc
                if attempt == self._retries:
                    break

            if response is None:
                detail = (
                    f"{type(failure).__name__}: {failure}"
                    if failure is not None
                    else "request failed"
                )
                return _pending(
                    requested_url,
                    current_url,
                    fetched_at,
                    attempts,
                    detail,
                )

            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return _pending(
                            requested_url,
                            current_url,
                            fetched_at,
                            attempts,
                            "redirect has no Location header",
                        )
                    if redirects >= self._max_redirects:
                        return _pending(
                            requested_url,
                            current_url,
                            fetched_at,
                            attempts,
                            "redirect limit exceeded",
                        )
                    next_url = urljoin(current_url, location)
                    next_host = self._validate_url(next_url)
                    if next_host != origin_host:
                        return _pending(
                            requested_url,
                            next_url,
                            fetched_at,
                            attempts,
                            "redirect to a different host is forbidden",
                        )
                    current_url = next_url
                    redirects += 1
                    continue

                if response.status_code != 200:
                    return _pending(
                        requested_url,
                        current_url,
                        fetched_at,
                        attempts,
                        f"unexpected HTTP status {response.status_code}",
                    )

                media_type = _media_type(response.headers.get("content-type"))
                if media_type not in self._content_types:
                    return _pending(
                        requested_url,
                        current_url,
                        fetched_at,
                        attempts,
                        f"unsupported Content-Type {media_type or '<missing>'}",
                    )

                length_error = _content_length_error(
                    response.headers.get("content-length"),
                    self._max_bytes,
                )
                if length_error:
                    return _pending(
                        requested_url,
                        current_url,
                        fetched_at,
                        attempts,
                        length_error,
                    )

                body = _read_limited(response, self._max_bytes)
                digest = hashlib.sha256(body).hexdigest()
                return FetchResult(
                    requested_url=requested_url,
                    source_url=current_url,
                    fetched_at=fetched_at,
                    status="fetched",
                    raw_sha256=digest,
                    raw_body=body,
                    content_type=media_type,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    attempts=attempts,
                )
            except Exception as exc:
                return _pending(
                    requested_url,
                    current_url,
                    fetched_at,
                    attempts,
                    f"response validation failed: {type(exc).__name__}: {exc}",
                )
            finally:
                response.close()

    def _validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https":
            raise ValueError("only HTTPS is allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are forbidden")
        if not parsed.hostname:
            raise ValueError("hostname is required")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid port") from exc
        if port not in (None, 443):
            raise ValueError("only the default HTTPS port is allowed")

        host = _normalize_domain(parsed.hostname)
        if host not in self._allowed_domains:
            raise ValueError(f"domain {host!r} is not allowlisted")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            addresses = tuple(self._resolver(host, 443))
            if not addresses:
                raise ValueError("hostname resolved to no addresses")
        else:
            addresses = (str(literal),)

        for address in addresses:
            _reject_non_public_address(address)
        return host


def _pending(
    requested_url: str,
    source_url: str,
    fetched_at: datetime,
    attempts: int,
    reason: str,
) -> FetchResult:
    return FetchResult(
        requested_url=requested_url,
        source_url=source_url,
        fetched_at=fetched_at,
        status="pending_review",
        review_reasons=(reason,),
        attempts=attempts,
    )


def _normalize_domain(value: str) -> str:
    text = str(value).strip().rstrip(".").casefold()
    if not text:
        raise ValueError("empty domain")
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid domain") from exc


def _resolve_addresses(host: str, port: int) -> Iterable[str]:
    return {
        item[4][0]
        for item in socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    }


def _reject_non_public_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"resolver returned invalid address {value!r}") from exc
    if not address.is_global:
        raise ValueError(f"non-public destination {address} is forbidden")


def _media_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _content_length_error(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return "invalid Content-Length"
    if length < 0:
        return "invalid Content-Length"
    if length > limit:
        return f"response exceeds {limit} bytes"
    return None


def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError(f"response exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
