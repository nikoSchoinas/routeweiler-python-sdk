"""Routewiler exception hierarchy."""

from __future__ import annotations


class RoutewilerError(Exception):
    """Base for all Routewiler exceptions."""


class PaymentError(RoutewilerError):
    """Raised when a 402 payment flow cannot be completed."""


class RailNotSupportedError(PaymentError):
    """No registered adapter can handle the 402 challenge."""


class ChallengeParseError(PaymentError):
    """The PAYMENT-REQUIRED header could not be decoded or validated."""


class SigningError(PaymentError):
    """The rail adapter failed to produce a signed payment payload."""


class NoFundingForRailError(PaymentError):
    """None of the available funding sources match the server's accepted payment options."""


class BudgetExceededError(PaymentError):
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


class EnvelopeNotFoundError(PaymentError):
    """No envelope row matches the requested id."""


class EnvelopeFrozenError(PaymentError):
    """Envelope status is not 'active' (frozen or revoked)."""


class EnvelopeExpiredError(PaymentError):
    """Envelope expires_at is in the past."""


class KeystoreError(RoutewilerError):
    """Base for keystore failures."""


class KeystoreNotFoundError(KeystoreError):
    """No key file exists for the given envelope id."""


class KeystoreAlreadyExistsError(KeystoreError):
    """A key file already exists for the given envelope id; will not overwrite."""


class ReceiptVerificationError(RoutewilerError):
    """Ed25519 signature on a DrawReceipt is invalid or the payload was tampered with."""


class FmvUnavailableError(PaymentError):
    """No cached FMV rate is available for the required currency pair."""
