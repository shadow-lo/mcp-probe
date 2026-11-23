from __future__ import annotations

import mcp_types as types

from mcp_probe.checks import RULES, check_listing, check_tool, schema_errors


def tool(name="search", description="Search the docs.", schema=None, **kw) -> types.Tool:
    schema = schema if schema is not None else {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "Query."}},
        "required": ["q"],
    }
    return types.Tool(name=name, description=description, input_schema=schema, **kw)


def codes_of(findings) -> set[str]:
    return {f.code for f in findings}


def test_good_tool_is_clean():
    assert check_tool(tool()) == []


def test_invalid_schema():
    t = tool(schema={"type": "object", "properties": {"q": {"type": "strng"}}})
    (f,) = [f for f in check_tool(t) if f.code == "MP002"]
    assert "properties.q.type" in f.message


def test_non_object_root():
    assert "MP003" in codes_of(check_tool(tool(schema={"type": "array"})))


def test_required_not_in_properties():
    t = tool(schema={"type": "object", "properties": {"a": {"type": "string", "description": "A."}}, "required": ["a", "b"]})
    assert codes_of(check_tool(t)) == {"MP005"}


def test_output_schema_checked_too():
    t = tool(output_schema={"type": "object", "properties": {"n": {"type": 3}}})
    assert "MP002" in codes_of(check_tool(t))


def test_missing_descriptions():
    t = tool(description=None, schema={"type": "object", "properties": {"q": {"type": "string"}}})
    assert codes_of(check_tool(t)) == {"MP101", "MP104"}


def test_tool_names():
    assert "MP102" in codes_of(check_tool(tool(name="search docs")))
    assert "MP103" in codes_of(check_tool(tool(name="docs.search")))
    assert "MP103" in codes_of(check_tool(tool(name="x" * 65)))
    assert not codes_of(check_tool(tool(name="docs_search-v2")))


def test_duplicates_and_descriptions():
    resources = [types.Resource(name="r", uri="file:///a"), types.Resource(name="r2", uri="file:///a", description="A.")]
    prompts = [types.Prompt(name="p")]
    found = check_listing([tool(), tool()], resources, [], prompts)
    assert {(f.code, f.subject) for f in found} >= {
        ("MP004", "tool search"),
        ("MP004", "resource file:///a"),
        ("MP105", "prompt p"),
        ("MP105", "resource file:///a"),
    }


def test_schema_errors():
    assert schema_errors({"type": "object"}) is None
    assert schema_errors([]) is not None
    assert schema_errors({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}) is None


def test_rule_codes_unique():
    assert len({r.code for r in RULES}) == len(RULES)
