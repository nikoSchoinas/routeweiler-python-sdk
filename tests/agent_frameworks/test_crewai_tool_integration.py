"""CrewAI integration smoke test — BaseTool subclass wrapping Routeweiler.

Verifies that:
  1. A ``crewai.tools.BaseTool`` subclass with an async ``_run`` method and a
     ``Routeweiler`` field can be constructed without Pydantic or import errors.
  2. ``await tool._run(url=...)`` drives the 402 → pay → 200 flow end-to-end.
  3. A trace row and a settled draw row are written to SQLite.
  4. Exceptions raised inside ``_run`` propagate to the caller unchanged.

No LLM or Crew is involved.  The tool's ``_run`` method is invoked directly from
the test, mirroring how CrewAI's executor calls it (minus the sync/thread wrapper
that CrewAI adds in production, which is CrewAI's concern, not ours).

Run with:
    hatch run test-agent-frameworks tests/agent_frameworks/test_crewai_tool_integration.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict

pytest.importorskip("crewai")

from crewai.tools import BaseTool

from routeweiler import Routeweiler
from routeweiler.errors import PolicyDeniedError

from .conftest import draw_rows, trace_rows

pytestmark = pytest.mark.agent_frameworks


class FetchResourceTool(BaseTool):
    """CrewAI tool that fetches a 402-protected resource via Routeweiler."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "fetch_resource"
    description: str = "Fetch a 402-protected resource, handling payment automatically."
    rw: Routeweiler

    async def _run(self, url: str, **kwargs: Any) -> str:  # type: ignore[override]
        resp = await self.rw.get(url)
        return resp.text


async def test_crewai_tool_pays_402(
    fw_rw_context: tuple[Routeweiler, Path],
    merchant_url: str,
) -> None:
    """BaseTool._run successfully pays a 402 and returns the 200 body."""
    rw, db_path = fw_rw_context

    fetch_tool = FetchResourceTool(rw=rw)
    result = await fetch_tool._run(url=merchant_url)

    assert "ok" in result

    traces = trace_rows(db_path)
    assert len(traces) == 1
    assert traces[0]["selected_rail"] == "x402"

    draws = draw_rows(db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"


async def test_crewai_tool_exception_propagates(
    fw_rw_context: tuple[Routeweiler, Path],
) -> None:
    """Exceptions raised inside BaseTool._run propagate to the caller unchanged."""
    rw, _ = fw_rw_context

    class ExplodingTool(BaseTool):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        name: str = "exploding_tool"
        description: str = "Always raises PolicyDeniedError."
        rw: Routeweiler

        async def _run(self, url: str, **kwargs: Any) -> str:  # type: ignore[override]
            raise PolicyDeniedError("test: policy denied")

    tool = ExplodingTool(rw=rw)
    with pytest.raises(PolicyDeniedError, match="test: policy denied"):
        await tool._run(url="http://example.com")
