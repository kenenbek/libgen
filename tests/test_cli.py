from __future__ import annotations

import json
from pathlib import Path

from libgen_service.api import LibgenAPI
from libgen_service.cli import build_parser, cmd_get, cmd_info, cmd_search


def test_cli_search_human_output(api: LibgenAPI, capsys):
    args = build_parser().parse_args(["search", "harry potter", "--per-page", "5"])
    assert cmd_search(args, api) == 0
    output = capsys.readouterr().out
    assert "Harry Potter" in output
    assert "184c16eae281bf2ebea72eb6d7aa6f6d" in output
    assert "Found 2" in output


def test_cli_search_json_output(api: LibgenAPI, capsys):
    args = build_parser().parse_args(["search", "harry potter", "--json"])
    assert cmd_search(args, api) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2
    assert payload["items"][0]["files"][0]["md5"]


def test_cli_info_json(api: LibgenAPI, capsys):
    args = build_parser().parse_args(["info", "147748488", "--json"])
    assert cmd_info(args, api) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][0]["title"].startswith("Python Essentials 1")
    assert payload["books"][0]["description"]


def test_cli_get_downloads(api: LibgenAPI, payload_md5: str, tmp_path: Path, capsys):
    args = build_parser().parse_args(["get", payload_md5, "-o", str(tmp_path / "out")])
    assert cmd_get(args, api) == 0
    output = capsys.readouterr().out
    assert "Saved:" in output
    files = list((tmp_path / "out").glob("*.pdf"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


def test_cli_requires_subcommand():
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args([])
