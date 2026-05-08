"""Routeweiler exception hierarchy. Full tree documented in CLAUDE.md."""

from __future__ import annotations


class RouteweilerError(Exception):
    """Base for all Routeweiler exceptions."""


class PaymentError(RouteweilerError):
    """Raised when a 402 payment flow cannot be completed."""


class RailParsingError(PaymentError):
    """Base for errors that occur while parsing a 402 challenge."""


class ChallengeParseError(RailParsingError):
    """The PAYMENT-REQUIRED header could not be decoded or validated."""


class ChallengeExpiredError(RailParsingError):
    """Rail challenge expired before the client could pay.

    Examples: BOLT-11 invoice expiry, L402 macaroon ``valid_until`` caveat,
    MPP challenge ``expires`` auth-param.
    """


class RailExecutionError(PaymentError):
    """Base for errors that occur while executing a payment."""


class SigningError(RailExecutionError):
    """The rail adapter failed to produce a signed payment payload."""


class InvoicePaymentError(RailExecutionError):
    """Lightning node returned a terminal payment failure (no_route, channel offline, etc.)."""


class PreimageMismatchError(RailExecutionError):
    """sha256(preimage) != invoice payment_hash; the node returned a corrupt preimage."""


class SptCreationError(RailExecutionError):
    """Stripe rejected or failed to create the Shared Payment Token.

    Raised when the Stripe API call in ``MppSptAdapter.pay()`` fails for any
    reason: network error, declined card, invalid customer or payment_method,
    expired payment method, Stripe API outage, etc.
    """


class MppChargeFailedError(RailExecutionError):
    """MPP server rejected the credential or payment did not settle.

    Returned when the server responds 402 with a Problem-Details body
    (``verification-failed``, ``payment-insufficient``, ``invalid-challenge``)
    or with a non-2xx status that lacks a ``Payment-Receipt`` header.
    """


class MppReceiptVerificationError(RailExecutionError):
    """The ``Payment-Receipt`` header is malformed or mismatches our credential.

    Raised when the receipt cannot be decoded, fails Pydantic validation, or
    the ``challengeId`` / ``method`` fields do not match what we sent.
    """


class BudgetError(PaymentError):
    """Base for budget envelope failures."""


class BudgetExceededError(BudgetError):
    """Drawing this amount would breach the envelope's flat cap."""

    def __init__(
        self,
        envelope_id: str,
        requested_minor_units: int,
        available_minor_units: int,
    ) -> None:
        super().__init__(
            f"Envelope '{envelope_id}': requested {requested_minor_units} minor units "
            f"but only {available_minor_units} available."
        )
        self.envelope_id = envelope_id
        self.requested_minor_units = requested_minor_units
        self.available_minor_units = available_minor_units


class EnvelopeNotFoundError(BudgetError):
    """No envelope row matches the requested id."""


class EnvelopeFrozenError(BudgetError):
    """Envelope status is not 'active' (frozen or revoked)."""


class EnvelopeExpiredError(BudgetError):
    """Envelope expires_at is in the past."""


class PolicyError(PaymentError):
    """Base for policy-engine failures."""


class PolicyDeniedError(PolicyError):
    """A policy rule with `deny: true` matched the challenge."""

    def __init__(self, reason: str | None = None, rule_name: str | None = None) -> None:
        detail = reason or rule_name or "policy denied this payment"
        super().__init__(detail)
        self.reason = reason
        self.rule_name = rule_name


class PolicyMaxPerCallExceededError(PolicyError):
    """The challenge amount exceeds the policy's `max_per_call_minor_units` limit."""

    def __init__(
        self,
        rule_name: str | None,
        requested: int,
        limit: int,
    ) -> None:
        super().__init__(
            f"Rule '{rule_name}': challenge amount {requested} minor units "
            f"exceeds max_per_call limit of {limit}."
        )
        self.rule_name = rule_name
        self.requested = requested
        self.limit = limit


class NoFeasibleRailError(PolicyError):
    """No rail remains after policy, funding, and failover filters are applied."""


class CredentialError(PaymentError):
    """Base for credential store failures."""


class CredentialNotFoundError(CredentialError):
    """No credential row matches the given id."""


class InvalidCredentialTransitionError(CredentialError):
    """Attempted state transition is not allowed by the credential state machine."""


class ManifestParseError(CredentialError):
    """A service-shape manifest YAML is malformed, fails schema validation, or contains an
    invalid id_extractor (unknown prefix, bad regex)."""


class KeystoreError(PaymentError):
    """Base for keystore failures."""


class KeystoreNotFoundError(KeystoreError):
    """No key file exists for the given envelope id."""


class KeystoreAlreadyExistsError(KeystoreError):
    """A key file already exists for the given envelope id; will not overwrite."""


class FmvUnavailableError(PaymentError):
    """No cached FMV rate is available for the required currency pair."""


class NoFundingForRailError(PaymentError):
    """None of the available funding sources match the server's accepted payment options."""


class RailNotSupportedError(PaymentError):
    """No registered adapter can handle the 402 challenge."""


class ReceiptVerificationError(PaymentError):
    """Ed25519 signature on a DrawReceipt is invalid or the payload was tampered with."""
