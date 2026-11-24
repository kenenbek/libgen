"""A clean, ad-free client for Library Genesis.

Public API::

    from libgen_service import LibgenAPI

    with LibgenAPI() as api:
        result = api.search("clean code", ext="pdf")
        api.download_to(result.items[0].files[0].md5, "~/Books")
"""

from .api import LibgenAPI, DownloadStream
from .config import Settings
from .errors import (
    DownloadError,
    FileUnavailable,
    InvalidInput,
    LibgenError,
    LibgenUnavailable,
    NotFound,
)
from .models import Book, BookFile, SearchResult

__all__ = [
    "LibgenAPI",
    "DownloadStream",
    "Settings",
    "Book",
    "BookFile",
    "SearchResult",
    "LibgenError",
    "LibgenUnavailable",
    "NotFound",
    "DownloadError",
    "FileUnavailable",
    "InvalidInput",
]

__version__ = "1.0.0"
