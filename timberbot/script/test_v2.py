#!/usr/bin/env python
"""Primary test harness for the Timberbot API.

Validates the live /api/* surface against a running game. Requires Timberborn
running with the Timberbot mod loaded. Works on any save game, any faction.

Test modes:
    smoke       . hit every GET endpoint, verify shapes and status codes
    freshness   . verify snapshots update after game state changes
    write_to_read. POST a change, GET to confirm, restore, GET to confirm restore
    performance . measure latency across all endpoints (use -n for iterations)
    concurrency . blast endpoints from multiple threads, verify no crashes
    all         . run every mode

Usage:
    python test_v2.py smoke
    python test_v2.py all -n 200
    python test_v2.py performance -n 500
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests
from test_v2_specs import ENDPOINT_SPECS, FRESHNESS_SCENARIOS, GROUP_NAMES, EndpointSpec

from timberbot import Timberbot

DEFAULT_TIMEOUT = 15
PERF_TIMEOUT = 30
DEFAULT_RADIUS = 20
DEFAULT_PERF_ITERATIONS = 100
DEFAULT_CONCURRENCY_WORKERS = 8
DEFAULT_CONCURRENCY_ITERATIONS = 25


@dataclass(frozen=True)
class CaseSpec:
    endpoint: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class HttpResult:
    data: Any
    status_code: int
    elapsed_ms: float


@dataclass
class DiscoveryState:
    settlement: str = ""
    map_x: int = 256
    map_y: int = 256
    center_x: int = 128
    center_y: int = 128
    counts: dict[str, int] = field(default_factory=dict)
    sample_names: dict[str, str] = field(default_factory=dict)
    sample_coords: dict[str, tuple[int, int]] = field(default_factory=dict)
    sample_ids: dict[str, list[int]] = field(default_factory=dict)
    sample_items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    building_targets: dict[str, Any] = field(default_factory=dict)
    placement_search: dict[str, int] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "settlement": self.settlement,
            "map": {"x": self.map_x, "y": self.map_y},
            "center": {"x": self.center_x, "y": self.center_y},
            "counts": self.counts,
            "sample_names": self.sample_names,
            "sample_coords": {
                k: {"x": v[0], "y": v[1]} for k, v in self.sample_coords.items()
            },
            "sample_ids": self.sample_ids,
            "building_targets": self.building_targets,
            "placement_search": self.placement_search,
        }


class RawHttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> HttpResult:
        started = time.perf_counter()
        response = self.session.get(
            f"{self.base_url}{path}", params=params or {}, timeout=timeout
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        body_text = response.text
        try:
            data = response.json()
        except Exception:
            data = {
                "error": "invalid_json",
                "statusCode": response.status_code,
                "body": body_text[:1000],
            }
        if response.status_code >= 400:
            raise RuntimeError(
                f"{response.status_code}: {json.dumps(data, sort_keys=True)[:500]}"
            )
        return HttpResult(
            data=data, status_code=response.status_code, elapsed_ms=elapsed_ms
        )

    def get_text(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> tuple[str, int]:
        response = self.session.get(
            f"{self.base_url}{path}", params=params or {}, timeout=timeout
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text[:500]}")
        return response.text, response.status_code

    def post_text(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> tuple[str, int]:
        payload = body or {}
        response = self.session.post(
            f"{self.base_url}{path}", json=payload, timeout=timeout
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text[:500]}")
        return response.text, response.status_code


class V2Runner:
    def __init__(
        self,
        run_id: str,
        host: str | None = None,
        port: int | None = None,
        log_writer=None,
        error_writer=None,
    ):
        self.bot = Timberbot(host=host, port=port, json_mode=True)
        self.raw = RawHttpClient(self.bot.url)
        self.run_id = run_id
        self.discovery: DiscoveryState | None = None
        self.case_results: list[dict[str, Any]] = []
        self.perf_results: list[dict[str, Any]] = []
        self.fixture_outputs: list[dict[str, Any]] = []
        self.concurrency_artifacts: list[dict[str, Any]] = []
        self.sections: list[str] = []
        self.failed = 0
        self.passed = 0
        self.skipped = 0
        self.artifact_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "test-results", "v2"
        )
        os.makedirs(self.artifact_dir, exist_ok=True)
        self._log_writer = log_writer
        self._error_writer = error_writer

    def _write_line(self, line: str, echo_error: bool = False):
        if self._log_writer is not None:
            self._log_writer.write(line + "\n")
            self._log_writer.flush()
        if echo_error and self._error_writer is not None:
            self._error_writer.write(line + "\n")
            self._error_writer.flush()

    def _fingerprint(self, value: Any) -> str:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _extract_items(self, value: Any) -> list[Any]:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
        if isinstance(value, list):
            return value
        return []

    def _concurrency_membership(self, value: Any) -> dict[str, Any]:
        items = self._extract_items(value)
        ids = []
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                ids.append(int(item["id"]))
        if isinstance(value, dict):
            total = value.get("total", len(items))
        else:
            total = len(items)
        return {
            "total": int(total),
            "count": len(items),
            "ids": ids,
        }

    def _first_row_differences(self, left: Any, right: Any) -> list[str]:
        left_items = self._extract_items(left)
        right_items = self._extract_items(right)
        limit = min(len(left_items), len(right_items))
        for idx in range(limit):
            lrow = left_items[idx]
            rrow = right_items[idx]
            if lrow == rrow or not isinstance(lrow, dict) or not isinstance(rrow, dict):
                continue
            differing = sorted(
                key
                for key in set(lrow.keys()) | set(rrow.keys())
                if lrow.get(key) != rrow.get(key)
            )
            row_id = lrow.get("id", rrow.get("id", idx))
            return [f"id={row_id}", *differing[:10]]
        return []

    def _classify_concurrency(
        self, spec: EndpointSpec, samples: dict[str, Any]
    ) -> tuple[str, str]:
        fingerprints = sorted(samples.keys())
        if len(fingerprints) <= 1:
            return "exact_match", ""

        memberships = {
            fp: self._concurrency_membership(payload) for fp, payload in samples.items()
        }
        membership_signatures = {
            fp: (
                memberships[fp]["total"],
                memberships[fp]["count"],
                tuple(memberships[fp]["ids"]),
            )
            for fp in fingerprints
        }
        if len(set(membership_signatures.values())) > 1:
            return (
                "membership_mismatch",
                f"inconsistent fingerprints={fingerprints[:5]}",
            )

        first = samples[fingerprints[0]]
        second = samples[fingerprints[1]]
        differing = self._first_row_differences(first, second)
        detail = f"field_drift fingerprints={fingerprints[:5]}"
        if differing:
            detail += f" differing={differing}"
        if spec.concurrency_mode == "exact_stable":
            return "field_drift_exact", detail
        return "field_drift", detail

    def _compare_summary(self, left: Any, right: Any) -> str:
        if left == right:
            return "match"
        left_items = self._extract_items(left)
        right_items = self._extract_items(right)
        if left_items or right_items:
            left_ids = [
                item.get("id") for item in left_items[:5] if isinstance(item, dict)
            ]
            right_ids = [
                item.get("id") for item in right_items[:5] if isinstance(item, dict)
            ]
            left_total = (
                left.get("total") if isinstance(left, dict) else len(left_items)
            )
            right_total = (
                right.get("total") if isinstance(right, dict) else len(right_items)
            )
            return (
                f"left_total={left_total} right_total={right_total} "
                f"left_hash={self._fingerprint(left)} right_hash={self._fingerprint(right)} "
                f"left_ids={left_ids} right_ids={right_ids}"
            )
        if isinstance(left, dict) and isinstance(right, dict):
            left_keys = sorted(left.keys())[:10]
            right_keys = sorted(right.keys())[:10]
            return (
                f"left_keys={left_keys} right_keys={right_keys} "
                f"left_hash={self._fingerprint(left)} right_hash={self._fingerprint(right)}"
            )
        return (
            f"left_type={type(left).__name__} right_type={type(right).__name__} "
            f"left_hash={self._fingerprint(left)} right_hash={self._fingerprint(right)}"
        )

    def _record(
        self,
        status: str,
        mode: str,
        name: str,
        detail: str = "",
        endpoint: str = "",
        params: dict[str, Any] | None = None,
    ):
        entry = {
            "status": status,
            "mode": mode,
            "name": name,
            "endpoint": endpoint,
            "params": params or {},
            "detail": detail,
        }
        self.case_results.append(entry)
        if status == "PASS":
            self.passed += 1
        elif status == "FAIL":
            self.failed += 1
        else:
            self.skipped += 1
        line = f"  {status:<4} {name}"
        if detail and status != "PASS":
            line += f". {detail}"
        self._write_line(line, echo_error=(status == "FAIL"))

    def discover(self) -> DiscoveryState:
        if self.discovery is not None:
            return self.discovery

        state = DiscoveryState()
        settlement = self.raw.get("/api/settlement").data
        if isinstance(settlement, dict):
            state.settlement = settlement.get("name", "")

        try:
            tiles = self.bot.tiles()
            map_size = tiles.get("mapSize", {}) if isinstance(tiles, dict) else {}
            state.map_x = int(map_size.get("x", 256))
            state.map_y = int(map_size.get("y", 256))
        except Exception:
            pass

        buildings_basic = self.raw.get(
            "/api/buildings", {"format": "json", "limit": 0}
        ).data
        buildings_full = self.raw.get(
            "/api/buildings", {"format": "json", "limit": 0, "detail": "full"}
        ).data
        beavers_basic = self.raw.get(
            "/api/beavers", {"format": "json", "limit": 0}
        ).data
        trees = self.raw.get("/api/trees", {"format": "json", "limit": 0}).data
        crops = self.raw.get("/api/crops", {"format": "json", "limit": 0}).data
        gatherables = self.raw.get(
            "/api/gatherables", {"format": "json", "limit": 0}
        ).data
        alerts = self.raw.get("/api/alerts", {"format": "json", "limit": 0}).data
        notifications = self.raw.get(
            "/api/notifications", {"format": "json", "limit": 0}
        ).data

        datasets = {
            "buildings": self._extract_items(buildings_basic),
            "beavers": self._extract_items(beavers_basic),
            "trees": self._extract_items(trees),
            "crops": self._extract_items(crops),
            "gatherables": self._extract_items(gatherables),
            "alerts": self._extract_items(alerts),
            "notifications": self._extract_items(notifications),
        }

        district_center = next(
            (
                b
                for b in datasets["buildings"]
                if isinstance(b, dict) and "DistrictCenter" in str(b.get("name", ""))
            ),
            None,
        )
        if district_center:
            state.center_x = int(district_center.get("x", state.map_x // 2))
            state.center_y = int(district_center.get("y", state.map_y // 2))
        elif datasets["buildings"]:
            state.center_x = int(datasets["buildings"][0].get("x", state.map_x // 2))
            state.center_y = int(datasets["buildings"][0].get("y", state.map_y // 2))
        else:
            state.center_x = state.map_x // 2
            state.center_y = state.map_y // 2

        state.placement_search = {
            "x1": max(0, state.center_x - 20),
            "y1": max(0, state.center_y - 20),
            "x2": min(state.map_x - 1, state.center_x + 20),
            "y2": min(state.map_y - 1, state.center_y + 20),
        }

        for name, items in datasets.items():
            state.counts[name] = len(items)
            state.sample_items[name] = items[:10]
            state.sample_ids[name] = [
                int(item["id"])
                for item in items
                if isinstance(item, dict) and item.get("id") is not None
            ][:10]
            sample_named = next(
                (item for item in items if isinstance(item, dict) and item.get("name")),
                None,
            )
            if sample_named:
                state.sample_names[name] = str(sample_named["name"])
            sample_xy = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and item.get("x") is not None
                    and item.get("y") is not None
                ),
                None,
            )
            if sample_xy:
                state.sample_coords[name] = (int(sample_xy["x"]), int(sample_xy["y"]))

        full_items = self._extract_items(buildings_full)
        workers_target = next(
            (
                b
                for b in full_items
                if int(b.get("finished", 0)) == 1
                and int(b.get("maxWorkers", 0)) >= 2
                and str(b.get("name", "")) != "Path"
            ),
            None,
        )
        pause_target = (
            next(
                (
                    b
                    for b in full_items
                    if int(b.get("finished", 0)) == 1
                    and str(b.get("name", "")) != "Path"
                    and b.get("paused") is not None
                ),
                None,
            )
            or workers_target
        )
        floodgate_target = next(
            (
                b
                for b in full_items
                if b.get("hasFloodgate")
                and float(b.get("floodgateMaxHeight", 0.0) or 0.0) > 0.0
            ),
            None,
        )
        clutch_target = next(
            (
                b
                for b in full_items
                if b.get("hasClutch") and int(b.get("finished", 0)) == 1
            ),
            None,
        )
        recipe_target = next(
            (
                b
                for b in full_items
                if isinstance(b.get("recipes"), list)
                and len(b.get("recipes")) >= 2
                and str(b.get("name", "")) != "Path"
            ),
            None,
        )

        if pause_target:
            state.building_targets["pause_target"] = int(pause_target["id"])
        if workers_target:
            state.building_targets["workers_target"] = int(workers_target["id"])
        if floodgate_target:
            state.building_targets["floodgate_target"] = int(floodgate_target["id"])
        if clutch_target:
            state.building_targets["clutch_target"] = int(clutch_target["id"])
        if recipe_target:
            state.building_targets["recipe_target"] = int(recipe_target["id"])

        self.discovery = state
        return state

    def _selected_specs(
        self, endpoint_filters: list[str] | None, group_filters: list[str] | None
    ) -> list[EndpointSpec]:
        specs = ENDPOINT_SPECS
        if endpoint_filters:
            wanted = set(endpoint_filters)
            specs = [spec for spec in specs if spec.name in wanted]
        if group_filters:
            wanted_groups = set(group_filters)
            specs = [spec for spec in specs if spec.group in wanted_groups]
        return specs

    def _pagination_variants(self, count: int) -> list[tuple[str, dict[str, Any]]]:
        variants: list[tuple[str, dict[str, Any]]] = [
            ("default", {}),
            ("limit0", {"limit": 0}),
            ("limit1_offset0", {"limit": 1, "offset": 0}),
        ]
        if count > 1:
            variants.append(("limit3_offset1", {"limit": 3, "offset": 1}))
        return variants

    def _list_matrix(
        self, spec: EndpointSpec, detail: str | None, discovery: DiscoveryState
    ) -> list[CaseSpec]:
        cases: list[CaseSpec] = []
        formats = ["json", "toon"] if spec.supports_format else [None]
        base_count = discovery.counts.get(spec.name, 0)
        page_variants = (
            self._pagination_variants(base_count)
            if spec.supports_pagination
            else [("default", {})]
        )

        for fmt in formats:
            for page_label, page_params in page_variants:
                params = dict(page_params)
                if fmt:
                    params["format"] = fmt
                if detail == "full":
                    params["detail"] = "full"
                elif detail == "basic-explicit":
                    params["detail"] = "basic"
                label_prefix = f"{spec.name}:{fmt or 'default'}"
                if detail == "full":
                    label_prefix += ":full"
                elif detail == "basic-explicit":
                    label_prefix += ":basic"
                cases.append(
                    CaseSpec(spec.name, f"{label_prefix}:{page_label}", params)
                )

                if spec.supports_name_filter and spec.name in discovery.sample_names:
                    name_params = dict(params)
                    name_params["name"] = discovery.sample_names[spec.name]
                    cases.append(
                        CaseSpec(
                            spec.name, f"{label_prefix}:{page_label}:name", name_params
                        )
                    )
                    if spec.supports_pagination:
                        name_page = dict(name_params)
                        name_page["limit"] = 1
                        name_page["offset"] = 0
                        cases.append(
                            CaseSpec(
                                spec.name,
                                f"{label_prefix}:{page_label}:name_limit1",
                                name_page,
                            )
                        )

                if spec.supports_radius_filter and spec.name in discovery.sample_coords:
                    x, y = discovery.sample_coords[spec.name]
                    radius_params = dict(params)
                    radius_params["x"] = x
                    radius_params["y"] = y
                    radius_params["radius"] = DEFAULT_RADIUS
                    cases.append(
                        CaseSpec(
                            spec.name,
                            f"{label_prefix}:{page_label}:radius",
                            radius_params,
                        )
                    )
                    if spec.supports_pagination:
                        radius_page = dict(radius_params)
                        radius_page["limit"] = 1
                        radius_page["offset"] = 0
                        cases.append(
                            CaseSpec(
                                spec.name,
                                f"{label_prefix}:{page_label}:radius_limit1",
                                radius_page,
                            )
                        )
        return cases

    def build_cases(
        self, spec: EndpointSpec, discovery: DiscoveryState
    ) -> list[CaseSpec]:
        if spec.group == "scalar":
            return [CaseSpec(spec.name, f"{spec.name}:default", {})]

        if spec.group == "format":
            return [
                CaseSpec(spec.name, f"{spec.name}:{fmt}", {"format": fmt})
                for fmt in ("json", "toon")
            ]

        if spec.group == "paged":
            cases: list[CaseSpec] = []
            for fmt in ("json", "toon"):
                for page_label, page_params in self._pagination_variants(
                    discovery.counts.get(spec.name, 0)
                ):
                    params = dict(page_params)
                    params["format"] = fmt
                    cases.append(
                        CaseSpec(spec.name, f"{spec.name}:{fmt}:{page_label}", params)
                    )
            return cases

        if spec.group == "list":
            return self._list_matrix(spec, None, discovery)

        if spec.group == "detail_list":
            cases = self._list_matrix(spec, None, discovery)
            cases.extend(self._list_matrix(spec, "basic-explicit", discovery))
            cases.extend(self._list_matrix(spec, "full", discovery))
            for fmt in ("json", "toon"):
                for entity_id in discovery.sample_ids.get(spec.name, [])[:10]:
                    params = {"format": fmt, "id": entity_id}
                    cases.append(
                        CaseSpec(spec.name, f"{spec.name}:{fmt}:id:{entity_id}", params)
                    )
            return cases

        return []

    def representative_cases(
        self, spec: EndpointSpec, discovery: DiscoveryState
    ) -> list[CaseSpec]:
        if spec.group == "scalar":
            return [CaseSpec(spec.name, f"{spec.name}:default", {})]
        if spec.group == "format":
            return [
                CaseSpec(spec.name, f"{spec.name}:json", {"format": "json"}),
                CaseSpec(spec.name, f"{spec.name}:toon", {"format": "toon"}),
            ]
        if spec.group == "paged":
            return [
                CaseSpec(
                    spec.name,
                    f"{spec.name}:json_limit0",
                    {"format": "json", "limit": 0},
                )
            ]
        if spec.group == "list":
            return [
                CaseSpec(
                    spec.name,
                    f"{spec.name}:json_limit0",
                    {"format": "json", "limit": 0},
                )
            ]
        if spec.group == "detail_list":
            cases = [
                CaseSpec(
                    spec.name,
                    f"{spec.name}:json_basic_limit0",
                    {"format": "json", "limit": 0},
                ),
                CaseSpec(
                    spec.name,
                    f"{spec.name}:json_full_limit0",
                    {"format": "json", "limit": 0, "detail": "full"},
                ),
            ]
            if discovery.sample_ids.get(spec.name):
                entity_id = discovery.sample_ids[spec.name][0]
                cases.append(
                    CaseSpec(
                        spec.name,
                        f"{spec.name}:json_id:{entity_id}",
                        {"format": "json", "id": entity_id},
                    )
                )
            return cases
        return []

    def _run_case(self, mode: str, spec: EndpointSpec, case: CaseSpec):
        try:
            result_data = self.raw.get(spec.path, params=case.params).data
        except Exception as ex:
            self._record("FAIL", mode, case.label, str(ex), spec.name, case.params)
            return

        # Basic functional verification for filters
        detail = ""
        ok = True
        items = self._extract_items(result_data)

        if "name" in case.params and items:
            name_filter = case.params["name"].lower()
            if not all(
                name_filter in str(item.get("name", "")).lower()
                for item in items
                if isinstance(item, dict)
            ):
                ok = False
                detail = "name filter violation"

        if "radius" in case.params and items:
            fx, fy, fr = case.params["x"], case.params["y"], case.params["radius"]
            if not all(
                abs(item.get("x", fx) - fx) + abs(item.get("y", fy) - fy) <= fr
                for item in items
                if isinstance(item, dict)
            ):
                ok = False
                detail = "radius filter violation"

        if ok:
            self._record(
                "PASS", mode, case.label, endpoint=spec.name, params=case.params
            )
        else:
            self._record("FAIL", mode, case.label, detail, spec.name, case.params)

    def run_smoke(self, specs: list[EndpointSpec]):
        self.sections.append("smoke")
        self._write_line("")
        self._write_line("=== smoke ===")
        self._write_line("")
        discovery = self.discover()
        for spec in specs:
            cases = self.build_cases(spec, discovery)
            if not cases:
                self._record(
                    "SKIP",
                    "smoke",
                    f"{spec.name}:no_cases",
                    "no generated cases",
                    spec.name,
                )
                continue
            self._run_case("smoke", spec, cases[0])

    def _building_v2_detail(self, building_id: int) -> dict[str, Any]:
        data = self.raw.get(
            "/api/buildings", {"format": "json", "id": building_id}
        ).data
        if isinstance(data, dict) and "id" in data:
            return data
        items = self._extract_items(data)
        if items and isinstance(items[0], dict):
            return items[0]
        return {}

    def _restore_pause(self, building_id: int, paused: bool):
        if paused:
            self.bot.pause_building(building_id)
        else:
            self.bot.unpause_building(building_id)

    def _choose_alternate_recipe(self, building: dict[str, Any]) -> str | None:
        recipes = [str(item) for item in building.get("recipes", []) if item]
        current = str(building.get("currentRecipe", "") or "")
        for recipe in recipes:
            if recipe != current:
                return recipe
        if recipes:
            return recipes[0]
        return None

    def _run_write_to_read_scenario(self, mode: str, scenario_name: str):
        discovery = self.discover()
        if scenario_name == "pause_toggle":
            building_id = discovery.building_targets.get("pause_target")
            if not building_id:
                self._record(
                    "SKIP",
                    mode,
                    "pause_toggle",
                    "no pause-capable building found",
                    "buildings",
                )
                return
            before = self._building_v2_detail(building_id)
            original = bool(before.get("paused"))
            try:
                self._restore_pause(building_id, not original)
                after = self._building_v2_detail(building_id)
                changed = bool(after.get("paused")) == (not original)
                self._restore_pause(building_id, original)
                restored = self._building_v2_detail(building_id)
                restored_ok = bool(restored.get("paused")) == original
                ok = changed and restored_ok
                detail = (
                    ""
                    if ok
                    else json.dumps({"after": after, "restored": restored})[:220]
                )
                self._record(
                    "PASS" if ok else "FAIL",
                    mode,
                    "pause_toggle",
                    detail,
                    "buildings",
                    {"id": building_id},
                )
            finally:
                self._restore_pause(building_id, original)
            return

        if scenario_name == "workers_change":
            building_id = discovery.building_targets.get("workers_target")
            if not building_id:
                self._record(
                    "SKIP",
                    mode,
                    "workers_change",
                    "no worker-capable building found",
                    "buildings",
                )
                return
            before = self._building_v2_detail(building_id)
            original = int(before.get("desiredWorkers", 0))
            max_workers = int(before.get("maxWorkers", 0))
            if max_workers <= 0:
                self._record(
                    "SKIP",
                    mode,
                    "workers_change",
                    "maxWorkers <= 0",
                    "buildings",
                    {"id": building_id},
                )
                return
            target = 0 if original > 0 else min(1, max_workers)
            try:
                self.bot.set_workers(building_id, target)
                after = self._building_v2_detail(building_id)
                changed = int(after.get("desiredWorkers", -1)) == target
                self.bot.set_workers(building_id, original)
                restored = self._building_v2_detail(building_id)
                restored_ok = int(restored.get("desiredWorkers", -1)) == original
                ok = changed and restored_ok
                detail = (
                    ""
                    if ok
                    else json.dumps({"after": after, "restored": restored})[:220]
                )
                self._record(
                    "PASS" if ok else "FAIL",
                    mode,
                    "workers_change",
                    detail,
                    "buildings",
                    {"id": building_id, "target": target},
                )
            finally:
                self.bot.set_workers(building_id, original)
            return

        if scenario_name == "floodgate_change":
            building_id = discovery.building_targets.get("floodgate_target")
            if not building_id:
                self._record(
                    "SKIP", mode, "floodgate_change", "no floodgate found", "buildings"
                )
                return
            before = self._building_v2_detail(building_id)
            original = float(before.get("floodgateHeight", 0.0))
            max_height = float(before.get("floodgateMaxHeight", 0.0) or 0.0)
            target = 0.0 if original > 0.0 else min(max_height, 1.0)
            if abs(target - original) < 0.001:
                self._record(
                    "SKIP",
                    mode,
                    "floodgate_change",
                    "no alternate floodgate height available",
                    "buildings",
                    {"id": building_id},
                )
                return
            try:
                self.bot.set_floodgate(building_id, target)
                after = self._building_v2_detail(building_id)
                changed = (
                    abs(float(after.get("floodgateHeight", -999.0)) - target) < 0.01
                )
                self.bot.set_floodgate(building_id, original)
                restored = self._building_v2_detail(building_id)
                restored_ok = (
                    abs(float(restored.get("floodgateHeight", -999.0)) - original)
                    < 0.01
                )
                ok = changed and restored_ok
                detail = (
                    ""
                    if ok
                    else json.dumps({"after": after, "restored": restored})[:220]
                )
                self._record(
                    "PASS" if ok else "FAIL",
                    mode,
                    "floodgate_change",
                    detail,
                    "buildings",
                    {"id": building_id, "target": target},
                )
            finally:
                self.bot.set_floodgate(building_id, original)
            return

        if scenario_name == "recipe_change":
            building_id = discovery.building_targets.get("recipe_target")
            if not building_id:
                self._record(
                    "SKIP",
                    mode,
                    "recipe_change",
                    "no recipe-capable building found",
                    "buildings",
                )
                return
            before = self._building_v2_detail(building_id)
            target = self._choose_alternate_recipe(before)
            original = str(before.get("currentRecipe", "") or "")
            if not target or target == original:
                self._record(
                    "SKIP",
                    mode,
                    "recipe_change",
                    "no alternate recipe available",
                    "buildings",
                    {"id": building_id},
                )
                return
            try:
                self.bot.set_recipe(building_id, target)
                after = self._building_v2_detail(building_id)
                expected = "" if target == "none" else target
                changed = str(after.get("currentRecipe", "") or "") == expected
                self.bot.set_recipe(building_id, original or "none")
                restored = self._building_v2_detail(building_id)
                restored_expected = original or ""
                restored_ok = (
                    str(restored.get("currentRecipe", "") or "") == restored_expected
                )
                ok = changed and restored_ok
                detail = (
                    ""
                    if ok
                    else json.dumps({"after": after, "restored": restored})[:220]
                )
                self._record(
                    "PASS" if ok else "FAIL",
                    mode,
                    "recipe_change",
                    detail,
                    "buildings",
                    {"id": building_id, "target": target},
                )
            finally:
                self.bot.set_recipe(building_id, original or "none")
            return

        if scenario_name == "clutch_change":
            building_id = discovery.building_targets.get("clutch_target")
            if not building_id:
                self._record(
                    "SKIP",
                    mode,
                    "clutch_change",
                    "no clutch building found",
                    "buildings",
                )
                return
            before = self._building_v2_detail(building_id)
            original = bool(before.get("clutchEngaged"))
            try:
                self.bot.set_clutch(building_id, not original)
                after = self._building_v2_detail(building_id)
                changed = bool(after.get("clutchEngaged")) == (not original)
                self.bot.set_clutch(building_id, original)
                restored = self._building_v2_detail(building_id)
                restored_ok = bool(restored.get("clutchEngaged")) == original
                ok = changed and restored_ok
                detail = (
                    ""
                    if ok
                    else json.dumps({"after": after, "restored": restored})[:220]
                )
                self._record(
                    "PASS" if ok else "FAIL",
                    mode,
                    "clutch_change",
                    detail,
                    "buildings",
                    {"id": building_id},
                )
            finally:
                self.bot.set_clutch(building_id, original)
            return

        if scenario_name == "place_demolish":
            search = discovery.placement_search
            try:
                found = self.bot.find_placement(
                    "Path", search["x1"], search["y1"], search["x2"], search["y2"]
                )
            except Exception as ex:
                self._record(
                    "FAIL",
                    mode,
                    "place_demolish",
                    f"find_placement failed: {ex}",
                    "buildings",
                )
                return
            spots = found.get("placements", []) if isinstance(found, dict) else []
            spot = next(
                (p for p in spots if p.get("reachable") and not p.get("flooded")), None
            ) or (spots[0] if spots else None)
            if not spot:
                self._record(
                    "SKIP",
                    mode,
                    "place_demolish",
                    "no placement spot found",
                    "buildings",
                )
                return
            placed = None
            try:
                before = self.raw.get(
                    "/api/buildings", {"format": "json", "limit": 0}
                ).data
                before_ids = {
                    int(item["id"])
                    for item in self._extract_items(before)
                    if isinstance(item, dict) and item.get("id") is not None
                }
                placed = self.bot.place_building(
                    "Path",
                    int(spot["x"]),
                    int(spot["y"]),
                    int(spot["z"]),
                    spot.get("orientation", "south"),
                )
                placed_id = placed.get("id") if isinstance(placed, dict) else None
                if not placed_id:
                    self._record(
                        "FAIL",
                        mode,
                        "place_demolish",
                        f"place failed: {json.dumps(placed)[:180]}",
                        "buildings",
                    )
                    return
                after_place = self.raw.get(
                    "/api/buildings", {"format": "json", "limit": 0}
                ).data
                after_ids = {
                    int(item["id"])
                    for item in self._extract_items(after_place)
                    if isinstance(item, dict) and item.get("id") is not None
                }
                place_ok = (
                    int(placed_id) in after_ids
                    and len(after_ids) == len(before_ids) + 1
                )
                if not place_ok:
                    self._record(
                        "FAIL",
                        mode,
                        "place_demolish",
                        "new building id not visible immediately after placement",
                        "buildings",
                        {"id": placed_id},
                    )
                    return
                self.bot.demolish_building(int(placed_id))
                after_demolish = self.raw.get(
                    "/api/buildings", {"format": "json", "limit": 0}
                ).data
                final_ids = {
                    int(item["id"])
                    for item in self._extract_items(after_demolish)
                    if isinstance(item, dict) and item.get("id") is not None
                }
                dem_ok = int(placed_id) not in final_ids and len(final_ids) == len(
                    before_ids
                )
                self._record(
                    "PASS" if dem_ok else "FAIL",
                    mode,
                    "place_demolish",
                    "" if dem_ok else "placed id still visible after demolish",
                    "buildings",
                    {"id": placed_id},
                )
            finally:
                if isinstance(placed, dict) and placed.get("id"):
                    try:
                        self.bot.demolish_building(int(placed["id"]))
                    except Exception:
                        pass
            return

        self._record("SKIP", mode, scenario_name, "unknown scenario", "buildings")

    def run_freshness(self):
        self.sections.append("freshness")
        self._write_line("")
        self._write_line("=== freshness ===")
        self._write_line("")
        self.discover()
        for scenario in FRESHNESS_SCENARIOS:
            try:
                self._run_write_to_read_scenario("freshness", scenario.name)
            except Exception as ex:
                self._record("FAIL", "freshness", scenario.name, str(ex), "buildings")

    def run_write_to_read(self):
        self.sections.append("write_to_read")
        self._write_line("")
        self._write_line("=== write_to_read ===")
        self._write_line("")
        self.discover()
        for scenario in FRESHNESS_SCENARIOS:
            try:
                self._run_write_to_read_scenario("write_to_read", scenario.name)
            except Exception as ex:
                self._record(
                    "FAIL", "write_to_read", scenario.name, str(ex), "buildings"
                )

    def _percentile(self, values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(round((len(ordered) - 1) * pct))
        return ordered[max(0, min(idx, len(ordered) - 1))]

    def _bench_path(
        self, path: str, params: dict[str, Any], iterations: int
    ) -> tuple[list[float], list[str]]:
        times: list[float] = []
        errors: list[str] = []
        for _ in range(iterations):
            try:
                times.append(
                    self.raw.get(path, params=params, timeout=PERF_TIMEOUT).elapsed_ms
                )
            except Exception as ex:
                errors.append(str(ex))
        return times, errors

    def _summarize_perf(
        self, values: list[float], errors: list[str], iterations: int
    ) -> dict[str, Any]:
        if not values:
            return {
                "avg_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "max_ms": 0.0,
                "ok": 0,
                "errors": len(errors),
                "iterations": iterations,
            }
        return {
            "avg_ms": round(sum(values) / len(values), 2),
            "p50_ms": round(self._percentile(values, 0.50), 2),
            "p95_ms": round(self._percentile(values, 0.95), 2),
            "max_ms": round(max(values), 2),
            "ok": len(values),
            "errors": len(errors),
            "iterations": iterations,
        }

    def run_performance(self, specs: list[EndpointSpec], iterations: int):
        self.sections.append("performance")
        self._write_line("")
        self._write_line("=== performance ===")
        self._write_line("")
        discovery = self.discover()
        for spec in specs:
            for case in self.representative_cases(spec, discovery):
                times, errors = self._bench_path(spec.path, case.params, iterations)
                row: dict[str, Any] = {
                    "endpoint": spec.name,
                    "case": case.label,
                    "params": case.params,
                    "current": self._summarize_perf(times, errors, iterations),
                }
                self.perf_results.append(row)
                failures = row["current"]["errors"]
                detail = ""
                if failures:
                    detail = f"errors={row['current']['errors']}"
                self._record(
                    "PASS" if not failures else "FAIL",
                    "performance",
                    case.label,
                    detail,
                    spec.name,
                    case.params,
                )

    def run_concurrency(self, specs: list[EndpointSpec], workers: int, iterations: int):
        self.sections.append("concurrency")
        self._write_line("")
        self._write_line("=== concurrency ===")
        self._write_line("")
        discovery = self.discover()
        for spec in specs:
            if not spec.projection_backed:
                self._record(
                    "SKIP",
                    "concurrency",
                    f"{spec.name}:projection_only",
                    "not projection-backed",
                    spec.name,
                )
                continue
            cases = [
                case
                for case in self.representative_cases(spec, discovery)
                if case.params.get("format") == "json"
            ]
            for case in cases:
                errors: list[str] = []
                samples: dict[str, Any] = {}
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(
                            self.raw.get, spec.path, case.params, DEFAULT_TIMEOUT
                        )
                        for _ in range(workers * iterations)
                    ]
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                            fingerprint = self._fingerprint(result.data)
                            if fingerprint not in samples:
                                samples[fingerprint] = result.data
                        except Exception as ex:
                            errors.append(str(ex))
                unique = sorted(samples.keys())
                classification = "exact_match"
                detail = ""
                if errors:
                    classification = "request_error"
                    detail = errors[0]
                else:
                    classification, detail = self._classify_concurrency(spec, samples)
                if classification != "exact_match":
                    self.concurrency_artifacts.append(
                        {
                            "endpoint": spec.name,
                            "case": case.label,
                            "params": case.params,
                            "classification": classification,
                            "detail": detail,
                            "fingerprints": unique,
                            "samples": samples,
                        }
                    )
                ok = classification in {"exact_match", "field_drift"}
                self._record(
                    "PASS" if ok else "FAIL",
                    "concurrency",
                    case.label,
                    detail,
                    spec.name,
                    case.params,
                )

    def list_endpoints(self):
        self._write_line("Available endpoints:")
        for spec in ENDPOINT_SPECS:
            self._write_line(
                f"  {spec.name:<14} group={spec.group:<11} projection_backed={spec.projection_backed} "
                f"concurrency_mode={spec.concurrency_mode}"
            )

    def list_cases(self, specs: list[EndpointSpec]):
        discovery = self.discover()
        for spec in specs:
            self._write_line("")
            self._write_line(f"[{spec.name}]")
            for case in self.build_cases(spec, discovery):
                self._write_line(
                    f"  {case.label}  params={json.dumps(case.params, sort_keys=True)}"
                )

    def write_artifacts(self, mode_name: str):
        base = os.path.join(self.artifact_dir, f"{self.run_id}-{mode_name}")
        diff_root = self._write_concurrency_artifacts(base)
        report = {
            "mode": mode_name,
            "sections": self.sections,
            "summary": {
                "passed": self.passed,
                "failed": self.failed,
                "skipped": self.skipped,
            },
            "discovery": self.discovery.to_jsonable() if self.discovery else {},
            "cases": self.case_results,
            "performance": self.perf_results,
            "fixtures": self.fixture_outputs,
            "concurrencyArtifacts": [
                {
                    "endpoint": item["endpoint"],
                    "case": item["case"],
                    "classification": item["classification"],
                    "detail": item["detail"],
                    "fingerprints": item["fingerprints"],
                }
                for item in self.concurrency_artifacts
            ],
        }
        json_path = base + ".json"
        md_path = base + ".md"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(self._render_markdown(report, json_path, diff_root))
        return md_path, json_path

    def _write_concurrency_artifacts(self, base: str) -> str | None:
        if not self.concurrency_artifacts:
            return None
        diff_root = base + "-concurrency-diffs"
        os.makedirs(diff_root, exist_ok=True)
        for index, item in enumerate(self.concurrency_artifacts, start=1):
            safe_case = "".join(
                ch if ch.isalnum() or ch in "-._" else "_" for ch in item["case"]
            )[:80]
            case_dir = os.path.join(diff_root, f"{index:02d}-{safe_case}")
            os.makedirs(case_dir, exist_ok=True)
            summary = {
                "endpoint": item["endpoint"],
                "case": item["case"],
                "params": item["params"],
                "classification": item["classification"],
                "detail": item["detail"],
                "fingerprints": item["fingerprints"],
                "memberships": {
                    fp: self._concurrency_membership(payload)
                    for fp, payload in item["samples"].items()
                },
            }
            with open(
                os.path.join(case_dir, "summary.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(summary, fh, indent=2, sort_keys=True)
            for fingerprint, payload in item["samples"].items():
                with open(
                    os.path.join(case_dir, f"sample-{fingerprint}.json"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
            with open(os.path.join(case_dir, "diff.md"), "w", encoding="utf-8") as fh:
                fh.write(f"# {item['case']}\n\n")
                fh.write(f"- Endpoint: `{item['endpoint']}`\n")
                fh.write(f"- Classification: `{item['classification']}`\n")
                fh.write(f"- Detail: {item['detail'] or 'n/a'}\n")
                fh.write(f"- Fingerprints: `{', '.join(item['fingerprints'])}`\n")
        return diff_root

    def _render_markdown(
        self, report: dict[str, Any], json_path: str, diff_root: str | None
    ) -> str:
        lines = [
            f"# V2 Test Report: {report['mode']}",
            "",
            f"- Passed: {report['summary']['passed']}",
            f"- Failed: {report['summary']['failed']}",
            f"- Skipped: {report['summary']['skipped']}",
            f"- JSON artifact: `{json_path}`",
        ]
        if diff_root:
            lines.append(f"- Concurrency diffs: `{diff_root}`")
        lines.extend(
            [
                "",
                "## Discovery",
                "",
                f"- Settlement: {report['discovery'].get('settlement', '')}",
                f"- Map: {report['discovery'].get('map', {}).get('x', 0)}x{report['discovery'].get('map', {}).get('y', 0)}",
                f"- Center: ({report['discovery'].get('center', {}).get('x', 0)}, {report['discovery'].get('center', {}).get('y', 0)})",
                "",
                "## Case Results",
                "",
                "| Status | Mode | Name | Detail |",
                "|---|---|---|---|",
            ]
        )
        for case in self.case_results:
            detail = case["detail"].replace("|", "/").replace("\n", " ")[:160]
            lines.append(
                f"| {case['status']} | {case['mode']} | {case['name']} | {detail} |"
            )
        if self.perf_results:
            lines.extend(
                [
                    "",
                    "## Performance",
                    "",
                    "| Endpoint | Case | Avg | P50 | P95 | Max | Errors |",
                    "|---|---|---:|---:|---:|---:|---:|",
                ]
            )
            for row in self.perf_results:
                lines.append(
                    f"| {row['endpoint']} | {row['case']} | {row['current']['avg_ms']:.2f} | "
                    f"{row['current']['p50_ms']:.2f} | {row['current']['p95_ms']:.2f} | "
                    f"{row['current']['max_ms']:.2f} | {row['current']['errors']} |"
                )
        if report["concurrencyArtifacts"]:
            lines.extend(
                [
                    "",
                    "## Concurrency Diagnostics",
                    "",
                    "| Endpoint | Case | Classification | Detail |",
                    "|---|---|---|---|",
                ]
            )
            for item in report["concurrencyArtifacts"]:
                detail = item["detail"].replace("|", "/").replace("\n", " ")[:160]
                lines.append(
                    f"| {item['endpoint']} | {item['case']} | {item['classification']} | {detail} |"
                )
        lines.append("")
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Dedicated Timberbot API harness")
    parser.add_argument(
        "mode",
        choices=[
            "smoke",
            "freshness",
            "write_to_read",
            "performance",
            "concurrency",
            "all",
            "list-endpoints",
            "list-cases",
        ],
    )
    parser.add_argument("--host", help="host IP of the game")
    parser.add_argument("--port", type=int, help="port of the Timberbot API")
    parser.add_argument(
        "--endpoint",
        action="append",
        default=[],
        help="limit to one or more endpoint names",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=GROUP_NAMES,
        help="limit to one or more endpoint groups",
    )
    parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        default=DEFAULT_PERF_ITERATIONS,
        help="iterations for performance or concurrency",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_CONCURRENCY_WORKERS,
        help="workers for concurrency mode",
    )
    args = parser.parse_args()

    artifact_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "test-results", "v2"
    )
    os.makedirs(artifact_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    transcript_path = os.path.join(artifact_dir, f"{timestamp}-{args.mode}.log")

    with open(transcript_path, "w", encoding="utf-8") as log_writer:
        runner = V2Runner(
            run_id=timestamp,
            host=args.host,
            port=args.port,
            log_writer=log_writer,
            error_writer=sys.stderr,
        )
        if not runner.bot.ping():
            sys.stderr.write("error: game not reachable\n")
            sys.stderr.flush()
            log_writer.write("error: game not reachable\n")
            log_writer.flush()
            sys.exit(1)

        specs = runner._selected_specs(args.endpoint, args.group)
        if args.mode == "list-endpoints":
            runner.list_endpoints()
            sys.stderr.write(f"results: {transcript_path}\n")
            sys.stderr.flush()
            return
        if not specs and args.mode != "freshness":
            msg = "error: no endpoints selected\n"
            sys.stderr.write(msg)
            sys.stderr.flush()
            log_writer.write(msg)
            log_writer.flush()
            sys.exit(1)

        if args.mode == "list-cases":
            runner.list_cases(specs)
        elif args.mode == "smoke":
            runner.run_smoke(specs)
        elif args.mode == "freshness":
            runner.run_freshness()
        elif args.mode == "write_to_read":
            runner.run_write_to_read()
        elif args.mode == "performance":
            runner.run_performance(specs, args.iterations)
        elif args.mode == "concurrency":
            iterations = args.iterations or DEFAULT_CONCURRENCY_ITERATIONS
            runner.run_concurrency(specs, args.workers, iterations)
        elif args.mode == "all":
            runner.run_smoke(specs)
            runner.run_freshness()
            runner.run_write_to_read()
            runner.run_performance(specs, args.iterations)
            runner.run_concurrency(specs, args.workers, DEFAULT_CONCURRENCY_ITERATIONS)

        md_path, json_path = runner.write_artifacts(args.mode)
        runner._write_line("")
        runner._write_line(f"results: {md_path}")
        runner._write_line(f"json: {json_path}")
        runner._write_line(f"transcript: {transcript_path}")
        summary = f"=== {runner.passed} passed, {runner.failed} failed"
        if runner.skipped:
            summary += f", {runner.skipped} skipped"
        summary += " ==="
        runner._write_line(summary)

        if runner.failed:
            sys.stderr.write(f"results: {md_path}\n")
            sys.stderr.write(f"transcript: {transcript_path}\n")
            sys.stderr.write(summary + "\n")
            sys.stderr.flush()
        sys.exit(1 if runner.failed else 0)


if __name__ == "__main__":
    main()
