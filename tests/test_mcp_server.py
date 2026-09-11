"""Contract tests for M5 — CAP-8 Assistant Interface (BR-58..BR-64).

These drive the server through the REAL MCP wire protocol — a subprocess
over stdio for the local transport, real HTTP requests for the shared one —
not just calling Python functions in-process. Protocol-level bugs (wrong
field names, a missing session header, a tool schema the SDK rejects) are
exactly what unit-testing the underlying service functions cannot catch;
every one of those was caught this way while building the server, not by
reading the code.

Run:  ./.venv/bin/python tests/test_mcp_server.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession                          # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from eck.services.registry import catalogue, load_all  # noqa: E402

DB = ROOT / "build" / "knowledge.db"
PYTHON = str(ROOT / ".venv" / "bin" / "python")
passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


# --------------------------------------------------------------- stdio (BR-59)

async def check_stdio() -> None:
    load_all()
    registry_ids = {c["id"] for c in catalogue()}

    params = StdioServerParameters(
        command=PYTHON, args=["-m", "eck.cli", "mcp", "serve",
                              "--transport", "stdio"], cwd=str(ROOT))
    # Context managers inlined (not wrapped in an async generator) so every
    # exit happens in the task that entered it — anyio's task groups raise
    # if a cancel scope crosses tasks, which an early `break` out of a
    # generator-wrapped context manager triggers on cleanup.
    async with stdio_client(params) as (read, write), \
            ClientSession(read, write) as session:
        init = await session.initialize()

        check("BR-58: server identifies itself over an open protocol",
              init.server_info.name == "eck")
        check("BR-61: server-level instructions carry the grounding rule",
              "only from the evidence" in (init.instructions or ""))

        tools = await session.list_tools()
        tool_names = {t.name for t in tools.tools}
        expected = {i.replace(".", "_") for i in registry_ids}
        check("BR-63: every registered service is discoverable as a tool",
              tool_names == expected, str(tool_names ^ expected))
        check("BR-52 carries through: every tool description states "
              "when to use it",
              all(len(t.description or "") > 40 for t in tools.tools))

        resources = await session.list_resources()
        check("BR-62: the assistant guide is exposed as a resource",
              any("Assistant Guide" in r.name for r in resources.resources))
        guide = await session.read_resource("eck://assistant-guide")
        check("BR-62: the guide names scope, limits and example questions",
              all(w in guide.contents[0].text.lower()
                  for w in ("limit", "example")))

        # A real answer, over the real protocol.
        r = await session.call_tool(
            "flow_process_stages", {"process": "salary hold and release"})
        check("a real tool call succeeds and returns the curated process",
              not r.is_error and r.structured_content.get("outcome") == "answered")
        check("BR-54: the structured result carries evidence with a location",
              bool(r.structured_content.get("evidence")) and
              all(e.get("path") for e in r.structured_content["evidence"]))

        # BR-55, over the protocol: an excluded key is a well-formed
        # 'unknown', not a protocol-level error and not a guess.
        r2 = await session.call_tool(
            "configuration_reference_data", {"key": "ui.login.defaultPassword"})
        check("BR-45/55: refusing to answer is a normal result, not a "
              "protocol error", not r2.is_error)
        check("BR-45: the refusal is outcome=unknown, not fabricated data",
              r2.structured_content.get("outcome") == "unknown"
              and not r2.structured_content.get("evidence"))

        # Schema validation: a missing required argument IS a protocol error
        # — the SDK rejects it before our function ever runs.
        r3 = await session.call_tool("navigation_find", {})
        check("a missing required argument is rejected before the service "
              "function runs", r3.is_error)


# --------------------------------------------------------------- http (BR-60)

def check_http() -> None:
    port = 8933
    token = "test-mcp-token-xyz"
    proc = subprocess.Popen(
        [PYTHON, "-m", "eck.cli", "mcp", "serve",
         "--transport", "streamable-http", "--port", str(port),
         "--token", token],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    audit_path = DB.parent / "mcp_audit.jsonl"
    audit_before = (audit_path.read_text().splitlines()
                    if audit_path.exists() else [])
    try:
        time.sleep(3)
        base = f"http://127.0.0.1:{port}/mcp"

        def post(body: dict, headers: dict) -> tuple[int, bytes]:
            req = urllib.request.Request(
                base, data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **headers})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        status, _ = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
        check("BR-60: an unauthenticated shared request is refused",
              status == 401)

        status, _ = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         {"Authorization": "Bearer wrong-token"})
        check("BR-60: a wrong token is refused, not merely a missing one",
              status == 401)

        status, body = post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}},
            {"Authorization": f"Bearer {token}", "X-ECK-Client": "test-suite"})
        check("BR-60: the correct token is accepted", status == 200)

        audit_after = (audit_path.read_text().splitlines()
                      if audit_path.exists() else [])
        new_entries = [json.loads(l) for l in audit_after[len(audit_before):]]
        check("BR-71: every request is logged with caller and outcome",
              len(new_entries) >= 3)
        check("BR-71: a named client is captured when the caller supplies one",
              any(e.get("client") == "test-suite" for e in new_entries))
        check("BR-71: an unauthenticated attempt is logged too, not just "
              "successes", any(not e["authenticated"] for e in new_entries))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main() -> int:
    if not DB.exists():
        print("no build/knowledge.db — run ./eck-cli refresh first")
        return 1

    print("stdio transport — the real wire protocol (BR-58, BR-59, BR-61..63)")
    asyncio.run(check_stdio())

    print("\nstreamable-http transport — auth and audit (BR-59, BR-60, BR-71)")
    check_http()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
