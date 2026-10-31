"""Fixtures for signed-in tests (§2). The token helper itself lives in tests/auth_helpers.py."""
from __future__ import annotations

import uuid

import pytest

from tests.auth_helpers import bearer, enable_signin
from tests.auth_helpers import make_token as _make_token


@pytest.fixture
def signin(monkeypatch):
    """Sign-in on for this test (KH_AUTH_ISSUER + local JWKS). Returns the JWKS fetch log."""
    return enable_signin(monkeypatch)


@pytest.fixture
def make_token():
    return _make_token


@pytest.fixture
def signed_client(signin, client):
    """TestClient whose lifespan ran with sign-in on."""
    return client


@pytest.fixture
def uid():
    """Short unique suffix so tests sharing the session DB do not collide."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
def as_user():
    """as_user(email, actor=None, **token_kwargs) -> request headers."""
    def _h(email, actor=None, **kw):
        return bearer(_make_token(email, **kw), actor=actor)
    return _h
