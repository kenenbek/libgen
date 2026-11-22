"""Data models shared by the CLI, the JSON API and the web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ISO 639-2/B codes used by Libgen -> human readable names (common subset).
LANGUAGES: dict[str, str] = {
    "eng": "English",
    "rus": "Russian",
    "fre": "French",
    "ger": "German",
    "spa": "Spanish",
    "ita": "Italian",
    "por": "Portuguese",
    "dut": "Dutch",
    "pol": "Polish",
    "ukr": "Ukrainian",
    "tur": "Turkish",
    "chi": "Chinese",
    "jpn": "Japanese",
    "kor": "Korean",
    "ara": "Arabic",
    "hin": "Hindi",
    "ben": "Bengali",
    "swe": "Swedish",
    "nor": "Norwegian",
    "dan": "Danish",
    "fin": "Finnish",
    "cze": "Czech",
    "slo": "Slovak",
    "hun": "Hungarian",
    "rum": "Romanian",
    "bul": "Bulgarian",
    "srp": "Serbian",
    "hrv": "Croatian",
    "gre": "Greek",
    "heb": "Hebrew",
    "per": "Persian",
    "ind": "Indonesian",
    "may": "Malay",
    "vie": "Vietnamese",
    "tha": "Thai",
    "cat": "Catalan",
    "baq": "Basque",
    "glg": "Galician",
    "est": "Estonian",
    "lav": "Latvian",
    "lit": "Lithuanian",
    "geo": "Georgian",
    "arm": "Armenian",
    "aze": "Azerbaijani",
    "kaz": "Kazakh",
    "uzb": "Uzbek",
    "kir": "Kyrgyz",
    "tat": "Tatar",
    "bel": "Belarusian",
    "mac": "Macedonian",
    "slv": "Slovenian",
    "alb": "Albanian",
    "lat": "Latin",
    "epo": "Esperanto",
}

TOPICS: dict[str, str] = {
    "l": "Libgen",
    "f": "Fiction",
    "c": "Comics",
    "m": "Magazines",
    "a": "Articles",
    "s": "Standards",
    "r": "Fiction (RUS)",
    "w": "Works",
}

EXTENSIONS = (
    "pdf",
    "epub",
    "mobi",
    "azw3",
    "djvu",
    "fb2",
    "cbz",
    "cbr",
    "chm",
    "txt",
    "rtf",
    "doc",
    "docx",
    "zip",
)

EXT_PRIORITY = {ext: index for index, ext in enumerate(EXTENSIONS)}


def human_size(size: int | str | None) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def language_name(code: str | None) -> str:
    if not code:
        return ""
    return LANGUAGES.get(code.lower(), code)


@dataclass
class BookFile:
    md5: str
    file_id: str = ""
    ext: str = ""
    size: int = 0
    pages: int = 0
    filename: str = ""
    topic: str = ""
    added: str = ""
    edition_id: str = ""
    libgen_id: str = ""

    @property
    def size_display(self) -> str:
        return human_size(self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "md5": self.md5,
            "file_id": self.file_id,
            "extension": self.ext,
            "size": self.size,
            "size_display": self.size_display,
            "pages": self.pages,
            "filename": self.filename,
            "topic": self.topic,
            "added": self.added,
            "edition_id": self.edition_id,
            "libgen_id": self.libgen_id,
            "download_url": f"/api/download/{self.md5}",
            "info_url": f"/api/file/{self.md5}",
        }


@dataclass
class Book:
    edition_id: str
    title: str = ""
    subtitle: str = ""
    series: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    pages: int = 0
    isbn: list[str] = field(default_factory=list)
    doi: str = ""
    description: str = ""
    toc: str = ""
    topic: str = ""
    kind: str = ""
    files: list[BookFile] = field(default_factory=list)
    cover_exists: bool = False
    relevance: int = 0

    @property
    def language_name(self) -> str:
        return language_name(self.language)

    @property
    def best_file(self) -> BookFile | None:
        if not self.files:
            return None
        return sorted(
            self.files,
            key=lambda f: (
                EXT_PRIORITY.get(f.ext.lower(), len(EXT_PRIORITY)),
                -f.size,
            ),
        )[0]

    @property
    def extensions(self) -> list[str]:
        seen: list[str] = []
        for book_file in self.files:
            ext = book_file.ext.lower()
            if ext and ext not in seen:
                seen.append(ext)
        return seen

    def to_dict(self, *, detailed: bool = False) -> dict[str, Any]:
        best = self.best_file
        cover = None
        if best is not None:
            cover = f"/api/cover/{best.md5}"
        data: dict[str, Any] = {
            "edition_id": self.edition_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "series": self.series,
            "author": self.author,
            "publisher": self.publisher,
            "year": self.year,
            "language": self.language,
            "language_name": self.language_name,
            "pages": self.pages,
            "isbn": self.isbn,
            "doi": self.doi,
            "topic": self.topic,
            "topic_name": TOPICS.get(self.topic, self.topic),
            "kind": self.kind,
            "extensions": self.extensions,
            "files": [f.to_dict() for f in self.files],
            "cover_url": cover,
            "has_cover": self.cover_exists,
            "detail_url": f"/api/book/{self.edition_id}",
        }
        if detailed:
            data["description"] = self.description
            data["toc"] = self.toc
        return data


@dataclass
class SearchResult:
    query: str
    mode: str
    total: int
    found: int
    page: int
    per_page: int
    items: list[Book]
    mirror: str = ""
    cached: bool = False
    elapsed_ms: int = 0
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def pages(self) -> int:
        if self.per_page <= 0:
            return 0
        return max(1, (self.found + self.per_page - 1) // self.per_page)

    def to_dict(self, *, detailed: bool = False) -> dict[str, Any]:
        return {
            "query": self.query,
            "mode": self.mode,
            "total": self.total,
            "found": self.found,
            "page": self.page,
            "per_page": self.per_page,
            "pages": self.pages,
            "mirror": self.mirror,
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
            "filters": self.filters,
            "items": [b.to_dict(detailed=detailed) for b in self.items],
        }
