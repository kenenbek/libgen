from __future__ import annotations

from pathlib import Path

from libgen_service.cache import TTLCache
from libgen_service.config import Settings
from libgen_service.infra import (
    InfraMonitor,
    build_download_chain,
    build_warnings,
    compare_snapshots,
    hostname_of,
    parse_dump_index,
    parse_dump_root,
    parse_transports,
)


def test_hostname_of_accepts_bare_hosts():
    assert hostname_of("https://libgen.li/") == "libgen.li"
    assert hostname_of("libgen.vg") == "libgen.vg"


def test_parse_dump_root_lists_collections():
    html = """
    <html><body><pre>
    <a href="../">../</a>
    <a href="comics/">comics/</a>
    <a href="libgen/">libgen/</a>
    <a href="worldcat/">worldcat/</a>
    </pre></body></html>
    """
    assert parse_dump_root(html) == ["comics", "libgen", "worldcat"]


def test_parse_dump_index_sums_sizes_and_finds_latest():
    html = """
    <html><body><pre>
    <a href="libgen_new_libgen-2026-11-22.part01.rar">libgen_new_libgen-2026-11-22.part01.rar</a>  22-Nov-2026 02:59  1073741824
    <a href="libgen_new_libgen-2026-11-22.part02.rar">libgen_new_libgen-2026-11-22.part02.rar</a>  22-Nov-2026 03:07  536870912
    </pre></body></html>
    """
    parsed = parse_dump_index(html)
    assert parsed["files"] == 2
    assert parsed["bytes"] == 1610612736
    assert parsed["latest_date"] == "2026-11-22"
    assert parsed["latest_file"].endswith("part02.rar")


def test_compare_snapshots_detects_ip_and_certificate_changes():
    previous = {
        "mirrors": [
            {
                "base": "https://libgen.li",
                "ip": "179.43.167.164",
                "cloudflare": False,
                "tls": {"fingerprint": "old"},
            }
        ]
    }
    current = [
        {
            "base": "https://libgen.li",
            "ip": "179.43.167.165",
            "cloudflare": False,
            "tls": {"fingerprint": "new"},
        }
    ]
    changes = compare_snapshots(previous, current)
    messages = [item["message"] for item in changes]
    assert "IP 179.43.167.164 → 179.43.167.165" in messages
    assert "TLS certificate replaced" in messages
    assert current[0]["changes"] == messages


def test_compare_snapshots_ignores_cloudflare_ip_rotation():
    previous = {
        "mirrors": [
            {
                "base": "https://libgen.la",
                "ip": "172.67.155.199",
                "cloudflare": True,
                "tls": {"fingerprint": "same"},
            }
        ]
    }
    current = [
        {
            "base": "https://libgen.la",
            "ip": "104.21.34.70",
            "cloudflare": True,
            "tls": {"fingerprint": "same"},
        }
    ]
    assert compare_snapshots(previous, current) == []


def test_build_warnings_flags_expiring_certificates():
    mirrors = [
        {
            "host": "libgen.li",
            "ip": "1.2.3.4",
            "http_status": 200,
            "healthy": True,
            "tls": {"days_left": 10},
        },
        {
            "host": "libgen.la",
            "ip": "5.6.7.8",
            "http_status": 200,
            "healthy": True,
            "tls": {"days_left": 3},
        },
    ]
    warnings = build_warnings(mirrors)
    levels = {(item["host"], item["level"]) for item in warnings}
    assert ("libgen.li", "warning") in levels
    assert ("libgen.la", "critical") in levels


def test_build_download_chain_uses_observed_cdn():
    settings = Settings(
        mirrors=("https://libgen.li", "https://libgen.vg"),
        cache_dir=Path("/tmp/libgen-test"),
    )
    chain = build_download_chain(
        settings, {"cdn_hosts": {"cdn5.booksdl.lc": 4, "other.cdn": 1}}
    )
    assert chain["mirrors"] == ["libgen.li", "libgen.vg"]
    assert chain["observed_cdn"] == ["cdn5.booksdl.lc", "other.cdn"]
    assert chain["steps"][-1]["host"] == "cdn5.booksdl.lc"


