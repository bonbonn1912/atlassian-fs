"""HTTP transport with rate-limiting, retry with backoff, and stop-awareness.

Provides a safe HTTP client for Atlassian Cloud and Server/DC REST APIs
using only the Python standard library.
"""
from __future__ import annotations

import base64
import email.utils
import http.client
import json
import logging
import os
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .models import (
    AtlassianError,
    BudgetExceeded,
    CrawlStopped,
    ForbiddenError,
    HostNotAllowed,
    InvalidResponseError,
    NotFoundError,
)

logger = logging.getLogger(__name__)

Transport = Callable[[str, dict[str, str], float, ssl.SSLContext | None, int], "HttpResponse"]


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]  # lower-case keys
    body: bytes


def make_ssl_context(verify_ssl: bool, ca_bundle: str = "") -> ssl.SSLContext:
    """Create an SSL context.

    If verify_ssl is False, disables certificate and hostname verification for
    internal self-signed CAs.
    """
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        # python.org builds on macOS ship without a CA store ("Install
        # Certificates.command" not run): fall back to certifi or the system bundle.
        for cafile in _fallback_ca_files():
            try:
                ctx.load_verify_locations(cafile=cafile)
                break
            except (OSError, ssl.SSLError):
                continue
    return ctx


def _fallback_ca_files() -> list[str]:
    files = []
    try:
        import certifi  # optional dependency
        files.append(certifi.where())
    except ImportError:
        pass
    files += ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"]
    return [f for f in files if os.path.isfile(f)]


def build_auth_headers(auth: dict | None) -> dict[str, str]:
    """Generate HTTP Authorization headers according to auth config."""
    if not auth:
        return {}
    auth_type = auth.get("type", "none")
    token = auth.get("token", "")
    username = auth.get("username", "")
    if auth_type == "bearer" and token:
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "basic" and (username or token):
        raw = f"{username}:{token}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {}


def build_gitlab_headers(token: str | None) -> dict[str, str]:
    """GitLab personal access token header (recommended over Bearer by GitLab)."""
    token = (token or "").strip()
    return {"PRIVATE-TOKEN": token} if token else {}


# Query parameters that may carry credentials (GitLab accepts ?private_token=).
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:private_token|access_token|job_token|token|password)=)[^&#\s]*")
REDACTED = "***"


def redact_url(url: str) -> str:
    """Mask credential query parameters and userinfo in a URL for logs/errors."""
    if not url:
        return url
    url = _SECRET_QUERY_RE.sub(lambda m: m.group(1) + REDACTED, url)
    return re.sub(r"(?i)^(https?://)[^/@\s]+@", lambda m: m.group(1) + REDACTED + "@", url)


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    """Remove known secret values (and credential query params) from a message."""
    text = redact_url(str(text))
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def _header_secrets(headers: dict[str, str]) -> tuple[str, ...]:
    """Secret parts of auth headers, e.g. the token of 'Bearer <token>'."""
    out: list[str] = []
    for key, value in headers.items():
        if key.lower() in ("authorization", "private-token", "job-token") and value:
            out.append(value)
            parts = value.split(None, 1)
            if len(parts) == 2:
                out.append(parts[1])
    return tuple(out)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disallows cross-host redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        orig = urllib.parse.urlsplit(req.full_url)
        dest = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if orig.scheme == dest.scheme and orig.netloc.lower() == dest.netloc.lower():
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        return None


def default_transport(use_system_proxy: bool) -> Transport:
    """Create the default urllib-based GET transport."""

    def transport(
        url: str,
        headers: dict[str, str],
        timeout: float,
        ssl_context: ssl.SSLContext | None,
        max_bytes: int,
    ) -> HttpResponse:
        handlers: list[urllib.request.BaseHandler] = [SafeRedirectHandler()]
        if not use_system_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read(max_bytes + 1)
                return HttpResponse(status=status, headers=resp_headers, body=body)
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()}
            body = exc.read(max_bytes + 1)
            return HttpResponse(status=exc.code, headers=resp_headers, body=body)

    return transport


