"""Build the smallest valid arguments for a JSON Schema, for smoke calls."""

from __future__ import annotations

from typing import Any, Optional

from jsonschema import Draft202012Validator, validators

_FORMATS = {
    "uri": "https://example.com",
    "url": "https://example.com",
    "email": "test@example.com",
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "time": "00:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "ipv4": "127.0.0.1",
    "hostname": "example.com",
}

_MAX_DEPTH = 8


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return schema
        node = node[part]
    return node if isinstance(node, dict) else schema


def _sample(schema: Any, root: dict[str, Any], depth: int) -> Any:
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None
    schema = _resolve(schema, root)
    for key in ("const", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            # prefer a non-null branch: Optional[str] is anyOf [string, null]
            non_null = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
            return _sample((non_null or options)[0], root, depth + 1)
    if isinstance(schema.get("allOf"), list) and schema["allOf"]:
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            if isinstance(part, dict):
                merged.update(_resolve(part, root))
        return _sample(merged, root, depth + 1)

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0] if kind else None)
    if kind is None:
        kind = "object" if "properties" in schema else "string"

    if kind == "object":
        props = schema.get("properties") or {}
        return {name: _sample(props.get(name, {}), root, depth + 1) for name in schema.get("required") or [] if isinstance(name, str)}
    if kind == "array":
        n = int(schema.get("minItems") or 0)
        return [_sample(schema.get("items", {}), root, depth + 1) for _ in range(n)]
    if kind == "string":
        value = _FORMATS.get(str(schema.get("format")), "test")
        min_len = int(schema.get("minLength") or 0)
        max_len = schema.get("maxLength")
        value = value.ljust(min_len, "x")
        if isinstance(max_len, int):
            value = value[:max_len]
        return value
    if kind in ("integer", "number"):
        lo = schema.get("minimum", schema.get("exclusiveMinimum"))
        hi = schema.get("maximum", schema.get("exclusiveMaximum"))
        value: float = 1
        if isinstance(lo, (int, float)):
            value = lo + (1 if "exclusiveMinimum" in schema and "minimum" not in schema else 0)
        if isinstance(hi, (int, float)) and value > hi:
            value = hi
        return int(value) if kind == "integer" else value
    if kind == "boolean":
        return False
    return None


def sample_arguments(schema: Any) -> Optional[dict[str, Any]]:
    """Return arguments that satisfy `schema`, or None when we cannot build any."""
    if not isinstance(schema, dict):
        return None
    try:
        args = _sample(schema, schema, 0)
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict):
        return None
    cls = validators.validator_for(schema, default=Draft202012Validator)
    try:
        if not cls(schema).is_valid(args):
            return None
    except Exception:  # broken schema: MP002 already reports it
        return None
    return args
