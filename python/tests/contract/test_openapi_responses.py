"""Round-trip validation that captured fixtures match the generated models.

Two layers:

1. **Fixture validation** (default, runs in CI): every JSON fixture under
   `python/tests/fixtures/openapi/` is parsed through its corresponding
   generated Pydantic model. This catches drift when someone edits
   `openapi.yaml` without re-capturing fixtures, or vice versa.

2. **Live-server validation** (opt-in via `TBOT_OPENAPI_LIVE=1`): the same
   set of endpoints is hit against a running mod and each response is
   validated through the model. This catches drift when the C# server
   changes a response shape without updating the spec.

Run live: ``TBOT_OPENAPI_LIVE=1 pytest tests/contract/``
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest
import requests

from tbot.api import models
from tbot.api.client import TimberbotClient

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "openapi"


# operationId -> (Pydantic model, optional URL path for live validation)
RESPONSE_MAP: dict[str, tuple[type, str]] = {
    "ping": (models.PingResponse, "/api/ping"),
    "settlement": (models.SettlementName, "/api/settlement"),
    "summary": (models.Summary, "/api/summary"),
    "time": (models.Time, "/api/time"),
    "weather": (models.Weather, "/api/weather"),
    "population": (models.Population, "/api/population"),
    "resources": (models.Resources, "/api/resources"),
    "districts": (models.DistrictList, "/api/districts"),
    "distribution": (models.Distribution, "/api/distribution"),
    "science": (models.Science, "/api/science"),
    "wellbeing": (models.WellbeingReport, "/api/wellbeing"),
    "workhours": (models.WorkHours, "/api/workhours"),
    "speed": (models.Speed, "/api/speed"),
    "prefabs": (models.Prefabs, "/api/prefabs"),
    "power": (models.PowerNetworks, "/api/power"),
    "tiles": (models.Tiles, "/api/tiles"),
    "tree_clusters": (models.TreeClusters, "/api/tree_clusters"),
    "food_clusters": (models.FoodClusters, "/api/food_clusters"),
    "alerts": (models.Alerts, "/api/alerts"),
    "notifications": (models.Notifications, "/api/notifications"),
    "buildings": (models.BuildingList, "/api/buildings"),
    "beavers": (models.BeaverList, "/api/beavers"),
    "trees": (models.TreeList, "/api/trees"),
    "crops": (models.CropList, "/api/crops"),
    "gatherables": (models.GatherableList, "/api/gatherables"),
    "list_webhooks": (models.WebhookList, "/api/webhooks"),
}


def _load_fixture(op_id: str) -> Any:
    path = FIXTURES_DIR / f"{op_id}.json"
    if not path.exists():
        pytest.skip(f"no fixture captured for {op_id} (run python/scripts/capture_fixtures.py)")
    return json.loads(path.read_text())


@pytest.mark.parametrize("op_id", sorted(RESPONSE_MAP.keys()))
def test_fixture_validates_against_model(op_id: str) -> None:
    """The captured fixture for each GET op parses cleanly through its model."""
    model, _ = RESPONSE_MAP[op_id]
    data = _load_fixture(op_id)
    model.model_validate(data)


def test_every_get_operation_has_a_response_model_mapping() -> None:
    """Every GET op in the spec is covered by RESPONSE_MAP (so future ops can't slip)."""
    import yaml  # type: ignore[import-untyped]

    spec_path = pathlib.Path(__file__).resolve().parents[3] / "openapi.yaml"
    spec = yaml.safe_load(spec_path.read_text())
    get_ops: set[str] = set()
    for _path, methods in spec.get("paths", {}).items():
        get_op = methods.get("get") if isinstance(methods, dict) else None
        if get_op and "operationId" in get_op:
            get_ops.add(get_op["operationId"])
    missing = get_ops - set(RESPONSE_MAP)
    assert not missing, (
        f"GET operations missing from RESPONSE_MAP: {sorted(missing)} - "
        "add a (model, path) entry or capture a fixture."
    )


# ---------------------------------------------------------------------------
# Live-server variant (opt-in)
# ---------------------------------------------------------------------------

_LIVE = os.environ.get("TBOT_OPENAPI_LIVE") == "1"


@pytest.mark.skipif(not _LIVE, reason="set TBOT_OPENAPI_LIVE=1 to validate against a running mod")
@pytest.mark.integration
@pytest.mark.live_game
@pytest.mark.parametrize("op_id", sorted(RESPONSE_MAP.keys()))
def test_live_response_validates_against_model(op_id: str) -> None:
    """When TBOT_OPENAPI_LIVE=1, hit the mod and validate each live response."""
    # Mono/HttpListener under Wine rejects Host: 127.0.0.1; use "localhost" by default.
    host = os.environ.get("TBOT_HOST", "localhost")
    client = TimberbotClient(host=host, json_mode=True)
    if not client.ping():
        pytest.skip(f"mod not reachable at {client.url}")
    model, path = RESPONSE_MAP[op_id]
    params: dict[str, str | int] = {"format": "json"}
    if op_id in {"buildings", "beavers"}:
        params["detail"] = "full"
    resp = requests.get(f"{client.url}{path}", params=params, timeout=10)
    resp.raise_for_status()
    model.model_validate(resp.json())
