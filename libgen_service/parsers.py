"""Parsers for Libgen pages.

Two independent sources are supported:

* ``batchsearchindex.php`` (POST) returns a compact list of record ids.
* ``index.php?req=...`` returns the classic results table.

Neither parser needs JavaScript and neither touches advertising markup; the
only thing we ever extract from an ad-wrapped page is the final
``get.php?...&key=...`` link.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from bs4 import BeautifulSoup, Tag

from .models import LANGUAGES

_MD5_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
_GET_LINK_RE = re.compile(
    r"get\.php\?md5=([0-9a-fA-F]{32})&(?:amp;)?key=([A-Za-z0-9]+)"
)
_SIZE_RE = re.compile(r"([\d.,]+)\s*(B|kB|KB|MB|GB|TB)?", re.IGNORECASE)
_TOPIC_RE = re.compile(r"\b([lafcmsrw])\s+(\d{3,})\b")
_LANG_BY_NAME = {v.lower(): k for k, v in LANGUAGES.items()}


def text_of(node: Tag | None, *, skip: tuple[str, ...] = ()) -> str:
    if node is None:
        return ""
    if skip:
        parts: list[str] = []
        for string in node.find_all(string=True):
            parent = string.parent
            if parent is not None and parent.name in skip:
                continue
            if any(
                p is not None and p.name in skip for p in string.parents
            ):
                continue
            value = str(string).strip()
            if value:
                parts.append(value)
        return " ".join(" ".join(parts).split())
    return " ".join(node.get_text(" ", strip=True).split())


def parse_json(text: str) -> Any:
    return json.loads(text.lstrip("\ufeff"))


# ---------------------------------------------------------------- batch search
def parse_batch_result(html: str) -> tuple[int, list[str], str]:
    """Parse a ``batchsearchindex.php`` response.

    Returns ``(total_found, ids, followup_link)``.  Depending on the search
    mode Libgen may omit the id list entirely and only provide a link to the
    regular results table; the caller follows that link.
    """
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    ids: list[str] = []
    link = ""
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        found = text_of(cells[2])
        if not found.isdigit():
            continue
        total = max(total, int(found))
        for token in re.split(r"[,\s]+", text_of(cells[3])):
            if token.isdigit():
                ids.append(token)
        if not link:
            anchor = cells[1].find("a", href=True)
            if anchor is not None:
                link = anchor["href"]
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return total, unique, link


def parse_batch_ids(html: str) -> tuple[int, list[str]]:
    total, ids, _ = parse_batch_result(html)
    return total, ids


# ------------------------------------------------------------------ downloads
def parse_download_key(html: str, md5: str) -> str | None:
    """Extract the real download link from an ``ads.php`` interstitial."""
    for match in _GET_LINK_RE.finditer(html):
        if match.group(1).lower() == md5.lower():
            return f"get.php?md5={match.group(1)}&key={match.group(2)}"
    return None


def _fix_mojibake(value: str) -> str:
    """HTTP headers are latin-1 decoded by requests; recover UTF-8 names."""
    if not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def parse_file_id(html: str) -> str:
    """Extract the numeric file id from a ``file.php?md5=`` page."""
    match = re.search(r"object=f&(?:amp;)?addkeys=\*&(?:amp;)?ids=(\d+)", html)
    if match:
        return match.group(1)
    match = re.search(r"File ID:\s*(\d+)", html)
    return match.group(1) if match else ""


def parse_content_disposition(value: str | None) -> str:
    if not value:
        return ""
    name = ""
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, re.IGNORECASE)
    if match:
        from urllib.parse import unquote

        name = unquote(match.group(1).strip().strip('"'))
    if not name:
        match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
    if not name:
        match = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
        if match:
            name = match.group(1).strip().strip('"')
    return _fix_mojibake(name)


def sanitize_filename(name: str, fallback: str = "book") -> str:
    name = unicodedata.normalize("NFC", (name or "").strip())
    name = name.replace("\\", "/").split("/")[-1]
    # Some Libgen mirrors HTML-escape unsafe characters in file names.
    name = (
        name.replace("&_039_", "'")
        .replace("&_quot_", '"')
        .replace("&_amp_", "&")
        .replace("&_lt_", "<")
        .replace("&_gt_", ">")
    )
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name)
    name = re.sub(r'[<>:"|?*]+', "_", name)
    name = name.strip(" .")
    if not name:
        name = fallback
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: 180 - len(ext) - 1] + "." + ext
        else:
            name = name[:180]
    return name


# --------------------------------------------------------------------- sizes
def parse_size(value: str | None) -> int:
    if not value:
        return 0
    match = _SIZE_RE.search(value.replace("\u00a0", " "))
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return 0
    unit = (match.group(2) or "B").lower()
    factor = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}[unit]
    return int(number * factor)


def parse_pages(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def language_code(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) == 3 and value.isalpha():
        return value.lower()
    return _LANG_BY_NAME.get(value.lower(), "")


# -------------------------------------------------------------- search table
def parse_search_rows(html: str) -> list[dict[str, Any]]:
    """Parse the classic ``table#tablelibgen`` results table.

    Column counts differ between repositories (fiction rows have no cover
    column), so the right-hand columns are addressed from the end.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tablelibgen")
    if table is None:
        return []
    body = table.find("tbody") or table
    rows: list[dict[str, Any]] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 8:
            continue
        row = _parse_search_row(cells)
        if row and row.get("md5"):
            rows.append(row)

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("edition_id", ""), row["md5"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _parse_title_cell(title_cell: Tag) -> dict[str, Any]:
    edition_id = ""
    for link in title_cell.find_all("a", href=True):
        match = re.search(r"edition\.php\?id=(\d+)", link["href"])
        if match:
            edition_id = match.group(1)
            break

    title = ""
    for link in title_cell.find_all("a", href=True):
        if "edition.php?id=" not in link["href"]:
            continue
        if link.find("font", attrs={"color": "green"}) is not None:
            continue
        title = text_of(link, skip=("i", "font"))
        if title:
            break

    series_tag = title_cell.find("b")
    series = text_of(series_tag) if series_tag is not None else ""

    isbn_tag = title_cell.find("font", attrs={"color": "green"})
    isbn_text = text_of(isbn_tag) if isbn_tag is not None else ""
    isbn = [part for part in re.split(r"[;,\s]+", isbn_text) if part]

    row_text = text_of(title_cell)
    topic = ""
    topic_match = _TOPIC_RE.search(row_text)
    if topic_match:
        topic = topic_match.group(1)
    kind = ""
    for badge in title_cell.find_all("span", class_="badge-primary"):
        badge_text = text_of(badge)
        if badge_text in {"l", "f", "c", "m", "a", "s", "r", "w"}:
            topic = topic or badge_text
        elif badge_text and not kind:
            kind = badge_text

    return {
        "edition_id": edition_id,
        "title": title,
        "series": series,
        "isbn": isbn,
        "topic": topic,
        "kind": kind,
    }


def _parse_search_row(cells: list[Tag]) -> dict[str, Any] | None:
    has_cover = len(cells) >= 10
    cover_url = ""
    title_cell = cells[1] if has_cover else cells[0]
    if has_cover:
        img = cells[0].find("img")
        if img is not None:
            cover_url = img.get("src") or ""

    info = _parse_title_cell(title_cell)
    edition_id = info["edition_id"]
    title = info["title"]
    series = info["series"]
    isbn = info["isbn"]
    topic = info["topic"]
    kind = info["kind"]

    file_id = ""
    md5 = ""
    for cell in cells:
        for link in cell.find_all("a", href=True):
            href = link["href"]
            file_match = re.search(r"file\.php\?id=(\d+)", href)
            if file_match and not file_id:
                file_id = file_match.group(1)
            md5_match = re.search(r"ads\.php\?md5=([0-9a-fA-F]{32})", href)
            if md5_match and not md5:
                md5 = md5_match.group(1).lower()
    if not md5:
        match = _MD5_RE.search(str(title_cell))
        if match:
            md5 = match.group(1).lower()

    size_text = text_of(cells[-3])
    pages_text = text_of(cells[-4])
    language_text = text_of(cells[-5])
    year_match = re.search(r"\d{4}", text_of(cells[-6]))
    ext = text_of(cells[-2]).lower()

    if not title:
        return None

    return {
        "edition_id": edition_id,
        "file_id": file_id,
        "md5": md5,
        "title": title,
        "series": series,
        "author": text_of(cells[-8]),
        "publisher": text_of(cells[-7]),
        "year": year_match.group(0) if year_match else "",
        "language": language_code(language_text),
        "language_name": language_text,
        "pages": parse_pages(pages_text),
        "size": parse_size(size_text),
        "size_text": size_text,
        "ext": ext,
        "cover_url": cover_url,
        "isbn": isbn,
        "topic": topic,
        "kind": kind,
    }


def parse_edition_rows(html: str) -> list[dict[str, Any]]:
    """Parse the editions tab (no file columns, no MD5 links).

    Used for ISBN/DOI searches where Libgen matches editions rather than
    files.  The resulting ids are enriched through ``json.php`` later.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tablelibgen")
    if table is None:
        return []
    body = table.find("tbody") or table
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tr in body.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
        title_index = None
        for index, cell in enumerate(cells):
            if cell.find("a", href=re.compile(r"edition\.php\?id=\d+")) is not None:
                title_index = index
                break
        if title_index is None:
            continue
        info = _parse_title_cell(cells[title_index])
        edition_id = info["edition_id"]
        if not edition_id or not info["title"] or edition_id in seen:
            continue
        seen.add(edition_id)
        rest = cells[title_index + 1 :]
        year_match = re.search(r"\d{4}", text_of(rest[2])) if len(rest) > 2 else None
        language_text = text_of(rest[4]) if len(rest) > 4 else ""
        rows.append(
            {
                "edition_id": edition_id,
                "file_id": "",
                "md5": "",
                "title": info["title"],
                "series": info["series"],
                "author": text_of(rest[0]) if rest else "",
                "publisher": text_of(rest[1]) if len(rest) > 1 else "",
                "year": year_match.group(0) if year_match else "",
                "language": language_code(language_text),
                "language_name": language_text,
                "pages": parse_pages(text_of(rest[3])) if len(rest) > 3 else 0,
                "size": 0,
                "size_text": "",
                "ext": "",
                "cover_url": "",
                "isbn": info["isbn"],
                "topic": info["topic"],
                "kind": info["kind"],
            }
        )
    return rows
