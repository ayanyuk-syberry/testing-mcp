from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from pydantic import BaseModel


class HelloResponse(BaseModel):
    message: str


app = FastAPI(
    title="Hello MCP API",
    description="Experimental FastAPI service that doubles as an MCP server.",
)


@app.get("/hello", operation_id="hello_world", response_model=HelloResponse)
async def hello(name: str = "world") -> HelloResponse:
    """Say hello to someone."""
    return HelloResponse(message=f"Hello, {name}!")


# FastApiMCP inspects the app's routes, so it must be created after they are declared.
mcp = FastApiMCP(app, name="hello-mcp")
mcp.mount_http()  # streamable-HTTP MCP endpoint at /mcp
