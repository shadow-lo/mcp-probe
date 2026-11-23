"""Connect to a server, list everything, check it and run smoke calls."""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar

import mcp_types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import ValidationError

from .checks import Finding, check_listing, finding
from .sample import sample_arguments

T = TypeVar("T")
_MAX_PAGES = 100


@dataclass
class Target:
    """What to connect to: a command line (stdio) or a URL (Streamable HTTP)."""

    command: list[str] = field(default_factory=list)
    url: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    server: Any = None  # an in-process Server / MCPServer, for tests

    @property
    def transport(self) -> str:
        if self.server is not None:
            return "in-process"
        return "http" if self.url else "stdio"

    @property
    def label(self) -> str:
        return self.url or " ".join(self.command) or "in-process server"


@dataclass
class Options:
    smoke: Optional[str] = None  # None, "safe" or "all"
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    timeout: float = 10.0
    slow_ms: float = 2000.0


@dataclass
class Call:
    kind: str  # "tool", "resource" or "prompt"
    name: str
    arguments: Optional[dict[str, Any]] = None
    ok: bool = False
    skipped: Optional[str] = None
    detail: str = ""
    ms: float = 0.0


@dataclass
class Report:
    target: Target
    connected: bool = False
    connect_ms: float = 0.0
    server_name: str = ""
    server_version: str = ""
    protocol: str = ""
    tools: list[types.Tool] = field(default_factory=list)
    resources: list[types.Resource] = field(default_factory=list)
    templates: list[types.ResourceTemplate] = field(default_factory=list)
    prompts: list[types.Prompt] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    server_stderr: str = ""


def describe_error(exc: BaseException) -> str:
    """Flatten exception groups into one readable line."""
    while isinstance(getattr(exc, "exceptions", None), (list, tuple)) and exc.exceptions:  # type: ignore[attr-defined]
        exc = exc.exceptions[0]  # type: ignore[attr-defined]
    text = str(exc).strip() or type(exc).__name__
    if type(exc).__name__ in ("TimeoutError", "TimeoutException") and "timed out" not in text.lower():
        text = f"timed out ({text})"
    return text.splitlines()[0] if text else type(exc).__name__


def _short(value: Any, limit: int = 80) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _transport(target: Target, errlog: Any) -> Any:
    if target.server is not None:
        return target.server
    if target.url:
        if not target.headers:
            return target.url
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        return streamable_http_client(target.url, http_client=httpx2.AsyncClient(headers=target.headers))
    params = StdioServerParameters(
        command=target.command[0], args=target.command[1:], env=target.env or None, cwd=target.cwd
    )
    return stdio_client(params, errlog=errlog)


async def _raw_page(client: Client, method: str, cursor: Optional[str], timeout: float) -> dict[str, Any]:
    """Fetch one page without the SDK's strict per-version validation."""
    session = client.session
    data: dict[str, Any] = {"method": method, "params": {"cursor": cursor} if cursor else {}}
    opts: dict[str, Any] = {"timeout": timeout}
    session._stamp(data, opts)  # type: ignore[attr-defined]  # protocol headers / _meta
    return await session._dispatcher.send_raw_request(method, data.get("params"), opts)  # type: ignore[attr-defined]


def _prime_output_schemas(client: Client, tools: list[types.Tool]) -> None:
    """Tell the SDK the output schemas we read raw, so call_tool does not re-list (and fail) again."""
    known = getattr(client.session, "_tool_output_schemas", None)
    if isinstance(known, dict):
        for tool in tools:
            known.setdefault(tool.name, tool.output_schema)


