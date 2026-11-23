from __future__ import annotations

from mcp_probe.sample import sample_arguments


def test_required_only():
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["city"],
    }
    assert sample_arguments(schema) == {"city": "test"}


def test_defaults_enums_and_bounds():
    schema = {
        "type": "object",
        "properties": {
            "unit": {"type": "string", "enum": ["c", "f"]},
            "n": {"type": "integer", "minimum": 5},
            "ratio": {"type": "number", "maximum": 0.5},
            "flag": {"type": "boolean", "default": True},
            "code": {"type": "string", "minLength": 6, "maxLength": 6},
            "when": {"type": "string", "format": "date"},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        },
        "required": ["unit", "n", "ratio", "flag", "code", "when", "tags"],
    }
    assert sample_arguments(schema) == {
        "unit": "c",
        "n": 5,
        "ratio": 0.5,
        "flag": True,
        "code": "testxx",
        "when": "2024-01-01",
        "tags": ["test", "test"],
    }


def test_nested_refs_and_optional():
    schema = {
        "type": "object",
        "$defs": {"Point": {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}},
        "properties": {
            "p": {"$ref": "#/$defs/Point"},
            "label": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        },
        "required": ["p", "label"],
    }
    assert sample_arguments(schema) == {"p": {"x": 1}, "label": "test"}


def test_unsatisfiable_returns_none():
    schema = {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[0-9]+$"}}, "required": ["id"]}
    assert sample_arguments(schema) is None
    assert sample_arguments({"type": "object", "properties": {"q": {"type": "strng"}}, "required": ["q"]}) is None
