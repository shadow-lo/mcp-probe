"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any, Optional, Sequence

import anyio

from . import __version__
from .checks import RULES, Finding, Severity
from .probe import Options, Report, Target, run_probe

EXAMPLES = """\
examples:
  mcp-probe python server.py                     launch a stdio server and check it
  mcp-probe --smoke npx -y @modelcontextprotocol/server-everything
  mcp-probe https://example.com/mcp -H "Authorization: Bearer $TOKEN"
  mcp-probe --call 'add={"a": 1, "b": 2}' python server.py
"""


def _parse_kv(values: list[str], sep: str, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        if sep not in item:
            raise SystemExit(f"mcp-probe: {what} {item!r} must look like KEY{sep}VALUE")
        key, value = item.split(sep, 1)
        out[key.strip()] = value.strip() if sep == ":" else value
    return out


def _parse_call(text: str) -> tuple[str, dict[str, Any]]:
    name, sep, raw = text.partition("=")
    if not name:
        raise SystemExit(f"mcp-probe: --call {text!r} needs a tool name")
    if not sep or not raw.strip():
        return name, {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"mcp-probe: --call {name}: arguments are not valid JSON ({e.msg})")
    if not isinstance(args, dict):
        raise SystemExit(f"mcp-probe: --call {name}: arguments must be a JSON object")
    return name, args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp-probe",
        description="Connect to an MCP server, list its tools, resources and prompts, check their schemas and smoke-test them.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("server", nargs=argparse.REMAINDER, help="a URL (Streamable HTTP) or the command that starts a stdio server")
    p.add_argument(
        "--smoke",
        action="store_true",
        help="call read-only tools (readOnlyHint) with generated arguments, read every resource and get every prompt",
    )
    p.add_argument("--smoke-all", action="store_true", help="like --smoke, but call every tool not marked destructiveHint")
    p.add_argument("--call", action="append", default=[], metavar="TOOL=JSON", help="call a tool with these arguments (repeatable)")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'KEY: VALUE'", help="HTTP header (repeatable)")
    p.add_argument("-e", "--env", action="append", default=[], metavar="KEY=VALUE", help="environment variable for a stdio server (repeatable)")
    p.add_argument("--cwd", help="working folder for a stdio server")
    p.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS", help="per-request timeout (default: 10)")
    p.add_argument("--slow", type=float, default=2000.0, metavar="MS", help="warn when a call takes longer (default: 2000)")
    p.add_argument("-f", "--format", choices=["text", "json"], default="text", help="output format (default: text)")
    p.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    p.add_argument("--ignore", default="", metavar="CODES", help="comma-separated rule codes to skip, e.g. MP104,MP105")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    p.add_argument("--list-rules", action="store_true", help="print every rule and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def format_text(report: Report, findings: list[Finding], use_color: bool) -> str:
    red, yellow, green, dim, bold, reset = (
        ("\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m") if use_color else ("",) * 6
    )
    t = report.target
    out: list[str] = []
    if report.connected:
        name = f"{report.server_name} {report.server_version}".strip() or "unnamed server"
        out.append(f"{bold}{name}{reset}  {dim}{t.transport} · protocol {report.protocol} · connected in {report.connect_ms:.0f} ms{reset}")
        out.append(
            "  "
            + " · ".join(
                [
                    _plural(len(report.tools), "tool"),
                    _plural(len(report.resources), "resource"),
                    _plural(len(report.templates), "resource template"),
                    _plural(len(report.prompts), "prompt"),
                ]
            )
        )
    else:
        out.append(f"{red}✗{reset} {bold}{t.label}{reset}")
    out.append("")

    by_subject: dict[str, list[Finding]] = {}
    for f in findings:
        by_subject.setdefault(f.subject, []).append(f)

    subjects = ["server"] + [f"tool {x.name}" for x in report.tools]
    subjects += [f"resource {x.uri}" for x in report.resources]
    subjects += [f"template {x.uri_template}" for x in report.templates]
    subjects += [f"prompt {x.name}" for x in report.prompts]
    subjects += [s for s in by_subject if s not in subjects]
    seen: set[str] = set()
    for subject in subjects:
        if subject in seen:
            continue
        seen.add(subject)
        items = by_subject.get(subject, [])
        if subject == "server" and not items:
            continue
        if not items:
            out.append(f"{green}✓{reset} {subject}")
            continue
        has_error = any(f.severity is Severity.ERROR for f in items)
        out.append(f"{red if has_error else yellow}✗{reset} {subject}")
        for f in sorted(items, key=lambda f: f.code):
            color = red if f.severity is Severity.ERROR else yellow
            out.append(f"     {color}{f.code}{reset}  {f.message}")

    if report.calls:
        out.append("")
        out.append(f"{bold}Calls{reset}")
        for c in report.calls:
            args = "" if c.arguments is None or c.kind == "resource" else " " + json.dumps(c.arguments, ensure_ascii=False)
            if c.skipped:
                out.append(f"  {dim}- {c.kind} {c.name}  skipped: {c.skipped}{reset}")
            elif c.ok:
                out.append(f"  {green}✓{reset} {c.kind} {c.name}{dim}{args}{reset}  {c.detail}  {dim}{c.ms:.0f} ms{reset}")
            else:
                out.append(f"  {red}✗{reset} {c.kind} {c.name}{dim}{args}{reset}  {c.detail}")

    if not report.connected and report.server_stderr.strip():
        out.append("")
        out.append(f"{dim}server stderr (last lines):{reset}")
        out += ["  " + line for line in report.server_stderr.strip().splitlines()[-10:]]

    errors = sum(f.severity is Severity.ERROR for f in findings)
    warnings = len(findings) - errors
    ran = sum(1 for c in report.calls if not c.skipped)
    out.append("")
    out.append(f"{_plural(ran, 'call')} made, {_plural(errors, 'error')}, {_plural(warnings, 'warning')}")
    return "\n".join(out)


def format_json(report: Report, findings: list[Finding]) -> str:
    payload = {
        "target": report.target.label,
        "transport": report.target.transport,
        "connected": report.connected,
        "server": {"name": report.server_name, "version": report.server_version, "protocol": report.protocol},
        "connect_ms": round(report.connect_ms, 1),
        "tools": [t.name for t in report.tools],
        "resources": [str(r.uri) for r in report.resources],
        "resource_templates": [r.uri_template for r in report.templates],
        "prompts": [p.name for p in report.prompts],
        "calls": [
            {
                "kind": c.kind,
                "name": c.name,
                "arguments": c.arguments,
                "ok": c.ok,
                "skipped": c.skipped,
                "detail": c.detail,
                "ms": round(c.ms, 1),
            }
            for c in report.calls
        ],
        "findings": [
            {"code": f.code, "severity": f.severity.value, "subject": f.subject, "message": f.message} for f in findings
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def make_target(args: argparse.Namespace) -> Target:
    server = list(args.server)
    if server and server[0] == "--":
        server = server[1:]
    if len(server) == 1 and " " in server[0] and not server[0].startswith(("http://", "https://")):
        server = shlex.split(server[0])  # mcp-probe "python server.py"
    if not server:
        raise SystemExit("mcp-probe: give a server URL or command, e.g. `mcp-probe python server.py` (see --help)")
    if server[0].startswith(("http://", "https://")):
        if len(server) > 1:
            raise SystemExit("mcp-probe: put options before the URL")
        return Target(url=server[0], headers=_parse_kv(args.header, ":", "header"))
    return Target(command=server, env=_parse_kv(args.env, "=", "--env"), cwd=args.cwd)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for rule in RULES:
            print(f"{rule.code}  {rule.severity.value:<7}  {rule.summary}")
        return 0

    target = make_target(args)
    opts = Options(
        smoke="all" if args.smoke_all else ("safe" if args.smoke else None),
        calls=[_parse_call(c) for c in args.call],
        timeout=args.timeout,
        slow_ms=args.slow,
    )
    report = anyio.run(run_probe, target, opts)

    ignore = frozenset(c.strip().upper() for c in args.ignore.split(",") if c.strip())
    findings = [f for f in report.findings if f.code not in ignore]

    if args.format == "json":
        print(format_json(report, findings))
    else:
        use_color = not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
        print(format_text(report, findings, use_color))

    if not report.connected:
        return 2
    if any(f.severity is Severity.ERROR for f in findings):
        return 1
    if args.strict and findings:
        return 1
    return 0
