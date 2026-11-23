from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from conftest import EXAMPLES, codes, probe
from mcp_probe.probe import Target


def test_weather_server_over_stdio(weather_cmd):
    r = probe(Target(command=weather_cmd), smoke="safe")
    assert r.connected and r.server_name == "weather-demo"
    assert [t.name for t in r.tools] == ["get_forecast", "list_cities"]
    assert all(c.ok for c in r.calls) and len(r.calls) == 4
    assert codes(r) == set()


def test_broken_server_over_stdio(broken_cmd):
    r = probe(Target(command=broken_cmd), smoke="all")
    assert r.connected and r.protocol == "2025-06-18"
    assert codes(r, "server") == {"MP008", "MP009"}
    assert codes(r, "tool search docs") == {"MP002", "MP101", "MP102", "MP104"}
    assert codes(r, "tool add") == {"MP003", "MP004", "MP005", "MP104"}
    assert codes(r, "tool divide") == {"MP106"}
    assert codes(r, "tool stats") == {"MP007"}
    assert codes(r, "tool crash") == {"MP006"}
    skipped = {c.name: c.skipped for c in r.calls if c.skipped}
    assert "search docs" in skipped and "add" in skipped


def test_explicit_call(broken_cmd):
    r = probe(Target(command=broken_cmd), calls=[("divide", {"a": 6, "b": 3}), ("missing", {})])
    divide, missing = r.calls
    assert divide.ok and divide.arguments == {"a": 6, "b": 3}
    assert missing.skipped and ("MP006", "tool missing") in {(f.code, f.subject) for f in r.findings}


def test_connect_failures(tmp_path):
    r = probe(Target(command=["definitely-not-a-command-xyz"]))
    assert not r.connected and codes(r) == {"MP001"}

    script = tmp_path / "dies.py"
    script.write_text("import sys\nprint('missing API key', file=sys.stderr)\nsys.exit(1)\n")
    r = probe(Target(command=[sys.executable, str(script)]))
    assert not r.connected and codes(r) == {"MP001"}
    assert "missing API key" in r.server_stderr


def _in_process_server() -> MCPServer:
    server = MCPServer("inproc")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def slow(seconds: float = 0.2) -> str:
        """Sleep a little."""
        time.sleep(seconds)
        return "done"

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def wipe() -> str:
        """Delete everything."""
        raise AssertionError("must never be called by a smoke test")

    @server.tool()
    def write_note(text: str) -> str:
        """Save a note."""
        return "saved"

    return server


def test_safe_and_all_modes_respect_annotations():
    r = probe(Target(server=_in_process_server()), smoke="safe", slow_ms=50)
    by_name = {c.name: c for c in r.calls}
    assert by_name["slow"].ok
    assert "readOnlyHint" in by_name["write_note"].skipped
    assert by_name["wipe"].skipped
    assert "MP107" in codes(r, "tool slow")

    r = probe(Target(server=_in_process_server()), smoke="all")
    by_name = {c.name: c for c in r.calls}
    assert by_name["write_note"].ok
    assert "destructiveHint" in by_name["wipe"].skipped


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(EXAMPLES / "weather_server.py"), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_streamable_http(http_url):
    r = probe(Target(url=http_url, headers={"X-Probe": "1"}), smoke="safe")
    assert r.connected and r.target.transport == "http"
    assert len(r.tools) == 2 and all(c.ok for c in r.calls)
