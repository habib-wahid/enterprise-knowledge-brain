"""CAP-8 — MCP server (BR-58..BR-64).

One MCP tool per registered CAP-7 service, generated from the SAME registry
that backs the CLI and the HTTP API. A service added to the registry
appears here with no new code in this file — BR-51 and BR-64 hold on this
surface exactly the way they hold on the other two.

Two transports (BR-59):
  stdio            local use — a single trusted process pipe, no auth needed
  streamable-http  shared-network use — requires a bearer token (BR-60)

The SDK's built-in auth support is OAuth-authorization-server shaped
(issuer_url, resource_server_url, client registration, ...) — real
machinery for a bigger requirement than BR-60 actually states (authenticate
the caller and identify them; nothing here asks for an authorization
server). So HTTP auth is a small bearer-check ASGI middleware wrapping the
SDK's plain Starlette app instead — the same shape as the audit middleware
already wrapping the FastAPI HTTP surface in api/http.py.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from .. import config
from ..services.registry import catalogue, get, load_all
from ..services.resolve import Context
from ..services.result import GROUNDING
from ..store.db import utc_now

ASSISTANT_GUIDE = config.project_root() / "ASSISTANT_GUIDE.md"


def _make_tool_fn(service_id: str, db_path: Path):
    """One wrapper per service, carrying a REAL, introspectable signature.

    The body just forwards **kwargs to the registered service function, but
    schema generation (inspect.signature(fn, eval_str=True), which respects
    a function's __signature__ attribute) needs named, typed, described
    parameters to produce a useful tool schema — so __signature__ is built
    from the same ServiceSpec.inputs/required the CLI already uses for its
    own argument parsing (one definition, every surface).
    """
    spec = get(service_id)

    params = []
    for name in spec.inputs:
        desc = spec.inputs[name]
        if name in spec.required:
            ann = Annotated[str, Field(description=desc)]
            default = inspect.Parameter.empty
        else:
            ann = Annotated[str | None, Field(description=desc)]
            default = None
        params.append(inspect.Parameter(
            name, kind=inspect.Parameter.KEYWORD_ONLY,
            default=default, annotation=ann))

    def _tool(**kwargs: Any) -> dict[str, Any]:
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        ctx = Context(db_path)
        try:
            result = spec.fn(ctx, **kwargs)
        finally:
            ctx.close()
        # Round-trip through json so Evidence dataclasses etc. become plain
        # dicts the MCP content layer can serialise without help.
        return json.loads(json.dumps(result.to_dict(), default=str))

    _tool.__name__ = service_id.replace(".", "_")
    _tool.__signature__ = inspect.Signature(
        parameters=params, return_annotation=dict[str, Any])
    _tool.__doc__ = f"{spec.question}\n\n{spec.when_to_use}"
    return _tool


def build_server(db_path: Path) -> MCPServer:
    load_all()
    server = MCPServer(
        name="eck",
        version="0.6.0",
        instructions=(
            "Enterprise Code Knowledge Platform. Read-only answer services "
            "over a registered software estate — no service here modifies "
            "anything. " + GROUNDING),
    )

    for c in catalogue():
        spec = get(c["id"])
        server.add_tool(
            _make_tool_fn(spec.id, db_path),
            name=spec.id.replace(".", "_"),
            description=f"{spec.question}\n\n{spec.when_to_use}",
            structured_output=True)

    if ASSISTANT_GUIDE.exists():                                   # BR-62
        server.add_resource(TextResource(
            uri="eck://assistant-guide",
            name="ECK Assistant Guide",
            description="Scope, limitations and good example questions.",
            mime_type="text/markdown",
            text=ASSISTANT_GUIDE.read_text(encoding="utf-8")))

    return server


# --------------------------------------------------------------- HTTP auth

class BearerAuthMiddleware:
    """BR-60 — authenticate a shared-network request and identify its caller.

    Deliberately not the SDK's OAuth machinery (see module docstring). One
    shared secret per deployment is what BR-60 asks for; a caller identifies
    itself with a client name header, logged alongside every call (BR-71).
    """

    def __init__(self, app: ASGIApp, token: str, audit_path: Path):
        self.app = app
        self.token = token
        self.audit_path = audit_path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        client_name = headers.get(b"x-eck-client", b"unknown").decode()
        caller = scope.get("client", ("unknown", 0))[0]

        ok = auth.startswith("Bearer ") and auth[7:] == self.token
        self._audit(caller, client_name, scope.get("path", ""), ok)

        if not ok:
            response = JSONResponse(
                {"error": "unauthorized",
                 "detail": "a valid Bearer token is required for shared "
                          "network access (BR-60)"},
                status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _audit(self, caller: str, client_name: str, path: str, ok: bool) -> None:
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "at": utc_now(), "caller": caller, "client": client_name,
                    "path": path, "authenticated": ok}) + "\n")
        except Exception:
            pass   # audit failure must never block or crash a real request


def build_http_app(db_path: Path, token: str, audit_path: Path) -> Starlette:
    # Created once, eagerly, at startup: a broken audit mount should fail
    # loudly here, not swallow every write silently for the life of the
    # process (the middleware's own try/except is for per-request hiccups,
    # not for masking a missing directory forever).
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    server = build_server(db_path)
    app = server.streamable_http_app()
    return BearerAuthMiddleware(app, token=token, audit_path=audit_path)
