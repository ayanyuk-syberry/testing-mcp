# testing-mcp

Experimental FastAPI service that doubles as an MCP server, built with
[fastapi-mcp](https://github.com/tadata-org/fastapi_mcp): regular REST endpoints are
automatically exposed as MCP tools over streamable HTTP.

## Run

```bash
uv run uvicorn app.main:app --reload --port 8000
```

- REST: http://127.0.0.1:8000/hello — interactive docs at http://127.0.0.1:8000/docs
- MCP (streamable HTTP): http://127.0.0.1:8000/mcp

> Note: this machine's shell exports `UV_DEFAULT_INDEX` pointing at a private
> CodeArtifact mirror. If its token is expired, prefix uv commands with
> `UV_DEFAULT_INDEX=https://pypi.org/simple`.

## Connect to Claude Code

```bash
claude mcp add --transport http hello-mcp http://127.0.0.1:8000/mcp
```

Then in a Claude Code session, `/mcp` should show `hello-mcp` with a `hello_world` tool.
Use `--scope project` instead if you want the config shared via `.mcp.json`.

## Best practices baked into `app/main.py`

- **`operation_id` is the MCP tool name.** Always set it explicitly on routes;
  otherwise FastAPI auto-generates names like `hello_hello_get`.
- **Docstring + `response_model` become the tool description and output schema**
  that the LLM sees — keep them accurate and specific.
- **`mount_http()`** uses streamable HTTP, the current standard MCP transport
  (SSE is deprecated).
- **`FastApiMCP(app)` is instantiated after routes are declared** — it inspects
  the app's routes at creation time.

## Toward remote hosting

- Bind to `0.0.0.0` and put it behind TLS (MCP clients expect HTTPS for remote servers).
- Add auth with regular FastAPI dependencies — fastapi-mcp supports
  `AuthConfig(dependencies=[Depends(...)])`, so bearer-token/OAuth flows reuse the
  same machinery as the REST API.
