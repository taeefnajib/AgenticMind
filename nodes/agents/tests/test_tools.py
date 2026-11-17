import httpx
import pytest
from agenticmind_node_sdk import ToolHandle
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_agents import tools as T
from agenticmind_nodes_agents.tools import ToolError, safe_eval
from pydantic import ValidationError


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2+3*4", 14),
        ("(2+3)*4", 20),
        ("2**10", 1024),
        ("2^3", 8),
        ("sqrt(16) + abs(-2)", 6.0),
        ("round(10/3, 2)", 3.33),
        ("max(1, 7, 3)", 7),
        ("-5 % 3", 1),
        ("pi > 3", True),
    ],
)
def test_calculator_evaluates_arithmetic(expr, expected):
    if expr == "pi > 3":
        with pytest.raises(ToolError):  # comparisons are not arithmetic
            safe_eval(expr)
        return
    assert safe_eval(expr) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo hi')",
        "().__class__",
        "open('x')",
        "lambda: 1",
        "[1,2]",
        "9**9**9",
        "1/0",
        "x + 1",
        "a" * 301,
    ],
)
def test_calculator_rejects_anything_else(expr):
    with pytest.raises(ToolError):
        safe_eval(expr)


async def test_tool_nodes_emit_handles():
    r = await invoke_node(T.calculator_tool)
    handle = ToolHandle.model_validate(r.outputs["tool"])
    assert handle.tool_type == "calculator" and handle.name == "calculator"
    assert handle.parameters["required"] == ["expression"]
    r = await invoke_node(T.datetime_tool, config={"name": "clock"})
    assert r.outputs["tool"]["name"] == "clock"


async def test_datetime_tool():
    tool = ToolHandle.model_validate((await invoke_node(T.datetime_tool)).outputs["tool"])
    out = await T.TOOL_IMPLEMENTATIONS["datetime"](None, tool, {"timezone": "Asia/Dhaka"})
    assert out["timezone"] == "Asia/Dhaka" and out["iso"].endswith("+06:00")
    assert (await T.TOOL_IMPLEMENTATIONS["datetime"](None, tool, {}))["iso"].endswith("+00:00")
    with pytest.raises(ToolError):
        await T.TOOL_IMPLEMENTATIONS["datetime"](None, tool, {"timezone": "Mars/Olympus"})


def test_web_fetch_requires_explicit_domains():
    with pytest.raises(ValidationError):
        T.WebFetchConfig(allowed_domains=[])
    with pytest.raises(ValidationError):
        T.WebFetchConfig(allowed_domains=["https://example.org/path"])
    assert T.WebFetchConfig(allowed_domains=[" .Example.ORG "]).allowed_domains == ["example.org"]
    assert T._domain_allowed("docs.example.org", ["example.org"])
    assert not T._domain_allowed("example.org.evil.com", ["example.org"])
    assert not T._domain_allowed("notexample.org", ["example.org"])


async def _web_tool(domains):
    r = await invoke_node(T.web_fetch_tool, config={"allowed_domains": domains})
    return ToolHandle.model_validate(r.outputs["tool"])


async def test_web_fetch_blocks_private_and_unlisted_hosts():
    fetch = T.TOOL_IMPLEMENTATIONS["web_fetch"]
    tool = await _web_tool(["localhost", "example.org"])
    with pytest.raises(ToolError, match="non-public"):
        await fetch(None, tool, {"url": "http://localhost:8000/api/health"})
    with pytest.raises(ToolError, match="not an allowed domain"):
        await fetch(None, tool, {"url": "https://wikipedia.org/"})
    with pytest.raises(ToolError, match="http"):
        await fetch(None, tool, {"url": "file:///etc/passwd"})


async def test_web_fetch_reads_allowed_page_and_rechecks_redirects(monkeypatch):
    async def public(host, port):
        return None

    monkeypatch.setattr(T, "_assert_public_host", public)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://evil.com/steal"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><h1>Title</h1><p>Hello world</p><script>x()</script></body></html>",
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    tool = await _web_tool(["example.org"])
    out = await T.TOOL_IMPLEMENTATIONS["web_fetch"](None, tool, {"url": "https://example.org/page"})
    assert out["text"] == "# Title\n\nHello world" and out["truncated"] is False
    with pytest.raises(ToolError, match="evil.com"):
        await T.TOOL_IMPLEMENTATIONS["web_fetch"](None, tool, {"url": "https://example.org/moved"})
