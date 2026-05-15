"""OpenAPI spec validity + TimberbotClient method-coverage tests.

The C# side already enforces 1:1 route coverage between TimberbotHttpServer.cs
and openapi.yaml (see OpenApiContractTests). This file enforces the Python-side
half: spec validates, operationIds are unique and non-empty, and every
operationId has a corresponding `TimberbotClient` method.

Response-shape validation against a live game is a separate file scheduled for
a follow-up commit in PR 3, once we capture real fixtures.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import timberbot
from timberbot.api.client import TimberbotClient

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "openapi.yaml"

# Some spec operations cover multiple convenience client methods (e.g.
# pause_building also exposes unpause_building, mark_trees also exposes
# clear_trees). The operationId names the canonical method; we accept any of
# its aliases as proof of coverage.
_OPERATION_ID_ALIASES: dict[str, list[str]] = {
    "pause_building": ["pause_building", "unpause_building"],
    "mark_trees": ["mark_trees", "clear_trees"],
    "register_webhook": ["register_webhook"],
    "unregister_webhook": ["unregister_webhook"],
}


@pytest.fixture(scope="module")
def spec() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _all_operations(spec: dict):
    """Yield (path, method, operation) for every documented operation."""
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method in {"get", "post", "put", "delete", "patch"}:
                yield path, method, op


def test_spec_file_exists():
    assert OPENAPI_PATH.is_file(), f"openapi.yaml not found at {OPENAPI_PATH}"


def test_spec_validates_against_openapi_3(spec):
    from openapi_spec_validator import validate

    # `validate` raises on failure; just calling it is the assertion.
    validate(spec)


def test_spec_version_matches_about_constant(spec):
    """The spec's `info.version` must equal `timberbot.OPENAPI_VERSION`."""
    assert spec["info"]["version"] == timberbot.OPENAPI_VERSION, (
        f"spec info.version={spec['info']['version']!r} but "
        f"timberbot.OPENAPI_VERSION={timberbot.OPENAPI_VERSION!r}. Bump them together."
    )


def test_every_operation_has_an_operation_id(spec):
    missing = [
        f"{method.upper()} {path}"
        for path, method, op in _all_operations(spec)
        if not op.get("operationId")
    ]
    assert not missing, "Operations missing operationId:\n  " + "\n  ".join(missing)


def test_operation_ids_are_unique(spec):
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for path, method, op in _all_operations(spec):
        op_id = op.get("operationId")
        if op_id in seen:
            dupes.append(f"{op_id} ({seen[op_id]} and {method.upper()} {path})")
        else:
            seen[op_id] = f"{method.upper()} {path}"
    assert not dupes, "Duplicate operationIds:\n  " + "\n  ".join(dupes)


def test_every_operation_id_has_a_client_method(spec):
    """Each spec operationId must map to a public method on TimberbotClient."""
    missing: list[str] = []
    for _path, _method, op in _all_operations(spec):
        op_id = op["operationId"]
        candidates = _OPERATION_ID_ALIASES.get(op_id, [op_id])
        if not any(
            hasattr(TimberbotClient, m) and callable(getattr(TimberbotClient, m))
            for m in candidates
        ):
            missing.append(f"{op_id} (looked for: {', '.join(candidates)})")
    assert not missing, (
        "Spec operationIds with no matching TimberbotClient method:\n  "
        + "\n  ".join(missing)
        + "\n\nEither add the method to the client or add an alias in "
        "_OPERATION_ID_ALIASES."
    )


def test_every_post_operation_declares_a_request_body(spec):
    """POST operations always carry a JSON body; missing requestBody is a smell."""
    missing: list[str] = []
    for path, method, op in _all_operations(spec):
        if method != "post":
            continue
        if "requestBody" not in op:
            missing.append(f"POST {path}")
    assert not missing, "POST operations without requestBody:\n  " + "\n  ".join(missing)


def test_request_bodies_have_application_json_schema(spec):
    for path, method, op in _all_operations(spec):
        if method != "post":
            continue
        rb = op.get("requestBody")
        if not rb:
            continue
        media = rb.get("content", {}).get("application/json")
        assert media is not None, f"POST {path} requestBody missing application/json content"
        assert "schema" in media, f"POST {path} requestBody.application/json missing schema"


def test_every_operation_has_a_200_response(spec):
    """Every operation must document at least a 200 success."""
    missing: list[str] = []
    for path, method, op in _all_operations(spec):
        responses = op.get("responses", {})
        if "200" not in responses:
            missing.append(f"{method.upper()} {path}")
    assert not missing, "Operations missing 200 response:\n  " + "\n  ".join(missing)