async def _paginate(
    client: Client,
    report: Report,
    method: str,
    fetch: Callable[[Optional[str]], Awaitable[Any]],
    attr: str,
    model: type,
    timeout: float,
) -> list[Any]:
    """Walk every page. When the SDK rejects a page, re-fetch it raw and keep the items it can read."""
    items: list[Any] = []
    cursor: Optional[str] = None
    for _ in range(_MAX_PAGES):
        try:
            page = await fetch(cursor)
            items.extend(getattr(page, attr))
            cursor = page.next_cursor
        except ValidationError as e:
            for err in e.errors()[:5]:
                where = ".".join(str(p) for p in err["loc"])
                report.findings.append(
                    finding("MP009", "server", f"{method} result breaks protocol {report.protocol} at {where}: {err['msg']}")
                )
            raw = await _raw_page(client, method, cursor, timeout)
            key = "resourceTemplates" if attr == "resource_templates" else attr
            for entry in raw.get(key) or []:
                try:
                    items.append(model.model_validate(entry))
                except ValidationError as item_err:
                    name = entry.get("name", "?") if isinstance(entry, dict) else "?"
                    report.findings.append(
                        finding("MP009", f"{attr.rstrip('s').replace('_', ' ')} {name}", f"unreadable entry: {item_err.errors()[0]['msg']}")
                    )
            cursor = raw.get("nextCursor")
            if method == "tools/list":
                _prime_output_schemas(client, items)
        if not cursor:
            break
    return items


async def _timed(coro: Awaitable[T]) -> tuple[T, float]:
    start = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - start) * 1000


def _content_summary(result: types.CallToolResult) -> str:
    kinds = [getattr(block, "type", "?") for block in result.content]
    parts = [f"{len(kinds)} content block(s)" + (f" ({', '.join(sorted(set(kinds)))})" if kinds else "")]
    if result.structured_content is not None:
        parts.append("structured")
    return ", ".join(parts)


def _tool_text(result: types.CallToolResult) -> str:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return text.strip().splitlines()[0][:120]
    return "no text content"


def _skip_reason(tool: types.Tool, mode: str) -> Optional[str]:
    ann = tool.annotations
    if mode == "safe" and not (ann and ann.read_only_hint):
        return "not marked readOnlyHint (use --smoke-all)"
    if ann and ann.destructive_hint and not ann.read_only_hint:
        return "marked destructiveHint (use --call to run it)"
    return None


async def _call_tool(client: Client, report: Report, opts: Options, tool_name: str, args: dict[str, Any]) -> None:
    call = Call("tool", tool_name, args)
    report.calls.append(call)
    subject = f"tool {tool_name}"
    try:
        result, call.ms = await _timed(client.call_tool(tool_name, args, read_timeout_seconds=opts.timeout))
    except RuntimeError as e:  # the SDK checks structuredContent against outputSchema
        msg = describe_error(e)
        call.detail = msg
        code = "MP007" if "structured content" in msg.lower() else "MP006"
        report.findings.append(finding(code, subject, msg))
        return
    except Exception as e:
        call.detail = describe_error(e)
        report.findings.append(finding("MP006", subject, f"call failed: {call.detail}"))
        return
    if result.is_error:
        call.detail = f"isError: {_tool_text(result)}"
        report.findings.append(finding("MP106", subject, f"returned isError for {_short(args)}: {_tool_text(result)}"))
    else:
        call.ok = True
        call.detail = _content_summary(result)
    if call.ms > opts.slow_ms:
        report.findings.append(finding("MP107", subject, f"took {call.ms:.0f} ms (threshold {opts.slow_ms:.0f} ms)"))


