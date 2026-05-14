"""Shared pytest fixtures for the Routeweiler test suite."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Auto-load tests/.env if it exists (live test credentials).
# Uses python-dotenv when available; silently skips if not installed.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    _ENV_FILE = Path(__file__).parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ModuleNotFoundError:
    pass

from eth_account import Account
from eth_account.signers.local import LocalAccount

from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.credentials.recovery import CredentialRecoverer, NoOpRecoveryStrategy
from routeweiler.credentials.store import CredentialStore
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.trace.sink_sqlite import SqliteTraceSink, TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE
from tests.fixtures.l402_mock_server import mock_l402_app as _mock_l402_app
from tests.fixtures.x402_mock_server import mock_x402_app as _mock_x402_app

# ---------------------------------------------------------------------------
# Test private key — DETERMINISTIC TEST KEY — DO NOT FUND
# This key is public knowledge; never use it with real funds.
# ---------------------------------------------------------------------------
_TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


# ---------------------------------------------------------------------------
# --run-live option + marker skip
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.live (requires funded Base-Sepolia wallet).",
    )
    parser.addoption(
        "--run-agent-frameworks",
        action="store_true",
        default=False,
        help="Run @pytest.mark.agent_frameworks tests (included in the dev extra).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="Pass --run-live to run live rail tests.")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip_live)

    if not config.getoption("--run-agent-frameworks"):
        skip_af = pytest.mark.skip(
            reason="Pass --run-agent-frameworks to run agent-framework integration tests."
        )
        for item in items:
            if item.get_closest_marker("agent_frameworks"):
                item.add_marker(skip_af)


# ---------------------------------------------------------------------------
# Core account / funding fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_account() -> LocalAccount:
    """A deterministic LocalAccount for signing tests. DO NOT FUND."""
    return Account.from_key(_TEST_PRIVATE_KEY)


@pytest.fixture(scope="session")
def base_usdc_funding(test_account: LocalAccount) -> EvmFundingSource:
    return EvmFundingSource(wallet=test_account, network="base", asset="usdc")


@pytest.fixture(scope="session")
def base_sepolia_usdc_funding(test_account: LocalAccount) -> EvmFundingSource:
    return EvmFundingSource(wallet=test_account, network="base-sepolia", asset="usdc")


# ---------------------------------------------------------------------------
# Fixture loader helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "x402"


def load_challenge_fixture(name: str) -> dict:  # type: ignore[type-arg]
    """Load a JSON challenge fixture and return the decoded dict."""
    return json.loads((_FIXTURE_DIR / name).read_text())


def encode_challenge(data: dict) -> str:  # type: ignore[type-arg]
    """Base64-encode a challenge dict as the PAYMENT-REQUIRED header value."""
    return base64.b64encode(json.dumps(data).encode()).decode()


@pytest.fixture(scope="session")
def challenge_base_usdc_dict() -> dict:  # type: ignore[type-arg]
    return load_challenge_fixture("challenge_base_usdc.json")


@pytest.fixture(scope="session")
def challenge_multi_accept_dict() -> dict:  # type: ignore[type-arg]
    return load_challenge_fixture("challenge_multi_accept.json")


@pytest.fixture(scope="session")
def challenge_base_usdc_header(challenge_base_usdc_dict: dict) -> str:  # type: ignore[type-arg]
    return encode_challenge(challenge_base_usdc_dict)


@pytest.fixture(scope="session")
def challenge_multi_accept_header(challenge_multi_accept_dict: dict) -> str:  # type: ignore[type-arg]
    return encode_challenge(challenge_multi_accept_dict)


# ---------------------------------------------------------------------------
# Trace-sink + ASGI mock server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_trace_db_path(tmp_path: Path) -> Path:
    """Return a fresh SQLite DB path inside pytest's tmp_path."""
    return tmp_path / "test-traces.db"


@pytest.fixture
def tmp_trace_sink(tmp_trace_db_path: Path) -> SqliteTraceSink:
    """A SqliteTraceSink backed by a temp DB. Automatically closed after the test."""
    return TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")


@pytest.fixture
def mock_x402_app() -> httpx.ASGITransport:
    """httpx transport backed by the in-process mock x402 Starlette app."""
    return httpx.ASGITransport(app=_mock_x402_app)  # type: ignore[arg-type]


@pytest.fixture
def mock_l402_app() -> httpx.ASGITransport:
    """httpx transport backed by the in-process mock L402 Starlette app."""
    return httpx.ASGITransport(app=_mock_l402_app)  # type: ignore[arg-type]


@pytest.fixture
def lightning_funding() -> LightningFundingSource:
    """A LightningFundingSource wired to a FakeLndClient for unit tests."""
    return LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )


@pytest.fixture
def tmp_keystore(tmp_path: Path) -> EnvelopeKeystore:
    """An EnvelopeKeystore backed by a temp directory."""
    return EnvelopeKeystore(root=tmp_path / "keys")


@pytest.fixture
async def tmp_budget_store(
    tmp_trace_db_path: Path, tmp_keystore: EnvelopeKeystore
) -> AsyncGenerator[BudgetStore, None]:
    """A BudgetStore backed by a fresh temp DB, with the default envelope seeded."""
    store = BudgetStore(tmp_trace_db_path, tmp_keystore)
    yield store
    await store.aclose()


@pytest.fixture
async def tmp_credential_store(
    tmp_trace_db_path: Path,
) -> AsyncGenerator[CredentialStore, None]:
    """A CredentialStore backed by a fresh temp DB. Shares the path with trace/budget stores."""
    store = CredentialStore(tmp_trace_db_path)
    yield store
    await store.aclose()


@pytest.fixture
async def tmp_recoverer(
    tmp_credential_store: CredentialStore,
) -> CredentialRecoverer:
    """A CredentialRecoverer wired to NoOpRecoveryStrategy (no emitter)."""
    return CredentialRecoverer(
        store=tmp_credential_store,
        strategy=NoOpRecoveryStrategy(),
        emitter=None,
    )
