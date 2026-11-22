"""Runtime configuration for the Libgen helper service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://libgen.li",
    "https://libgen.la",
    "https://libgen.bz",
    "https://libgen.gl",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "libgen-service"


@dataclass
class Settings:
    """Settings are intentionally simple so the service can run anywhere."""

    mirrors: tuple[str, ...] = DEFAULT_MIRRORS
    timeout: float = 20.0
    retries: int = 3
    min_interval: float = 0.4
    cache_dir: Path = field(default_factory=_default_cache_dir)
    cache_ttl: int = 600
    user_agent: str = DEFAULT_USER_AGENT
    proxy: str | None = None

    @classmethod
    def load(cls) -> "Settings":
        mirrors_raw = os.environ.get("LIBGEN_MIRRORS", "")
        if mirrors_raw.strip():
            mirrors = tuple(
                m.strip().rstrip("/")
                for m in mirrors_raw.split(",")
                if m.strip()
            )
        else:
            mirrors = DEFAULT_MIRRORS

        proxy = os.environ.get("LIBGEN_PROXY") or os.environ.get("HTTPS_PROXY")
        return cls(
            mirrors=mirrors,
            timeout=_env_float("LIBGEN_TIMEOUT", 20.0),
            retries=max(1, _env_int("LIBGEN_RETRIES", 3)),
            min_interval=max(0.0, _env_float("LIBGEN_MIN_INTERVAL", 0.4)),
            cache_dir=Path(
                os.environ.get("LIBGEN_CACHE_DIR") or _default_cache_dir()
            ).expanduser(),
            cache_ttl=max(0, _env_int("LIBGEN_CACHE_TTL", 600)),
            user_agent=os.environ.get("LIBGEN_USER_AGENT") or DEFAULT_USER_AGENT,
            proxy=proxy or None,
        )

    def with_overrides(self, **kwargs) -> "Settings":
        data = {
            "mirrors": self.mirrors,
            "timeout": self.timeout,
            "retries": self.retries,
            "min_interval": self.min_interval,
            "cache_dir": self.cache_dir,
            "cache_ttl": self.cache_ttl,
            "user_agent": self.user_agent,
            "proxy": self.proxy,
        }
        data.update({k: v for k, v in kwargs.items() if v is not None})
        return Settings(**data)
