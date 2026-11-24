"""Exception types used across the service."""

from __future__ import annotations


class LibgenError(Exception):
    """Base error for all service failures."""


class LibgenUnavailable(LibgenError):
    """Every configured mirror failed or returned an unusable response."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


class NotFound(LibgenError):
    """The requested book, file or download link does not exist."""


class DownloadError(LibgenError):
    """The remote file could not be fetched or failed validation."""


class FileUnavailable(DownloadError):
    """The catalogue record exists but the file is missing from Libgen storage.

    Libgen answers ``HTTP 500`` with "File not found in DB" when the metadata
    row is present but the actual file was never uploaded (or was removed).
    This is a property of the file, not of a mirror, so it must not put the
    mirror into backoff.
    """


class InvalidInput(LibgenError):
    """A caller supplied a malformed query parameter."""
