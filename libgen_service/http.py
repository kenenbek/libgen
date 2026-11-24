"""HTTP transport with mirror failover, throttling and health tracking.

All remote requests go through this class.  It never follows links to ad
networks: callers ask for specific Libgen paths and the client only talks to
the configured mirror hosts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

from .config import Settings
from .errors import FileUnavailable, LibgenUnavailable


@dataclass
class MirrorState:
    base: str
    failures: int = 0
    blocked_until: float = 0.0
    last_request: float = 0.0
    last_ok: float | None = None
    last_error: str | None = None
    checks: int = 0
    last_checked: float | None = None

    def as_dict(self) -> dict:
        now = time.monotonic()
        return {
            "base": self.base,
            "healthy": self.blocked_until <= now,
            "failures": self.failures,
            "blocked_for": round(max(0.0, self.blocked_until - now), 1),
            "last_error": self.last_error,
            "last_ok": self.last_ok,
        }


@dataclass
class ProbeResult:
    base: str
    ok: bool
    latency_ms: int
    status: int | None = None
    error: str | None = None


def _mentions_missing_file(resp: requests.Response) -> bool:
    """Libgen uses HTTP 500 + "File not found in DB" for dead catalogue rows."""
    try:
        body = resp.text
    except Exception:  # noqa: BLE001 - a broken body is just "not this case"
        return False
    return "file not found in db" in body.lower()


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._states: list[MirrorState] = [
            MirrorState(base=b) for b in settings.mirrors
        ]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/json;q=0.9,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.8,ru;q=0.6",
            }
        )
        if settings.proxy:
            self.session.proxies.update(
                {"http": settings.proxy, "https": settings.proxy}
            )

    # ------------------------------------------------------------------ state
    def mirrors(self) -> list[dict]:
        with self._lock:
            return [s.as_dict() for s in self._states]

    def _candidates(self) -> list[MirrorState]:
        now = time.monotonic()
        with self._lock:
            states = list(self._states)
        healthy = [s for s in states if s.blocked_until <= now]
        blocked = sorted(
            (s for s in states if s.blocked_until > now),
            key=lambda s: s.blocked_until,
        )
        return healthy + blocked

    def _throttle(self, state: MirrorState) -> None:
        interval = self.settings.min_interval
        with self._lock:
            now = time.monotonic()
            wait = state.last_request + interval - now
            state.last_request = max(now, state.last_request + interval)
        if wait > 0:
            time.sleep(wait)

    def _mark_failure(self, state: MirrorState, error: str) -> None:
        with self._lock:
            state.failures += 1
            state.last_error = error
            # 45s, 90s, 180s ... capped at 10 minutes
            backoff = min(600.0, 45.0 * (2 ** (state.failures - 1)))
            state.blocked_until = time.monotonic() + backoff

    def _mark_success(self, state: MirrorState) -> None:
        with self._lock:
            state.failures = 0
            state.blocked_until = 0.0
            state.last_ok = time.time()
            state.last_error = None

    # ---------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> requests.Response:
        """Perform a request, failing over across mirrors.

        ``path`` is normally relative ("/index.php?...").  Absolute URLs are
        also accepted (used when a mirror hands us its own CDN link).
        """
        errors: list[str] = []
        missing_file = False
        max_attempts = attempts or max(1, self.settings.retries)
        candidates = self._candidates()
        tried = 0
        base_headers = dict(headers or {})
        for state in candidates:
            if tried >= max_attempts:
                break
            tried += 1
            self._throttle(state)
            url = path if path.startswith("http") else state.base + path
            # "__BASE__" lets callers build a Referer for whichever mirror is
            # actually used (Libgen's cover/CDN endpoints check it).
            request_headers = {
                key: (
                    value.replace("__BASE__", state.base)
                    if isinstance(value, str)
                    else value
                )
                for key, value in base_headers.items()
            }
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                    timeout=timeout or self.settings.timeout,
                    stream=stream,
                    allow_redirects=allow_redirects,
                )
            except requests.RequestException as exc:
                self._mark_failure(state, str(exc))
                errors.append(f"{state.base}: {exc}")
                continue

            if resp.status_code >= 400:
                if _mentions_missing_file(resp):
                    # The metadata row exists but the file itself is gone.
                    # That is a property of the file, not of the mirror, so
                    # do not put the mirror into backoff.
                    resp.close()
                    missing_file = True
                    errors.append(f"{state.base}: file missing")
                    continue
                # A 4xx may be mirror-specific; try the next mirror but do not
                # punish this one with a long backoff.
                errors.append(f"{state.base}: HTTP {resp.status_code}")
                resp.close()
                if resp.status_code == 429 or resp.status_code >= 500:
                    self._mark_failure(state, f"HTTP {resp.status_code}")
                continue

            self._mark_success(state)
            try:
                resp.mirror_base = state.base  # type: ignore[attr-defined]
            except Exception:
                pass
            return resp

        if missing_file:
            raise FileUnavailable(
                "The file is missing from Libgen storage (File not found in DB)"
            )
        raise LibgenUnavailable(
            "All Libgen mirrors failed. " + ("; ".join(errors[-4:]) or "no mirrors configured"),
            errors[-4:],
        )

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, data: dict, **kwargs) -> requests.Response:
        return self.request("POST", path, data=data, **kwargs)

    # ------------------------------------------------------------ health check
    def probe(self, base: str, timeout: float = 8.0) -> ProbeResult:
        url = base.rstrip("/") + "/index.php?req=test&res=1"
        started = time.monotonic()
        try:
            resp = self.session.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
            )
            elapsed = int((time.monotonic() - started) * 1000)
            ok = resp.status_code == 200 and "tablelibgen" in resp.text
            resp.close()
            result = ProbeResult(base=base, ok=ok, latency_ms=elapsed, status=resp.status_code)
        except requests.RequestException as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            result = ProbeResult(base=base, ok=False, latency_ms=elapsed, error=str(exc))
        with self._lock:
            for state in self._states:
                if state.base == base:
                    state.checks += 1
                    state.last_checked = time.time()
                    if result.ok:
                        state.failures = 0
                        state.blocked_until = 0.0
                        state.last_ok = time.time()
                        state.last_error = None
                    else:
                        state.last_error = result.error or f"HTTP {result.status}"
        return result

    def probe_all(self, timeout: float = 8.0) -> list[ProbeResult]:
        return [self.probe(s.base, timeout=timeout) for s in list(self._states)]

    def close(self) -> None:
        self.session.close()


def strip_bom(text: str) -> str:
    return text.lstrip("\ufeff")
