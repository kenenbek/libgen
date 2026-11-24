"""Infrastructure telemetry for Library Genesis.

Libgen has no status page or public API for its own infrastructure, so this
module builds a best-effort picture from the outside: DNS, ASN, TLS
certificates, edge/CDN headers and the public database dumps.  Everything is
cached and every network call is optional: a failed lookup degrades to an
empty field instead of breaking the panel.
"""

from __future__ import annotations

import copy
import hashlib
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import human_size

DUMP_BASE = "https://libgen.li/dbdumps/"
DUMP_TTL = 6 * 3600
SNAPSHOT_TTL = 300
PREVIOUS_TTL = 30 * 24 * 3600
ASN_TTL = 7 * 24 * 3600

DUMP_FILE_RE = re.compile(
    r'<a[^>]*>([^<]+\.(?:rar|sql|gz|zip|7z|torrent))</a>\s+\S+\s+\S+\s+(\d+)'
)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
ASN_RE = re.compile(r"^(AS\d+)\s+(.*)$", re.IGNORECASE)


def hostname_of(base: str) -> str:
    parsed = urlparse(base if "://" in base else "https://" + base)
    return (parsed.hostname or "").lower()


def parse_dump_root(html: str) -> list[str]:
    """Return collection directory names from /dbdumps/."""
    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href.endswith("/") or href.startswith("..") or "://" in href:
            continue
        name = href.rstrip("/")
        if name and name not in names:
            names.append(name)
    return names


def parse_transports(html: str) -> dict[str, Any]:
    """Parse /mirrors.php: distribution transports per repository section.

    The table lists every way Libgen hands out files: HTTP mirrors, torrents
    per 1000 files, IPFS gateways, DC++, eD2k, magnet and the Tor onion.
    """
    soup = BeautifulSoup(html, "html.parser")
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) == 1 and cells[0].get("colspan"):
            name = cells[0].get_text(" ", strip=True) or "other"
            current = {"name": name, "rows": []}
            sections.append(current)
            continue
        vals = [c.get_text(" ", strip=True) for c in cells]
        if len(vals) < 9 or vals[0].lower() == "type":
            continue
        if current is None:
            current = {"name": "other", "rows": []}
            sections.append(current)
        current["rows"].append(
            {
                "type": vals[0].lower(),
                "mirror": vals[1],
                "default": vals[2] == "1",
                "order": vals[3],
                "tooltip": vals[4],
                "name": vals[6] or vals[5],
                "url": vals[7],
                "active": vals[8] == "1",
            }
        )

    totals: dict[str, int] = {}
    active: dict[str, int] = {}
    gateways: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        counts: dict[str, int] = {}
        for row in section["rows"]:
            kind = row["type"]
            counts[kind] = counts.get(kind, 0) + 1
            totals[kind] = totals.get(kind, 0) + 1
            if row["active"]:
                active[kind] = active.get(kind, 0) + 1
            if kind in {"ipfs", "tor", "dc", "ed2k", "magnet"}:
                entries = gateways.setdefault(kind, [])
                known = next(
                    (e for e in entries if e["url"] == row["url"]), None
                )
                if known:
                    if section["name"] not in known["sections"]:
                        known["sections"].append(section["name"])
                else:
                    entries.append(
                        {
                            "name": row["name"] or row["tooltip"] or row["mirror"],
                            "url": row["url"],
                            "active": row["active"],
                            "sections": [section["name"]],
                        }
                    )
        section["counts"] = counts
        del section["rows"]
    return {
        "sections": sections,
        "totals": totals,
        "active": active,
        "gateways": gateways,
    }