async def _smoke(client: Client, report: Report, opts: Options) -> None:
    explicit = {name for name, _ in opts.calls}
    tools = {t.name: t for t in report.tools}
    for name, args in opts.calls:
        if name not in tools:
            report.calls.append(Call("tool", name, args, skipped="not listed by the server"))
            report.findings.append(finding("MP006", f"tool {name}", "--call names a tool the server does not list"))
            continue
        await _call_tool(client, report, opts, name, args)

    if not opts.smoke:
        return

    seen: set[str] = set()
    for tool in report.tools:
        if tool.name in explicit:
            continue
        if tool.name in seen:
            report.calls.append(Call("tool", tool.name, skipped="duplicate name, already called"))
            continue
        seen.add(tool.name)
        reason = _skip_reason(tool, opts.smoke)
        args = None if reason else sample_arguments(tool.input_schema)
        if reason is None and args is None:
            reason = "could not build valid arguments from inputSchema (use --call)"
        if reason:
            report.calls.append(Call("tool", tool.name, skipped=reason))
            continue
        await _call_tool(client, report, opts, tool.name, args or {})

    for res in report.resources:
        call = Call("resource", str(res.uri))
        report.calls.append(call)
        try:
            result, call.ms = await _timed(client.read_resource(str(res.uri)))
            call.ok = True
            call.detail = f"{len(result.contents)} content item(s)"
        except Exception as e:
            call.detail = describe_error(e)
            report.findings.append(finding("MP006", f"resource {res.uri}", f"read failed: {call.detail}"))

    for prompt in report.prompts:
        args = {a.name: "test" for a in (prompt.arguments or []) if a.required}
        call = Call("prompt", prompt.name, args)
        report.calls.append(call)
        try:
            result, call.ms = await _timed(client.get_prompt(prompt.name, args))
            call.ok = True
            call.detail = f"{len(result.messages)} message(s)"
        except Exception as e:
            call.detail = describe_error(e)
            report.findings.append(finding("MP006", f"prompt {prompt.name}", f"get failed: {call.detail}"))


async def _list_all(client: Client, report: Report, timeout: float) -> None:
    caps = client.server_capabilities
    wanted = [
        ("tools", caps.tools, "tools/list", lambda c: client.list_tools(cursor=c), "tools", types.Tool),
        ("resources", caps.resources, "resources/list", lambda c: client.list_resources(cursor=c), "resources", types.Resource),
        (
            "templates",
            caps.resources,
            "resources/templates/list",
            lambda c: client.list_resource_templates(cursor=c),
            "resource_templates",
            types.ResourceTemplate,
        ),
        ("prompts", caps.prompts, "prompts/list", lambda c: client.list_prompts(cursor=c), "prompts", types.Prompt),
    ]
    for field_name, cap, method, fetch, attr, model in wanted:
        if cap is None:
            continue
        try:
            setattr(report, field_name, await _paginate(client, report, method, fetch, attr, model, timeout))
        except Exception as e:
            if field_name == "templates":  # optional even when resources are offered
                continue
            report.findings.append(
                finding("MP008", "server", f"advertises {field_name} but listing them failed: {describe_error(e)}")
            )


async def run_probe(target: Target, opts: Optional[Options] = None) -> Report:
    opts = opts or Options()
    report = Report(target)
    errlog = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    try:
        async with AsyncExitStack() as stack:
            client = Client(
                _transport(target, errlog),
                read_timeout_seconds=opts.timeout,
                client_info=types.Implementation(name="mcp-probe", version=_version()),
            )
            try:
                start = time.perf_counter()
                await stack.enter_async_context(client)
                report.connect_ms = (time.perf_counter() - start) * 1000
            except Exception as e:
                report.findings.append(finding("MP001", "server", f"could not connect: {describe_error(e)}"))
                return report
            report.connected = True
            info = client.server_info
            report.server_name = info.name if info else ""
            report.server_version = info.version if info else ""
            report.protocol = client.protocol_version
            await _list_all(client, report, opts.timeout)
            report.findings += check_listing(report.tools, report.resources, report.templates, report.prompts)
            await _smoke(client, report, opts)
    except Exception as e:  # the server died while we were talking to it
        if report.connected:
            report.findings.append(finding("MP006", "server", f"connection broke: {describe_error(e)}"))
        elif not any(f.code == "MP001" for f in report.findings):
            report.findings.append(finding("MP001", "server", f"could not connect: {describe_error(e)}"))
    finally:
        errlog.seek(0)
        report.server_stderr = errlog.read()
        errlog.close()
    return report


def _version() -> str:
    from . import __version__

    return __version__
