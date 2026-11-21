"""``/api/mcp``: exposed projects as MCP tools over Streamable HTTP, called by the MCP client nodes.

The API runs in a real uvicorn server (on a free local port) so the MCP client speaks actual HTTP to it.
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_runtime.secrets import MappingSecretResolver

mc = importlib.import_module("agenticmind_nodes_agents.mcp_client")

TOKEN = "fixture-mcp-token"


def greeter_doc(expose: bool) -> dict:
    return {
        "ir_version": "1.0",
        "project": {"id": "template", "name": "Greeter", "description": "Greets someone by name."},
        "nodes": [
            {"id": "name", "type": "agenticmind.core.manual_input", "version": "1.0.0",
             "config": {"key": "name", "value_type": "String", "required": True}},
            {"id": "hello", "type": "agenticmind.core.text_template", "version": "1.0.0",
             "config": {"template": "Hello {{ text }}!", "strict": True}},
            {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "greeting"}},
        ],
        "edges": [
            {"id": "e1", "kind": "data", "source": {"node": "name", "port": "value"},
             "target": {"node": "hello", "port": "text"}},
            {"id": "e2", "kind": "data", "source": {"node": "hello", "port": "text"},
             "target": {"node": "out", "port": "value"}},
        ],
        "metadata": {"mcp": {"expose": True, "tool_name": "say_hello"}} if expose else {},
    }  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_token=TOKEN,
    )
    app = create_app(settings, secrets=MappingSecretResolver({}))
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield base
    srv.should_exit = True
    thread.join(timeout=10)


def mcp_config(base: str, token: str | None = TOKEN) -> dict:
    cfg = {"transport": "http", "url": f"{base}/api/mcp"}
    if token:
        cfg["bearer_token"] = {"$secret": "MCP_TOKEN"}
    return cfg


async def test_exposed_projects_are_mcp_tools(server):
    hidden = httpx.post(f"{server}/api/projects", json={"name": "Hidden", "document": greeter_doc(False)})
    shown = httpx.post(f"{server}/api/projects", json={"name": "Greeter", "document": greeter_doc(True)})
    assert hidden.status_code == 201 and shown.status_code == 201
    secrets = {"MCP_TOKEN": TOKEN}
    listed = await invoke_node(mc.mcp_tools, config=mcp_config(server), secrets=secrets)
    [tool] = listed.outputs["catalog"]
    assert tool["name"] == "say_hello" and tool["parameters"]["required"] == ["name"]
    r = await invoke_node(mc.mcp_call, config={**mcp_config(server), "tool": "say_hello"},
                          inputs={"arguments": {"name": "Ada"}}, secrets=secrets)  # fmt: skip
    assert r.outputs["result"] == {"greeting": "Hello Ada!"}
    # the call was an ordinary, recorded run of the project
    runs = await asyncio.to_thread(httpx.get, f"{server}/api/projects/{shown.json()['id']}/runs")
    assert runs.json()[0]["outputs"] == {"greeting": "Hello Ada!"} and runs.json()[0]["status"] == "succeeded"


async def test_token_is_required_when_configured(server):
    with pytest.raises(NodeError) as exc:
        await invoke_node(mc.mcp_tools, config=mcp_config(server, token=None))
    assert exc.value.code == "MCP_CONNECTION_FAILED"
    wrong = await asyncio.to_thread(
        httpx.post, f"{server}/api/mcp", json={}, headers={"Authorization": "Bearer wrong"}
    )
    assert wrong.status_code == 401 and wrong.json()["detail"]["code"] == "MCP_TOKEN_REQUIRED"
    cross_site = await asyncio.to_thread(
        httpx.post, f"{server}/api/mcp", json={},
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
    )  # fmt: skip
    assert cross_site.status_code == 403
    # other API routes are unaffected by the MCP token
    assert (await asyncio.to_thread(httpx.get, f"{server}/api/projects")).status_code == 200
