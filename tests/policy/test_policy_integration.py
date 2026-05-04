"""Integration tests: policy enforcement in the auth flow.

Verifies that `deny: true` and `max_per_call_minor_units` are enforced by
RoutewilerAuth before any budget draw is attempted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routewiler import Routewiler
from routewiler.errors import PolicyDeniedError, PolicyMaxPerCallExceededError
from routewiler.funding.evm import EvmFundingSource
from routewiler.policy.dsl import PolicyFile
from routewiler.trace.sink_sqlite import TraceSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draw_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    conn.close()
    return int(count)


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_client(
    test_account,  # type: ignore[no-untyped-def]
    transport: httpx.ASGITransport,
    db_path: Path,
    *,
    policy: PolicyFile | None = None,
) -> Routewiler:
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    with patch("routewiler.rails.x402.x402Client") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(
            return_value={
                "x402Version": 1,
                "payload": {
                    "authorization": {
                        "from": test_account.address,
                        "to": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                        "value": "1000",
                        "validAfter": "0",
                        "validBefore": "9999999999",
                        "nonce": "0xdeadbeef",
                    },
                    "signature": "0x" + "ab" * 65,
                },
            }
        )
        mock_cls.return_value = mock_instance

        client = Routewiler(
            funding=[EvmFundingSource(wallet=test_account, network="base-sepolia", asset="usdc")],
            trace_sink=sink,
            policy=policy,
        )
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=transport,
        )
    return client


# ---------------------------------------------------------------------------
# deny: true enforcement
# ---------------------------------------------------------------------------


async def test_deny_rule_blocks_payment_and_emits_error_trace(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """A deny rule raises PolicyDeniedError; no draw is inserted."""
    policy_path = tmp_path / "deny_policy.yaml"
    policy_path.write_text("""
version: 1
default:
  rail: x402
rules:
  - name: deny-mock
    when:
      url_matches: "http://mock/*"
    deny: true
    reason: "test deny"
""")
    policy = PolicyFile(policy_path)
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, policy=policy)

    with pytest.raises(PolicyDeniedError) as exc_info:
        await client.get("http://mock/protected")
    await client.aclose()

    assert "test deny" in str(exc_info.value)

    # No draw should have been attempted.
    assert _draw_count(tmp_trace_db_path) == 0

    # An error trace must have been emitted.
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["http_status"] == 402
    assert rows[0]["service_delivered"] == 0


# ---------------------------------------------------------------------------
# max_per_call_minor_units enforcement
# ---------------------------------------------------------------------------


async def test_max_per_call_blocks_oversized_payment(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """max_per_call_minor_units=1 rejects the 1000-unit USDC challenge."""
    policy_path = tmp_path / "max_policy.yaml"
    policy_path.write_text("""
version: 1
default:
  rail: x402
rules:
  - name: tiny-cap
    when:
      url_matches: "http://mock/*"
    max_per_call_minor_units: 0
""")
    policy = PolicyFile(policy_path)
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, policy=policy)

    with pytest.raises(PolicyMaxPerCallExceededError) as exc_info:
        await client.get("http://mock/protected")
    await client.aclose()

    # The mock challenge is 1000 USDC base units = 0.001 USDC = 1 USD cent (ceiling).
    assert exc_info.value.limit == 0
    assert exc_info.value.requested > 0

    # No draw inserted.
    assert _draw_count(tmp_trace_db_path) == 0

    # Error trace emitted.
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["service_delivered"] == 0


# ---------------------------------------------------------------------------
# policy_hash in trace
# ---------------------------------------------------------------------------


async def test_policy_hash_in_trace_matches_file(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """The policy_hash in the emitted trace matches the loaded file's hash."""
    policy_path = tmp_path / "hash_policy.yaml"
    policy_path.write_text("""
version: 1
default:
  rail: x402
""")
    policy = PolicyFile(policy_path)
    expected_hash = policy.policy_hash
    assert expected_hash.startswith("sha256:")

    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, policy=policy)
    await client.get("http://mock/protected")
    await client.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["policyHash"] == expected_hash
