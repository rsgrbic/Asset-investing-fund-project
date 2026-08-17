import pytest

from _loader import auth_app

_is_valid_email = auth_app._is_valid_email
_is_valid_password = auth_app._is_valid_password
_get_str = auth_app._get_str


@pytest.mark.parametrize("email", [
    "director@example.com",
    "a.b+tag@sub.domain.co.uk",
    "user123@gmail.com",
])
def test_valid_emails(email):
    assert _is_valid_email(email)


@pytest.mark.parametrize("email", [
    "",
    "no-at-sign",
    "@example.com",
    "user@",
    "user@localhost",   # single-label domain, no TLD
    "user@example.c",   # TLD shorter than 2 chars
    "user @example.com",
])
def test_invalid_emails(email):
    assert not _is_valid_email(email)


@pytest.mark.parametrize("length,expected", [
    (0, False), (7, False), (8, True), (100, True), (256, True), (257, False),
])
def test_password_length_boundaries(length, expected):
    assert _is_valid_password("x" * length) is expected


def test_get_str_returns_value():
    assert _get_str({"name": "value"}, "name") == "value"


@pytest.mark.parametrize("data,key", [
    ({}, "missing"),
    ({"name": ""}, "name"),        # empty string is rejected
    ({"name": 42}, "name"),        # wrong type
    ({"name": None}, "name"),
    ({"name": ["a"]}, "name"),
    ("not-a-dict", "name"),
    (None, "name"),
])
def test_get_str_returns_none(data, key):
    assert _get_str(data, key) is None
