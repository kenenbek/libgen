"""Exception types used across the service."""

from __future__ import annotations


class LibgenError(Exception):
    """Base error for all service failures."""


class LibgenUnavailable(LibgenError):
    """Every configured mirror failed or returned an unusable response."""


class NotFound(LibgenError):
    """The requested book, file or download link does not exist."""


class DownloadError(LibgenError):
    """The remote file could not be fetched or failed validation."""


class InvalidInput(LibgenError):
    """A caller supplied a malformed query parameter."""
