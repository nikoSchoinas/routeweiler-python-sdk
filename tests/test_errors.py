"""Tests for the Routeweiler exception hierarchy."""

import pytest

from routeweiler.errors import (
    BudgetError,
    BudgetExceededError,
    ChallengeExpiredError,
    ChallengeParseError,
    CredentialError,
    EnvelopeExpiredError,
    InvoicePaymentError,
    KeystoreError,
    MppChargeFailedError,
    MppReceiptVerificationError,
    NoFeasibleRailError,
    NoFundingForRailError,
    PaymentError,
    PolicyDeniedError,
    PolicyError,
    RailExecutionError,
    RailNotSupportedError,
    RailParsingError,
    ReceiptVerificationError,
    RouteweilerError,
    SigningError,
    SptCreationError,
)


def test_hierarchy():
    assert issubclass(PaymentError, RouteweilerError)
    assert issubclass(RailNotSupportedError, PaymentError)
    assert issubclass(ChallengeParseError, PaymentError)
    assert issubclass(SigningError, PaymentError)
    assert issubclass(NoFundingForRailError, PaymentError)


def test_intermediate_base_classes():
    # Rail parsing
    assert issubclass(RailParsingError, PaymentError)
    assert issubclass(ChallengeParseError, RailParsingError)
    assert issubclass(ChallengeExpiredError, RailParsingError)
    # ChallengeExpiredError is no longer a ChallengeParseError subclass
    assert not issubclass(ChallengeExpiredError, ChallengeParseError)

    # Rail execution
    assert issubclass(RailExecutionError, PaymentError)
    assert issubclass(SigningError, RailExecutionError)
    assert issubclass(InvoicePaymentError, RailExecutionError)
    assert issubclass(SptCreationError, RailExecutionError)
    assert issubclass(MppChargeFailedError, RailExecutionError)
    assert issubclass(MppReceiptVerificationError, RailExecutionError)

    # Budget
    assert issubclass(BudgetError, PaymentError)
    assert issubclass(BudgetExceededError, BudgetError)
    assert issubclass(EnvelopeExpiredError, BudgetError)

    # Policy
    assert issubclass(PolicyError, PaymentError)
    assert issubclass(PolicyDeniedError, PolicyError)
    assert issubclass(NoFeasibleRailError, PolicyError)

    # Credentials
    assert issubclass(CredentialError, PaymentError)

    # Keystore (moved under PaymentError)
    assert issubclass(KeystoreError, PaymentError)

    # Receipt verification (moved under PaymentError)
    assert issubclass(ReceiptVerificationError, PaymentError)


def test_all_are_exceptions():
    for cls in (
        RouteweilerError,
        PaymentError,
        RailNotSupportedError,
        ChallengeParseError,
        SigningError,
        NoFundingForRailError,
    ):
        assert issubclass(cls, Exception)


def test_raise_and_catch_base():
    with pytest.raises(RouteweilerError):
        raise ChallengeParseError("bad header")


def test_message_propagates():
    exc = NoFundingForRailError("no match for network=base")
    assert "no match for network=base" in str(exc)
