"""LangGraph integration smoke test — ToolNode invoking a Routeweiler-backed tool.

Verifies that:
  1. An async LangChain ``@tool`` wrapping ``Routeweiler.get`` can be registered
     with a LangGraph ``ToolNode`` without type or import errors.
  2. A compiled ``StateGraph`` containing a ``ToolNode`` drives the 402 → pay → 200 flow.
  3. The resulting ``ToolMessage`` carries the merchant's response body.
  4. A trace row and a settled draw row are written to SQLite.

No LLM is involved.  A synthetic ``AIMessage`` with a pre-built tool-call spec
is fed directly into the compiled graph, bypassing the model routing layer.
In LangGraph 1.x, ``ToolNode`` must run inside a compiled graph — standalone
``ToolNode.ainvoke`` is not supported.

Run with:
    hatch run test-agent-frameworks tests/agent_frameworks/test_langgraph_tool_integration.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from routeweiler import Routeweiler

from .conftest import draw_rows, trace_rows

pytestmark = pytest.mark.agent_frameworks


async def test_langgraph_tool_node_pays_402(
    fw_rw_context: tuple[Routeweiler, Path],
    merchant_url: str,
) -> None:
    """Compiled ToolNode graph successfully pays a 402 and returns the 200 body."""
    rw, db_path = fw_rw_context

    @tool
    async def fetch_resource(url: str) -> str:
        """Fetch a 402-protected resource via Routeweiler."""
        resp = await rw.get(url)
        return resp.text

    builder: StateGraph = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([fetch_resource]))
    builder.set_entry_point("tools")
    builder.set_finish_point("tools")
    graph = builder.compile()

    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-fw-1",
                "name": "fetch_resource",
                "args": {"url": merchant_url},
                "type": "tool_call",
            }
        ],
    )

    result = await graph.ainvoke({"messages": [ai_msg]})
    tool_msgs = result["messages"]
    assert any("ok" in m.content for m in tool_msgs)

    traces = trace_rows(db_path)
    assert len(traces) == 1
    assert traces[0]["selected_rail"] == "x402"

    draws = draw_rows(db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"


async def test_langgraph_tool_node_error_propagation(
    fw_rw_context: tuple[Routeweiler, Path],
) -> None:
    """ToolNode wraps a raising tool; error surfaces as ToolMessage content."""
    _rw, _ = fw_rw_context

    @tool
    async def always_fails(url: str) -> str:
        """Always raise to verify error propagation through ToolNode."""
        raise ValueError("intentional tool failure")

    builder: StateGraph = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([always_fails], handle_tool_errors=True))
    builder.set_entry_point("tools")
    builder.set_finish_point("tools")
    graph = builder.compile()

    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-fw-err",
                "name": "always_fails",
                "args": {"url": "http://example.com"},
                "type": "tool_call",
            }
        ],
    )

    result = await graph.ainvoke({"messages": [ai_msg]})
    tool_msgs = result["messages"]
    assert any("intentional tool failure" in m.content for m in tool_msgs)
