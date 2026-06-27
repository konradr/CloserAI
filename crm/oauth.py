"""Attio MCP OAuth plumbing + JSON-Schema -> Gemini conversion.

Attio's hosted MCP authenticates with OAuth only (no API key). On first run a
browser opens for you to log in to Attio; the token is cached locally so later
runs (and the service) reuse it without prompting.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .config import repo_root

MCP_ENDPOINT = "https://mcp.attio.com/mcp"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
TOKEN_FILE = repo_root() / ".attio_mcp_token.json"


class FileTokenStorage(TokenStorage):
    """Persists the OAuth token + client registration to a local JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (ValueError, OSError):
                self._data = {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = info.model_dump(mode="json")
        self._save()


def _wait_for_oauth_callback() -> tuple[str, str | None]:
    result: dict[str, str | None] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;background:#0f0f0f;"
                b"color:#fff;text-align:center;padding-top:80px'>"
                b"<h2>CloserAI connected to Attio</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )

        def log_message(self, *_args) -> None:
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.handle_request()
    server.server_close()
    return result.get("code", ""), result.get("state")


async def _redirect_handler(auth_url: str) -> None:
    print("\n[crm] Opening browser to authorise CloserAI with Attio...")
    print(f"[crm] If it doesn't open, paste this URL:\n   {auth_url}\n")
    webbrowser.open(auth_url)


async def _callback_handler() -> tuple[str, str | None]:
    return await asyncio.to_thread(_wait_for_oauth_callback)


def make_oauth() -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=MCP_ENDPOINT,
        client_metadata=OAuthClientMetadata(
            client_name="CloserAI",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(TOKEN_FILE),
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )


# --------------------------------------------------------------------------- #
# JSON-Schema -> Gemini function declaration conversion
# --------------------------------------------------------------------------- #

def _norm_type(t) -> str:
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    return str(t or "string").upper()


def sanitize_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return {"type": "STRING"}
    allowed = {"type", "description", "enum", "properties", "required", "items"}
    out = {k: v for k, v in schema.items() if k in allowed}
    if "type" in out:
        out["type"] = _norm_type(out["type"])
    if "properties" in out and isinstance(out["properties"], dict):
        out["properties"] = {k: sanitize_schema(v) for k, v in out["properties"].items()}
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = sanitize_schema(out["items"])
    out.setdefault("type", "OBJECT" if "properties" in out else "STRING")
    return out


def mcp_tools_to_genai(tools, types) -> list:
    """Build a single Gemini Tool wrapping all MCP tools as function declarations."""
    decls = []
    for t in tools:
        schema = sanitize_schema(t.inputSchema or {"type": "object", "properties": {}})
        if schema.get("type") == "OBJECT" and not schema.get("properties"):
            schema["properties"] = {}
        decls.append(
            types.FunctionDeclaration(
                name=t.name,
                description=(t.description or "")[:1000],
                parameters=schema,
            )
        )
    return [types.Tool(function_declarations=decls)]
