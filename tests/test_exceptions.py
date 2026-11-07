"""Tests for the biomapper exception hierarchy."""

from __future__ import annotations

import pytest

from biomapper.exceptions import (
    BioMapperAuthError,
    BioMapperConfigError,
    BioMapperError,
    BioMapperRateLimitError,
    BioMapperServerError,
    BioMapperTimeoutError,
)

SUBCLASSES = [
    BioMapperAuthError,
    BioMapperConfigError,
    BioMapperRateLimitError,
    BioMapperServerError,
    BioMapperTimeoutError,
]


def _make(cls: type[BioMapperError], message: str = "boom") -> BioMapperError:
    if cls is BioMapperServerError:
        return cls(message, status_code=503)
    return cls(message)


def test_base_is_exception() -> None:
    assert issubclass(BioMapperError, Exception)


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_subclasses_base(cls: type[BioMapperError]) -> None:
    assert issubclass(cls, BioMapperError)


@pytest.mark.parametrize("cls", [BioMapperError, *SUBCLASSES])
def test_raised_and_caught_as_base(cls: type[BioMapperError]) -> None:
    with pytest.raises(BioMapperError) as excinfo:
        raise _make(cls, "something failed")
    assert type(excinfo.value) is cls
    assert str(excinfo.value) == "something failed"


def test_rate_limit_retry_after_defaults_to_none() -> None:
    err = BioMapperRateLimitError("slow down")
    assert err.retry_after is None
    assert str(err) == "slow down"


def test_rate_limit_keeps_retry_after_and_message() -> None:
    err = BioMapperRateLimitError("slow down", retry_after=2.5)
    assert err.retry_after == 2.5
    assert str(err) == "slow down"
    assert err.args == ("slow down",)


def test_server_error_keeps_status_code_and_message() -> None:
    err = BioMapperServerError("upstream exploded", status_code=502)
    assert err.status_code == 502
    assert str(err) == "upstream exploded"
    assert err.args == ("upstream exploded",)


def test_server_error_attrs_survive_catch_as_base() -> None:
    with pytest.raises(BioMapperError) as excinfo:
        raise BioMapperServerError("bad gateway", status_code=502)
    assert isinstance(excinfo.value, BioMapperServerError)
    assert excinfo.value.status_code == 502


def test_rate_limit_attrs_survive_catch_as_base() -> None:
    with pytest.raises(BioMapperError) as excinfo:
        raise BioMapperRateLimitError("429", retry_after=10)
    assert isinstance(excinfo.value, BioMapperRateLimitError)
    assert excinfo.value.retry_after == 10
