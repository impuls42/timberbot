"""Tests for the client-side OpenAPI version-mismatch warning."""
from __future__ import annotations

import warnings

import pytest

pytest.importorskip("pytest_httpserver")

from timberbot.__about__ import OPENAPI_VERSION  # noqa: E402
from timberbot.api.client import TimberbotClient  # noqa: E402


def _other_major() -> str:
    """Build a version string that disagrees on the major component."""
    major = int(OPENAPI_VERSION.split(".", 1)[0])
    return f"{major + 1}.0.0"


def test_ping_emits_warning_on_major_mismatch(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True, "openapiVersion": _other_major()}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        assert client.ping() is True
    matches = [w for w in caught if "OpenAPI version mismatch" in str(w.message)]
    assert len(matches) == 1


def test_ping_silent_on_matching_major(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True, "openapiVersion": OPENAPI_VERSION}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        client.ping()
    assert [w for w in caught if "OpenAPI version mismatch" in str(w.message)] == []


def test_ping_silent_on_missing_openapi_version_field(httpserver):
    """Older mods don't report a version; pinging them must not warn."""
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        client.ping()
    assert [w for w in caught if "OpenAPI version mismatch" in str(w.message)] == []


def test_ping_warns_only_once_per_client(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True, "openapiVersion": _other_major()}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        client.ping()
        client.ping()
        client.ping()
    matches = [w for w in caught if "OpenAPI version mismatch" in str(w.message)]
    assert len(matches) == 1


def test_ping_silent_on_garbage_openapi_version(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True, "openapiVersion": "not-a-version"}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        client.ping()
    assert [w for w in caught if "OpenAPI version mismatch" in str(w.message)] == []
