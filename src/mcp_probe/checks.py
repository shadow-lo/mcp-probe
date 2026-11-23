"""Static checks on what a server lists: names, descriptions and JSON schemas."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

import mcp_types as types
from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Rule:
    code: str
    severity: Severity
    summary: str


RULES: tuple[Rule, ...] = (
    Rule("MP001", Severity.ERROR, "the server starts and completes the MCP handshake"),
    Rule("MP002", Severity.ERROR, "every inputSchema / outputSchema is a valid JSON Schema"),
    Rule("MP003", Severity.ERROR, "every inputSchema / outputSchema has root type \"object\""),
    Rule("MP004", Severity.ERROR, "tool names, prompt names and resource URIs are unique"),
    Rule("MP005", Severity.ERROR, "every name in 'required' is defined under 'properties'"),
    Rule("MP006", Severity.ERROR, "smoke calls finish without a protocol error, crash or timeout"),
    Rule("MP007", Severity.ERROR, "structuredContent matches the tool's outputSchema"),
    Rule("MP008", Severity.ERROR, "an advertised capability can actually be listed"),
    Rule("MP009", Severity.ERROR, "list results conform to the negotiated protocol version"),
    Rule("MP101", Severity.WARNING, "every tool has a description"),
    Rule("MP102", Severity.WARNING, "tool names use only A-Z a-z 0-9 _ - . and are 1-128 characters (MCP spec)"),
    Rule("MP103", Severity.WARNING, "tool names fit the common LLM-API pattern ^[a-zA-Z0-9_-]{1,64}$"),
    Rule("MP104", Severity.WARNING, "every top-level input property has a description"),
    Rule("MP105", Severity.WARNING, "prompts, resources and resource templates have a description"),
    Rule("MP106", Severity.WARNING, "smoke calls do not come back with isError: true"),
    Rule("MP107", Severity.WARNING, "smoke calls answer faster than the --slow threshold"),
)
RULES_BY_CODE = {r.code: r for r in RULES}

_SPEC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_API_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class Finding:
    code: str
    subject: str  # e.g. "tool search", "prompt summarize", "server"
    message: str

    @property
    def severity(self) -> Severity:
        return RULES_BY_CODE[self.code].severity


def finding(code: str, subject: str, message: str) -> Finding:
    return Finding(code, subject, message)


def schema_errors(schema: Any) -> Optional[str]:
    """Return why `schema` is not a valid JSON Schema, or None when it is.

    Schemas without `$schema` are checked as JSON Schema 2020-12, the MCP default.
    """
    if not isinstance(schema, dict):
        return f"must be an object, got {type(schema).__name__}"
    cls = validators.validator_for(schema, default=Draft202012Validator)
    try:
        cls.check_schema(schema)
    except SchemaError as e:
        where = ".".join(str(p) for p in e.absolute_path)
        return f"{e.message}" + (f" (at {where})" if where else "")
    return None


def _check_object_schema(schema: Any, subject: str, label: str) -> list[Finding]:
    out: list[Finding] = []
    problem = schema_errors(schema)
    if problem:
        out.append(finding("MP002", subject, f"{label} is not a valid JSON Schema: {problem}"))
        return out
    if schema.get("type") != "object":
        out.append(finding("MP003", subject, f"{label} root type must be \"object\", got {schema.get('type')!r}"))
        return out
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if isinstance(name, str) and name not in props:
            out.append(finding("MP005", subject, f"{label} requires {name!r}, which is not under 'properties'"))
    return out


def check_tool(tool: types.Tool) -> list[Finding]:
    subject = f"tool {tool.name}"
    out = _check_object_schema(tool.input_schema, subject, "inputSchema")
    if tool.output_schema is not None:
        out += _check_object_schema(tool.output_schema, subject, "outputSchema")

    if not (tool.description or "").strip():
        out.append(finding("MP101", subject, "has no description, so a model has to guess what it does"))
    if not _SPEC_NAME.match(tool.name):
        out.append(finding("MP102", subject, f"name {tool.name!r} has characters or a length the MCP spec does not allow"))
    elif not _API_NAME.match(tool.name):
        out.append(finding("MP103", subject, f"name {tool.name!r} does not match ^[a-zA-Z0-9_-]{{1,64}}$ and may be rejected by LLM provider APIs"))

    props = tool.input_schema.get("properties") if isinstance(tool.input_schema, dict) else None
    if isinstance(props, dict):
        missing = [k for k, v in props.items() if isinstance(v, dict) and not str(v.get("description", "")).strip()]
        if missing:
            out.append(finding("MP104", subject, "input properties without a description: " + ", ".join(missing)))
    return out


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(k for k, n in Counter(values).items() if n > 1)


def check_listing(
    tools: list[types.Tool],
    resources: list[types.Resource],
    templates: list[types.ResourceTemplate],
    prompts: list[types.Prompt],
) -> list[Finding]:
    out: list[Finding] = []
    for name in _duplicates(t.name for t in tools):
        out.append(finding("MP004", f"tool {name}", "tool name is listed more than once"))
    for name in _duplicates(p.name for p in prompts):
        out.append(finding("MP004", f"prompt {name}", "prompt name is listed more than once"))
    for uri in _duplicates(str(r.uri) for r in resources):
        out.append(finding("MP004", f"resource {uri}", "resource URI is listed more than once"))

    for tool in tools:
        out += check_tool(tool)
    for p in prompts:
        if not (p.description or "").strip():
            out.append(finding("MP105", f"prompt {p.name}", "has no description"))
    for r in resources:
        if not (r.description or "").strip():
            out.append(finding("MP105", f"resource {r.uri}", "has no description"))
    for t in templates:
        if not (t.description or "").strip():
            out.append(finding("MP105", f"template {t.uri_template}", "has no description"))
    return out
