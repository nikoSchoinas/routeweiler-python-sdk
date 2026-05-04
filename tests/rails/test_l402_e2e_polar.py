"""Live L402 end-to-end test against a local Polar regtest network.

Requires:
    - Polar running locally with at least two LND nodes.
    - The following environment variables (set in tests/.env or shell):
        POLAR_LND_HOST         gRPC endpoint for the payer node (host:port)
        POLAR_LND_TLS_CERT     Path to payer node TLS cert
        POLAR_LND_MACAROON     Path to payer node admin.macaroon
        POLAR_MERCHANT_HOST    gRPC endpoint for the merchant/server LND node
        POLAR_MERCHANT_TLS_CERT
        POLAR_MERCHANT_MACAROON
    - Both nodes funded and a channel open between them.

Gated behind --run-live (same mechanism as x402 CDP live tests).

How to run:
    hatch run test-live tests/rails/test_l402_e2e_polar.py

See README for Polar setup instructions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routewiler import Routewiler
from routewiler.budgets.fmv_provider import CoinGeckoProvider
from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.local import BudgetStore, ensure_default_envelope
from routewiler.funding.lightning import LightningFundingSource, LndClient
from routewiler.trace.sink_sqlite import TraceSink

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def polar_payer_client() -> LndClient:
    """Construct an LndClient pointing at the Polar payer node."""
    host = os.environ.get("POLAR_LND_HOST", "localhost:10001")
    tls_cert = os.environ.get("POLAR_LND_TLS_CERT")
    macaroon = os.environ.get("POLAR_LND_MACAROON")

    grpc_host, _, grpc_port = host.rpartition(":")
    return LndClient(
        grpc_host=grpc_host or "localhost",
        grpc_port=int(grpc_port or 10001),
        tls_cert_path=tls_cert,
        macaroon_path=macaroon,
    )


@pytest.fixture(scope="module")
def polar_merchant_client() -> LndClient:
    """Construct an LndClient pointing at the Polar merchant node."""
    host = os.environ.get("POLAR_MERCHANT_HOST", "localhost:10002")
    tls_cert = os.environ.get("POLAR_MERCHANT_TLS_CERT")
    macaroon = os.environ.get("POLAR_MERCHANT_MACAROON")

    grpc_host, _, grpc_port = host.rpartition(":")
    return LndClient(
        grpc_host=grpc_host or "localhost",
        grpc_port=int(grpc_port or 10002),
        tls_cert_path=tls_cert,
        macaroon_path=macaroon,
    )


@pytest.fixture
async def polar_payer_source(polar_payer_client: LndClient) -> LightningFundingSource:
    """LightningFundingSource for the payer node on regtest."""
    return await LightningFundingSource.create(
        polar_payer_client,
        "bitcoin-regtest",
    )


@pytest.fixture
def polar_l402_server(polar_merchant_client: LndClient) -> httpx.ASGITransport:
    """A minimal in-process L402 server that issues real invoices via the merchant LND."""
    client = polar_merchant_client

    async def protected(request: Request) -> Response:  # type: ignore[return]
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.lower().startswith("l402 "):

            def _add_invoice() -> tuple[str, str]:
                invoice_response = client._make_client().add_invoice(  # type: ignore[attr-defined]
                    value=1,
                    memo="Routewiler Polar live test",
                )
                payment_hash_hex = invoice_response.r_hash.hex()
                bolt11 = invoice_response.payment_request
                return payment_hash_hex, bolt11

            payment_hash_hex, bolt11 = await asyncio.to_thread(_add_invoice)
            macaroon_b64 = base64.b64encode(
                b"polar-test-macaroon-" + payment_hash_hex.encode()
            ).decode()

            return Response(
                content=b"payment required",
                status_code=402,
                headers={
                    "WWW-Authenticate": (f'L402 macaroon="{macaroon_b64}", invoice="{bolt11}"')
                },
            )

        try:
            _, cred = auth_header.split(" ", 1)
            _mac, preimage_hex = cred.rsplit(":", 1)
            preimage_bytes = bytes.fromhex(preimage_hex)
            actual_hash = hashlib.sha256(preimage_bytes).hexdigest()

            def _check_payment() -> bool:
                invoice = client._make_client().lookup_invoice(actual_hash)  # type: ignore[attr-defined]
                _lnd_settled = 1
                return invoice.state == _lnd_settled  # type: ignore[attr-defined]

            paid = await asyncio.to_thread(_check_payment)
            if not paid:
                return Response(b"payment not found", status_code=402)
        except Exception as exc:
            return Response(f"error: {exc}".encode(), status_code=401)

        return JSONResponse({"result": "ok", "rail": "l402", "live": True})

    async def free(request: Request) -> Response:
        return JSONResponse({"free": True})

    app = Starlette(routes=[Route("/protected", protected), Route("/free", free)])
    return httpx.ASGITransport(app=app)  # type: ignore[arg-type]


class TestL402LivePolar:
    async def test_real_lightning_payment_succeeds(
        self,
        polar_payer_source: LightningFundingSource,
        polar_l402_server: httpx.ASGITransport,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        db_path = tmp_path / "polar-test.db"  # type: ignore[operator]
        sink = TraceSink.sqlite(db_path, url_mode="raw")
        client = Routewiler(funding=[polar_payer_source], trace_sink=sink)
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=polar_l402_server,
        )

        response = await client.get("http://polar-mock/protected")
        await client.aclose()

        assert response.status_code == 200
        data = response.json()
        assert data["rail"] == "l402"
        assert data["live"] is True

    async def test_real_lightning_payment_draws_usd_envelope(
        self,
        polar_payer_source: LightningFundingSource,
        polar_l402_server: httpx.ASGITransport,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        """Live L402 payment draws from a USD envelope, verifying FMV budget enforcement.

        The sats→USD rate is read from ROUTEWILER_TEST_SATS_USD if set (e.g.
        ``ROUTEWILER_TEST_SATS_USD=0.00065``), otherwise fetched live from CoinGecko.
        """
        db_path: Path = tmp_path / "polar-usd-test.db"  # type: ignore[operator]
        keystore_root: Path = tmp_path / "polar-usd-keys"  # type: ignore[operator]
        keystore = EnvelopeKeystore(root=keystore_root)
        ensure_default_envelope(db_path, keystore)

        # Use an env-override rate if provided so CI doesn't need a live CoinGecko call;
        # fall back to real CoinGecko for manual Polar runs.
        env_rate = os.environ.get("ROUTEWILER_TEST_SATS_USD")

        class _StaticProvider:
            async def fetch_btc_to(self, currency: str) -> Decimal:
                return Decimal(env_rate)  # type: ignore[arg-type]

        fmv_provider = _StaticProvider() if env_rate else CoinGeckoProvider()

        setup_store = BudgetStore(db_path, keystore, fmv_provider=fmv_provider)
        await setup_store.create_envelope(
            "polar-usd",
            cap_minor_units=1_000_000,  # $10 000.00
            cap_currency="usd",
            allowed_rails=["l402"],
            ttl_seconds=3600,
        )
        await setup_store.aclose()

        sink = TraceSink.sqlite(db_path, url_mode="raw")
        client = Routewiler(
            funding=[polar_payer_source],
            trace_sink=sink,
            budget_envelope="polar-usd",
            keystore_root=keystore_root,
        )
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=polar_l402_server,
        )

        response = await client.get("http://polar-mock/protected")
        await client.aclose()

        assert response.status_code == 200

        # Verify the draw row was created with a positive USD amount.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        draws = conn.execute("SELECT * FROM draws WHERE envelope_id = 'polar-usd'").fetchall()
        conn.close()
        assert len(draws) == 1
        assert draws[0]["state"] == "settled"
        assert draws[0]["amount_reserved_minor_units"] > 0

        # Verify the trace event records a non-null amount_envelope with coingecko_simple quality.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        traces = conn.execute("SELECT * FROM trace_events WHERE selected_rail = 'l402'").fetchall()
        conn.close()
        assert len(traces) == 1
        payload = json.loads(traces[0]["payload"])
        assert payload["payment"]["fmvQuality"] == "coingecko_simple"
        assert payload["payment"]["amountEnvelope"] is not None
        assert payload["payment"]["amountEnvelopeCurrency"] == "usd"
