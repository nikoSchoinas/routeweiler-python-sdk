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