class RequestBudget:
    """Thread-safe request and time budget tracker."""

    def __init__(self, max_requests: int, max_duration_seconds: float, clock=time.monotonic):
        self._max_requests = max_requests
        self._max_duration_seconds = max_duration_seconds
        self._clock = clock
        self._start = clock()
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def remaining_seconds(self) -> float:
        elapsed = self._clock() - self._start
        return self._max_duration_seconds - elapsed

    def check(self) -> None:
        with self._lock:
            if self._used >= self._max_requests:
                raise BudgetExceeded(
                    f"Request-Budget erschöpft ({self._used}/{self._max_requests})"
                )
            if self.remaining_seconds() <= 0:
                raise BudgetExceeded(
                    f"Zeitbudget erschöpft ({self._max_duration_seconds}s)"
                )

    def consume(self) -> None:
        with self._lock:
            if self._used >= self._max_requests:
                raise BudgetExceeded(
                    f"Request-Budget erschöpft ({self._used}/{self._max_requests})"
                )
            if self.remaining_seconds() <= 0:
                raise BudgetExceeded(
                    f"Zeitbudget erschöpft ({self._max_duration_seconds}s)"
                )
            self._used += 1


def parse_retry_after(value: str, current_time_func=time.time) -> float:
    """Parse a Retry-After header value as seconds or HTTP date."""
    val = value.strip()
    try:
        sec = float(val)
        return max(0.0, sec)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        curr = current_time_func()
        return max(0.0, dt.timestamp() - curr)
    except Exception:
        return 0.0


