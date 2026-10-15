"""Budgeted, robots-aware HTTP with evidence-grade capture (hash, timestamps, redirects)."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from . import USER_AGENT
from .budget import Budget, BudgetExhausted

MAX_BODY_BYTES = 2_500_000


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    retrieved_at: str = ""
    elapsed_ms: int = 0
    redirects: list[str] = field(default_factory=list)
    error: str | None = None
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.body).hexdigest() if self.body else None

    def text(self) -> str:
        if not self.body:
            return ""
        enc = "utf-8"
        lowered = self.content_type.lower()
        if "charset=" in lowered:
            enc = lowered.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8"))


_host_safety: dict[str, bool] = {}
_host_lock = threading.Lock()


def is_safe_public_url(url: str) -> bool:
    """Outbound URL policy: http(s) only, no credentials, no private/loopback/link-local targets."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower().rstrip(".")
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        return False
    with _host_lock:
        if host in _host_safety:
            return _host_safety[host]
    safe = True
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                safe = False
                break
    except (socket.gaierror, UnicodeError, ValueError):
        safe = False
    with _host_lock:
        _host_safety[host] = safe
    return safe


class Http:
    def __init__(self, budget: Budget, workers: int = 16):
        self.budget = budget
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=workers * 2, pool_maxsize=workers * 2, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6"})
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._domain_locks: dict[str, threading.Lock] = {}
        self.dead_hosts: set[str] = set()
        self._domain_last: dict[str, float] = {}

    def _domain_lock(self, host: str) -> threading.Lock:
        with self._robots_lock:
            return self._domain_locks.setdefault(host, threading.Lock())

    def get(
        self,
        url: str,
        source_class: str,
        *,
        timeout: float = 12.0,
        reserved: bool = False,
        accept: str | None = None,
        polite_delay: float = 0.0,
        max_bytes: int = MAX_BODY_BYTES,
        extra_headers: dict | None = None,
    ) -> FetchResult:
        result = FetchResult(url=url, retrieved_at=utc_now())
        if not is_safe_public_url(url):
            result.error = "unsafe_or_unresolvable_url"
            return result
        try:
            self.budget.take(source_class, reserved=reserved)
        except BudgetExhausted as exc:
            result.error = "budget_exhausted"
            result.blocked = True
            return result
        host = urlsplit(url).hostname or ""
        if polite_delay:
            lock = self._domain_lock(host)
            with lock:
                wait = self._domain_last.get(host, 0) + polite_delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._domain_last[host] = time.monotonic()
        headers = {"Accept": accept} if accept else {}
        headers.update(extra_headers or {})
        started = time.monotonic()
        timeout = max(2.0, min(timeout, self.budget.time_left() - 5))
        try:
            with self.session.get(url, headers=headers, timeout=(min(6.0, timeout), timeout), stream=True, allow_redirects=True) as response:
                result.status = response.status_code
                result.final_url = response.url
                result.redirects = [r.url for r in response.history]
                result.content_type = response.headers.get("Content-Type", "")
                chunks, total = [], 0
                for chunk in response.iter_content(65536):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes or time.monotonic() - started > timeout + 5:
                        break
                result.body = b"".join(chunks)
                self.budget.add_bytes(total)
                if result.final_url and not is_safe_public_url(result.final_url):
                    result.error = "redirected_to_unsafe_url"
                    result.body = b""
        except requests.RequestException as exc:
            result.error = f"{type(exc).__name__}"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.retrieved_at = utc_now()
        return result

    def get_with_headers(self, url: str, source_class: str, headers: dict, timeout: float = 30.0) -> FetchResult:
        return self.get(url, source_class, timeout=timeout, extra_headers=headers, max_bytes=8_000_000)

    def download(self, url: str, path, source_class: str, timeout: float = 600.0) -> FetchResult:
        """Stream a large official bulk file to disk (one budgeted request)."""
        from pathlib import Path

        result = FetchResult(url=url, retrieved_at=utc_now())
        try:
            self.budget.take(source_class, reserved=True)
        except BudgetExhausted:
            result.error = "budget_exhausted"
            return result
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        started = time.monotonic()
        try:
            with self.session.get(url, timeout=(20, 120), stream=True) as response:
                result.status = response.status_code
                result.final_url = response.url
                result.content_type = response.headers.get("Content-Type", "")
                if response.status_code != 200:
                    result.error = f"http_{response.status_code}"
                    return result
                total = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(1 << 20):
                        handle.write(chunk)
                        total += len(chunk)
                        if time.monotonic() - started > timeout:
                            raise requests.Timeout("bulk download exceeded time limit")
                self.budget.add_bytes(total)
            temporary.replace(target)
        except (requests.RequestException, OSError) as exc:
            result.error = type(exc).__name__
            temporary.unlink(missing_ok=True)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.retrieved_at = utc_now()
        return result

    # robots.txt ----------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        # One robots.txt per site: www./bare host and http/https share the same rules in practice.
        site_key = (parts.hostname or "").lower().removeprefix("www.")
        with self._robots_lock:
            known = site_key in self._robots
            parser = self._robots.get(site_key)
        if not known:
            parser = None
            res = self.get(origin + "/robots.txt", "company_website_robots", timeout=8)
            if res.status == 0 and res.error and res.error not in ("budget_exhausted", "unsafe_or_unresolvable_url"):
                self.dead_hosts.add(site_key)  # connection failed: the site itself is unreachable
            if res.status in (401, 403):
                parser = RobotFileParser()
                parser.disallow_all = True
            elif res.ok and res.body:
                parser = RobotFileParser()
                parser.parse(res.text().splitlines())
            with self._robots_lock:
                self._robots[site_key] = parser
        if parser is None:
            return True
        if parser.disallow_all:
            return False
        return parser.can_fetch(USER_AGENT, url)
