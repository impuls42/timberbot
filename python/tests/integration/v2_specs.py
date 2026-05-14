"""Test specifications for test_v2.py.

Each EndpointSpec declares the capabilities of an API endpoint. whether it
supports toon/json format, pagination, filtering, detail levels, etc. The test
harness uses these specs to auto-generate test cases: it knows which endpoints
to test with ?format=json, which to test with limit/offset, which to blast
with concurrent requests, and which to verify freshness guarantees on.

FreshnessScenarios declare how to test data freshness: mutate something in the
game (e.g. change speed), then verify the next GET reflects the change.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    path: str
    group: str
    supports_format: bool = False
    supports_pagination: bool = False
    supports_name_filter: bool = False
    supports_radius_filter: bool = False
    supports_detail_basic: bool = False
    supports_detail_full: bool = False
    supports_detail_id: bool = False
    projection_backed: bool = False
    compare_mode: str = "exact"
    concurrency_mode: str = "exact_stable"


@dataclass(frozen=True)
class FreshnessScenario:
    name: str
    kind: str
    discover_key: str = ""


ENDPOINT_SPECS = [
    EndpointSpec("ping", "/api/ping", "scalar"),
    EndpointSpec("settlement", "/api/settlement", "scalar"),
    EndpointSpec("population", "/api/population", "scalar"),
    EndpointSpec("time", "/api/time", "scalar"),
    EndpointSpec("weather", "/api/weather", "scalar"),
    EndpointSpec("workhours", "/api/workhours", "scalar"),
    EndpointSpec("speed", "/api/speed", "scalar"),
    EndpointSpec("prefabs", "/api/prefabs", "scalar"),
    EndpointSpec("summary", "/api/summary", "format", supports_format=True),
    EndpointSpec("resources", "/api/resources", "format", supports_format=True),
    EndpointSpec("districts", "/api/districts", "format", supports_format=True),
    EndpointSpec("distribution", "/api/distribution", "format", supports_format=True),
    EndpointSpec("science", "/api/science", "format", supports_format=True),
    EndpointSpec("wellbeing", "/api/wellbeing", "format", supports_format=True),
    EndpointSpec("power", "/api/power", "format", supports_format=True),
    EndpointSpec("tree_clusters", "/api/tree_clusters", "format", supports_format=True),
    EndpointSpec("food_clusters", "/api/food_clusters", "format", supports_format=True),
    EndpointSpec("alerts", "/api/alerts", "paged", supports_format=True, supports_pagination=True),
    EndpointSpec("notifications", "/api/notifications", "paged", supports_format=True, supports_pagination=True),
    EndpointSpec(
        "buildings", "/api/buildings", "detail_list",
        supports_format=True, supports_pagination=True, supports_name_filter=True,
        supports_radius_filter=True, supports_detail_basic=True, supports_detail_full=True,
        supports_detail_id=True, projection_backed=True, compare_mode="list", concurrency_mode="snapshot_stable",
    ),
    EndpointSpec(
        "beavers", "/api/beavers", "detail_list",
        supports_format=True, supports_pagination=True, supports_name_filter=True,
        supports_radius_filter=True, supports_detail_basic=True, supports_detail_full=True,
        supports_detail_id=True, projection_backed=True, compare_mode="list", concurrency_mode="snapshot_stable",
    ),
    EndpointSpec(
        "trees", "/api/trees", "list",
        supports_format=True, supports_pagination=True, supports_name_filter=True,
        supports_radius_filter=True, projection_backed=True, compare_mode="list", concurrency_mode="snapshot_stable",
    ),
    EndpointSpec(
        "crops", "/api/crops", "list",
        supports_format=True, supports_pagination=True, supports_name_filter=True,
        supports_radius_filter=True, projection_backed=True, compare_mode="list", concurrency_mode="snapshot_stable",
    ),
    EndpointSpec(
        "gatherables", "/api/gatherables", "list",
        supports_format=True, supports_pagination=True, supports_name_filter=True,
        supports_radius_filter=True, projection_backed=True, compare_mode="list", concurrency_mode="snapshot_stable",
    ),
]


FRESHNESS_SCENARIOS = [
    FreshnessScenario("pause_toggle", "pause_toggle", "pause_target"),
    FreshnessScenario("workers_change", "workers_change", "workers_target"),
    FreshnessScenario("place_demolish", "place_demolish", "placement_search"),
    FreshnessScenario("floodgate_change", "floodgate_change", "floodgate_target"),
    FreshnessScenario("recipe_change", "recipe_change", "recipe_target"),
    FreshnessScenario("clutch_change", "clutch_change", "clutch_target"),
]


ENDPOINT_MAP = {spec.name: spec for spec in ENDPOINT_SPECS}
GROUP_NAMES = sorted({spec.group for spec in ENDPOINT_SPECS})
