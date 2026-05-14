"""OpenAI Agents SDK integration smoke test — @function_tool wrapping Routeweiler.

Verifies that:
  1. A ``@function_tool``-decorated async function with a Routeweiler call is
     created without import or schema-generation errors.
  2. ``FunctionTool.on_invoke_tool`` drives the 402 → pay → 200 flow end-to-end.
  3. The tool returns a string (``response.text``), not a raw ``httpx.Response``,
     confirming the documented integration shape (Agents SDK expects serializable output).
  4. A trace row and a settled draw row are written to SQLite.

No LLM or Runner is involved.  ``on_invoke_tool`` is called directly with a
``ToolContext`` (the SDK's invocation context dataclass, a subclass of
``RunContextWrapper``) and a JSON-encoded argument string, mirroring what
``Runner.run`` does when the model selects this tool.

Run with:
    hatch run test-agent-frameworks tests/agent_frameworks/test_openai_agents_tool_integration.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("agents")

from agents import function_tool
from agents.tool_context import ToolContext

from routeweiler import Routeweiler

from .conftest import draw_rows, trace_rows

pytestmark = pytest.mark.agent_frameworks


async def test_openai_agents_function_tool_pays_402(
    fw_rw_context: tuple[Routeweiler, Path],
    merchant_url: str,
) -> None:
    """@function_tool successfully pays a 402 and returns the response body as a string."""
    rw, db_path = fw_rw_context

    @function_tool
    async def fetch_resource(url: str) -> str:
        """Fetch a 402-protected resource, handling payment automatically."""
        resp = await rw.get(url)
        return resp.text

    # Verify schema was generated correctly.
    schema = fetch_resource.params_json_schema
    assert "url" in schema.get("properties", {})

    args_json = json.dumps({"url": merchant_url})
    ctx = ToolContext(
        context=None,
        tool_name=fetch_resource.name,
        tool_call_id="test-call-1",
        tool_arguments=args_json,
    )
    result = await fetch_resource.on_invoke_tool(ctx, args_json)

    assert "ok" in result

    traces = trace_rows(db_path)
    assert len(traces) == 1
    assert traces[0]["selected_rail"] == "x402"

    draws = draw_rows(db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"


async def test_openai_agents_tool_name_and_description(
    fw_rw_context: tuple[Routeweiler, Path],
) -> None:
    """The @function_tool decorator infers name and description from the function."""
    rw, _ = fw_rw_context

    @function_tool
    async def fetch_resource(url: str) -> str:
        """Fetch a 402-protected resource, handling payment automatically."""
        resp = await rw.get(url)
        return resp.text

    assert fetch_resource.name == "fetch_resource"
    assert "402" in fetch_resource.description
