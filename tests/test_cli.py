from __future__ import annotations

import json
import sys

import pytest

from mcp_probe.cli import main


def test_list_rules(capsys):
    assert main(["--list-rules"]) == 0
    out = capsys.readouterr().out
    assert "MP001" in out and "MP107" in out


def test_clean_server_exits_zero(weather_cmd, capsys):
    assert main(["--smoke", "--no-color", *weather_cmd]) == 0
    out = capsys.readouterr().out
    assert "weather-demo 1.0.0" in out and "✓ tool list_cities" in out
    assert "4 calls made, 0 errors, 0 warnings" in out


def test_strict_and_ignore(broken_cmd, weather_cmd, capsys):
    assert main(["--strict", *weather_cmd]) == 0
    assert main(["--ignore", "MP002,MP003,MP004,MP005,MP008,MP009", *broken_cmd]) == 0
    assert main(["--strict", "--ignore", "MP002,MP003,MP004,MP005,MP008,MP009", *broken_cmd]) == 1


def test_broken_server_json(broken_cmd, capsys):
    assert main(["--smoke", "--format", "json", "--", *broken_cmd]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["server"]["name"] == "broken-demo"
    assert {"MP002", "MP003", "MP007"} <= {f["code"] for f in data["findings"]}


def test_call_argument(broken_cmd, capsys):
    main(["--format", "json", "--call", 'divide={"a": 1, "b": 4}', *broken_cmd])
    (call,) = json.loads(capsys.readouterr().out)["calls"]
    assert call["ok"] and call["arguments"] == {"a": 1, "b": 4}


def test_single_quoted_command(capsys):
    from conftest import EXAMPLES

    assert main(["--strict", f"{sys.executable} {EXAMPLES / 'weather_server.py'}"]) == 0


def test_unreachable_exits_two(capsys):
    assert main(["definitely-not-a-command-xyz"]) == 2


@pytest.mark.parametrize("bad", ["--call=add={oops", "--call=add=[1]", "--env=NOVALUE"])
def test_bad_arguments(bad, weather_cmd):
    with pytest.raises(SystemExit):
        main([bad, *weather_cmd])


def test_no_server():
    with pytest.raises(SystemExit):
        main([])
