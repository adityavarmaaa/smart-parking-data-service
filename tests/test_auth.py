import pytest
from fastapi import HTTPException

from app.auth import require_s2_api_key


def test_missing_api_key_rejected(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "test-only-key")

    with pytest.raises(HTTPException) as exc:
        require_s2_api_key(None)

    assert exc.value.status_code == 401


def test_invalid_api_key_rejected(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "test-only-key")

    with pytest.raises(HTTPException) as exc:
        require_s2_api_key("incorrect-key")

    assert exc.value.status_code == 401


def test_valid_api_key_accepted(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "test-only-key")

    assert require_s2_api_key("test-only-key") is None


def test_missing_server_configuration_returns_503(monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)

    with pytest.raises(HTTPException) as exc:
        require_s2_api_key("test-only-key")

    assert exc.value.status_code == 503
