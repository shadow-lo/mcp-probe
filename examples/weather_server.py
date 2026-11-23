"""A small, well-behaved MCP server used in the README and the tests."""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

mcp = MCPServer("weather-demo", version="1.0.0")

FORECASTS = {"istanbul": "sunny, 18°C", "ankara": "cloudy, 11°C"}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_forecast(city: Annotated[str, Field(description="City name, e.g. Istanbul.")]) -> str:
    """Get today's forecast for a city."""
    return FORECASTS.get(city.lower(), f"no forecast for {city}")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_cities() -> list[str]:
    """List the cities that have a forecast."""
    return sorted(FORECASTS)


@mcp.resource("weather://cities", description="Every city with a forecast, one per line.")
def cities() -> str:
    return "\n".join(sorted(FORECASTS))


@mcp.prompt(description="Ask for a packing list based on the forecast.")
def packing_list(city: str) -> str:
    return f"The forecast for {city} is {get_forecast(city)}. What should I pack?"


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--http"]:  # python weather_server.py --http 8000  ->  http://127.0.0.1:8000/mcp
        mcp.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        mcp.run()
