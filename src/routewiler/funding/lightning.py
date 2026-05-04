"""Lightning funding source — wraps an LND gRPC client for L402 payments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class LightningNodeClient(Protocol):
    """Minimum interface any Lightning node client must implement.

    Concrete implementations: LndClient (gRPC), FakeLndClient (tests).
    The protocol is intentionally narrow — only the two operations L402 needs.
    """

    async def pay_invoice(self, bolt11: str, *, max_fee_msat: int) -> bytes:
        """Pay a BOLT-11 invoice, returning the 32-byte payment preimage.

        Raises InvoicePaymentError on any terminal failure (no_route, expired,
        insufficient_balance, etc.).
        """
        ...

    async def get_node_pubkey(self) -> str:
        """Return the hex-encoded 33-byte compressed pubkey of this node."""
        ...


@dataclass(frozen=True)
class LightningFundingSource:
    """A Lightning node client plus the network it operates on.

    `client` must satisfy the LightningNodeClient protocol.  Pass an
    LndClient for real usage, or a FakeLndClient in tests.

    `network` must match the BOLT-11 HRP prefix of invoices this source
    can pay:
        "bitcoin"          → lnbc...
        "bitcoin-testnet"  → lntb...
        "bitcoin-regtest"  → lnbcrt...
        "bitcoin-signet"   → lntbs...

    `node_pubkey` is populated at construction time (via `get_node_pubkey`)
    and used in trace events for audit.  Pass it explicitly to avoid an
    extra async round-trip when constructing in sync context, or use the
    `create()` factory which awaits it automatically.

    `max_fee_msat` is the per-payment fee cap passed to the node.  Can be
    overridden per-call in LightningFundingSource.pay_invoice().
    """

    client: LightningNodeClient
    network: Literal["bitcoin", "bitcoin-testnet", "bitcoin-regtest", "bitcoin-signet"]
    node_pubkey: str
    max_fee_msat: int = 1000

    @classmethod
    async def create(
        cls,
        client: LightningNodeClient,
        network: Literal["bitcoin", "bitcoin-testnet", "bitcoin-regtest", "bitcoin-signet"],
        *,
        max_fee_msat: int = 1000,
    ) -> LightningFundingSource:
        """Async factory that populates node_pubkey from the client."""
        pubkey = await client.get_node_pubkey()
        return cls(client=client, network=network, node_pubkey=pubkey, max_fee_msat=max_fee_msat)

    async def pay_invoice(self, bolt11: str, *, max_fee_msat: int | None = None) -> bytes:
        """Delegate to the underlying client, applying the per-source fee cap."""
        fee = max_fee_msat if max_fee_msat is not None else self.max_fee_msat
        return await self.client.pay_invoice(bolt11, max_fee_msat=fee)


# LND PaymentStatus enum values (routerrpc.PaymentStatus)
_LND_STATUS_SUCCEEDED = 2
_LND_STATUS_FAILED = 3

# ---------------------------------------------------------------------------
# LndClient — concrete implementation via lnd-grpc-client
# ---------------------------------------------------------------------------


@dataclass
class LndClient:
    """Async-compatible LND gRPC client.

    Wraps `lnd_grpc.Client` (synchronous) in `asyncio.to_thread` so the
    blocking gRPC call does not stall the event loop.

    Attributes:
        grpc_host:        Hostname or IP of the LND node.
        grpc_port:        gRPC port (default 10009).
        macaroon_path:    Path to the `admin.macaroon` file, OR None if
                          `macaroon_hex` is provided.
        macaroon_hex:     Hex-encoded macaroon bytes (alternative to path).
        tls_cert_path:    Path to the LND TLS certificate, OR None if
                          `tls_cert_pem` is provided.
        tls_cert_pem:     PEM-encoded TLS certificate string (alternative).
        timeout_seconds:  How long to wait for a payment before giving up.
    """

    grpc_host: str
    grpc_port: int = 10009
    macaroon_path: str | None = None
    macaroon_hex: str | None = None
    tls_cert_path: str | None = None
    tls_cert_pem: str | None = None
    timeout_seconds: int = 60

    def _make_client(self) -> object:
        """Construct an lndgrpc.LNDClient lazily (imported inside to avoid hard dep at module load).

        The lazy import keeps lnd-grpc-client optional at module load time.
        """
        try:
            import lndgrpc  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "lnd-grpc-client is required for L402 payments. "
                "Install it with: pip install lnd-grpc-client"
            ) from exc

        kwargs: dict[str, object] = {
            "ip_address": f"{self.grpc_host}:{self.grpc_port}",
        }
        if self.macaroon_path is not None:
            kwargs["macaroon_filepath"] = self.macaroon_path
        elif self.macaroon_hex is not None:
            kwargs["macaroon"] = bytes.fromhex(self.macaroon_hex)

        if self.tls_cert_path is not None:
            kwargs["cert_filepath"] = self.tls_cert_path
        elif self.tls_cert_pem is not None:
            kwargs["cert"] = self.tls_cert_pem.encode() if isinstance(self.tls_cert_pem, str) else self.tls_cert_pem

        return lndgrpc.LNDClient(**kwargs)

    async def pay_invoice(self, bolt11: str, *, max_fee_msat: int) -> bytes:
        """Pay the invoice via LND's routerrpc.SendPaymentV2 (streaming RPC).

        Iterates the server-streaming response until a terminal status
        (SUCCEEDED or FAILED) is received.

        Returns:
            32-byte payment preimage on success.

        Raises:
            InvoicePaymentError: if LND returns a FAILED terminal status.
        """
        from routewiler.errors import InvoicePaymentError  # noqa: PLC0415

        client = self._make_client()

        def _send() -> bytes:
            payments = client.send_payment_v2(  # type: ignore[attr-defined]
                payment_request=bolt11,
                fee_limit_msat=max_fee_msat,
                timeout_seconds=self.timeout_seconds,
                allow_self_payment=False,
            )
            for payment in payments:
                status = payment.status
                if status == _LND_STATUS_SUCCEEDED:
                    raw = payment.payment_preimage
                    if isinstance(raw, str):
                        return bytes.fromhex(raw)
                    return bytes(raw)
                if status == _LND_STATUS_FAILED:
                    reason = getattr(payment, "failure_reason", "unknown")
                    raise InvoicePaymentError(
                        f"LND payment failed: {reason} (invoice={bolt11[:40]}...)"
                    )
            raise InvoicePaymentError(
                f"LND stream ended without a terminal status (invoice={bolt11[:40]}...)"
            )

        return await asyncio.to_thread(_send)

    async def get_node_pubkey(self) -> str:
        """Return this node's 33-byte compressed pubkey as a hex string."""
        client = self._make_client()

        def _info() -> str:
            info = client.get_info()  # type: ignore[attr-defined]
            pubkey: str = info.identity_pubkey
            return pubkey

        return await asyncio.to_thread(_info)