def test_parse_transports_groups_rows_by_section():
    html = """
    <table>
    <tr><th>Type</th><th>Mirror</th><th>Default</th><th>Order</th><th>Tooltip</th>
        <th>Name</th><th>Name alt</th><th>URL</th><th>Active</th></tr>
    <tr><td colspan=10 bgcolor=silver><strong>Libgen</strong></td></tr>
    <tr><td>http</td><td>libgen.li</td><td>1</td><td>1</td><td>libgen</td>
        <td>1</td><td>Libgen</td><td>/ads.php?md5=[md5]</td><td>1</td></tr>
    <tr><td>ipfs</td><td>cloudflare</td><td>0</td><td>10</td><td>IPFS cloudflare</td>
        <td></td><td>IPFS cloudflare</td>
        <td>https://cloudflare-ipfs.com/ipfs/[ipfs]</td><td>1</td></tr>
    <tr><td>tor</td><td>tor</td><td>0</td><td>30</td><td>need Tor Browser</td>
        <td></td><td>TOR</td><td>http://example.onion/LG/[md5]</td><td>1</td></tr>
    <tr><td colspan=10 bgcolor=silver><strong>Fiction</strong></td></tr>
    <tr><td>ipfs</td><td>cloudflare</td><td>0</td><td>10</td><td>IPFS cloudflare</td>
        <td></td><td>IPFS cloudflare</td>
        <td>https://cloudflare-ipfs.com/ipfs/[ipfs]</td><td>1</td></tr>
    <tr><td>1000torrent</td><td>libgen.li</td><td>0</td><td>70</td><td>torrent</td>
        <td>Torrent per 1000 books</td><td>Torrent per 1000 books</td>
        <td>/torrents/fiction/f_[idrange].torrent</td><td>1</td></tr>
    </table>
    """
    parsed = parse_transports(html)
    assert parsed["totals"] == {"http": 1, "ipfs": 2, "tor": 1, "1000torrent": 1}
    assert parsed["active"]["ipfs"] == 2
    names = [section["name"] for section in parsed["sections"]]
    assert names == ["Libgen", "Fiction"]
    assert parsed["sections"][0]["counts"] == {"http": 1, "ipfs": 1, "tor": 1}
    ipfs = parsed["gateways"]["ipfs"]
    assert len(ipfs) == 1
    assert ipfs[0]["sections"] == ["Libgen", "Fiction"]
    assert ipfs[0]["url"].startswith("https://cloudflare-ipfs.com")
    tor = parsed["gateways"]["tor"]
    assert tor[0]["url"].endswith(".onion/LG/[md5]")


class StubMonitor(InfraMonitor):
    def __init__(self, settings: Settings, cache: TTLCache) -> None:
        super().__init__(settings, cache, interval=0)
        self.collect_calls = 0

    def collect_mirrors(self):
        self.collect_calls += 1
        return [
            {
                "base": "https://libgen.li",
                "host": "libgen.li",
                "ip": "179.43.167.164",
                "asn": "AS51852",
                "org": "Private Layer",
                "country": "CH",
                "cloudflare": False,
                "tls": {"issuer": "Let's Encrypt", "days_left": 76},
                "http_status": 200,
                "server": "nginx",
                "latency_ms": 120,
                "healthy": True,
                "changes": [],
            }
        ]

    def dumps(self, *, refresh: bool = False):
        return {
            "collections": [{"name": "libgen", "files": 9, "bytes": 1024}],
            "total_files": 9,
            "total_bytes": 1024,
        }

    def transports(self, *, refresh: bool = False):
        return {
            "totals": {"http": 26, "ipfs": 8},
            "active": {"http": 24, "ipfs": 8},
            "sections": [],
            "gateways": {},
        }


def test_monitor_report_is_cached_and_includes_dumps(tmp_path: Path):
    settings = Settings(
        mirrors=("https://libgen.li",),
        cache_dir=tmp_path,
        monitor_interval=0,
    )
    cache = TTLCache(tmp_path / "cache.sqlite3", 600)
    monitor = StubMonitor(settings, cache)
    try:
        first = monitor.report(dumps=True)
        second = monitor.report(dumps=True)
        assert first["mirrors"][0]["ip"] == "179.43.167.164"
        assert first["dumps"]["total_files"] == 9
        assert second["cached"] is True
        assert monitor.collect_calls == 1
    finally:
        cache.close()
