"""Tests for the Routewiler exception hierarchy."""

import pytest

from routewiler.errors import (
    ChallengeParseError,
    NoFundingForRailError,
    PaymentError,
    RailNotSupportedError,
    RoutewilerError,
    SigningError,
)


def test_hierarchy():
    assert issubclass(PaymentError, RoutewilerError)
    assert issubclass(RailNotSupportedError, PaymentError)
    assert issubclass(ChallengeParseError, PaymentError)
    assert issubclass(SigningError, PaymentError)
    assert issubclass(NoFundingForRailError, PaymentError)


def test_all_are_exceptions():
    for cls in (
        RoutewilerError,
        PaymentError,
        RailNotSupportedError,
        ChallengeParseError,
        SigningError,
        NoFundingForRailError,
    ):
        assert issubclass(cls, Exception)


def test_raise_and_catch_base():
    with pytest.raises(RoutewilerError):
        raise ChallengeParseError("bad header")


def test_message_propagates():
    exc = NoFundingForRailError("no match for network=base")
    assert "no match for network=base" in str(exc)