class RateLimitedClient:
    """HTTP client with per-source rate limiting, retries, and stop detection."""

    def __init__(
        self,
        source: str,
        base_url: str,
        *,
        allowed_bases: Sequence[str],
        sleep_seconds: float,
        timeout: float,
        max_retries: int,
        stop_event: threading.Event,
        budget: RequestBudget,
        ssl_context: ssl.SSLContext | None,
        auth_headers: dict[str, str],
        user_agent: str = "local-knowledge/1.0",
        max_response_bytes: int = 20 * 2**20,
        transport: Transport | None = None,
        on_event: Callable[[dict], None] | None = None,
        clock=time.monotonic,
        rand=random.random,
    ):
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.allowed_bases = tuple(b.rstrip("/") for b in allowed_bases if b)
        self.sleep_seconds = float(sleep_seconds)
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.stop_event = stop_event
        self.budget = budget
        self.ssl_context = ssl_context
        self.auth_headers = dict(auth_headers)
        self._secrets = _header_secrets(self.auth_headers)
        self.user_agent = user_agent
        self.max_response_bytes = int(max_response_bytes)
        self.transport = transport or default_transport(use_system_proxy=True)
        self.on_event = on_event
        self._clock = clock
        self._rand = rand
        self._last_request_start: float | None = None

    def _build_url(self, path_or_url: str, params: dict | None = None) -> str:
        s = path_or_url.strip()
        if s.startswith("http://") or s.startswith("https://"):
            url = s
            all_allowed = (self.base_url,) + self.allowed_bases
            allowed = False
            for b in all_allowed:
                if url == b or url.startswith(b + "/") or url.startswith(b + "?"):
                    allowed = True
                    break
            if not allowed:
                raise HostNotAllowed(self.redact(f"Host nicht erlaubt für Request: {url}"))
        else:
            if not s.startswith("/"):
                s = "/" + s
            url = f"{self.base_url}{s}"

        if params:
            items = []
            for k, v in params.items():
                if v is not None:
                    if isinstance(v, bool):
                        items.append((k, "true" if v else "false"))
                    else:
                        items.append((k, str(v)))
            if items:
                qs = urllib.parse.urlencode(items)
                url = f"{url}{'&' if '?' in url else '?'}{qs}"
        return url

    def __repr__(self) -> str:   # never expose auth headers
        return f"RateLimitedClient(source={self.source!r}, base_url={self.base_url!r})"

    def redact(self, text: str) -> str:
        return redact_secrets(text, self._secrets)

    def get_json(self, path_or_url: str, params: dict | None = None) -> Any:
        data, _ = self.get_json_with_headers(path_or_url, params=params)
        return data

    def get_json_with_headers(
        self, path_or_url: str, params: dict | None = None
    ) -> tuple[Any, dict[str, str]]:
        url = self._build_url(path_or_url, params=params)
        safe_url = self.redact(url)

        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        headers.update(self.auth_headers)

        attempt = 0
        retry_wait = 0.0
        retry_reason: str | None = None

        while True:
            if self.stop_event.is_set():
                raise CrawlStopped()
            # Fail fast before waiting; the attempt itself is consumed after the wait
            # so that a time budget expiring during the wait is honoured.
            self.budget.check()

            wait_time = 0.0
            reason = "sleep"

            if retry_wait > 0.0:
                wait_time = retry_wait
                reason = retry_reason or "retry"
                retry_wait = 0.0
                retry_reason = None
            elif self._last_request_start is not None and self.sleep_seconds > 0:
                elapsed = self._clock() - self._last_request_start
                if elapsed < self.sleep_seconds:
                    wait_time = self.sleep_seconds - elapsed
                    reason = "sleep"

            if wait_time > 0:
                if self.on_event:
                    self.on_event({
                        "phase": "rate_limit_wait",
                        "source": self.source,
                        "seconds": wait_time,
                        "reason": reason,
                    })
                if self.stop_event.wait(wait_time) or self.stop_event.is_set():
                    raise CrawlStopped()

            self.budget.consume()
            self._last_request_start = self._clock()
            if self.on_event:
                self.on_event({
                    "phase": "request",
                    "source": self.source,
                    "url": safe_url,
                })

            try:
                resp = self.transport(
                    url, headers, self.timeout, self.ssl_context, self.max_response_bytes
                )
            except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError):
                    raise AtlassianError(
                        "TLS-Zertifikatsprüfung fehlgeschlagen – in den Einstellungen "
                        "'TLS-Zertifikate prüfen' deaktivieren oder ein CA-Bundle angeben",
                        status=None, url=safe_url) from exc
                if attempt < self.max_retries:
                    attempt += 1
                    backoff = min(300.0, 2.0 ** attempt)
                    jitter = backoff * 0.1 * self._rand()
                    retry_wait = backoff + jitter
                    retry_reason = "retry"
                    continue
                raise AtlassianError(
                    self.redact(f"Netzwerkfehler nach {attempt} Wiederholungen: {exc}"),
                    status=None,
                    url=safe_url,
                ) from exc

            status = resp.status

            if status == 429:
                if attempt < self.max_retries:
                    attempt += 1
                    ra_hdr = resp.headers.get("retry-after")
                    ra_sec = parse_retry_after(ra_hdr) if ra_hdr else 0.0
                    if ra_sec > 0:
                        retry_wait = ra_sec
                    else:
                        backoff = min(300.0, 2.0 ** attempt)
                        jitter = backoff * 0.1 * self._rand()
                        retry_wait = backoff + jitter
                    retry_reason = "429"
                    continue
                raise AtlassianError(
                    f"Rate Limit (HTTP 429) nach {attempt} Wiederholungen erschöpft",
                    status=429,
                    url=safe_url,
                )

            if status in (502, 503, 504):
                if attempt < self.max_retries:
                    attempt += 1
                    ra_hdr = resp.headers.get("retry-after")
                    ra_sec = parse_retry_after(ra_hdr) if ra_hdr else 0.0
                    if ra_sec > 0:
                        retry_wait = ra_sec
                    else:
                        backoff = min(300.0, 2.0 ** attempt)
                        jitter = backoff * 0.1 * self._rand()
                        retry_wait = backoff + jitter
                    retry_reason = "retry"
                    continue
                raise AtlassianError(
                    f"Server-Fehler (HTTP {status}) nach {attempt} Wiederholungen",
                    status=status,
                    url=safe_url,
                )

            if status == 404:
                raise NotFoundError(f"Nicht gefunden (HTTP 404): {safe_url}", status=404, url=safe_url)

            if status in (401, 403):
                raise ForbiddenError(
                    f"Zugriff verweigert (HTTP {status}) – Berechtigung/Token prüfen",
                    status=status,
                    url=safe_url,
                )

            if 400 <= status < 500:
                raise AtlassianError(f"Client-Fehler (HTTP {status})", status=status, url=safe_url)

            if status >= 500:
                raise AtlassianError(f"Server-Fehler (HTTP {status})", status=status, url=safe_url)

            if 300 <= status < 400:
                raise AtlassianError(f"Unerwarteter Redirect (HTTP {status})", status=status, url=safe_url)

            if len(resp.body) > self.max_response_bytes:
                raise InvalidResponseError(
                    f"Antwort überschreitet Maximalgröße von {self.max_response_bytes} Bytes",
                    status=status,
                    url=safe_url,
                )

            content_type = resp.headers.get("content-type", "").lower()
            if "text/html" in content_type or resp.body.lstrip().startswith(
                (b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML")
            ):
                raise ForbiddenError(
                    "Zugriff verweigert (HTML-Login-Redirect – nicht angemeldet)",
                    status=status,
                    url=safe_url,
                )

            try:
                data = json.loads(resp.body.decode("utf-8"))
            except Exception as exc:
                raise InvalidResponseError(
                    self.redact(f"Ungültiges JSON empfangen: {exc}"), status=status, url=safe_url
                ) from exc

            return data, resp.headers
