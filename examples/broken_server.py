"""A hand-written stdio MCP server with the mistakes mcp-probe looks for.

It speaks raw JSON-RPC with no SDK, like many quick servers in the wild.
"""

import json
import sys

TOOLS = [
    {
        "name": "search docs",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "strng"}},
            "required": ["query"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number", "description": "Second number."}},
            "required": ["a", "b", "c"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "divide",
        "description": "Divide a by b.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "Dividend."},
                "b": {"type": "number", "description": "Divisor.", "default": 0},
            },
            "required": ["a", "b"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "crash",
        "description": "Always fails with an internal error.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "stats",
        "description": "Count indexed documents.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "add", "description": "Same name again.", "inputSchema": {"type": "array"}},
]


def reply(msg_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": msg_id}
    body["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


def call_tool(params):
    name, args = params.get("name"), params.get("arguments") or {}
    if name == "divide":
        if args.get("b") == 0:
            return {"content": [{"type": "text", "text": "division by zero"}], "isError": True}
        return {"content": [{"type": "text", "text": str(args["a"] / args["b"])}]}
    if name == "stats":  # structuredContent does not match outputSchema
        return {"content": [{"type": "text", "text": "many"}], "structuredContent": {"count": "many"}}
    if name == "crash":
        raise RuntimeError("boom")
    return {"content": [{"type": "text", "text": "ok"}]}


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notification
    method, params = msg.get("method"), msg.get("params") or {}
    try:
        if method == "initialize":
            reply(msg["id"], {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "broken-demo", "version": "0.0.1"},
            })
        elif method == "ping":
            reply(msg["id"], {})
        elif method == "tools/list":
            reply(msg["id"], {"tools": TOOLS})
        elif method == "tools/call":
            reply(msg["id"], call_tool(params))
        else:  # prompts/list is advertised but not implemented
            reply(msg["id"], error={"code": -32601, "message": f"Method not found: {method}"})
    except Exception as e:
        reply(msg["id"], error={"code": -32603, "message": f"Internal error: {e}"})