def parse_dump_index(html: str) -> dict[str, Any]:
    """Parse one collection directory page into file count and size."""
    files: list[dict[str, Any]] = []
    for match in DUMP_FILE_RE.finditer(html):
        filename, size = match.group(1), int(match.group(2))
        date_match = DATE_RE.search(filename)
        files.append(
            {
                "name": filename,
                "bytes": size,
                "date": date_match.group(1) if date_match else None,
            }
        )
    total = sum(item["bytes"] for item in files)
    latest = max(
        (item for item in files if item.get("date")),
        key=lambda item: (item["date"], item["name"]),
        default=None,
    )
    return {
        "files": len(files),
        "bytes": total,
        "human": human_size(total),
        "latest_file": latest["name"] if latest else None,
        "latest_date": latest["date"] if latest else None,
        "items": files,
    }


def compare_snapshots(
    previous: dict[str, Any] | None, current: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Annotate mirrors with changes since the previous snapshot."""
    old_by_base = {
        item.get("base"): item
        for item in ((previous or {}).get("mirrors") or [])
        if item.get("base")
    }
    changes: list[dict[str, str]] = []
    for mirror in current:
        items: list[str] = []
        old = old_by_base.get(mirror.get("base"))
        if old:
            old_ip, new_ip = old.get("ip"), mirror.get("ip")
            # Cloudflare rotates edge IPs constantly; only flag moves on
            # origin-hosted mirrors where an IP change means a real migration.
            rotating = bool(old.get("cloudflare")) or bool(mirror.get("cloudflare"))
            if old_ip and new_ip and old_ip != new_ip and not rotating:
                items.append(f"IP {old_ip} → {new_ip}")
            old_tls = old.get("tls") or {}
            new_tls = mirror.get("tls") or {}
            if (
                old_tls.get("fingerprint")
                and new_tls.get("fingerprint")
                and old_tls["fingerprint"] != new_tls["fingerprint"]
            ):
                items.append("TLS certificate replaced")
            if bool(old.get("cloudflare")) != bool(mirror.get("cloudflare")):
                items.append("edge changed")
        else:
            items.append("new mirror")
        mirror["changes"] = items
        for message in items:
            changes.append({"base": mirror.get("base") or "", "message": message})
    return changes


def build_warnings(mirrors: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    for mirror in mirrors:
        host = mirror.get("host") or mirror.get("base") or "?"
        tls = mirror.get("tls") or {}
        if tls.get("error"):
            warnings.append(
                {
                    "level": "error",
                    "host": host,
                    "message": f"TLS check failed: {tls['error']}",
                }
            )
        else:
            days = tls.get("days_left")
            if isinstance(days, (int, float)):
                if days <= 7:
                    level = "critical"
                elif days <= 30:
                    level = "warning"
                else:
                    level = ""
                if level:
                    warnings.append(
                        {
                            "level": level,
                            "host": host,
                            "message": f"TLS certificate expires in {days:.0f} days",
                        }
                    )
        if not mirror.get("ip"):
            warnings.append(
                {"level": "error", "host": host, "message": "DNS resolution failed"}
            )
        if mirror.get("http_status") not in (200, None) or mirror.get("healthy") is False:
            detail = mirror.get("http_error") or f"HTTP {mirror.get('http_status')}"
            warnings.append(
                {"level": "warning", "host": host, "message": f"HTTP probe: {detail}"}
            )
        for change in mirror.get("changes") or []:
            warnings.append({"level": "info", "host": host, "message": change})
    return warnings


def build_download_chain(
    settings: Settings, stats: dict[str, Any] | None = None
) -> dict[str, Any]:
    stats = stats or {}
    mirrors = list(settings.mirrors)
    host = hostname_of(mirrors[0]) if mirrors else "libgen.li"
    observed = sorted((stats.get("cdn_hosts") or {}).keys())
    cdn = observed[0] if observed else "cdn5.booksdl.lc"
    return {
        "steps": [
            {
                "step": "search",
                "host": host,
                "detail": "POST /batchsearchindex.php → /json.php",
            },
            {
                "step": "metadata",
                "host": host,
                "detail": "GET /json.php?object=e|f",
            },
            {
                "step": "ad page",
                "host": host,
                "detail": "GET /ads.php?md5=… (parsed locally)",
            },
            {
                "step": "key",
                "host": host,
                "detail": "GET /get.php?md5=…&key=… → 307",
            },
            {
                "step": "cdn",
                "host": cdn,
                "detail": "file bytes, streamed through this service",
            },
        ],
        "observed_cdn": observed,
        "mirrors": [hostname_of(m) for m in mirrors],
    }


def _flatten_name(name: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for group in name or ():
        for key, value in group:
            result[key] = value
    return result


def _iso_from_cert_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        seconds = ssl.cert_time_to_seconds(value)
    except (ValueError, OverflowError):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _asn_from_org(org: str | None) -> tuple[str | None, str | None]:
    if not org:
        return None, None
    match = ASN_RE.match(org.strip())
    if match:
        return match.group(1).upper(), match.group(2).strip()
    return None, org.strip()


class InfraMonitor:
    """Collects and caches infrastructure snapshots."""

    def __init__(
        self,
        settings: Settings,
        cache: Any = None,
        *,
        client: Any = None,
        session: requests.Session | None = None,
        interval: float | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.client = client
        self.interval = (
            settings.monitor_interval if interval is None else max(0.0, interval)
        )
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: float | None = None
        self.next_run: float | None = None
        self.last_warnings: list[dict[str, str]] = []

    # ---------------------------------------------------------------- network
    def _get(self, url: str, timeout: float = 20.0) -> requests.Response:
        return self._session.get(url, timeout=timeout, allow_redirects=True)

    def resolve(self, host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(
                host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        except OSError:
            return []
        seen: list[str] = []
        for info in infos:
            address = info[4][0]
            if address not in seen:
                seen.append(address)
        # Prefer IPv4: it is easier to correlate with public ASN data.
        seen.sort(key=lambda value: ":" in value)
        return seen

    def tls_info(self, host: str, timeout: float = 8.0) -> dict[str, Any]:
        context = ssl.create_default_context()
        try:
            with socket.create_connection((host, 443), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert()
                    der = tls.getpeercert(binary_form=True) or b""
        except (OSError, ssl.SSLError) as exc:
            return {"error": str(exc)}
        issuer = _flatten_name(cert.get("issuer"))
        subject = _flatten_name(cert.get("subject"))
        not_after = _iso_from_cert_time(cert.get("notAfter"))
        days_left: float | None = None
        if not_after:
            try:
                days_left = round(
                    (
                        datetime.fromisoformat(not_after).timestamp()
                        - time.time()
                    )
                    / 86400,
                    1,
                )
            except ValueError:
                days_left = None
        return {
            "issuer": issuer.get("organizationName")
            or issuer.get("commonName")
            or "",
            "issuer_cn": issuer.get("commonName", ""),
            "subject": subject.get("commonName", ""),
            "not_after": not_after,
            "days_left": days_left,
            "fingerprint": hashlib.sha256(der).hexdigest() if der else "",
            "error": None,
        }

    def http_info(self, base: str, timeout: float = 8.0) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = self._get(base.rstrip("/") + "/", timeout=timeout)
            elapsed = int((time.monotonic() - started) * 1000)
            body = ""
            try:
                body = response.text
            except Exception:  # noqa: BLE001
                body = ""
            result = {
                "http_status": response.status_code,
                "server": (response.headers.get("Server") or "").strip(),
                "cf_ray": bool(response.headers.get("CF-Ray")),
                "latency_ms": elapsed,
                "final_url": response.url,
                "healthy": response.status_code == 200
                and "library genesis" in body.lower(),
                "http_error": None,
            }
            response.close()
            return result
        except requests.RequestException as exc:
            return {
                "http_status": None,
                "server": "",
                "cf_ray": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "final_url": None,
                "healthy": False,
                "http_error": str(exc),
            }

    def asn_info(self, ip: str, timeout: float = 5.0) -> dict[str, Any]:
        if not ip:
            return {}
        key = f"infra:asn:{ip}"
        cached = self.cache.get(key) if self.cache else None
        if isinstance(cached, dict):
            return cached
        try:
            response = self._get(f"https://ipinfo.io/{ip}/json", timeout=timeout)
            data = response.json()
            response.close()
        except (requests.RequestException, ValueError):
            return {}
        asn, org = _asn_from_org(data.get("org"))
        info = {
            "asn": asn,
            "org": org,
            "country": data.get("country"),
            "city": data.get("city"),
            "hostname": data.get("hostname"),
        }
        if self.cache:
            self.cache.set(key, info, ttl=ASN_TTL)
        return info

    def _collect_mirror(self, base: str) -> dict[str, Any]:
        host = hostname_of(base)
        ips = self.resolve(host)
        ip = ips[0] if ips else None
        tls = self.tls_info(host)
        http = self.http_info(base)
        asn = self.asn_info(ip or "")
        cloudflare = (
            "cloudflare" in (http.get("server") or "").lower()
            or bool(http.get("cf_ray"))
            or asn.get("asn") == "AS13335"
        )
        return {
            "base": base,
            "host": host,
            "ip": ip,
            "ips": ips,
            "asn": asn.get("asn"),
            "org": asn.get("org"),
            "country": asn.get("country"),
            "city": asn.get("city"),
            "hostname": asn.get("hostname"),
            "cloudflare": cloudflare,
            "tls": tls,
            "http_status": http.get("http_status"),
            "server": http.get("server") or "",
            "latency_ms": http.get("latency_ms"),
            "healthy": bool(http.get("healthy")),
            "http_error": http.get("http_error"),
            "changes": [],
        }

    def collect_mirrors(self) -> list[dict[str, Any]]:
        bases = list(self.settings.mirrors)
        if not bases:
            return []
        workers = min(8, len(bases))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            mirrors = list(pool.map(self._collect_mirror, bases))
        if self.client is not None:
            health = {
                item.get("base"): item
                for item in (self.client.mirrors() or [])
                if item.get("base")
            }
            for mirror in mirrors:
                state = health.get(mirror["base"]) or {}
                mirror["client_healthy"] = state.get("healthy")
                mirror["failures"] = state.get("failures", 0)
                mirror["blocked_for"] = state.get("blocked_for", 0)
        return mirrors

    # ------------------------------------------------------------------ dumps
    def dumps(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and self.cache:
            cached = self.cache.get("infra:dumps")
            if isinstance(cached, dict):
                return cached
        result = self._collect_dumps()
        if self.cache and not result.get("error"):
            self.cache.set("infra:dumps", result, ttl=DUMP_TTL)
        return result

    def _collect_dumps(self) -> dict[str, Any]:
        base = next(
            (m for m in self.settings.mirrors if "libgen.li" in m),
            self.settings.mirrors[0] if self.settings.mirrors else DUMP_BASE,
        )
        root = base.rstrip("/") + "/dbdumps/"
        try:
            response = self._get(root, timeout=25)
            names = parse_dump_root(response.text)
            response.close()
        except requests.RequestException as exc:
            return {
                "base": root,
                "fetched_at": _now_iso(),
                "collections": [],
                "total_files": 0,
                "total_bytes": 0,
                "error": str(exc),
            }
        collections: list[dict[str, Any]] = []

        def fetch(name: str) -> dict[str, Any]:
            # libgen.li rate-limits the dump listing, so fetch sequentially
            # and retry 503s once instead of hammering it in parallel.
            for attempt in range(2):
                try:
                    page = self._get(f"{root}{name}/", timeout=25)
                    status = page.status_code
                    if status == 200:
                        parsed = parse_dump_index(page.text)
                        page.close()
                        return {"name": name, **parsed}
                    page.close()
                    if status != 503 or attempt:
                        return {
                            "name": name,
                            "error": f"HTTP {status}",
                            "files": 0,
                            "bytes": 0,
                        }
                    time.sleep(3.0)
                except requests.RequestException as exc:
                    if attempt:
                        return {
                            "name": name,
                            "error": str(exc),
                            "files": 0,
                            "bytes": 0,
                        }
                    time.sleep(3.0)
            return {"name": name, "error": "unreachable", "files": 0, "bytes": 0}

        collections = [fetch(name) for name in names]
        total_files = sum(int(item.get("files") or 0) for item in collections)
        total_bytes = sum(int(item.get("bytes") or 0) for item in collections)
        return {
            "base": root,
            "fetched_at": _now_iso(),
            "collections": collections,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_human": human_size(total_bytes),
            "error": None,
        }

    # --------------------------------------------------------------- snapshot
    def transports(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and self.cache:
            cached = self.cache.get("infra:transports")
            if isinstance(cached, dict):
                return cached
        base = next(
            (m for m in self.settings.mirrors if "libgen.li" in m),
            self.settings.mirrors[0] if self.settings.mirrors else "",
        )
        url = base.rstrip("/") + "/mirrors.php"
        try:
            response = self._get(url, timeout=30)
            if response.status_code != 200:
                response.close()
                return {"source": url, "error": f"HTTP {response.status_code}"}
            parsed = parse_transports(response.text)
            response.close()
        except requests.RequestException as exc:
            return {"source": url, "error": str(exc)}
        result = {"source": url, "fetched_at": _now_iso(), "error": None, **parsed}
        if self.cache:
            self.cache.set("infra:transports", result, ttl=DUMP_TTL)
        return result

    def report(self, *, refresh: bool = False, dumps: bool = False) -> dict[str, Any]:
        snapshot = None
        if not refresh and self.cache:
            cached = self.cache.get("infra:snapshot")
            if isinstance(cached, dict) and (
                time.time() - float(cached.get("generated_ts") or 0) < SNAPSHOT_TTL
            ):
                snapshot = copy.deepcopy(cached)
        if snapshot is None:
            previous = self.cache.get("infra:previous") if self.cache else None
            mirrors = self.collect_mirrors()
            compare_snapshots(previous, mirrors)
            snapshot = {
                "generated_at": _now_iso(),
                "generated_ts": time.time(),
                "cached": False,
                "mirrors": mirrors,
                "warnings": build_warnings(mirrors),
            }
            self.last_run = time.time()
            if self.cache:
                self.cache.set("infra:snapshot", snapshot, ttl=3600)
                self.cache.set("infra:previous", snapshot, ttl=PREVIOUS_TTL)
        else:
            snapshot["cached"] = True
        snapshot["dumps"] = self.dumps(refresh=refresh) if dumps else (
            self.cache.get("infra:dumps") if self.cache else None
        )
        snapshot["transports"] = self.transports(refresh=refresh) if dumps else (
            self.cache.get("infra:transports") if self.cache else None
        )
        snapshot["monitor"] = {
            "interval_seconds": self.interval,
            "running": bool(self._thread and self._thread.is_alive()),
            "last_run": self.last_run,
            "next_run": self.next_run,
        }
        return snapshot

    # --------------------------------------------------------------- monitor
    def start(self) -> None:
        if self.interval <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="libgen-infra-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                report = self.report(refresh=True)
                self.last_warnings = report.get("warnings") or []
                for warning in self.last_warnings:
                    if warning.get("level") in {"warning", "critical", "error"}:
                        print(
                            "[infra] "
                            f"{warning['host']}: {warning['message']}",
                            flush=True,
                        )
            except Exception as exc:  # noqa: BLE001 - monitor must not die
                print(f"[infra] check failed: {exc}", flush=True)
            self.next_run = time.time() + self.interval
            if self._stop.wait(self.interval):
                break


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
