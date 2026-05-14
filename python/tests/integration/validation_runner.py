"""Timberbot API test suite.

Tests every API endpoint with functional validation and performance profiling.
Works on any save game, any faction. Uses discovery to detect map bounds,
faction, and existing buildings. Tests gracefully skip when buildings are missing.

Usage:
    python test_validation.py              # run all tests
    python test_validation.py speed webhooks  # run specific tests
    python test_validation.py --perf       # performance only
    python test_validation.py --perf -n 500  # 500 iterations
    python test_validation.py --benchmark  # in-game benchmark endpoint
    python test_validation.py --benchmark -n 200
    python test_validation.py --list       # show all test names
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any
from typing_extensions import override

from tbot import Timberbot, TimberbotError

NAVMESH_SETTLE_WAIT = 3.0  # extra settle time for navmesh-dependent reachability checks


class TestRunner:
    # Tell pytest not to auto-collect this class - it's a legacy test harness,
    # not a pytest TestCase. The pytest-shaped wrappers live in
    # test_validation_methods.py and call into this via the validation_runner
    # session fixture.
    __test__ = False

    def __init__(self):
        self.bot = Timberbot(
            json_mode=True, write_timeout=300
        )  # functional tests use JSON for structured data
        # disable TimberbotError raising so tests can inspect error dicts directly
        self.bot._check = lambda data: data
        # separate bot with error raising enabled for TimberbotError tests
        self.strict_bot = Timberbot(json_mode=True, write_timeout=300)
        # toon bot for format validation tests
        self.toon_bot = Timberbot(write_timeout=300)
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        # discovery state (set by discover())
        self.faction = ""
        self.map_x = 256
        self.map_y = 256
        self.center_x = 128
        self.center_y = 128
        self.x1 = 98
        self.y1 = 98
        self.x2 = 158
        self.y2 = 158
        self.prefab_names = set()
        self._doc_contract_cache = None
        self.debug_enabled = False
        self.fails_only = False
        self.perf_iterations: int = 100
        self._locked_prefab: str | None = None
        self.callback_host: str | None = None

    def _safe_call(self, fn, fallback=None):
        """Call a bot method, return fallback on any error (bad JSON, timeout, etc)."""
        try:
            return fn()
        except Exception:
            return fallback

    def _as_list(
        self, result: list[dict[str, Any]] | dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Narrow an API result to list[dict[str, Any]], returning [] for non-list results."""
        return result if isinstance(result, list) else []

    def _fingerprint(self, value):
        """Stable compact fingerprint for parity checks without dumping payloads."""
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _compare_compact(self, left, right):
        """Return a short mismatch summary instead of printing full payloads."""
        if left == right:
            return "match"
        left_is_list = isinstance(left, list)
        right_is_list = isinstance(right, list)
        if left_is_list and right_is_list:
            left_ids = [item.get("id") for item in left[:5] if isinstance(item, dict)]
            right_ids = [item.get("id") for item in right[:5] if isinstance(item, dict)]
            return (
                f"left_count={len(left)} right_count={len(right)} "
                f"left_hash={self._fingerprint(left)} right_hash={self._fingerprint(right)} "
                f"left_ids={left_ids} right_ids={right_ids}"
            )
        return (
            f"left_type={type(left).__name__} right_type={type(right).__name__} "
            f"left_hash={self._fingerprint(left)} right_hash={self._fingerprint(right)}"
        )

    def _compare_building_lists(self, legacy, v2):
        """Exact building-list comparison with compact mismatch reporting."""
        if not isinstance(legacy, list) or not isinstance(v2, list):
            return False, self._compare_compact(legacy, v2)
        if legacy == v2:
            return True, (
                f"count={len(legacy)} hash={self._fingerprint(legacy)} "
                f"sample_ids={[item.get('id') for item in legacy[:5] if isinstance(item, dict)]}"
            )

        legacy_by_id = {
            item.get("id"): item
            for item in legacy
            if isinstance(item, dict) and "id" in item
        }
        v2_by_id = {
            item.get("id"): item
            for item in v2
            if isinstance(item, dict) and "id" in item
        }
        legacy_ids = {k for k in legacy_by_id if k is not None}
        v2_ids = {k for k in v2_by_id if k is not None}
        missing_in_v2 = sorted(legacy_ids - v2_ids)[:5]
        extra_in_v2 = sorted(v2_ids - legacy_ids)[:5]
        if missing_in_v2 or extra_in_v2:
            return False, (
                f"count_legacy={len(legacy)} count_v2={len(v2)} "
                f"legacy_hash={self._fingerprint(legacy)} v2_hash={self._fingerprint(v2)} "
                f"missing_in_v2={missing_in_v2} extra_in_v2={extra_in_v2}"
            )

        mismatches = []
        for bid in sorted(legacy_ids):
            if legacy_by_id[bid] != v2_by_id[bid]:
                mismatches.append(
                    (
                        bid,
                        self._fingerprint(legacy_by_id[bid]),
                        self._fingerprint(v2_by_id[bid]),
                    )
                )
                if len(mismatches) >= 5:
                    break
        return False, (
            f"count={len(legacy)} legacy_hash={self._fingerprint(legacy)} v2_hash={self._fingerprint(v2)} "
            f"mismatches={[{'id': bid, 'legacy': lh, 'v2': vh} for bid, lh, vh in mismatches]}"
        )

    def discover(self):
        """Detect game state: faction, map bounds, existing buildings, prefabs."""
        print("\n=== discovery ===\n")

        buildings = self._safe_call(lambda: self.bot.buildings(), [])

        # detect faction from prefab list (building names are now faction-qualified)
        try:
            prefabs = self.bot.prefabs()
        except Exception:
            prefabs = []
            print(
                "  WARNING: prefabs endpoint returned bad data, faction detection may fail"
            )
        self.faction = ""
        if isinstance(prefabs, list):
            for p in prefabs:
                name = p.get("name", "") if isinstance(p, dict) else ""
                if ".IronTeeth" in name:
                    self.faction = "IronTeeth"
                    break
                if ".Folktails" in name:
                    self.faction = "Folktails"
                    break

        # map bounds
        mapinfo = self.bot.tiles()
        ms = mapinfo.get("mapSize", {}) if isinstance(mapinfo, dict) else {}
        self.map_x = ms.get("x", 256)
        self.map_y = ms.get("y", 256)

        # find district center for coordinate center
        dc_id = self.find_building("DistrictCenter")
        if dc_id and isinstance(buildings, list):
            dcb = next((b for b in buildings if b.get("id") == dc_id), None)
            if dcb:
                self.center_x = dcb.get("x", self.map_x // 2)
                self.center_y = dcb.get("y", self.map_y // 2)
        else:
            self.center_x = self.map_x // 2
            self.center_y = self.map_y // 2

        # search area: 30-tile radius around DC
        self.x1 = max(0, self.center_x - 30)
        self.y1 = max(0, self.center_y - 30)
        self.x2 = min(self.map_x - 1, self.center_x + 30)
        self.y2 = min(self.map_y - 1, self.center_y + 30)

        # available prefabs (already loaded above for faction detection)
        self.prefab_names = set()
        if isinstance(prefabs, list):
            for p in prefabs:
                name = p.get("name", "") if isinstance(p, dict) else ""
                if name:
                    self.prefab_names.add(name)

        # find a locked building for the "not unlocked" test
        self._locked_prefab = None
        try:
            science = self.bot.science()
            if isinstance(science, dict):
                for u in science.get("unlockables", []):
                    if (
                        isinstance(u, dict)
                        and not u.get("unlocked")
                        and u.get("cost", 0) > 5000
                    ):
                        self._locked_prefab = u.get("name", "")
                        break
        except Exception:
            print("  WARNING: science endpoint returned bad data")

        print(f"  faction: {self.faction or 'unknown'}")
        print(f"  map: {self.map_x}x{self.map_y}")
        print(f"  center: ({self.center_x},{self.center_y})")
        print(f"  search: ({self.x1},{self.y1}) to ({self.x2},{self.y2})")
        print(f"  prefabs: {len(self.prefab_names)}")
        bcount = len(buildings) if isinstance(buildings, list) else 0
        print(f"  buildings: {bcount}")

        # detect debug endpoint availability
        probe = self._safe_call(lambda: self.bot.debug(target="help"), {})
        self.debug_enabled = isinstance(probe, dict) and "error" not in probe
        print(f"  debug: {'enabled' if self.debug_enabled else 'disabled'}")

    def prefab(self, base):
        """Return faction-qualified prefab name. 'Barrack' -> 'Barrack.IronTeeth'"""
        # some prefabs have no faction suffix
        if base == "Path":
            return base
        # try with faction suffix
        qualified = f"{base}.{self.faction}" if self.faction else base
        if qualified in self.prefab_names:
            return qualified
        # fallback: search for any prefab starting with base
        for p in self.prefab_names:
            if p.startswith(base + ".") or p == base:
                return p
        return base

    def wait_for_navmesh_settle(self):
        """Wait for Timberborn navmesh/path reachability to catch up after path edits."""
        time.sleep(NAVMESH_SETTLE_WAIT)

    def prime_validation_snapshots(self):
        """Force fresh snapshot publishes before debug validation checks."""
        self._safe_call(lambda: self.bot.buildings(limit=0), None)
        self._safe_call(lambda: self.bot.beavers(limit=0), None)
        self._safe_call(lambda: self.bot.trees(limit=0), None)
        self._safe_call(lambda: self.bot.crops(limit=0), None)
        self._safe_call(lambda: self.bot.districts(), None)

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            if not self.fails_only:
                print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}")
            if detail:
                print(f"         {detail}")

    def skip(self, name, reason=""):
        self.skipped += 1
        if not self.fails_only:
            print(f"  SKIP  {name}" + (f" ({reason})" if reason else ""))

    def has(self, result, key):
        """check result dict has key"""
        return isinstance(result, dict) and key in result

    def err(self, result):
        """check result is an error"""
        return isinstance(result, dict) and "error" in result

    def debug_get(self, path):
        """get a value from game internals via debug endpoint"""
        if not self.debug_enabled:
            return {"skipped": True}
        return self.bot.debug(target="get", path=path)

    def debug_call(self, path, method, **kwargs):
        """call a method on a game object via debug endpoint"""
        if not self.debug_enabled:
            return {"skipped": True}
        args = {"target": "call", "path": path, "method": method}
        args.update(kwargs)
        return self.bot.debug(**args)

    def debug_component_field(self, entity_id, type_suffix, field):
        """read a field from a component on an entity via debug.
        scans AllComponents for the type, then reads the field. returns value or None."""
        if not self.debug_enabled:
            return None
        self.debug_call("Write._cache", "FindEntity", arg0=str(entity_id))
        # scan in blocks: check [40],[45],[50],[55],[60] first (storage components cluster here)
        for start in [40, 0, 60, 80]:
            for i in range(start, start + 20):
                r = self.debug_get(f"$.AllComponents.[{i}]")
                res = r.get("result")
                if res is None:
                    if i > 70:
                        return None
                    continue
                if not isinstance(res, dict):
                    continue
                if type_suffix in res.get("type", ""):
                    # re-fetch entity and read field at this index
                    self.debug_call("Write._cache", "FindEntity", arg0=str(entity_id))
                    val = self.debug_get(f"$.AllComponents.[{i}].{field}")
                    return val.get("result")
        return None

    def find_spot(self, prefab="Path"):
        """find a valid placement spot for prefab using discovered search area"""
        result = self.bot.find_placement(prefab, self.x1, self.y1, self.x2, self.y2)
        placements = result.get("placements", []) if isinstance(result, dict) else []
        # prefer reachable with water (for pumps), then non-flooded reachable
        for p in placements:
            if p.get("reachable") and p.get("waterDepth", 0) > 0:
                return p
        for p in placements:
            if p.get("reachable") and not p.get("flooded", 0):
                return p
        return placements[0] if placements else None

    def tile_has(self, tile, name):
        """check if a tile has an occupant matching name"""
        occ = tile.get("occupants")
        if isinstance(occ, list):
            return any(name in o.get("name", "") for o in occ)
        if isinstance(occ, str):  # toon format fallback
            return name in occ
        return False

    def _find_clear_block(self, w, h):
        """Find an empty flat w x h patch of land in the search area."""
        res = self.bot.tiles(self.x1, self.y1, self.x2, self.y2)
        tile_list = res.get("tiles", []) if isinstance(res, dict) else []
        grid = {(t.get("x"), t.get("y")): t for t in tile_list if isinstance(t, dict)}
        for y in range(self.y1, self.y2 - h + 1):
            for x in range(self.x1, self.x2 - w + 1):
                candidate_ok = True
                first_tile = grid.get((x, y))
                if not first_tile:
                    continue
                z_val = first_tile.get("terrain")
                z_base = int(z_val) if z_val is not None else 0
                for dy in range(h):
                    for dx in range(w):
                        t = grid.get((x + dx, y + dy))
                        if (
                            not t
                            or t.get("terrain", 0) != z_base
                            or t.get("occupants")
                            or (t.get("water", 0) or 0) > 0
                        ):
                            candidate_ok = False
                            break
                    if not candidate_ok:
                        break
                if candidate_ok and z_base > 0:
                    return x, y, z_base
        return None

    def _is_on_map(self, x1, y1, x2=None, y2=None):
        """Check if points are within known map bounds."""
        if not (0 <= x1 < self.map_x and 0 <= y1 < self.map_y):
            return False
        if x2 is not None and (y2 is None or not (0 <= x2 < self.map_x and 0 <= y2 < self.map_y)):
            return False
        return True

    def find_building(self, name) -> int | None:
        """find first building matching name, return id"""
        raw = self.bot._get("/api/buildings", params={"limit": 0})
        buildings = (
            raw
            if isinstance(raw, list)
            else raw.get("items", [])
            if isinstance(raw, dict)
            else []
        )
        for b in buildings:
            if isinstance(b, dict) and name.lower() in str(b.get("name", "")).lower():
                return b.get("id")
        return None

    def _load_doc_contracts(self):
        if self._doc_contract_cache is not None:
            return self._doc_contract_cache

        # File lives at python/tests/integration/validation_runner.py; the repo
        # root is four dirname()s up.
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "docs",
            "api-reference.md",
        )
        if not os.path.exists(path):
            self._doc_contract_cache = {}
            return self._doc_contract_cache

        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

        contracts = {}
        current_endpoint = None
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(r"^###\s+(GET|POST)\s+(/api/[^\s]+)", line)
            if m:
                current_endpoint = m.group(2)
                i += 1
                continue

            if (
                current_endpoint
                and line.startswith("#### Response")
                and "error" not in line.lower()
            ):
                j = i + 1
                while j < len(lines) and not lines[j].strip().startswith(
                    "| Field | Type | Description |"
                ):
                    if lines[j].strip().startswith("### "):
                        break
                    j += 1
                if j >= len(lines) or not lines[j].strip().startswith(
                    "| Field | Type | Description |"
                ):
                    i += 1
                    continue

                table = []
                j += 2  # skip header + separator
                while j < len(lines):
                    row = lines[j].strip()
                    if not row.startswith("|"):
                        break
                    parts = [p.strip() for p in row.strip("|").split("|")]
                    if len(parts) >= 3:
                        field, type_name, desc = parts[0], parts[1], parts[2]
                        for expanded in self._expand_doc_field_names(field):
                            table.append(
                                {
                                    "field": expanded,
                                    "type": self._normalize_doc_type(type_name),
                                    "description": desc,
                                    "optional": self._is_optional_doc_field(desc),
                                    "full_only": "detail=full" in desc.lower(),
                                    "id_only": "detail=id" in desc.lower(),
                                }
                            )
                    j += 1
                if table and current_endpoint not in contracts:
                    contracts[current_endpoint] = table
                i = j
                continue

            i += 1

        self._doc_contract_cache = contracts
        return contracts

    def _expand_doc_field_names(self, field):
        if "," in field and "." in field:
            prefix, suffix = field.rsplit(".", 1)
            return [
                f"{prefix}.{part.strip()}" for part in suffix.split(",") if part.strip()
            ]
        if "," in field:
            return [part.strip() for part in field.split(",") if part.strip()]
        return [field.strip()]

    def _normalize_doc_type(self, type_name):
        t = type_name.strip().lower()
        if t == "string":
            return str
        if t == "int":
            return int
        if t == "float":
            return float
        if t == "bool":
            return bool
        if t == "array":
            return list
        if t == "object":
            return dict
        return object

    def _is_optional_doc_field(self, description):
        d = description.lower()
        return "(optional" in d or "omitted if" in d or "absent fields mean" in d

    def _doc_contract_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "label": "summary",
                "path": "/api/summary",
                "fetch": lambda: self.bot.summary(),
                "kind": "object",
            },
            {
                "label": "time",
                "path": "/api/time",
                "fetch": lambda: self.bot.time(),
                "kind": "object",
            },
            {
                "label": "weather",
                "path": "/api/weather",
                "fetch": lambda: self.bot.weather(),
                "kind": "object",
            },
            {
                "label": "population",
                "path": "/api/population",
                "fetch": lambda: self.bot.population(),
                "kind": "list",
            },
            {
                "label": "resources",
                "path": "/api/resources",
                "fetch": lambda: self.bot.resources(),
                "kind": "object",
            },
            {
                "label": "districts",
                "path": "/api/districts",
                "fetch": lambda: self.bot.districts(),
                "kind": "list",
            },
            {
                "label": "buildings_full",
                "path": "/api/buildings",
                "fetch": lambda: self.bot.buildings(limit=0, detail="full"),
                "kind": "list",
            },
            {
                "label": "trees",
                "path": "/api/trees",
                "fetch": lambda: self.bot.trees(limit=0),
                "kind": "list",
            },
            {
                "label": "crops",
                "path": "/api/crops",
                "fetch": lambda: self.bot.crops(limit=0),
                "kind": "list",
            },
            {
                "label": "gatherables",
                "path": "/api/gatherables",
                "fetch": lambda: self.bot.gatherables(limit=0),
                "kind": "list",
            },
            {
                "label": "beavers_full",
                "path": "/api/beavers",
                "fetch": lambda: self.bot.beavers(limit=0, detail="full"),
                "kind": "list",
            },
            {
                "label": "prefabs",
                "path": "/api/prefabs",
                "fetch": lambda: self.bot.prefabs(),
                "kind": "list",
            },
            {
                "label": "power",
                "path": "/api/power",
                "fetch": lambda: self.bot.power(),
                "kind": "list",
            },
            {
                "label": "alerts",
                "path": "/api/alerts",
                "fetch": lambda: self.bot.alerts(),
                "kind": "list",
            },
            {
                "label": "wellbeing",
                "path": "/api/wellbeing",
                "fetch": lambda: self.bot.wellbeing(),
                "kind": "object",
            },
            {
                "label": "notifications",
                "path": "/api/notifications",
                "fetch": lambda: self.bot.notifications(),
                "kind": "list",
            },
            {
                "label": "distribution",
                "path": "/api/distribution",
                "fetch": lambda: self.bot.distribution(),
                "kind": "list",
            },
            {
                "label": "science",
                "path": "/api/science",
                "fetch": lambda: self.bot.science(),
                "kind": "object",
            },
            {
                "label": "speed",
                "path": "/api/speed",
                "fetch": lambda: self.bot.speed(),
                "kind": "object",
            },
            {
                "label": "workhours",
                "path": "/api/workhours",
                "fetch": lambda: self.bot.workhours(),
                "kind": "object",
            },
            {
                "label": "tree_clusters",
                "path": "/api/tree_clusters",
                "fetch": lambda: self.bot.tree_clusters(),
                "kind": "list",
            },
            {
                "label": "tiles",
                "path": "/api/tiles",
                "fetch": lambda: self.bot.tiles(
                    self.center_x, self.center_y, self.center_x + 3, self.center_y + 3
                ),
                "kind": "object",
            },
        ]

    def _doc_row_conditions(self, endpoint_path):
        if endpoint_path == "/api/buildings":
            return {
                "pausable": lambda row: True,
                "statuses": lambda row: bool(row.get("statuses")),
                "height": lambda row: (
                    row.get("name", "").lower().find("floodgate") >= 0
                ),
                "maxHeight": lambda row: (
                    row.get("name", "").lower().find("floodgate") >= 0
                ),
                "needsNutrients": lambda row: (
                    "breedingpod" in row.get("name", "").lower()
                ),
                "nutrients": lambda row: "breedingpod" in row.get("name", "").lower(),
                "isClutch": lambda row: (
                    row.get("name", "").lower().find("clutch") >= 0
                    or bool(row.get("isClutch"))
                ),
                "clutchEngaged": lambda row: (
                    row.get("name", "").lower().find("clutch") >= 0
                    or bool(row.get("isClutch"))
                ),
                "dwellers": lambda row: any(
                    token in row.get("name", "").lower()
                    for token in ("lodge", "barracks")
                ),
                "maxDwellers": lambda row: any(
                    token in row.get("name", "").lower()
                    for token in ("lodge", "barracks")
                ),
                "recipes": lambda row: (
                    "factory" in row.get("name", "").lower()
                    or "assembler" in row.get("name", "").lower()
                    or bool(row.get("recipes"))
                ),
                "currentRecipe": lambda row: (
                    "factory" in row.get("name", "").lower()
                    or "assembler" in row.get("name", "").lower()
                    or bool(row.get("recipes"))
                ),
                "entranceX": lambda row: (
                    "entranceX" in row or "entranceY" in row or "entranceZ" in row
                ),
                "entranceY": lambda row: (
                    "entranceX" in row or "entranceY" in row or "entranceZ" in row
                ),
                "entranceZ": lambda row: (
                    "entranceX" in row or "entranceY" in row or "entranceZ" in row
                ),
            }
        if endpoint_path == "/api/beavers":
            return {
                "carrying": lambda row: bool(row.get("carrying")),
                "carryAmount": lambda row: bool(row.get("carrying")),
                "overburdened": lambda row: bool(row.get("carrying")),
                "deterioration": lambda row: bool(row.get("isBot")),
                "activity": lambda row: "activity" in row,
            }
        if endpoint_path == "/api/prefabs":
            return {
                "scienceCost": lambda row: (
                    bool(row.get("scienceCost")) or "unlocked" in row
                ),
                "unlocked": lambda row: "scienceCost" in row or "unlocked" in row,
            }
        if endpoint_path == "/api/alerts":
            return {
                "workers": lambda row: row.get("type") == "unstaffed",
                "status": lambda row: row.get("type") == "status",
            }
        return {}

    def _collect_path_values(self, value, path):
        tokens = path.split(".") if path else []

        def walk(current, idx):
            if idx >= len(tokens):
                return [current]
            token = tokens[idx]
            if token.endswith("[]"):
                key = token[:-2]
                items = current.get(key) if isinstance(current, dict) else None
                if not isinstance(items, list):
                    return []
                values = []
                for item in items:
                    values.extend(walk(item, idx + 1))
                return values
            if not isinstance(current, dict) or token not in current:
                return []
            return walk(current[token], idx + 1)

        return walk(value, 0)

    def _doc_type_matches(self, value, expected):
        if expected == float and isinstance(value, (int, float)):
            return True
        if expected == bool:
            return isinstance(value, bool) or (
                isinstance(value, int) and value in (0, 1)
            )
        if expected == int:
            return isinstance(value, int) and not isinstance(value, bool)
        return isinstance(value, expected)

    def _audit_doc_contracts(self):
        print("\n=== docs contract audit ===\n")

        contracts = self._load_doc_contracts()
        specs = self._doc_contract_specs()
        for spec in specs:
            fields = contracts.get(spec["path"])
            if spec["path"] == "/api/crops" and not fields:
                fields = contracts.get("/api/trees")
            if not fields:
                self.skip(
                    f"docs contract {spec['label']}",
                    f"no docs table for {spec['path']}",
                )
                continue

            payload = spec["fetch"]()
            rows = (
                payload
                if spec["kind"] == "list" and isinstance(payload, list)
                else None
            )
            if spec["kind"] == "object" and not isinstance(payload, dict):
                self.check(
                    f"docs contract {spec['label']}",
                    False,
                    f"unexpected {type(payload).__name__}",
                )
                continue
            if spec["kind"] == "list" and not isinstance(rows, list):
                self.check(
                    f"docs contract {spec['label']}",
                    False,
                    f"unexpected {type(payload).__name__}",
                )
                continue
            if spec["kind"] == "list" and not rows:
                self.skip(f"docs contract {spec['label']}", "no rows on current save")
                continue
            if not isinstance(rows, list):
                continue

            conditions = self._doc_row_conditions(spec["path"])
            missing = []
            mismatched = []
            uncovered = []

            for field in fields:
                path = field["field"]
                expected = field["type"]
                if spec["kind"] == "object":
                    values = self._collect_path_values(payload, path)
                    if not values:
                        if not field["optional"]:
                            missing.append(path)
                        continue
                    if not all(
                        self._doc_type_matches(v, expected) for v in values[:20]
                    ):
                        mismatched.append(path)
                    continue

                predicate = conditions.get(path)
                if predicate:
                    candidates = [
                        row for row in rows if isinstance(row, dict) and predicate(row)
                    ]
                    if not candidates:
                        uncovered.append(path)
                        continue
                    values = []
                    for row in candidates:
                        values.extend(self._collect_path_values(row, path))
                    if not values:
                        missing.append(path)
                        continue
                    if not all(
                        self._doc_type_matches(v, expected) for v in values[:20]
                    ):
                        mismatched.append(path)
                    continue

                required_rows = [row for row in rows if isinstance(row, dict)]
                if not required_rows:
                    self.skip(
                        f"docs contract {spec['label']}", "no dict rows on current save"
                    )
                    break
                for row in required_rows[:20]:
                    values = self._collect_path_values(row, path)
                    if not values:
                        if not field["optional"]:
                            missing.append(path)
                        break
                    if not all(self._doc_type_matches(v, expected) for v in values[:5]):
                        mismatched.append(path)
                        break

            detail = []
            if missing:
                detail.append(f"missing={sorted(set(missing))[:8]}")
            if mismatched:
                detail.append(f"type={sorted(set(mismatched))[:8]}")
            if uncovered:
                detail.append(f"uncovered={sorted(set(uncovered))[:8]}")
            ok = not missing and not mismatched
            self.check(f"docs contract {spec['label']}", ok, "; ".join(detail))

    GROUPS = {
        "read": [
            "read_endpoints",
            "summary_projection",
            "map_moisture",
            "map_render",
            "map_stacking",
            "find_helper",
            "json_schema",
            "data_accuracy",
            "district_accuracy",
        ],
        "write": [
            "speed",
            "pause",
            "priority",
            "workers",
            "floodgate",
            "clutch",
            "haul_priority",
            "recipe",
            "farmhouse_action",
            "plantable_priority",
            "storage",
            "workhours",
            "distribution",
            "migrate",
            "unlock",
        ],
        "placement": [
            "placement_and_demolish",
            "orientation",
            "find_placement",
            "water_placement",
            "overridable_placement",
            "blocker_tracking",
        ],
        "path": [
            "path_flat",
            "path_1z",
            "path_1z_east",
            "path_1z_north",
            "path_1z_south",
            "path_1z_west",
            "path_2z",
            "path_2z_east",
            "path_2z_north",
            "path_2z_south",
            "path_2z_west",
            "path_errors",
            "path_astar_diagonal",
            "path_astar_obstacle",
            "path_astar_no_route",
            "path_sections",
        ],
        "crops": [
            "crops",
            "tree_marking",
            "find_planting",
            "clear_planting",
            "clear_trees",
            "demolish_crop",
        ],
        "buildings": [
            "building_detail",
            "building_inventory",
            "building_range",
            "building_recipes",
            "prefab_costs",
            "power_networks",
        ],
        "beavers": [
            "beaver_detail",
            "beaver_needs",
            "beaver_position",
            "beaver_district",
            "carried_goods",
            "bot_data",
            "bot_buildings",
            "bot_in_summary",
            "bot_toon_format",
            "bot_durability",
        ],
        "webhooks": ["webhooks"],
        "security": ["security"],
        "automation": ["automation"],
        "cli": ["cli_commands", "error_codes"],
        "perf": [
            "performance",
            "building_endpoint_perf",
            "brain_perf",
            "buildings_v2_parity",
        ],
        "wipe": ["wipe_all"],
    }

    # ordered list of groups for default run (perf and wipe excluded)
    DEFAULT_GROUPS = [
        "read",
        "write",
        "placement",
        "path",
        "crops",
        "buildings",
        "beavers",
        "webhooks",
        "security",
        "cli",
        "automation",
    ]

    def run(self):
        if not self.bot.ping():
            print("error: game not reachable")
            sys.exit(1)

        self.discover()
        self.run_groups(self.DEFAULT_GROUPS)

    def run_groups(self, groups):
        test_map = {
            name.replace("test_", ""): getattr(self, name)
            for name in dir(self)
            if name.startswith("test_")
        }
        for group in groups:
            tests = self.GROUPS.get(group, [])
            for name in tests:
                if name in test_map:
                    test_map[name]()

    def test_read_endpoints(self):
        print("\n=== read endpoints ===\n")

        # each read endpoint should return data without error
        reads = [
            ("ping", lambda: self.bot.ping()),
            ("summary", lambda: self.bot.summary()),
            ("alerts", lambda: self.bot.alerts()),
            ("buildings", lambda: self.bot.buildings()),
            ("trees", lambda: self.bot.trees()),
            ("gatherables", lambda: self.bot.gatherables()),
            ("beavers", lambda: self.bot.beavers()),
            ("resources", lambda: self.bot.resources()),
            ("population", lambda: self.bot.population()),
            ("weather", lambda: self.bot.weather()),
            ("time", lambda: self.bot.time()),
            ("districts", lambda: self.bot.districts()),
            ("distribution", lambda: self.bot.distribution()),
            ("science", lambda: self.bot.science()),
            ("notifications", lambda: self.bot.notifications()),
            ("workhours", lambda: self.bot.workhours()),
            ("speed", lambda: self.bot.speed()),
            ("prefabs", lambda: self.bot.prefabs()),
            (
                "tiles",
                lambda: self.bot.tiles(
                    self.center_x, self.center_y, self.center_x + 5, self.center_y + 5
                ),
            ),
            ("tree_clusters", lambda: self.bot.tree_clusters()),
            ("wellbeing", lambda: self.bot.wellbeing()),
        ]
        for name, fn in reads:
            result = fn()
            self.check(
                f"GET {name}",
                not self.err(result),
                json.dumps(result)[:100] if self.err(result) else "",
            )

    def test_speed(self):
        print("\n=== speed ===\n")

        # save original
        orig = self.bot.speed()
        orig_speed = orig.get("speed", 1) if isinstance(orig, dict) else 1

        # set to 0 (pause)
        result = self.bot.set_speed(0)
        self.check("set speed 0", not self.err(result))

        # verify via debug
        verify = self.debug_get("Write._speedManager.CurrentSpeed")
        if "skipped" in verify:
            self.skip("verify speed=0 via debug", "debug disabled")
        else:
            self.check(
                "verify speed=0 via debug",
                str(verify.get("result", "")) in ("0", "0.0"),
                f"got: {verify.get('result')}",
            )

        # restore
        self.bot.set_speed(orig_speed)

    def test_placement_and_demolish(self):
        print("\n=== placement + demolish ===\n")

        # find a valid spot dynamically for placement tests
        spot = self.find_spot("Path")
        if not spot:
            self.skip("placement tests", "no valid Path spot found")
            return
        sx, sy, sz = spot["x"], spot["y"], spot["z"]

        # error cases
        tests = [
            ("off map", lambda: self.bot.place_building("Path", 999, 999, 2)),
            ("z too high", lambda: self.bot.place_building("Path", sx, sy, sz + 10)),
            (
                "z too low",
                lambda: self.bot.place_building("Path", sx, sy, max(0, sz - 5)),
            ),
        ]
        for name, fn in tests:
            result = fn()
            self.check(name, self.err(result), json.dumps(result)[:100])

        # specific error codes
        specific_tests = [
            (
                "unknown prefab",
                lambda: self.bot.place_building("Fake", sx, sy, sz),
                "invalid_prefab",
            ),
            (
                "invalid orientation",
                lambda: self.bot.place_building(
                    "Path", sx, sy, sz, orientation="bogus"
                ),
                "invalid_param",
            ),
            (
                "locked building",
                lambda: self.bot.place_building(
                    self._locked_prefab or "FakeLockedBuilding", sx, sy, sz
                ),
                "not_unlocked",
            ),
        ]
        for name, fn, expect_code in specific_tests:
            result = fn()
            self.check(
                name,
                self.err(result) and result["error"].startswith(expect_code),
                json.dumps(result)[:100],
            )

        # valid placement using find_spot coords
        result = self.bot.place_building(
            "Path", sx, sy, sz, spot.get("orientation", "south")
        )
        self.check("valid placement", self.has(result, "id"))

        if self.has(result, "id"):
            placed_id = result["id"]

            # verify via map
            tile = self.bot.tiles(sx, sy, sx, sy)
            tiles = tile.get("tiles", [])
            has_path = any(self.tile_has(t, "Path") for t in tiles)
            self.check("verify placement via map", has_path)

            # demolish
            dem = self.bot.demolish_building(placed_id)
            self.check("demolish", self.has(dem, "demolished") or not self.err(dem))

            # verify gone
            import time

            no_path = False
            for _ in range(30):
                tile2 = self.bot.tiles(sx, sy, sx, sy)
                tiles2 = tile2.get("tiles", [])
                no_path = not any(self.tile_has(t, "Path") for t in tiles2)
                if no_path:
                    break
                time.sleep(0.2)
            self.check("verify demolish via map", no_path)

        # multi-tile z mismatch: find a spot where terrain changes within a footprint
        found_mismatch = False
        for tx in range(self.x1, self.x2 - 2):
            region = self.bot.tiles(tx, self.center_y, tx + 2, self.center_y + 1)
            tiles = region.get("tiles", [])
            if len(tiles) < 6:
                continue
            heights = set(t.get("terrain", 0) for t in tiles)
            occupied = any(t.get("occupants") for t in tiles)
            if len(heights) > 1 and not occupied and all(h >= 2 for h in heights):
                z = min(heights)
                result = self.bot.place_building(
                    self.prefab("Barrack"), tx, self.center_y, z
                )
                self.check(
                    "multi-tile z mismatch rejected",
                    self.err(result),
                    json.dumps(result)[:100],
                )
                found_mismatch = True
                break
        if not found_mismatch:
            self.skip("multi-tile z mismatch", "no height transition found")

    def test_priority(self):
        print("\n=== priority ===\n")

        # find the DC (always exists)
        dc_id = None
        buildings = self.bot.buildings()
        if isinstance(buildings, list):
            for b in buildings:
                if "DistrictCenter" in str(b.get("name", "")):
                    dc_id = b.get("id")
                    break
        if not dc_id:
            self.skip("priority tests", "no DC found")
            return

        # set workplace priority
        result = self.bot.set_priority(dc_id, "VeryHigh", type="workplace")
        self.check(
            "set priority VeryHigh",
            self.has(result, "workplacePriority")
            and result["workplacePriority"] == "VeryHigh",
            json.dumps(result)[:100],
        )

        # smart auto-detect (omit type): finished DC should pick workplace
        result_auto = self.bot.set_priority(dc_id, "High")
        self.check(
            "smart auto-detect priority (finished workplace)",
            self.has(result_auto, "workplacePriority")
            and result_auto["workplacePriority"] == "High",
            json.dumps(result_auto)[:100],
        )

        # restore
        self.bot.set_priority(dc_id, "Normal", type="workplace")

    def test_workers(self):
        print("\n=== workers ===\n")

        # find a workplace building
        dc_id = None
        buildings = self.bot.buildings()
        if isinstance(buildings, list):
            for b in buildings:
                if "DistrictCenter" in str(b.get("name", "")):
                    dc_id = b.get("id")
                    break
        if not dc_id:
            self.skip("worker tests", "no DC found")
            return

        # set workers to 1
        result = self.bot.set_workers(dc_id, 1)
        self.check(
            "set workers 1",
            self.has(result, "desiredWorkers") and result["desiredWorkers"] == 1,
            json.dumps(result)[:100],
        )

        # restore to 3
        self.bot.set_workers(dc_id, 3)

    def test_pause(self):
        print("\n=== pause/unpause ===\n")

        dc_id = self.find_building("DistrictCenter")
        if not dc_id:
            self.skip("pause tests", "no DC found")
            return

        # pause
        result = self.bot.pause_building(dc_id)
        self.check("pause building", not self.err(result))

        # verify via buildings endpoint
        buildings = self.bot.buildings()
        paused = False
        if isinstance(buildings, list):
            for b in buildings:
                if b.get("id") == dc_id:
                    paused = b.get("paused", False)
                    break
        self.check("verify paused", paused)

        # unpause
        self.bot.unpause_building(dc_id)

        # verify unpaused
        buildings2 = self.bot.buildings()
        unpaused = True
        if isinstance(buildings2, list):
            for b in buildings2:
                if b.get("id") == dc_id:
                    unpaused = not b.get("paused", True)
                    break
        self.check("verify unpaused", unpaused)

    def test_crops(self):
        print("\n=== crops ===\n")

        # find a farmhouse to get valid planting area
        fh = self.find_building("FarmHouse")
        if not fh:
            self.skip("crops", "no FarmHouse found")
            return
        fb = self.bot.buildings(id=fh)
        if not fb or not isinstance(fb, list) or not fb:
            self.skip("crops", "cannot get farmhouse details")
            return
        b = fb[0]
        bx, by, bz = b.get("x", 0), b.get("y", 0), b.get("z", 0)

        # plant near farmhouse
        result = self.bot.plant_crop(bx - 3, by - 3, bx + 3, by + 3, bz, "Carrot")
        self.check("plant crops", self.has(result, "planted"), json.dumps(result)[:100])

        # clear
        self.bot.clear_planting(bx - 3, by - 3, bx + 3, by + 3, bz)

    def test_tree_marking(self):
        print("\n=== tree marking ===\n")

        # find actual tree coords dynamically
        trees = self.bot.trees()
        alive_trees = (
            [t for t in trees if t.get("alive")] if isinstance(trees, list) else []
        )
        if not alive_trees:
            self.skip("tree marking", "no alive trees")
            return
        t = alive_trees[0]
        tx, ty, tz = t["x"], t["y"], t["z"]

        # mark trees in area around the found tree
        result = self.bot.mark_trees(tx - 3, ty - 3, tx + 3, ty + 3, tz)
        self.check(
            "mark trees",
            not self.err(result),
            json.dumps(result)[:100] if self.err(result) else "",
        )

        # verify via trees endpoint - some should be marked
        trees = self.bot.trees()
        marked = 0
        if isinstance(trees, list):
            marked = sum(1 for t in trees if t.get("marked"))
        self.check("verify trees marked", marked > 0, f"marked count: {marked}")

        # clear
        self.bot.clear_trees(tx - 3, ty - 3, tx + 3, ty + 3, tz)

    def test_storage(self):
        print("\n=== storage ===\n")

        # find any storage building (tank, warehouse, or pile)
        sid = (
            self.find_building("SmallTank")
            or self.find_building("MediumTank")
            or self.find_building("Warehouse")
            or self.find_building("Pile")
        )
        if not sid:
            self.skip("storage tests", "no storage building found")
            return

        # read current state from buildings detail:full
        detail: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings(
            id=int(sid), detail="full"
        )
        items: list[dict[str, Any]] = (
            detail if isinstance(detail, list) else detail.get("items", [detail])
        )
        b = items[0] if items else {}
        self.check(
            "has storageMode field", "storageMode" in b, f"keys: {list(b.keys())[:10]}"
        )
        self.check(
            "has allowedGood field", "allowedGood" in b, f"keys: {list(b.keys())[:10]}"
        )
        orig_mode = b.get("storageMode", "accept")
        orig_good = b.get("allowedGood", "")

        # set good to Water
        result = self.bot.set_storage(sid, good="Water")
        self.check(
            "set good to Water",
            not self.err(result),
            json.dumps(result)[:120] if self.err(result) else "",
        )
        self.check(
            "response has good=Water",
            isinstance(result, dict) and result.get("good") == "Water",
            f"got: {result.get('good', '?')}",
        )

        # verify via buildings read
        detail_v: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings(
            id=int(sid), detail="full"
        )
        items_v: list[dict[str, Any]] = (
            detail_v
            if isinstance(detail_v, list)
            else detail_v.get("items", [detail_v])
        )
        bv = items_v[0] if items_v else {}
        self.check(
            "read confirms good=Water",
            bv.get("allowedGood") == "Water",
            f"got: {bv.get('allowedGood', '?')}",
        )

        # verify via debug (ground truth)
        debug_good = self.debug_component_field(sid, "SingleGoodAllower", "AllowedGood")
        if debug_good is not None:
            self.check(
                "debug confirms good=Water",
                str(debug_good) == "Water",
                f"got: {debug_good}",
            )

        # clear good
        result = self.bot.set_storage(sid, good="none")
        self.check(
            "clear good",
            not self.err(result),
            json.dumps(result)[:120] if self.err(result) else "",
        )
        self.check(
            "response has good=empty",
            isinstance(result, dict) and result.get("good", "x") == "",
            f"got: {result.get('good', '?')}",
        )

        # test each storage mode
        for mode in ["obtain", "supply", "empty", "accept"]:
            result = self.bot.set_storage(sid, mode=mode)
            self.check(
                f"set mode {mode}",
                not self.err(result),
                json.dumps(result)[:120] if self.err(result) else "",
            )
            self.check(
                f"response mode={mode}",
                isinstance(result, dict) and result.get("mode") == mode,
                f"got: {result.get('mode', '?')}",
            )

        # verify mode via buildings read after accept
        detail_m: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings(
            id=int(sid), detail="full"
        )
        items_m: list[dict[str, Any]] = (
            detail_m
            if isinstance(detail_m, list)
            else detail_m.get("items", [detail_m])
        )
        bm = items_m[0] if items_m else {}
        self.check(
            "read confirms mode=accept",
            bm.get("storageMode") == "accept",
            f"got: {bm.get('storageMode', '?')}",
        )

        # verify via debug (ground truth)
        debug_accept = self.debug_component_field(
            sid, "StockpilePriority", "IsAcceptActive"
        )
        if debug_accept is not None:
            self.check(
                "debug confirms accept mode",
                str(debug_accept).lower() in ("true", "1"),
                f"got: {debug_accept}",
            )

        # test set both at once
        result = self.bot.set_storage(sid, good="Water", mode="obtain")
        self.check(
            "set good+mode together",
            not self.err(result),
            json.dumps(result)[:120] if self.err(result) else "",
        )
        self.check(
            "combined: good=Water",
            isinstance(result, dict) and result.get("good") == "Water",
            f"got: {result.get('good', '?')}",
        )
        self.check(
            "combined: mode=obtain",
            isinstance(result, dict) and result.get("mode") == "obtain",
            f"got: {result.get('mode', '?')}",
        )

        # verify via buildings read that storageMode updated
        detail2: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings(
            id=int(sid), detail="full"
        )
        items2: list[dict[str, Any]] = (
            detail2 if isinstance(detail2, list) else detail2.get("items", [detail2])
        )
        b2 = items2[0] if items2 else {}
        self.check(
            "read storageMode=obtain",
            b2.get("storageMode") == "obtain",
            f"got: {b2.get('storageMode', '?')}",
        )
        self.check(
            "read allowedGood=Water",
            b2.get("allowedGood") == "Water",
            f"got: {b2.get('allowedGood', '?')}",
        )

        # bad mode
        result = self.bot.set_storage(sid, mode="badvalue")
        self.check(
            "bad mode returns error",
            self.err(result) and "invalid_param" in str(result.get("error", "")),
            json.dumps(result)[:120],
        )

        # non-storage building
        path_id = self.find_building("Path")
        if path_id:
            result = self.bot.set_storage(path_id, mode="obtain")
            self.check(
                "non-storage returns error",
                self.err(result) and "invalid_type" in str(result.get("error", "")),
                json.dumps(result)[:120],
            )

        # restore original state
        self.bot.set_storage(sid, good=orig_good or "none", mode=orig_mode or "accept")

    def test_orientation(self):
        print("\n=== orientation ===\n")

        # find flat test area dynamically
        test_spot = None
        need = 5

        # bulk read tiles to avoid thousands of network calls
        region = self.bot.tiles(self.x1, self.y1, self.x2, self.y2)
        tiles_list = region.get("tiles", []) if isinstance(region, dict) else []
        tm = {(t["x"], t["y"]): t for t in tiles_list}

        for cy in range(self.y1, self.y2 - need + 1):
            for cx in range(self.x1, self.x2 - need + 1):
                sub = [
                    tm.get((cx + dx, cy + dy))
                    for dy in range(need)
                    for dx in range(need)
                ]
                if any(t is None for t in sub):
                    continue
                heights = set(t.get("terrain", 0) for t in sub if t)
                if len(heights) != 1:
                    continue
                tz = heights.pop()
                if tz < 2:
                    continue
                occupants = [
                    t
                    for t in sub
                    if t and (t.get("occupants") or (t.get("water", 0) or 0) > 0)
                ]
                if occupants:
                    continue

                test = self.bot.place_building("Path", cx, cy, tz, orientation="south")
                if "id" in test:
                    self.bot.demolish_building(test["id"])
                    test_spot = (cx, cy, tz)
                    break
            if test_spot:
                break

        if not test_spot:
            self.skip("orientation tests", "no flat 5x5 area")
            return

        bx, by, bz = test_spot
        print(f"  using ({bx},{by},z={bz})\n")

        # find multi-tile prefabs dynamically from the prefab list
        multi_tile = []
        prefabs = self.bot.prefabs()
        if isinstance(prefabs, list):
            for p in prefabs:
                sx = p.get("sizeX", 1)
                sy = p.get("sizeY", 1)
                name = p.get("name", "")
                if (sx > 1 or sy > 1) and sx <= 3 and sy <= 3 and name:
                    multi_tile.append((name, sx, sy))
                    if len(multi_tile) >= 3:
                        break
        if not multi_tile:
            self.skip("orientation tests", "no multi-tile prefabs found")
            return

        for prefab, sx, sy in multi_tile:
            for orient in ["south", "west", "north", "east"]:
                result = self.bot.place_building(prefab, bx, by, bz, orientation=orient)
                if "id" not in result:
                    if "not_unlocked" in str(result.get("error", "")):
                        self.skip(f"{prefab.split('.')[0]} {orient}", "not unlocked")
                        continue
                    self.check(
                        f"{prefab.split('.')[0]} {orient}",
                        False,
                        json.dumps(result)[:100],
                    )
                    continue

                # verify origin via map
                region = self.bot.tiles(bx - 1, by - 1, bx + sx, by + sy)
                pname = prefab.split(".")[0]
                occupied = []
                for t in region.get("tiles", []):
                    if self.tile_has(t, pname):
                        occupied.append((t["x"], t["y"]))
                min_x = min(t[0] for t in occupied) if occupied else -1
                min_y = min(t[1] for t in occupied) if occupied else -1
                self.check(
                    f"{prefab.split('.')[0]} {orient} origin=({min_x},{min_y})",
                    min_x == bx and min_y == by,
                    f"expected ({bx},{by})",
                )
                self.bot.demolish_building(result["id"])

    def test_find_placement(self):
        print("\n=== find_placement ===\n")

        result = self.bot.find_placement(
            self.prefab("Inventor"), self.x1, self.y1, self.x2, self.y2
        )
        self.check(
            "returns results",
            self.has(result, "placements") and len(result.get("placements", [])) > 0,
        )

        placements = result.get("placements", [])
        if not placements:
            return

        # check result fields
        p0 = placements[0]
        for field in [
            "x",
            "y",
            "z",
            "orientation",
            "entranceX",
            "entranceY",
            "pathAccess",
            "reachable",
            "distance",
            "nearPower",
            "flooded",
        ]:
            self.check(f"result has {field}", field in p0, f"keys: {list(p0.keys())}")

        # reachable spots
        reachable = [p for p in placements if p.get("reachable")]
        unreachable = [p for p in placements if not p.get("reachable", 0)]
        self.check(
            "has reachable placements",
            len(reachable) > 0,
            f"got {len(reachable)} reachable of {len(placements)}",
        )

        # verify reachable spot is actually placeable and connected
        if reachable:
            p = reachable[0]
            self.check("reachable has pathAccess", p.get("pathAccess") == 1)
            self.check(
                "reachable has distance >= 0",
                p.get("distance", -1) >= 0,
                f"distance={p.get('distance')}",
            )

            placed = self.bot.place_building(
                self.prefab("Inventor"),
                p["x"],
                p["y"],
                p["z"],
                orientation=p["orientation"],
            )
            if self.has(placed, "id"):
                alerts = self.bot.alerts()
                disconnected = False
                if isinstance(alerts, list):
                    disconnected = any(
                        a.get("id") == placed["id"]
                        and "not connected" in str(a.get("status", ""))
                        for a in alerts
                    )
                self.check("reachable spot is connected", not disconnected)
                self.bot.demolish_building(placed["id"])
            else:
                self.check("place at reachable spot", False, json.dumps(placed)[:100])

        # verify unreachable spots have distance -1
        if unreachable:
            p = unreachable[0]
            self.check(
                "unreachable has distance -1",
                p.get("distance") == -1,
                f"distance={p.get('distance')}",
            )

        # verify unreachable spot is actually disconnected (if we have one with pathAccess)
        unreachable_with_path = [p for p in unreachable if p.get("pathAccess", 0)]
        if unreachable_with_path:
            p = unreachable_with_path[0]
            placed = self.bot.place_building(
                self.prefab("Inventor"),
                p["x"],
                p["y"],
                p["z"],
                orientation=p["orientation"],
            )
            if self.has(placed, "id"):
                alerts = self.bot.alerts()
                disconnected = (
                    any(
                        a.get("id") == placed["id"]
                        and "not connected" in str(a.get("status", ""))
                        for a in alerts
                    )
                    if isinstance(alerts, list)
                    else False
                )
                self.check(
                    "unreachable spot IS disconnected",
                    disconnected,
                    f"expected disconnect alert for ({p['x']},{p['y']})",
                )
                self.bot.demolish_building(placed["id"])
            else:
                # placement rejected = also proves unreachable
                self.check("unreachable spot rejected by game", True)
        else:
            self.skip("unreachable verification", "no unreachable+pathAccess spots")

        # verify sort order: reachable before unreachable
        if reachable and unreachable:
            first_unreachable_idx = next(
                (i for i, p in enumerate(placements) if not p.get("reachable")),
                len(placements),
            )
            last_reachable_idx = max(
                i for i, p in enumerate(placements) if p.get("reachable")
            )
            self.check(
                "reachable sorted before unreachable",
                last_reachable_idx < first_unreachable_idx,
            )

        # nearPower check: find a spot near power and verify
        powered = [p for p in placements if p.get("nearPower")]
        if powered:
            self.check("nearPower spots exist", True)
        else:
            self.skip("nearPower verification", "no powered spots in results")

        # bounds check: no results should have coords outside map
        map_info = self.bot.tiles()
        if isinstance(map_info, dict) and "mapSize" in map_info:
            mx = map_info["mapSize"]["x"]
            my = map_info["mapSize"]["y"]
            oob = [
                p
                for p in placements
                if p["x"] < 0 or p["x"] >= mx or p["y"] < 0 or p["y"] >= my
            ]
            self.check(
                "no out-of-bounds results",
                len(oob) == 0,
                f"found {len(oob)} OOB placements",
            )

        # unknown prefab
        bad = self.bot.find_placement(
            "FakeBuilding", self.x1, self.y1, self.x1 + 10, self.y1 + 10
        )
        self.check("find_placement unknown prefab", self.err(bad))

        # entrance coords: entranceX/Y should be outside the building footprint
        # (it's the doorstep tile where a path goes, not inside the building)
        bsx, bsy = result.get("sizeX", 1), result.get("sizeY", 1)
        for p in placements[:3]:
            ex, ey = p.get("entranceX", 0), p.get("entranceY", 0)
            bx, by = p["x"], p["y"]
            orient = p["orientation"]
            sx, sy = bsx, bsy
            if orient in ("west", "east"):
                sx, sy = bsy, bsx
            inside = bx <= ex < bx + sx and by <= ey < by + sy
            self.check(
                f"entrance ({ex},{ey}) outside footprint ({bx},{by})-({bx + sx - 1},{by + sy - 1})",
                not inside,
            )

        # entrance adjacency: entrance tile should be exactly 1 tile from building edge
        for p in placements[:3]:
            ex, ey = p.get("entranceX", 0), p.get("entranceY", 0)
            bx, by = p["x"], p["y"]
            orient = p["orientation"]
            sx, sy = bsx, bsy
            if orient in ("west", "east"):
                sx, sy = bsy, bsx
            # entrance should be adjacent to footprint (distance 1 from edge)
            adj_x = (ex == bx - 1 or ex == bx + sx) and by <= ey < by + sy
            adj_y = (ey == by - 1 or ey == by + sy) and bx <= ex < bx + sx
            self.check(
                f"entrance ({ex},{ey}) adjacent to footprint",
                adj_x or adj_y,
                f"footprint ({bx},{by})-({bx + sx - 1},{by + sy - 1})",
            )

        # reachability via doorstep: build path from DC to a spot, verify reachable
        dc = self.find_building("DistrictCenter")
        if dc:
            dc_bld: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings(id=dc)
            dc_info = dc_bld[0] if isinstance(dc_bld, list) and dc_bld else None
            if dc_info:
                dcx, dcy = dc_info["x"], dc_info["y"]
                # find a placement spot on same z as DC
                dc_z = dc_info.get("z", 2)
                same_z = [p for p in placements if p["z"] == dc_z]
                if same_z:
                    spot = same_z[0]
                    ex, ey = spot["entranceX"], spot["entranceY"]
                    # build path from DC area to entrance (and track ids for cleanup)
                    bld_pre = self.bot.buildings()
                    ids_before = (
                        set(b["id"] for b in bld_pre)
                        if isinstance(bld_pre, list)
                        else set()
                    )
                    _path1 = self.bot.place_path(dcx, dcy - 1, ex, dcy - 1)
                    _path2 = self.bot.place_path(ex, dcy - 1, ex, ey)
                    self.wait_for_navmesh_settle()
                    # re-query placement to check reachability
                    result2 = self.bot.find_placement(
                        self.prefab("Inventor"),
                        spot["x"] - 1,
                        spot["y"] - 1,
                        spot["x"] + 1,
                        spot["y"] + 1,
                    )
                    p2 = (
                        result2.get("placements", [])
                        if isinstance(result2, dict)
                        else []
                    )
                    matching = [
                        p for p in p2 if p["x"] == spot["x"] and p["y"] == spot["y"]
                    ]
                    if matching:
                        self.check(
                            "spot reachable after path built",
                            matching[0].get("reachable") == 1,
                            f"reachable={matching[0].get('reachable')}",
                        )
                        self.check(
                            "pathAccess after path built",
                            matching[0].get("pathAccess") == 1,
                            f"pathAccess={matching[0].get('pathAccess')}",
                        )
                    else:
                        self.skip(
                            "reachability after path", "spot not in re-query results"
                        )
                    # cleanup: demolish paths placed by the test
                    bld_post = self.bot.buildings()
                    ids_after = (
                        set(b["id"] for b in bld_post)
                        if isinstance(bld_post, list)
                        else set()
                    )
                    for nid in ids_after - ids_before:
                        self.bot.demolish_building(nid)
                else:
                    self.skip("reachability via doorstep", "no spots at DC z-level")
            else:
                self.skip("reachability via doorstep", "could not read DC")
        else:
            self.skip("reachability via doorstep", "no DC found")

    def test_water_placement(self):
        print("\n=== water placement ===\n")

        # find the water pump prefab for this faction
        pump_prefab = self.prefab("WaterPump")
        if pump_prefab not in self.prefab_names:
            pump_prefab = self.prefab("DeepWaterPump")
        if pump_prefab not in self.prefab_names:
            self.skip("water placement", "no water pump prefab found")
            return

        # search wide area for pump placements
        result = self.bot.find_placement(
            pump_prefab, self.x1 - 20, self.y1 - 20, self.x2 + 20, self.y2 + 20
        )
        placements = result.get("placements", []) if isinstance(result, dict) else []
        if not placements:
            self.skip("water placement", "no pump placements found")
            return

        # water buildings should have waterDepth and entrance fields
        p0 = placements[0]
        self.check(
            "pump has waterDepth field", "waterDepth" in p0, f"keys: {list(p0.keys())}"
        )
        self.check("pump has flooded field", "flooded" in p0)
        self.check("pump has entranceX", "entranceX" in p0)
        self.check("pump has entranceY", "entranceY" in p0)
        # booleans should be 0/1 not true/false
        self.check(
            "flooded is int",
            isinstance(p0.get("flooded"), int),
            f"type={type(p0.get('flooded')).__name__}",
        )

        # find spots with water access
        with_water = [p for p in placements if p.get("waterDepth", 0) > 0]
        without_water = [p for p in placements if p.get("waterDepth", 0) == 0]
        self.check(
            "found spots with waterDepth > 0",
            len(with_water) > 0,
            f"got {len(with_water)} of {len(placements)}",
        )

        # waterDepth should sort before no-water spots
        if with_water and without_water:
            first_dry = next(
                (i for i, p in enumerate(placements) if p.get("waterDepth", 0) == 0),
                len(placements),
            )
            last_wet = max(
                i for i, p in enumerate(placements) if p.get("waterDepth", 0) > 0
            )
            self.check(
                "waterDepth spots sorted before dry spots",
                last_wet < first_dry,
                f"last wet={last_wet}, first dry={first_dry}",
            )

        # pump at water edge should NOT be flagged flooded
        # (MatterBelow.Any tiles with water are expected, not flooded)
        wet_not_flooded = [p for p in with_water if not p.get("flooded", 0)]
        self.check(
            "pump with water not flagged flooded",
            len(wet_not_flooded) > 0,
            f"all {len(with_water)} wet spots are flooded",
        )

        # waterDepth should be a reasonable float (not 0, not huge)
        if with_water:
            wd = with_water[0]["waterDepth"]
            self.check(
                "waterDepth is float > 0",
                isinstance(wd, (int, float)) and wd > 0,
                f"got {wd}",
            )
            self.check("waterDepth < 10 (reasonable)", wd < 10, f"got {wd}")

        # place a pump at the best water spot and verify it works
        if wet_not_flooded:
            p = wet_not_flooded[0]
            placed = self.bot.place_building(
                pump_prefab, p["x"], p["y"], p["z"], orientation=p["orientation"]
            )
            if self.has(placed, "id"):
                self.check("pump placed successfully", True)
                # verify it shows up in buildings
                bld = self.bot.buildings(id=placed["id"])
                if isinstance(bld, list) and bld:
                    b = bld[0]
                    self.check(
                        "pump is water building",
                        b.get("name", "").lower().find("pump") >= 0
                        or "Water" in str(b.get("recipes", b.get("currentRecipe", ""))),
                    )
                self.bot.demolish_building(placed["id"])
            else:
                self.check(
                    "pump placement at water spot", False, json.dumps(placed)[:100]
                )

        # non-water building should NOT have waterDepth field
        lodge_result = self.bot.find_placement(
            self.prefab("Lodge")
            if "Lodge" in self.prefab("Lodge")
            else self.prefab("Rowhouse"),
            self.x1,
            self.y1,
            self.x2,
            self.y2,
        )
        lodge_placements = (
            lodge_result.get("placements", []) if isinstance(lodge_result, dict) else []
        )
        if lodge_placements:
            self.check(
                "non-water building has no waterDepth",
                "waterDepth" not in lodge_placements[0],
                f"keys: {list(lodge_placements[0].keys())}",
            )
        else:
            self.skip("non-water waterDepth check", "no lodge placements")

        # tiles water accuracy: check a water tile and a dry tile
        if with_water:
            p = with_water[0]
            tiles = self.bot.tiles(p["x"], p["y"], p["x"] + 1, p["y"] + 1)
            tile_list = tiles.get("tiles", []) if isinstance(tiles, dict) else []
            water_tiles = [t for t in tile_list if (t.get("water", 0) or 0) > 0]
            _dry_tiles = [t for t in tile_list if (t.get("water", 0) or 0) == 0]
            if water_tiles:
                wt = water_tiles[0]
                self.check(
                    "water tile has float depth",
                    isinstance(wt["water"], (int, float))
                    and wt["water"] > 0
                    and wt["water"] < 10,
                    f"water={wt['water']}",
                )

    def _path_demolish_range(self, x1, y1, x2, y2):
        """demolish all paths/stairs/platforms in a range"""
        buildings = self.bot.buildings()
        if not isinstance(buildings, list):
            return
        for b in buildings:
            bx, by = b.get("x", -1), b.get("y", -1)
            name = str(b.get("name", ""))
            if (
                name in ("Path", "Stairs", "Platform")
                and min(x1, x2) <= bx <= max(x1, x2)
                and min(y1, y2) <= by <= max(y1, y2)
            ):
                self.bot.demolish_building(b["id"])

    def test_wipe_all(self):
        print("\n=== wipe all buildings and crops ===\n")
        buildings = self.bot.buildings()
        if not isinstance(buildings, list):
            self.check(
                "wipe all buildings and crops",
                False,
                "buildings endpoint returned invalid data",
            )
            return
        targets = [b for b in buildings if b.get("id") is not None]
        for b in targets:
            self.bot.demolish_building(b["id"])

        crops = self.bot.crops(limit=0)
        deleted_crops = 0
        crop_items = []
        if isinstance(crops, dict):
            crop_items = crops.get("items", [])
        elif isinstance(crops, list):
            crop_items = crops
        for crop in crop_items:
            crop_id = crop.get("id")
            if crop_id is None:
                continue
            self.bot.demolish_crop(crop_id)
            deleted_crops += 1

        remaining = self.bot.buildings()
        remaining_crops = self.bot.crops()
        remaining_count = 0
        remaining_crop_count = 0
        if isinstance(remaining, list):
            remaining_count = len([b for b in remaining if b.get("id") is not None])
        if isinstance(remaining_crops, list):
            remaining_crop_count = len(remaining_crops)
        self.check(
            "wipe all buildings and crops",
            remaining_count == 0 and remaining_crop_count == 0,
            f"remaining_buildings={remaining_count} remaining_crops={remaining_crop_count} deleted_buildings={len(targets)} deleted_crops={deleted_crops}",
        )

    def _path_place_lumberjacks_north(self, x1, y1, x2, y2):
        """place a lumberjack flag 1 tile north of each endpoint if not already present"""
        prefab = self.prefab("LumberjackFlag")
        buildings = self.bot.buildings()
        existing = set()
        if isinstance(buildings, list):
            for b in buildings:
                if "LumberjackFlag" in str(b.get("name", "")):
                    existing.add((b.get("x"), b.get("y"), b.get("z")))
        endpoints = {(x1, y1), (x2, y2)}
        for ex, ey in endpoints:
            bx = ex
            by = ey + 1
            tiles = self.bot.tiles(bx, by, bx, by)
            tile_list = tiles.get("tiles", []) if isinstance(tiles, dict) else []
            if not tile_list:
                continue
            bz = tile_list[0].get("terrain", 0)
            if bz <= 0:
                continue
            if (bx, by, bz) in existing:
                continue
            result = self.bot.place_building(prefab, bx, by, bz, "south")
            if isinstance(result, dict) and result.get("id"):
                existing.add((bx, by, bz))

    def _path_place_and_check(
        self, label, x1, y1, x2, y2, expect_stairs=False, expect_platforms=False
    ):
        """place a path and verify results. no cleanup, paths stay for visual inspection"""
        result = self.bot.place_path(x1, y1, x2, y2)
        placed = result.get("placed", {}) if isinstance(result, dict) else {}
        errs = str(result.get("errors", ""))

        if "not unlocked" in errs or "not_unlocked" in errs:
            self.skip(label, "stairs/platforms not unlocked")
            return

        self.check(
            f"{label}: paths placed",
            placed.get("paths", 0) > 0,
            json.dumps(result)[:120],
        )
        if expect_stairs:
            self.check(
                f"{label}: stairs placed",
                placed.get("stairs", 0) > 0,
                json.dumps(result)[:120],
            )
        if expect_platforms:
            self.check(
                f"{label}: platforms placed",
                placed.get("platforms", 0) > 0,
                json.dumps(result)[:120],
            )
        self.check(
            f"{label}: no skipped",
            result.get("skipped", 0) == 0,
            json.dumps(result)[:120],
        )

    def test_path_flat(self):
        print("\n=== path routing: flat ===\n")
        block = self._find_clear_block(5, 5)
        if not block:
            self.skip("flat path tests", "no 5x5 empty clearing found")
            return
        bx, by, _ = block
        self._path_place_and_check("flat east", bx + 0, by + 4, bx + 4, by + 4)
        self._path_place_and_check("flat west", bx + 4, by + 3, bx + 0, by + 3)
        self._path_place_and_check("flat north", bx + 0, by + 0, bx + 0, by + 4)
        self._path_place_and_check("flat south", bx + 1, by + 4, bx + 1, by + 0)

    def test_path_1z_east(self):
        print("\n=== path routing: 1 z-level east ===\n")
        if not self._is_on_map(160, 144, 177, 151):
            self.skip("1z east", "hardcoded region outside map")
            return
        self._path_demolish_range(160, 144, 177, 151)
        self._path_place_lumberjacks_north(161, 145, 176, 150)
        self._path_place_and_check("1z east", 161, 145, 176, 150, expect_stairs=True)

    def test_path_1z_west(self):
        print("\n=== path routing: 1 z-level west ===\n")
        if not self._is_on_map(160, 148, 172, 150):
            self.skip("1z west", "hardcoded region outside map")
            return
        self._path_demolish_range(160, 148, 172, 150)
        self._path_place_lumberjacks_north(171, 149, 161, 149)
        self._path_place_and_check("1z west", 171, 149, 161, 149, expect_stairs=True)

    def test_path_1z_north(self):
        print("\n=== path routing: 1 z-level north ===\n")
        if not self._is_on_map(158, 148, 160, 155):
            self.skip("1z north", "hardcoded region outside map")
            return
        self._path_demolish_range(158, 148, 160, 155)
        self._path_place_and_check("1z north", 159, 149, 159, 154, expect_stairs=True)

    def test_path_1z_south(self):
        print("\n=== path routing: 1 z-level south ===\n")
        if not self._is_on_map(162, 144, 164, 155):
            self.skip("1z south", "hardcoded region outside map")
            return
        self._path_demolish_range(162, 144, 164, 155)
        self._path_place_and_check("1z south", 163, 154, 163, 145, expect_stairs=True)

    def test_path_1z(self):
        print("\n=== path routing: 1 z-level ===\n")
        self.test_path_1z_east()
        self.test_path_1z_west()
        self.test_path_1z_north()
        self.test_path_1z_south()

    def test_path_2z_north(self):
        print("\n=== path routing: 2 z-level north ===\n")
        if not self._is_on_map(136, 145, 138, 153):
            self.skip("2z north", "hardcoded region outside map")
            return
        self._path_demolish_range(136, 145, 138, 153)
        self._path_place_and_check(
            "2z north", 137, 146, 137, 152, expect_stairs=True, expect_platforms=True
        )

    def test_path_2z_south(self):
        print("\n=== path routing: 2 z-level south ===\n")
        if not self._is_on_map(136, 145, 138, 153):
            self.skip("2z south", "hardcoded region outside map")
            return
        self._path_demolish_range(136, 145, 138, 153)
        self._path_place_and_check(
            "2z south", 137, 152, 137, 146, expect_stairs=True, expect_platforms=True
        )

    def test_path_2z_east(self):
        print("\n=== path routing: 2 z-level east ===\n")
        if not self._is_on_map(147, 153, 155, 155):
            self.skip("2z east", "hardcoded region outside map")
            return
        self._path_demolish_range(147, 153, 155, 155)
        self._path_place_and_check(
            "2z east", 148, 154, 154, 154, expect_stairs=True, expect_platforms=True
        )

    def test_path_2z_west(self):
        print("\n=== path routing: 2 z-level west ===\n")
        if not self._is_on_map(147, 154, 155, 156):
            self.skip("2z west", "hardcoded region outside map")
            return
        self._path_demolish_range(147, 154, 155, 156)
        self._path_place_and_check(
            "2z west", 154, 155, 148, 155, expect_stairs=True, expect_platforms=True
        )

    def test_path_2z(self):
        print("\n=== path routing: 2 z-level ===\n")
        self.test_path_2z_north()
        self.test_path_2z_south()
        self.test_path_2z_east()
        self.test_path_2z_west()

    def test_path_errors(self):
        print("\n=== path routing: errors ===\n")
        # out-of-bounds coordinates should fail (beyond map edge)
        result = self.bot.place_path(
            self.map_x + 10, self.map_y + 10, self.map_x + 15, self.map_y + 10
        )
        self.check(
            "out-of-bounds path fails",
            self.err(result) or result.get("placed", {}).get("paths", 0) == 0,
            json.dumps(result)[:100],
        )

    def test_path_astar_diagonal(self):
        """A* routes from the bottom-left corner to the top-right corner."""
        print("\n=== path routing: A* diagonal ===\n")

        def print_timings(label, result):
            timings = result.get("timings", {}) if isinstance(result, dict) else {}
            if not timings:
                return
            print(
                f"  {label} timings: total={timings.get('totalMs', 0):.1f}ms "
                f"snapshot={timings.get('snapshotMs', 0):.1f}ms "
                f"graph={timings.get('graphMs', 0):.1f}ms "
                f"astar={timings.get('astarMs', 0):.1f}ms "
                f"placement={timings.get('placementMs', 0):.1f}ms "
                f"queuedFrames={timings.get('framesQueued', 0)} "
                f"activeFrames={timings.get('framesActive', 0)} "
                f"attempts={timings.get('placementsAttempted', 0)} "
                f"nodes={timings.get('graphNodes', 0)} "
                f"pathNodes={timings.get('pathNodes', 0)}"
            )

        block = self._find_clear_block(10, 10)
        if block:
            bx, by, _ = block
            x1, y1 = bx, by
            x2, y2 = bx + 9, by + 9
        else:
            x1, y1, x2, y2 = self.x1, self.y1, self.x1 + 9, self.y1 + 9
        result = self.bot.place_path(x1, y1, x2, y2, timings=True)
        print_timings("diagonal", result)
        placed = result.get("placed", {}) if isinstance(result, dict) else {}
        self.check(
            "diagonal: paths placed via A*",
            placed.get("paths", 0) > 0,
            json.dumps(result)[:120],
        )
        self.check(
            "diagonal: no fallback",
            not result.get("fallback", False),
            json.dumps(result)[:120],
        )
        self.check(
            "diagonal: no skipped",
            result.get("skipped", 0) == 0,
            json.dumps(result)[:120],
        )
        self.check(
            "diagonal: no errors", not result.get("errors"), json.dumps(result)[:120]
        )

        # second path: top-left to bottom-right (within same clear block)
        bx = by = 0
        if block:
            bx, by, _ = block
            x3, y3 = bx, by + 9
            x4, y4 = bx + 9, by
        else:
            bx, by = 0, 0
            x3, y3 = 0, min(10, self.map_y - 1)
            x4, y4 = min(10, self.map_x - 1), 0
        result2 = self.bot.place_path(x3, y3, x4, y4, timings=True)
        print_timings("diagonal2", result2)
        placed2 = result2.get("placed", {}) if isinstance(result2, dict) else {}
        self.check(
            "diagonal2: paths placed via A*",
            placed2.get("paths", 0) > 0,
            json.dumps(result2)[:120],
        )
        self.check(
            "diagonal2: no fallback",
            not result2.get("fallback", False),
            json.dumps(result2)[:120],
        )
        self.check(
            "diagonal2: no skipped",
            result2.get("skipped", 0) == 0,
            json.dumps(result2)[:120],
        )
        self.check(
            "diagonal2: no errors", not result2.get("errors"), json.dumps(result2)[:120]
        )

    def test_path_astar_obstacle(self):
        """A* routes around a building placed in the middle of a straight path."""
        print("\n=== path routing: A* obstacle avoidance ===\n")
        # isolated y=147 row, away from flat tests (y=142-143) and 1z tests
        x1, y1, x2, y2 = 160, 147, 168, 147

        # place a blocker building in the middle of the straight line
        mid_x = (x1 + x2) // 2  # 164
        blocker_prefab = self.prefab("Inventor")
        spot = self.bot.find_placement(
            blocker_prefab, mid_x - 1, y1 - 1, mid_x + 1, y1 + 1
        )
        placements = spot.get("placements", []) if isinstance(spot, dict) else []
        if not placements:
            self.skip(
                "astar obstacle",
                f"no valid spot for {blocker_prefab} near ({mid_x},{y1})",
            )
            return
        p = placements[0]
        placed_blocker = self.bot.place_building(
            blocker_prefab, p["x"], p["y"], p.get("z", 0), p.get("orientation", "south")
        )
        blocker_id = (
            placed_blocker.get("id") if isinstance(placed_blocker, dict) else None
        )
        if not blocker_id:
            self.skip("astar obstacle", f"could not place {blocker_prefab}")
            return

        result = self.bot.place_path(x1, y1, x2, y2)
        placed = result.get("placed", {}) if isinstance(result, dict) else {}
        straight_dist = abs(x2 - x1)  # 8 tiles
        self.check(
            "obstacle: paths placed",
            placed.get("paths", 0) > 0,
            json.dumps(result)[:120],
        )
        self.check(
            "obstacle: detour taken (paths > straight distance)",
            placed.get("paths", 0) > straight_dist,
            f"paths={placed.get('paths', 0)}, straight={straight_dist}",
        )
        self.check(
            "obstacle: no fallback",
            not result.get("fallback", False),
            json.dumps(result)[:120],
        )

    def test_path_astar_no_route(self):
        """A* returns error or no paths when coords are out of bounds."""
        print("\n=== path routing: A* no route ===\n")
        # coords well beyond map bounds. A* grid won't contain valid tiles
        mx, my = self.map_x + 50, self.map_y + 50
        result = self.bot.place_path(mx, my, mx + 5, my)
        no_route = (
            self.err(result)
            or result.get("placed", {}).get("paths", 0) == 0
            or result.get("fallback", False)
        )
        self.check("out-of-bounds: no route", no_route, json.dumps(result)[:120])

    def test_path_sections(self):
        """Verify sections param stops path placement after N connector crossings."""
        print("\n=== path routing: sections limit ===\n")
        # route a long diagonal path with sections=1. should stop after first connector
        block = self._find_clear_block(10, 10)
        if block:
            bx, by, _ = block
            x1, y1 = bx, by
            x2, y2 = bx + 9, by + 9
        else:
            x1, y1, x2, y2 = self.x1, self.y1, self.x1 + 9, self.y1 + 9
        result = self.bot.place_path(x1, y1, x2, y2, sections=1)
        placed = result.get("placed", {}) if isinstance(result, dict) else {}
        stopped = result.get("stoppedAt") if isinstance(result, dict) else None
        self.check(
            "sections: paths placed",
            placed.get("paths", 0) > 0,
            json.dumps(result)[:120],
        )
        self.check(
            "sections: stopped before reaching goal",
            stopped is not None,
            f"stoppedAt={stopped}",
        )

    def test_demolish_crop(self):
        """Verify demolish_crop returns success for a valid crop."""
        print("\n=== demolish crop ===\n")
        crops = self.bot.crops()
        crop_list = (
            crops.get("items", [])
            if isinstance(crops, dict)
            else (crops if isinstance(crops, list) else [])
        )
        if not crop_list:
            self.skip("demolish crop", "no crops on map")
            return
        crop = crop_list[0]
        crop_id = crop.get("id")
        if crop_id is None:
            self.skip("demolish crop", "crop has no id")
            return
        result = self.bot.demolish_crop(crop_id)
        self.check(
            "demolish crop: no error",
            not self.err(result),
            json.dumps(result)[:120] if isinstance(result, dict) else str(result)[:120],
        )
        # verify it's gone
        crops_after = self.bot.crops()
        crop_list_after = (
            crops_after.get("items", [])
            if isinstance(crops_after, dict)
            else (crops_after if isinstance(crops_after, list) else [])
        )
        ids_after = {c.get("id") for c in crop_list_after}
        self.check(
            "demolish crop: crop removed",
            crop_id not in ids_after,
            f"id {crop_id} still present in {len(crop_list_after)} crops",
        )

    def test_blocker_tracking(self):
        """Verify uncached block objects (ruins, editor objects) appear in tiles and block placement."""
        print("\n=== blocker tracking (ruins / editor objects) ===\n")

        # scan a large tile region for any ruin/editor occupants
        tiles_result = self.bot._get_json(
            "/api/tiles", {"x1": 0, "y1": 0, "x2": self.map_x - 1, "y2": self.map_y - 1}
        )
        if self.err(tiles_result):
            self.skip("blocker tracking", "tiles endpoint error")
            return

        ruin_tiles = []
        if not isinstance(tiles_result, dict):
            self.skip("blocker tracking", "tiles not dict")
            return
        for t in tiles_result.get("tiles", []):
            for occ in t.get("occupants") or []:
                name = occ if isinstance(occ, str) else occ.get("name", "")
                if "Ruin" in name or "Underground" in name or "MapEditor" in name:
                    ruin_tiles.append((t.get("x"), t.get("y"), name))

        self.check(
            "blockers: ruins visible in /api/tiles",
            len(ruin_tiles) > 0,
            f"found {len(ruin_tiles)} ruin occupant tiles",
        )

        if not ruin_tiles:
            self.skip(
                "blocker placement", "no ruin tiles found to test placement against"
            )
            return

        # try placing on a ruin tile. should fail with a named blocker, not "unknown"
        rx, ry, _rname = ruin_tiles[0]
        path_prefab = "Path"
        tz = 0
        # get terrain height at that tile
        spot_tiles = self.bot._get_json(
            "/api/tiles", {"x1": rx, "y1": ry, "x2": rx, "y2": ry}
        )
        if not isinstance(spot_tiles, dict):
            return
        for t in spot_tiles.get("tiles", []):
            if t.get("x") == rx and t.get("y") == ry:
                tz = t.get("terrainHeight", 0)
                break

        result = self.bot.place_building(path_prefab, rx, ry, tz, "south")
        if isinstance(result, dict) and result.get("error"):
            err_msg = result["error"]
            self.check(
                "blockers: placement names the blocker (not 'unknown')",
                "unknown" not in err_msg,
                err_msg[:120],
            )
        else:
            # placement succeeded (ruin might be overridable). that's fine, not a failure
            self.check(
                "blockers: placement on ruin tile",
                True,
                "placement succeeded (ruin may be overridable)",
            )

    def test_overridable_placement(self):
        print("\n=== overridable placement ===\n")

        # find a non-overridable tree (dead standing or alive). should block placement
        # find an overridable entity (empty cut stump). should allow placement
        # use debug to check BlockObject.Overridable on each

        trees = self.bot.trees(limit=500)
        if not isinstance(trees, list) or len(trees) == 0:
            self.skip("overridable placement", "no trees found")
            return

        blocking_tree = None
        overridable_tree = None

        def is_overridable(entity_id):
            """check BlockObject.Overridable via debug endpoint"""
            self.bot.debug(target="call", method="FindEntity", arg0=str(entity_id))
            # BlockObject is at index 20 for natural resources (Pine, Birch, etc.)
            r = self.bot.debug(target="get", path="$.AllComponents.[20].Overridable")
            val = r.get("value")
            if val in ("True", "False"):
                return val == "True"
            # fallback: scan nearby indices
            for idx in range(18, 25):
                self.bot.debug(target="call", method="FindEntity", arg0=str(entity_id))
                r = self.bot.debug(target="get", path=f"$.AllComponents.[{idx}]")
                if "BlockObject" in r.get("type", "") and "Spec" not in r.get(
                    "type", ""
                ):
                    self.bot.debug(
                        target="call", method="FindEntity", arg0=str(entity_id)
                    )
                    ov = self.bot.debug(
                        target="get", path=f"$.AllComponents.[{idx}].Overridable"
                    )
                    return ov.get("value") == "True"
            return False

        # living trees are always non-overridable
        # dead trees might be overridable (empty cut stumps)
        for t in trees:
            if not t.get("id"):
                continue
            if t.get("alive") and blocking_tree is None:
                blocking_tree = t
            elif not t.get("alive") and overridable_tree is None:
                if is_overridable(t["id"]):
                    overridable_tree = t

            if blocking_tree and overridable_tree:
                break

        # test: non-overridable tree blocks placement
        if blocking_tree:
            bx, by, bz = blocking_tree["x"], blocking_tree["y"], blocking_tree["z"]
            result = self.bot.place_building("Path", bx, by, bz)
            self.check(
                "non-overridable tree blocks",
                self.err(result),
                json.dumps(result)[:100],
            )
        else:
            self.skip("non-overridable tree blocks", "no blocking tree found")

        # test: overridable stump allows placement
        if overridable_tree:
            ox, oy, oz = (
                overridable_tree["x"],
                overridable_tree["y"],
                overridable_tree["z"],
            )
            result = self.bot.place_building("Path", ox, oy, oz)
            placed = self.has(result, "id")
            self.check(
                "overridable stump allows placement", placed, json.dumps(result)[:100]
            )
            # clean up
            if placed:
                self.bot.demolish_building(result["id"])
        else:
            self.skip(
                "overridable stump allows placement", "no overridable stump found"
            )

    def test_summary_projection(self):
        print("\n=== summary projection ===\n")

        # toon summary has flat fields (foodDays, waterDays, etc)
        result = self.toon_bot.summary()
        if isinstance(result, dict):
            self.check("foodDays present", "foodDays" in result)
            self.check("waterDays present", "waterDays" in result)
            if "foodDays" in result:
                fd = result["foodDays"]
                self.check(
                    "foodDays > 0",
                    isinstance(fd, (int, float)) and fd > 0,
                    f"got: {fd}",
                )
            if "waterDays" in result:
                wd = result["waterDays"]
                self.check(
                    "waterDays >= 0",
                    isinstance(wd, (int, float)) and wd >= 0,
                    f"got: {wd}",
                )
            self.check(
                "logDays present",
                "logDays" in result,
                f"keys: {[k for k in result if 'Days' in k]}",
            )
            self.check("plankDays present", "plankDays" in result)
            self.check("gearDays present", "gearDays" in result)

            # verify bot exclusion in organic metrics
            # foodDays = totalFood / organicTotal. If bots don't eat, they shouldn't lower the foodDays.
            pop = self._as_list(self.bot.population())
            if isinstance(pop, list) and len(pop) > 0:
                d = pop[0]
                total_pop = d.get("adults", 0) + d.get("children", 0) + d.get("bots", 0)
                organic_pop = d.get("adults", 0) + d.get("children", 0)
                if organic_pop > 0 and d.get("bots", 0) > 0:
                    resources = self.bot.resources()
                    total_food, total_logs, total_planks, total_gears = 0, 0, 0, 0
                    # Sum items to match explicit backend summation list
                    fkeys = {
                        "Berries",
                        "Kohlrabi",
                        "Carrot",
                        "Potato",
                        "Wheat",
                        "Bread",
                        "Cassava",
                        "Corn",
                        "Eggplant",
                        "Soybean",
                        "MapleSyrup",
                    }

                    def acc(item, qty):
                        nonlocal total_food, total_logs, total_planks, total_gears
                        if item in fkeys:
                            total_food += qty
                        elif item == "Log":
                            total_logs += qty
                        elif item == "Plank":
                            total_planks += qty
                        elif item == "Gear":
                            total_gears += qty

                    res_data: Any = resources
                    if isinstance(res_data, dict):
                        for dg in res_data.values():
                            if isinstance(dg, dict):
                                for k, v in dg.items():
                                    if isinstance(v, dict):
                                        val = v.get("available", 0)
                                        if not isinstance(val, (int, float)):
                                            val = 0
                                        acc(k, val)
                    elif isinstance(res_data, list):
                        for r in res_data:
                            if isinstance(r, dict):
                                val = r.get("available", 0)
                                if not isinstance(val, (int, float)):
                                    val = 0
                                acc(r.get("good"), val)

                    expected_food_days = float(total_food) / organic_pop
                    actual_food_days = result.get("foodDays", 0)
                    self.check(
                        "foodDays excludes bots from population",
                        abs(float(actual_food_days) - expected_food_days) < 0.1,
                        f"actual={actual_food_days}, expected_organic={expected_food_days:.1f}, total_pop={total_pop}",
                    )

                    checks = [
                        ("logDays", total_logs),
                        ("plankDays", total_planks),
                        ("gearDays", total_gears),
                    ]
                    for key, stock in checks:
                        expected = float(stock) / organic_pop
                        actual = result.get(key, 0)
                        self.check(
                            f"{key} excludes bots from population",
                            abs(float(actual) - expected) < 0.1,
                            f"actual={actual}, expected_organic={expected:.1f}",
                        )

    def test_map_moisture(self):
        print("\n=== map moisture ===\n")

        # check tiles near a water pump for moist field
        pump = self.find_building("DeepWaterPump")
        if not pump:
            self.skip("map moisture", "no water pump found")
            return
        pb = self.bot.buildings(id=pump)
        if not pb or not isinstance(pb, list) or not pb:
            self.skip("map moisture", "cannot get pump details")
            return
        px, py = pb[0].get("x", 120), pb[0].get("y", 130)
        result = self.bot.tiles(px - 3, py - 3, px + 3, py + 3)
        tiles = result.get("tiles", [])
        moist_count = sum(1 for t in tiles if t.get("moist"))
        self.check(
            "moist tiles near water", moist_count > 0, f"got {moist_count} moist tiles"
        )

    def test_unlock(self):
        print("\n=== unlock ===\n")

        # check science points
        sci = self.bot.science()
        points = sci.get("points", 0) if isinstance(sci, dict) else 0
        if points < 50:
            self.skip("unlock test", f"only {points} science")
            return

        # find a cheap unlockable
        unlockables = sci.get("unlockables", [])
        target = None
        for u in unlockables:
            if not u.get("unlocked") and u.get("cost", 999) <= points:
                target = u
                break
        if not target:
            self.skip("unlock test", "nothing affordable")
            return

        before = points
        result = self.bot.unlock_building(target["name"])
        self.check(
            f"unlock {target['name']}",
            self.has(result, "unlocked") and result["unlocked"] == True,
            json.dumps(result)[:100],
        )

        if self.has(result, "remaining"):
            expected = before - target["cost"]
            self.check(
                "science deducted",
                result["remaining"] == expected,
                f"expected {expected}, got {result['remaining']}",
            )

        # try unlocking same building again. should say already unlocked, no point change
        points_after = result.get("remaining", 0)
        result2 = self.bot.unlock_building(target["name"])
        self.check(
            "already unlocked returns note",
            self.has(result2, "note") and "already" in str(result2.get("note", "")),
            json.dumps(result2)[:100],
        )
        self.check(
            "no points deducted on re-unlock",
            result2.get("remaining") == points_after,
            f"expected {points_after}, got {result2.get('remaining')}",
        )

    def test_floodgate(self):
        print("\n=== floodgate ===\n")

        fid = self.find_building("Floodgate")
        if not fid:
            self.skip("floodgate", "no floodgate found")
            return

        result = self.bot.set_floodgate(fid, 1.5)
        self.check(
            "set floodgate height", self.has(result, "height"), json.dumps(result)[:100]
        )

    def test_haul_priority(self):
        print("\n=== haul priority ===\n")

        # find a breeding pod or stockpile
        bid = self.find_building("BreedingPod") or self.find_building("SmallTank")
        if not bid:
            self.skip("haul priority", "no suitable building found")
            return

        result = self.bot.set_haul_priority(bid, True)
        self.check(
            "set haul priority",
            self.has(result, "haulPrioritized") and result["haulPrioritized"] == True,
            json.dumps(result)[:100],
        )

        # reset
        self.bot.set_haul_priority(bid, False)

    def test_recipe(self):
        print("\n=== recipe ===\n")

        mid = self.find_building("IndustrialLumberMill")
        if not mid:
            self.skip("recipe", "no lumber mill found")
            return

        # set recipe. use invalid name first to see what's available
        result = self.bot.set_recipe(mid, "InvalidRecipe")
        self.check(
            "invalid recipe returns error", self.err(result), json.dumps(result)[:100]
        )

    def test_farmhouse_action(self):
        print("\n=== farmhouse action ===\n")

        fid = self.find_building("FarmHouse")
        if not fid:
            self.skip("farmhouse action", "no farmhouse found")
            return

        result = self.bot.set_farmhouse_action(fid, "planting")
        self.check(
            "set farmhouse planting",
            self.has(result, "action") and result["action"] == "planting",
            json.dumps(result)[:100],
        )

        # reset
        self.bot.set_farmhouse_action(fid, "harvesting")

    def test_plantable_priority(self):
        print("\n=== plantable priority ===\n")

        fid = self.find_building("Forester")
        if not fid:
            self.skip("plantable priority", "no forester found")
            return

        result = self.bot.set_plantable_priority(fid, "Pine")
        self.check(
            "set plantable priority",
            self.has(result, "prioritized") and result["prioritized"] == "Pine",
            json.dumps(result)[:100],
        )

        # clear
        self.bot.set_plantable_priority(fid, "none")

    def test_workhours(self):
        print("\n=== workhours ===\n")

        # get current
        orig = self.bot.workhours()
        orig_hours = orig.get("endHours", 18) if isinstance(orig, dict) else 18

        result = self.bot.set_workhours(20)
        self.check("set workhours", not self.err(result), json.dumps(result)[:100])

        # verify via debug
        verify = self.debug_get("Write._workingHoursManager.EndHours")
        if "skipped" in verify:
            self.skip("verify workhours via debug", "debug disabled")
        else:
            self.check(
                "verify workhours via debug",
                str(verify.get("result", "")) in ("20", "20.0"),
                f"got: {verify.get('result')}",
            )

        # restore
        self.bot.set_workhours(orig_hours)

    def test_distribution(self):
        print("\n=== distribution ===\n")

        # get current distribution
        dist = self.bot.distribution()
        if not isinstance(dist, list) or len(dist) == 0:
            self.skip("distribution", "no distribution data")
            return

        # find a district with a good
        district = None
        good = None
        for d in dist:
            goods = d.get("goods", [])
            if goods and d.get("district"):
                district = d["district"]
                good = goods[0].get("good")
                break
        if not district or not good:
            self.skip("distribution", "no district/good pair found")
            return

        result = self.bot.set_distribution(district, good, "ImportDisabled", 0)
        self.check("set distribution", not self.err(result), json.dumps(result)[:100])

    def test_automation(self):
        """Test automation state and configuration readout."""
        print("\n=== automation ===\n")

        # find a building with automation
        buildings = self.bot.buildings(detail="full")
        if not isinstance(buildings, list):
            self.skip("automation", "buildings endpoint returned invalid data")
            return

        auto_bld = next(
            (
                b
                for b in buildings
                if isinstance(b, dict) and b.get("automation", {}).get("type")
            ),
            None,
        )
        if not auto_bld:
            self.skip("automation", "no building with automation found")
            return

        bid = auto_bld["id"]
        auto_data = auto_bld["automation"]
        atype = auto_data.get("type", "")
        print(f"  testing building {bid} ({auto_bld.get('name')}) type: {atype}")

        self.check("automation has type", atype != "")
        self.check("automation has config", "config" in auto_data)
        self.check("automation has inputs", isinstance(auto_data.get("inputs"), list))
        self.check("automation has outputs", isinstance(auto_data.get("outputs"), list))

        # verify wiring if present
        inputs = auto_data.get("inputs", [])
        if inputs and isinstance(inputs[0], dict):
            self.check(
                "automation inputs have status",
                "status" in inputs[0],
                json.dumps(inputs[0])[:100],
            )

        outputs = auto_data.get("outputs", [])
        if outputs and isinstance(outputs[0], dict):
            self.check(
                "automation outputs have state",
                "state" in outputs[0],
                json.dumps(outputs[0])[:100],
            )

        # test configuration if it's a Relay
        if atype == "Relay":
            original_mode = auto_data.get("config", {}).get("mode", "And")
            target_mode = "Or" if original_mode == "And" else "And"

            result = self.bot.configure_automation(bid, "mode", target_mode)
            self.check(
                "configure automation (Relay mode)",
                not self.err(result) and result.get("value") == target_mode,
                json.dumps(result)[:100],
            )

            # restore
            self.bot.configure_automation(bid, "mode", original_mode)

        # test configuration if it's a Lever
        if atype == "Lever":
            original_state = auto_data.get("config", {}).get("state", 0)
            target_state = "1" if original_state == 0 else "0"

            result = self.bot.configure_automation(bid, "state", target_state)
            self.check(
                "configure automation (Lever state)",
                not self.err(result) and str(result.get("value")) == target_state,
                json.dumps(result)[:100],
            )

            # restore
            self.bot.configure_automation(bid, "state", str(original_state))

    def test_beaver_needs(self):
        print("\n=== beaver needs ===\n")

        # get beavers in JSON mode for full need data
        result = self.bot.beavers(detail="full")
        if not isinstance(result, list) or len(result) == 0:
            self.skip("beaver needs", "no beavers found")
            return

        beaver = result[0]
        self.check(
            "beaver has needs list",
            "needs" in beaver and isinstance(beaver["needs"], list),
            f"keys: {list(beaver.keys())}",
        )

        if isinstance(beaver.get("needs"), list) and len(beaver["needs"]) > 0:
            need = beaver["needs"][0]
            self.check("need has id", "id" in need, json.dumps(need)[:100])
            self.check(
                "need has wellbeing", "wellbeing" in need, json.dumps(need)[:100]
            )
            self.check(
                "need has favorable", "favorable" in need, json.dumps(need)[:100]
            )

        # TOON format should have unmet field
        toon = self.bot.beavers(limit=1)
        if isinstance(toon, list) and len(toon) > 0:
            self.check(
                "toon has unmet field", "unmet" in toon[0], json.dumps(toon[0])[:100]
            )

    def test_building_range(self):
        print("\n=== building range ===\n")

        # find a farmhouse
        fid = self.find_building("FarmHouse")
        if not fid:
            self.skip("building range", "no farmhouse found")
            return

        result = self.bot.building_range(fid)
        self.check(
            "range returns tiles",
            self.has(result, "tiles") and result["tiles"] > 0,
            json.dumps(result)[:100],
        )
        self.check("range has moist count", "moist" in result, json.dumps(result)[:100])
        self.check(
            "range has bounds", self.has(result, "bounds"), json.dumps(result)[:100]
        )

        # also test on a building without range
        dc_id = self.find_building("DistrictCenter")
        if dc_id:
            dc_result = self.bot.building_range(dc_id)
            # DC may or may not have range. just check no crash
            self.check(
                "range on DC no crash",
                not self.err(dc_result)
                or "invalid_type" in str(dc_result.get("error", "")),
                json.dumps(dc_result)[:100],
            )

    def test_find_planting(self):
        print("\n=== find planting ===\n")

        # area mode
        bx, by, bz = self.x1, self.y1, 2
        block = self._find_clear_block(5, 5)
        if block:
            bx, by, bz = block
        result = self.bot.find_planting(
            "Carrot", x1=bx, y1=by, x2=bx + 5, y2=by + 5, z=bz
        )
        self.check(
            "find_planting area returns spots",
            self.has(result, "spots") and len(result.get("spots", [])) > 0,
            json.dumps(result)[:100],
        )

        if result.get("spots"):
            spot = result["spots"][0]
            self.check("spot has moist field", "moist" in spot, json.dumps(spot)[:60])
            self.check(
                "spot has planted field", "planted" in spot, json.dumps(spot)[:60]
            )

        # building mode
        fid = self.find_building("FarmHouse")
        if fid:
            result2 = self.bot.find_planting("Carrot", id=fid)
            self.check(
                "find_planting by building",
                self.has(result2, "spots"),
                json.dumps(result2)[:100],
            )
        else:
            self.skip("find_planting by building", "no farmhouse found")

    def test_prefab_costs(self):
        print("\n=== prefab costs ===\n")

        prefabs = self.bot.prefabs()
        if not isinstance(prefabs, list) or len(prefabs) == 0:
            self.skip("prefab costs", "no prefabs")
            return

        # find a building with costs
        with_cost = None
        for p in prefabs:
            if p.get("cost") and len(p.get("cost", [])) > 0:
                with_cost = p
                break

        if with_cost:
            self.check(
                "prefab has cost array",
                isinstance(with_cost["cost"], list),
                json.dumps(with_cost)[:100],
            )
            cost0 = with_cost["cost"][0]
            self.check("cost has good field", "good" in cost0, json.dumps(cost0)[:60])
            self.check(
                "cost has amount field", "amount" in cost0, json.dumps(cost0)[:60]
            )
        else:
            self.skip("prefab cost fields", "no prefab with costs found")

        # find a building with science cost
        with_science = None
        for p in prefabs:
            if p.get("scienceCost") and p["scienceCost"] > 0:
                with_science = p
                break

        if with_science:
            self.check(
                "prefab has scienceCost",
                with_science["scienceCost"] > 0,
                json.dumps(with_science)[:100],
            )
            self.check(
                "prefab has unlocked field",
                "unlocked" in with_science,
                json.dumps(with_science)[:100],
            )
        else:
            self.skip("prefab science fields", "no prefab with science cost")

    def test_building_inventory(self):
        print("\n=== building inventory ===\n")

        # find a tank or warehouse with stock/capacity
        buildings = self.bot.buildings(detail="full")
        if not isinstance(buildings, list):
            self.skip("building inventory", "no buildings")
            return

        with_capacity = None
        for b in buildings:
            if b.get("capacity") and b["capacity"] > 0:
                with_capacity = b
                break

        if with_capacity:
            self.check(
                "building has stock",
                "stock" in with_capacity,
                f"{with_capacity.get('name')}: stock={with_capacity.get('stock')}",
            )
            self.check(
                "building has capacity",
                with_capacity["capacity"] > 0,
                f"{with_capacity.get('name')}: capacity={with_capacity.get('capacity')}",
            )

            # verify via debug
            bid = with_capacity["id"]
            self.bot.debug(target="call", method="FindEntity", arg0=str(bid))
            # find Inventories component and check TotalAmountInStock
            r = self.bot.debug(target="get", path="$~Inventories")
            self.check(
                "debug confirms Inventories component",
                r.get("value") != "null" or "error" not in r,
                json.dumps(r)[:100],
            )
        else:
            self.skip("building inventory", "no building with capacity found")

    def test_building_recipes(self):
        print("\n=== building recipes ===\n")

        buildings = self.bot.buildings(detail="full")
        if not isinstance(buildings, list):
            self.skip("building recipes", "no buildings")
            return

        with_recipes = None
        for b in buildings:
            if b.get("recipes") and len(b.get("recipes", [])) > 0:
                with_recipes = b
                break

        if with_recipes:
            self.check(
                "building has recipes array",
                isinstance(with_recipes["recipes"], list)
                and len(with_recipes["recipes"]) > 0,
                json.dumps(with_recipes["recipes"])[:100],
            )
            self.check(
                "building has currentRecipe",
                "currentRecipe" in with_recipes,
                f"currentRecipe={with_recipes.get('currentRecipe')}",
            )
        else:
            self.skip("building recipes", "no manufactory found")

        # find a breeding pod
        with_breeding = None
        for b in buildings:
            if "BreedingPod" in str(b.get("name", "")):
                with_breeding = b
                break

        if with_breeding:
            self.check(
                "breeding pod has needsNutrients",
                "needsNutrients" in with_breeding,
                json.dumps(with_breeding)[:100],
            )
        else:
            self.skip("breeding pod status", "no breeding pod found")

    def test_clutch(self):
        print("\n=== clutch ===\n")

        # find a building with clutch
        buildings = self.bot.buildings(detail="full")
        if not isinstance(buildings, list):
            self.skip("clutch", "no buildings")
            return

        clutch_id = None
        for b in buildings:
            if b.get("isClutch"):
                clutch_id = b["id"]
                break

        if not clutch_id:
            self.skip("clutch", "no building with clutch found")
            return

        # disengage
        result = self.bot.set_clutch(clutch_id, False)
        self.check(
            "disengage clutch",
            self.has(result, "engaged") and result["engaged"] == False,
            json.dumps(result)[:100],
        )

        # verify via debug
        self.bot.debug(target="call", method="FindEntity", arg0=str(clutch_id))
        r = self.bot.debug(target="get", path="$~Clutch.IsEngaged")
        self.check(
            "verify disengaged via debug",
            r.get("value") == "False",
            f"got: {r.get('value')}",
        )

        # re-engage
        result2 = self.bot.set_clutch(clutch_id, True)
        self.check(
            "re-engage clutch",
            self.has(result2, "engaged") and result2["engaged"] == True,
            json.dumps(result2)[:100],
        )

    def test_beaver_detail(self):
        """Test detail mode shows all needs with group categories, and bot tiers."""
        # basic mode (default detail)
        basic = self.bot.beavers()
        if not isinstance(basic, list) or not basic:
            self.skip("beaver_detail", "no beavers/bots")
            return

        beaver: dict[str, Any] = {}
        beaver_full: dict[str, Any] = {}
        full_needs: int = 0
        beavers_only = [b for b in basic if not b.get("isBot")]
        bots_only = [b for b in basic if b.get("isBot")]

        if beavers_only:
            beaver = beavers_only[0]
            basic_needs = len(beaver.get("needs", []))

            # full detail mode
            full = self.bot.beavers(detail="full")
            if isinstance(full, list):
                beaver_full_list = [b for b in full if b.get("id") == beaver.get("id")]
                if beaver_full_list:
                    beaver_full = beaver_full_list[0]
                    full_needs = len(beaver_full.get("needs", []))
                    self.check(
                        "full has more needs than basic",
                        full_needs >= basic_needs,
                        f"full={full_needs} basic={basic_needs}",
                    )

        if bots_only:
            bot = bots_only[0]
            # bots should always be "operational" in TOON format
            toon_beavers = self.toon_bot.beavers()
            toon_bot_list = [
                b for b in self._as_list(toon_beavers) if b.get("id") == bot.get("id")
            ]
            if toon_bot_list:
                toon_bot = toon_bot_list[0]
                self.check(
                    "bot tier is operational",
                    toon_bot.get("tier") == "operational",
                    f"got tier: {toon_bot.get('tier')}",
                )
        groups_seen = set()
        for n in beaver_full.get("needs", []):
            has_group = "group" in n
            self.check(
                f"need {n['id']} has group", has_group, f"keys: {list(n.keys())}"
            )
            if has_group:
                groups_seen.add(n["group"])
        self.check(
            "multiple need groups found", len(groups_seen) > 1, f"groups: {groups_seen}"
        )

        # single beaver by id
        single = self._as_list(self.bot.beavers(id=int(beaver["id"])))
        self.check(
            "single beaver returns 1 result", len(single) == 1, f"got {len(single)}"
        )
        self.check(
            "single has all needs",
            len(single[0].get("needs", [])) == full_needs,
            f"single={len(single[0].get('needs', []))} full={full_needs}",
        )

        # basic mode should NOT have group field
        for n in beaver.get("needs", []):
            self.check(f"basic need {n['id']} no group", "group" not in n)

    def test_bot_data(self):
        """Test bot-specific fields in beavers endpoint."""
        raw = self.bot._get("/api/beavers", params={"detail": "full", "limit": 0})
        beavers = (
            raw
            if isinstance(raw, list)
            else raw.get("items", raw)
            if isinstance(raw, dict)
            else []
        )
        bots = [b for b in beavers if isinstance(b, dict) and b.get("isBot")]
        if not bots:
            self.skip("bot_data", "no bots in colony")
            return
        bot = bots[0]
        self.check("bot has isBot=true", bot["isBot"] == True)
        self.check("bot has needs", "needs" in bot and len(bot["needs"]) > 0)

        # bot should have specific needs based on faction (always, even if inactive)
        need_ids = {n["id"] for n in bot.get("needs", [])}
        if self.faction and self.faction.lower() == "folktails":
            expected = ["Biofuel", "Catalyst", "PunchCard"]
        else:
            expected = ["Energy", "ControlTower", "Grease"]
        for need in expected:
            self.check(f"bot has {need} need", need in need_ids, f"got: {need_ids}")

        # each need should have points between 0 and 1
        for need in bot.get("needs", []):
            pts = need.get("points", -1)
            self.check(
                f"bot need {need['id']} points in range", 0 <= pts <= 1, f"points={pts}"
            )

    def test_bot_buildings(self):
        """Test bot production buildings have correct detail fields."""
        buildings: list[dict[str, Any]] | dict[str, Any] = self.bot.buildings()
        if not isinstance(buildings, list):
            self.skip("bot_buildings", "buildings not list")
            return

        # BotPartFactory
        factories = [b for b in buildings if "BotPartFactory" in str(b.get("name", ""))]
        if factories:
            fid = factories[0]["id"]
            detail = self._as_list(self.bot.buildings(id=int(fid)))
            if detail:
                d = detail[0]
                self.check("factory has recipes", "recipes" in d)
                self.check("factory has productionProgress", "productionProgress" in d)
                self.check("factory has inventory", "inventory" in d)
                self.check(
                    "factory has powered field",
                    "powered" in d,
                    f"keys={list(d.keys())[:10]}",
                )
        else:
            self.skip("bot_factory", "no BotPartFactory")

        # BotAssembler
        assemblers = [b for b in buildings if "BotAssembler" in str(b.get("name", ""))]
        if assemblers:
            aid = assemblers[0]["id"]
            detail = self._as_list(self.bot.buildings(id=int(aid)))
            if detail:
                d = detail[0]
                self.check("assembler has recipes", "recipes" in d)
                self.check(
                    "assembler has a recipe set",
                    d.get("currentRecipe", "") != "",
                    f"recipe={d.get('currentRecipe')}",
                )
                self.check(
                    "assembler has productionProgress", "productionProgress" in d
                )
        else:
            self.skip("bot_assembler", "no BotAssembler")

        # ChargingStation
        chargers = [b for b in buildings if "ChargingStation" in str(b.get("name", ""))]
        if chargers:
            cid = chargers[0]["id"]
            detail = self._as_list(self.bot.buildings(id=int(cid)))
            if detail:
                self.check(
                    "charger has powered field", "powered" in self._as_list(detail)[0]
                )
        else:
            self.skip("charging_station", "no ChargingStation")

    def test_bot_in_summary(self):
        """Verify bots appear in summary and population counts."""
        summary = self.toon_bot.summary()
        self.check(
            "summary has bots field",
            "bots" in summary,
            f"keys: {list(summary.keys())[:20]}",
        )

        pop = self._as_list(self.bot.population())
        if pop:
            self.check(
                "population has bots", "bots" in pop[0], f"keys: {list(pop[0].keys())}"
            )

    def test_bot_toon_format(self):
        """Verify bot shows correctly in TOON beavers output."""
        beavers = self._as_list(self.toon_bot.beavers())
        bots = [b for b in beavers if b.get("isBot")]
        if bots:
            bot = bots[0]
            self.check("toon bot has isBot", "isBot" in bot)
            self.check("toon bot isBot=True", bot["isBot"] == True)
            self.check("toon bot has name", "name" in bot and "Bot" in str(bot["name"]))
        else:
            self.skip("bot_toon", "no bots in colony")

    def test_beaver_position(self):
        """Test beaver/bot x,y,z grid position."""
        print("\n=== beaver position ===\n")
        beavers = self._as_list(self.bot.beavers(detail="full"))
        if not beavers:
            self.skip("beaver_position", "no beavers")
            return
        b = beavers[0]
        self.check("beaver has x", "x" in b, f"keys: {list(b.keys())[:10]}")
        self.check("beaver has y", "y" in b)
        self.check("beaver has z", "z" in b)
        if "x" in b:
            self.check("x is int", isinstance(b["x"], int), f"type={type(b['x'])}")
            self.check("x in range", 0 <= b["x"] <= 256, f"x={b['x']}")
            self.check("y in range", 0 <= b["y"] <= 256, f"y={b['y']}")
            self.check("z >= 0", b["z"] >= 0, f"z={b['z']}")

        # bot should also have position
        bots = [x for x in beavers if x.get("isBot")]
        if bots:
            bot = bots[0]
            self.check("bot has x", "x" in bot)
            self.check("bot has y", "y" in bot)
        else:
            self.skip("bot_position", "no bots in colony")

    def test_beaver_district(self):
        """Test district field on beavers."""
        print("\n=== beaver district ===\n")
        beavers = self._as_list(self.bot.beavers(detail="full"))
        if not beavers:
            self.skip("beaver_district", "no beavers")
            return
        # at least one beaver should have a district
        with_district = [b for b in beavers if "district" in b]
        self.check(
            "some beavers have district",
            len(with_district) > 0,
            f"{len(with_district)}/{len(beavers)} have district",
        )
        if with_district:
            self.check(
                "district is string", isinstance(with_district[0]["district"], str)
            )
            self.check("district is not empty", len(with_district[0]["district"]) > 0)

    def test_map_stacking(self):
        """Test tiles occupants format in both json and toon modes."""
        print("\n=== map stacking ===\n")
        # find an area with buildings by using DC location
        px, py = self.center_x, self.center_y
        # JSON format: occupants is array of {name, z}
        json_result = self.bot.tiles(px - 1, py - 1, px + 1, py + 1)
        json_tiles = json_result.get("tiles", [])
        self.check("json tiles returned", len(json_tiles) > 0)
        json_occupied = [
            t
            for t in json_tiles
            if isinstance(t.get("occupants"), list) and len(t.get("occupants")) > 0
        ]
        if json_occupied:
            occ = json_occupied[0]["occupants"]
            self.check(
                "json occupants is array",
                isinstance(occ, list),
                f"type={type(occ).__name__}",
            )
            if isinstance(occ, list) and occ:
                self.check("json occupant has name", "name" in occ[0])
                self.check("json occupant has z", "z" in occ[0])
        else:
            self.skip("json occupants", "no occupied tiles near DC")

        # TOON format: occupants is flat string "Name:z+Name:z"
        toon_result = self.toon_bot.tiles(px - 1, py - 1, px + 1, py + 1)
        toon_tiles = toon_result.get("tiles", [])
        self.check("toon tiles returned", len(toon_tiles) > 0)
        toon_occupied = [
            t
            for t in toon_tiles
            if isinstance(t.get("occupants"), str) and len(t.get("occupants")) > 0
        ]
        if toon_occupied:
            occ = toon_occupied[0]["occupants"]
            self.check(
                "toon occupants is string",
                isinstance(occ, str),
                f"type={type(occ).__name__}",
            )
            if isinstance(occ, str):
                self.check("toon occupants has name:z format", ":" in occ, f"got {occ}")
        else:
            self.skip("toon occupants", "no occupied tiles near DC")

        # no tile should have old singular "occupant" key in either format
        for t in json_tiles + toon_tiles:
            self.check(
                f"tile ({t['x']},{t['y']}) no singular occupant", "occupant" not in t
            )

    def test_carried_goods(self):
        """Test carried goods fields on beavers."""
        print("\n=== carried goods ===\n")
        beavers = self._as_list(self.bot.beavers(detail="full"))
        carriers = [b for b in beavers if b.get("carrying")]
        if carriers:
            c = carriers[0]
            self.check("carrying is string", isinstance(c["carrying"], str))
            self.check("carryAmount is int", isinstance(c["carryAmount"], int))
            self.check("carryAmount > 0", c["carryAmount"] > 0)
        else:
            self.skip("carried_goods", "no beaver currently carrying")

        # detail:full should have liftingCapacity
        full = self._as_list(self.bot.beavers(detail="full"))
        beaver = [b for b in full if not b.get("isBot")][0]
        self.check(
            "detail has liftingCapacity",
            "liftingCapacity" in beaver,
            f"keys: {[k for k in beaver.keys() if 'lift' in k.lower() or 'carry' in k.lower()]}",
        )
        if "liftingCapacity" in beaver:
            self.check("liftingCapacity > 0", beaver["liftingCapacity"] > 0)

    def test_bot_durability(self):
        """Test deterioration field on bots."""
        print("\n=== bot durability ===\n")
        beavers = self._as_list(self.bot.beavers(detail="full"))
        bots = [b for b in beavers if b.get("isBot")]
        if not bots:
            self.skip("bot_durability", "no bots in colony")
            return
        bot = bots[0]
        self.check(
            "bot has deterioration", "deterioration" in bot, f"keys: {list(bot.keys())}"
        )
        if "deterioration" in bot:
            self.check(
                "deterioration is number",
                isinstance(bot["deterioration"], (int, float)),
            )
            self.check(
                "deterioration in range 0-1",
                0 <= bot["deterioration"] <= 1,
                f"deterioration={bot['deterioration']}",
            )

    def test_power_networks(self):
        """Test power network endpoint."""
        print("\n=== power networks ===\n")
        networks = self._as_list(self.bot.power())
        self.check("power returns list", isinstance(networks, list))
        self.check("has networks", len(networks) > 0, f"count={len(networks)}")

        if networks:
            net = networks[0]
            self.check("network has id", "id" in net)
            self.check("network has supply", "supply" in net)
            self.check("network has demand", "demand" in net)
            self.check("network has buildings", "buildings" in net)
            self.check("buildings is list", isinstance(net["buildings"], list))

            if net["buildings"]:
                b = net["buildings"][0]
                self.check("building has name", "name" in b)
                self.check("building has id", "id" in b)
                self.check("building has isGenerator", "isGenerator" in b)
                self.check("building has nominalOutput", "nominalOutput" in b)
                self.check("building has nominalInput", "nominalInput" in b)

            # find a network with a generator
            gen_nets = [
                n
                for n in networks
                if any(
                    isinstance(b, dict) and b.get("isGenerator")
                    for b in (n.get("buildings") or [])
                    if isinstance(n, dict)
                )
            ]
            if gen_nets:
                self.check(
                    "generator network has demand field", "demand" in gen_nets[0]
                )
            else:
                self.skip("power_generator", "no networks with generators")

    def test_webhooks(self):
        """Test webhook registration, listing, event delivery, and unregistration."""
        print("\n=== webhooks ===\n")

        # spin up a tiny HTTP server to receive webhook events
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

        def _get_local_ip():
            if self.callback_host:
                return self.callback_host
            try:
                import socket

                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # connect on actual game port to maximize connect logic reliability
                dest_port = self.bot.port if self.bot.port else 8085
                dest = (
                    self.bot.host
                    if self.bot.host not in ("localhost", "127.0.0.1")
                    else "8.8.8.8"
                )
                s.connect((dest, dest_port))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except Exception:
                return "127.0.0.1"

        local_ip = _get_local_ip()
        print(f"  test machine is at {local_ip}")

        received_events = []
        slow_state = {"active": 0, "max_active": 0, "requests": 0}
        fail_state = {"requests": 0}
        state_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    parsed = json.loads(body)
                    # webhooks send batched arrays: [{event1}, {event2}]
                    if isinstance(parsed, list):
                        received_events.extend(parsed)
                    else:
                        received_events.append(parsed)
                except Exception:
                    received_events.append({"raw": body})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            @override
            def log_message(self, format, *args) -> None:
                pass  # silence logs

        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
                with state_lock:
                    slow_state["active"] += 1
                    slow_state["requests"] += 1
                    slow_state["max_active"] = max(
                        slow_state["max_active"], slow_state["active"]
                    )
                try:
                    time.sleep(6)
                    self.send_response(200)
                    self.end_headers()
                    try:
                        self.wfile.write(b"slow")
                    except Exception:
                        pass
                finally:
                    with state_lock:
                        slow_state["active"] -= 1

            @override
            def log_message(self, format, *args) -> None:
                pass

        class FailHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
                with state_lock:
                    fail_state["requests"] += 1
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"fail")

            @override
            def log_message(self, format, *args) -> None:
                pass

        def webhook_value(obj, key, default=None):
            if not isinstance(obj, dict):
                return default
            for k, v in obj.items():
                if str(k).lower() == key.lower():
                    return v
            return default

        server = HTTPServer(("0.0.0.0", 19876), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        slow_server = ThreadingHTTPServer(("0.0.0.0", 19877), SlowHandler)
        slow_thread = threading.Thread(target=slow_server.serve_forever, daemon=True)
        slow_thread.start()
        fail_server = ThreadingHTTPServer(("0.0.0.0", 19878), FailHandler)
        fail_thread = threading.Thread(target=fail_server.serve_forever, daemon=True)
        fail_thread.start()

        try:
            orig_speed_info = self.bot.speed()
            orig_speed = (
                orig_speed_info.get("speed", 1)
                if isinstance(orig_speed_info, dict)
                else 1
            )
            alt_speed = 2 if orig_speed != 2 else 1

            # register webhook for building events
            result = self.bot.register_webhook(
                f"http://{local_ip}:19876/test",
                ["building.placed", "building.demolished"],
            )
            self.check(
                "register webhook",
                self.has(result, "id"),
                json.dumps(result)[:100] if not self.has(result, "id") else "",
            )
            wh_id = result.get("id", "")

            # list webhooks
            wh_list = self.bot.list_webhooks()
            self.check(
                "list webhooks",
                isinstance(wh_list, list) and len(wh_list) > 0,
                json.dumps(wh_list)[:100],
            )

            # trigger an event: place a building
            spot = self.find_spot("Path")
            if spot:
                placed = self.bot.place_building(
                    "Path",
                    spot["x"],
                    spot["y"],
                    spot["z"],
                    spot.get("orientation", "south"),
                )
                if self.has(placed, "id"):
                    # wait for webhook delivery
                    time.sleep(2)
                    placed_events = [
                        e
                        for e in received_events
                        if e.get("event") == "building.placed"
                    ]
                    self.check(
                        "webhook received building.placed",
                        len(placed_events) > 0,
                        f"received {len(received_events)} events: {[e.get('event') for e in received_events]}",
                    )

                    if placed_events:
                        evt = placed_events[-1]
                        self.check("webhook has event field", "event" in evt)
                        self.check("webhook has day field", "day" in evt)
                        self.check("webhook has timestamp field", "timestamp" in evt)
                        self.check("webhook has data field", "data" in evt)

                    # demolish and check that event
                    received_events.clear()
                    self.bot.demolish_building(placed["id"])
                    time.sleep(2)
                    demo_events = [
                        e
                        for e in received_events
                        if e.get("event") == "building.demolished"
                    ]
                    self.check(
                        "webhook received building.demolished",
                        len(demo_events) > 0,
                        f"received {len(received_events)} events: {[e.get('event') for e in received_events]}",
                    )
                else:
                    self.skip("webhook event delivery", f"placement failed: {placed}")
            else:
                self.skip("webhook event delivery", "no valid spot")

            # register a second webhook for all events
            result2 = self.bot.register_webhook(f"http://{local_ip}:19876/all")
            self.check("register webhook (all events)", self.has(result2, "id"))

            # unregister first webhook
            unreg = self.bot.unregister_webhook(wh_id)
            self.check("unregister webhook", unreg.get("removed", False))

            # verify list shows only the second
            wh_list2 = self.bot.list_webhooks()
            self.check(
                "list after unregister",
                isinstance(wh_list2, list) and len(wh_list2) == 1,
                f"expected 1, got {len(wh_list2) if isinstance(wh_list2, list) else '?'}",
            )

            # cleanup: unregister second
            if self.has(result2, "id"):
                self.bot.unregister_webhook(result2["id"])

            # --- test: event filtering (should NOT receive unsubscribed events) ---
            received_events.clear()
            filtered = self.bot.register_webhook(
                f"http://{local_ip}:19876/filtered", ["drought.start"]
            )
            self.check("register filtered webhook", self.has(filtered, "id"))
            if spot:
                placed2 = self.bot.place_building(
                    "Path",
                    spot["x"],
                    spot["y"],
                    spot["z"],
                    spot.get("orientation", "south"),
                )
                if self.has(placed2, "id"):
                    time.sleep(2)
                    building_events = [
                        e
                        for e in received_events
                        if e.get("event") == "building.placed"
                    ]
                    self.check(
                        "filtered webhook ignores building.placed",
                        len(building_events) == 0,
                        f"got {len(building_events)} building.placed events (expected 0)",
                    )
                    self.bot.demolish_building(placed2["id"])
                    time.sleep(1)
            if self.has(filtered, "id"):
                self.bot.unregister_webhook(filtered["id"])

            # --- test: bad URL resilience (mod doesn't crash) ---
            bad = self.bot.register_webhook(
                f"http://{local_ip}:1/bad", ["building.placed"]
            )
            self.check("register bad URL webhook", self.has(bad, "id"))
            if spot:
                placed3 = self.bot.place_building(
                    "Path",
                    spot["x"],
                    spot["y"],
                    spot["z"],
                    spot.get("orientation", "south"),
                )
                if self.has(placed3, "id"):
                    time.sleep(2)
                    # verify mod still responds
                    ping = self.bot.ping()
                    self.check("mod alive after bad webhook URL", ping)
                    self.bot.demolish_building(placed3["id"])
                    time.sleep(1)
            if self.has(bad, "id"):
                self.bot.unregister_webhook(bad["id"])

            # --- test: slow endpoint stays single-flight while timing out ---
            slow = self.bot.register_webhook(
                f"http://{local_ip}:19877/slow", ["speed.changed"]
            )
            self.check("register slow webhook", self.has(slow, "id"))
            if self.has(slow, "id"):
                for i in range(6):
                    self.bot.set_speed(alt_speed if i % 2 == 0 else orig_speed)
                    time.sleep(0.35)
                time.sleep(6.5)
                self.check(
                    "slow webhook received requests",
                    slow_state["requests"] > 0,
                    f"requests={slow_state['requests']}",
                )
                self.check(
                    "slow webhook max concurrency",
                    slow_state["max_active"] == 1,
                    f"max_active={slow_state['max_active']}, requests={slow_state['requests']}",
                )
                self.bot.unregister_webhook(slow["id"])
                self.bot.set_speed(orig_speed)

            # --- test: non-2xx responses count toward circuit breaker ---
            failing = self.bot.register_webhook(
                f"http://{local_ip}:19878/fail", ["speed.changed"]
            )
            self.check("register failing webhook", self.has(failing, "id"))
            if self.has(failing, "id"):
                for i in range(32):
                    self.bot.set_speed(alt_speed if i % 2 == 0 else orig_speed)
                    time.sleep(0.3)
                time.sleep(1)
                failing_list = self.bot.list_webhooks()
                failing_entry = (
                    next(
                        (
                            w
                            for w in failing_list
                            if str(webhook_value(w, "id", "")) == failing["id"]
                        ),
                        None,
                    )
                    if isinstance(failing_list, list)
                    else None
                )
                self.check(
                    "failing webhook listed",
                    failing_entry is not None,
                    json.dumps(failing_list)[:200] if not failing_entry else "",
                )
                self.check(
                    "failing webhook disabled after non-2xx",
                    bool(webhook_value(failing_entry, "disabled", False)),
                    json.dumps(failing_entry)[:200] if failing_entry else "missing",
                )
                self.check(
                    "failing webhook tracked failures",
                    int(webhook_value(failing_entry, "failures", 0) or 0) >= 30,
                    json.dumps(failing_entry)[:200] if failing_entry else "missing",
                )
                self.check(
                    "failing webhook received requests",
                    fail_state["requests"] >= 30,
                    f"requests={fail_state['requests']}",
                )
                self.bot.unregister_webhook(failing["id"])
                self.bot.set_speed(orig_speed)

            # --- test: payload data accuracy ---
            received_events.clear()
            accurate = self.bot.register_webhook(
                f"http://{local_ip}:19876/accurate", ["building.placed"]
            )
            self.check("register accuracy webhook", self.has(accurate, "id"))
            current_day = (
                self.bot.time().get("dayNumber", 0)
                if isinstance(self.bot.time(), dict)
                else 0
            )
            if spot:
                placed4 = self.bot.place_building(
                    "Path",
                    spot["x"],
                    spot["y"],
                    spot["z"],
                    spot.get("orientation", "south"),
                )
                if self.has(placed4, "id"):
                    time.sleep(2)
                    placed_evts = [
                        e
                        for e in received_events
                        if e.get("event") == "building.placed"
                    ]
                    if placed_evts:
                        evt = placed_evts[-1]
                        self.check(
                            "payload data.name contains Path",
                            "Path" in str(evt.get("data", {}).get("name", "")),
                            f"data={evt.get('data')}",
                        )
                        self.check(
                            "payload data.id is nonzero int",
                            isinstance(evt.get("data", {}).get("id"), int)
                            and evt["data"]["id"] != 0,
                            f"id={evt.get('data', {}).get('id')}",
                        )
                        self.check(
                            "payload day matches game day",
                            evt.get("day") == current_day
                            or abs(evt.get("day", 0) - current_day) <= 1,
                            f"webhook day={evt.get('day')}, game day={current_day}",
                        )
                    else:
                        self.skip(
                            "payload accuracy", "no building.placed event received"
                        )
                    self.bot.demolish_building(placed4["id"])
                    time.sleep(1)
            if self.has(accurate, "id"):
                self.bot.unregister_webhook(accurate["id"])

        finally:
            server.shutdown()
            slow_server.shutdown()
            fail_server.shutdown()

    def test_building_detail(self):
        print("\n=== building detail ===\n")

        # basic (default) should return compact rows
        basic = self._as_list(self.bot.buildings())
        self.check("basic returns list", len(basic) > 0)
        if basic:
            first = basic[0]
            self.check("basic has id", "id" in first)
            self.check("basic has name", "name" in first)
            # basic should NOT have inventory or effectRadius
            self.check("basic omits inventory", "inventory" not in first)

        # full detail
        full = self._as_list(self.bot.buildings(detail="full"))
        self.check("full returns list", len(full) > 0)
        if full:
            # find a manufactory to check productionProgress
            manufactory = None
            for b in full:
                if "productionProgress" in b:
                    manufactory = b
                    break
            if manufactory:
                self.check(
                    "full has productionProgress", "productionProgress" in manufactory
                )
                self.check("full has readyToProduce", "readyToProduce" in manufactory)
            else:
                self.skip("manufactory fields", "no active manufactory")

            # find a building with effectRadius > 0
            ranged = None
            for b in full:
                if b.get("effectRadius", 0) > 0:
                    ranged = b
                    break
            if ranged:
                self.check("full has effectRadius", ranged["effectRadius"] > 0)
            else:
                self.skip("effectRadius", "no ranged effect building")

        # single building by id
        if basic:
            bid = basic[0]["id"]
            single = self._as_list(self.bot.buildings(id=int(bid)))
            self.check("single returns list", isinstance(single, list))
            self.check("single has 1 result", len(single) == 1)
            if single:
                self.check("single has full fields", "finished" in single[0])

    def test_map_render(self):
        """Test the ASCII map render (was visual)."""
        print("\n=== map render ===\n")
        result = self.bot.map(
            self.center_x, self.center_y, self.center_x + 5, self.center_y + 5
        )
        self.check(
            "map returns rendered dict",
            isinstance(result, dict) and result.get("rendered"),
            f"got: {type(result).__name__}",
        )

    def test_find_helper(self):
        """Test the find CLI search helper and server-side filtering."""
        print("\n=== find helper ===\n")

        # find buildings by name
        name_filter = "DistrictCenter"
        result = self.bot.find(source="buildings", name=name_filter)
        self.check(
            "find building by name",
            isinstance(result, list) and len(result) > 0,
            f"got {len(result) if isinstance(result, list) else 0} results",
        )
        if isinstance(result, list) and len(result) > 0:
            all_match = all(
                name_filter.lower() in b.get("name", "").lower()
                for b in result
                if isinstance(b, dict)
            )
            self.check("all results match name filter", all_match)

        # find buildings by location
        radius = 10
        result2 = self.bot.find(
            source="buildings", x=self.center_x, y=self.center_y, radius=radius
        )
        self.check(
            "find building by location",
            isinstance(result2, list) and len(result2) > 0,
            f"got {len(result2) if isinstance(result2, list) else 0} results",
        )
        if isinstance(result2, list) and len(result2) > 0:
            in_range = all(
                abs(b.get("x", 0) - self.center_x) + abs(b.get("y", 0) - self.center_y)
                <= radius
                for b in result2
                if isinstance(b, dict) and "x" in b and "y" in b
            )
            self.check("all results match proximity filter", in_range)

        # find trees
        result3 = self.bot.find(source="trees", name="Pine")
        self.check(
            "find trees by name",
            isinstance(result3, list),
            f"got: {type(result3).__name__}",
        )
        if isinstance(result3, list) and len(result3) > 0:
            all_trees_match = all(
                "Pine" in t.get("name", "") for t in result3 if isinstance(t, dict)
            )
            self.check("all tree results match name filter", all_trees_match)

    def test_clear_planting(self):
        """Test clearing planting marks."""
        print("\n=== clear planting ===\n")
        result = self.bot.clear_planting(self.x1, self.y1, self.x1 + 3, self.y1 + 3, 2)
        self.check(
            "clear_planting no error",
            not self.err(result),
            json.dumps(result)[:100] if self.err(result) else "",
        )

    def test_clear_trees(self):
        """Test clearing tree cutting marks."""
        print("\n=== clear trees ===\n")
        # mark then clear to exercise both directions
        self.bot.mark_trees(self.x1, self.y1, self.x1 + 5, self.y1 + 5, 2)
        result = self.bot.clear_trees(self.x1, self.y1, self.x1 + 5, self.y1 + 5, 2)
        self.check(
            "clear_trees no error",
            not self.err(result),
            json.dumps(result)[:100] if self.err(result) else "",
        )

    def test_migrate(self):
        """Test beaver migration between districts."""
        print("\n=== migrate ===\n")
        districts = self.bot.districts()
        if not isinstance(districts, list) or len(districts) < 2:
            self.skip("migrate", "only 1 district")
            return
        d1 = districts[0].get("name", "")
        d2 = districts[1].get("name", "")
        result = self.bot.migrate(d1, d2, 0)
        self.check(
            "migrate call returns dict",
            isinstance(result, dict),
            f"got: {type(result).__name__}",
        )

    def test_security(self):
        """Test security hardening: removed endpoints, SSRF blocking, body size limits."""
        print("\n=== security ===\n")

        # agent endpoints should be removed (no HTTP-based process launching)
        try:
            r = self.bot.s.post(
                f"{self.bot.url}/api/agent/start",
                json={"binary": "claude", "goal": "test"},
                timeout=5,
            )
            data = r.json()
            self.check(
                "agent/start removed",
                isinstance(data, dict)
                and "error" in data
                and "unknown_endpoint" in data.get("error", ""),
                f"expected unknown_endpoint error, got: {data}",
            )
        except Exception as e:
            self.check("agent/start removed", False, f"exception: {e}")

        try:
            r = self.bot.s.post(f"{self.bot.url}/api/agent/stop", json={}, timeout=5)
            data = r.json()
            self.check(
                "agent/stop removed",
                isinstance(data, dict)
                and "error" in data
                and "unknown_endpoint" in data.get("error", ""),
                f"expected unknown_endpoint error, got: {data}",
            )
        except Exception as e:
            self.check("agent/stop removed", False, f"exception: {e}")

        try:
            r = self.bot.s.get(f"{self.bot.url}/api/agent/status", timeout=5)
            data = r.json()
            self.check(
                "agent/status removed",
                isinstance(data, dict)
                and "error" in data
                and "unknown_endpoint" in data.get("error", ""),
                f"expected unknown_endpoint error, got: {data}",
            )
        except Exception as e:
            self.check("agent/status removed", False, f"exception: {e}")

        # check if validation is enabled on the server by trying one bad URL
        validation_enabled = True
        try:
            chk_r = self.bot.s.post(
                f"{self.bot.url}/api/webhooks",
                json={"url": "http://127.0.0.1:9999/"},
                timeout=5,
            ).json()
            if isinstance(chk_r, dict) and "id" in chk_r:
                validation_enabled = False
                self.skip("webhook security", "webhook validation disabled on server")
                self.bot.s.delete(f"{self.bot.url}/api/webhooks/{chk_r['id']}")
        except Exception:
            pass

        # webhook SSRF: private IPs should be rejected
        if validation_enabled:
            for bad_url, label in [
                ("http://169.254.169.254/latest/meta-data", "cloud metadata"),
                ("http://127.0.0.1:6443/", "loopback"),
                ("http://10.0.0.1/", "rfc1918 10.x"),
                ("http://192.168.1.1/", "rfc1918 192.168.x"),
                ("file:///etc/passwd", "file scheme"),
                ("ftp://example.com/", "ftp scheme"),
            ]:
                try:
                    r = self.bot.s.post(
                        f"{self.bot.url}/api/webhooks", json={"url": bad_url}, timeout=5
                    )
                    data = r.json()
                    blocked = (
                        isinstance(data, dict)
                        and "error" in data
                        and "invalid_webhook_url" in data.get("error", "")
                    )
                    self.check(
                        f"webhook blocks {label}",
                        blocked,
                        f"url={bad_url} response={data}",
                    )
                except Exception as e:
                    self.check(f"webhook blocks {label}", False, f"exception: {e}")

        # body size limit: >1MB should return 413
        try:
            big_body = "x" * (1048576 + 100)
            r = self.bot.s.post(
                f"{self.bot.url}/api/speed",
                data=big_body,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            self.check(
                "body size limit enforced",
                r.status_code == 413,
                f"expected 413, got {r.status_code}",
            )
        except Exception as e:
            self.check("body size limit enforced", False, f"exception: {e}")

    def test_cli_commands(self):
        """Test every CLI command runs without crashing via subprocess."""
        print("\n=== cli commands ===\n")

        py = sys.executable

        def cli(*args, timeout=10):
            from urllib.parse import urlparse

            p = urlparse(self.bot.url)
            extra = []
            if p.hostname:
                extra.append(f"--host={p.hostname}")
            if p.port:
                extra.append(f"--port={p.port}")
            return subprocess.run(
                [py, "-m", "tbot.cli"] + extra + list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )

        # all read commands that take no args
        read_cmds = [
            "ping",
            "summary",
            "speed",
            "time",
            "weather",
            "population",
            "resources",
            "districts",
            "distribution",
            "science",
            "notifications",
            "workhours",
            "alerts",
            "buildings",
            "trees",
            "crops",
            "gatherables",
            "beavers",
            "prefabs",
            "power",
            "wellbeing",
            "tree_clusters",
            "list_webhooks",
        ]
        for cmd in read_cmds:
            r = cli(cmd, "--json")
            self.check(
                f"cli {cmd}",
                r.returncode == 0,
                (r.stderr or r.stdout)[:120] if r.returncode != 0 else "",
            )

        # parameterized read commands
        param_cmds = [
            ("buildings detail:full", ["buildings", "detail:full", "--json"]),
            ("beavers detail:full", ["beavers", "detail:full", "--json"]),
            ("tiles", ["tiles", "--json"]),
            (
                "map",
                [
                    "map",
                    f"x1:{self.center_x - 5}",
                    f"y1:{self.center_y - 5}",
                    f"x2:{self.center_x + 5}",
                    f"y2:{self.center_y + 5}",
                ],
            ),
            ("find buildings", ["find", "source:buildings", "limit:5", "--json"]),
        ]
        for name, argv in param_cmds:
            r = cli(*argv)
            self.check(
                f"cli {name}",
                r.returncode == 0,
                (r.stderr or r.stdout)[:120] if r.returncode != 0 else "",
            )

        # top dashboard: run briefly, should not crash
        try:
            r = cli("top", "interval:1", timeout=3)
            has_traceback = "Traceback" in r.stderr
        except subprocess.TimeoutExpired:
            r = None
            has_traceback = False  # timeout is expected (top runs forever)
        self.check(
            "cli top (no crash)",
            not has_traceback,
            (r.stderr[-200:] if r else "") if has_traceback else "",
        )

    def test_error_codes(self):
        """Test structured error codes and TimberbotError exception on every write endpoint."""
        print("\n=== error codes (TimberbotError) ===\n")

        bot = self.strict_bot  # this bot raises TimberbotError
        spot = self.find_spot("Path")
        sx = spot["x"] if spot else 100
        sy = spot["y"] if spot else 100
        sz = spot["z"] if spot else 2

        # every error-producing call with its expected code prefix
        error_cases = [
            ("set_speed invalid", lambda: bot.set_speed(99), "invalid_param"),
            ("set_workhours invalid", lambda: bot.set_workhours(0), "invalid_param"),
            ("pause nonexistent", lambda: bot.pause_building(999999), "not_found"),
            ("unpause nonexistent", lambda: bot.unpause_building(999999), "not_found"),
            (
                "set_clutch nonexistent",
                lambda: bot.set_clutch(999999, True),
                "not_found",
            ),
            (
                "set_floodgate nonexistent",
                lambda: bot.set_floodgate(999999, 1.0),
                "not_found",
            ),
            (
                "set_priority nonexistent",
                lambda: bot.set_priority(999999, "Normal"),
                "not_found",
            ),
            (
                "set_haul_priority nonexistent",
                lambda: bot.set_haul_priority(999999),
                "not_found",
            ),
            (
                "set_recipe nonexistent",
                lambda: bot.set_recipe(999999, "Fake"),
                "not_found",
            ),
            (
                "set_farmhouse_action nonexistent",
                lambda: bot.set_farmhouse_action(999999, "planting"),
                "not_found",
            ),
            (
                "set_plantable_priority nonexistent",
                lambda: bot.set_plantable_priority(999999, "Pine"),
                "not_found",
            ),
            (
                "set_workers nonexistent",
                lambda: bot.set_workers(999999, 1),
                "not_found",
            ),
            (
                "demolish nonexistent",
                lambda: bot.demolish_building(999999),
                "not_found",
            ),
            (
                "place unknown prefab",
                lambda: bot.place_building("Fake", sx, sy, sz),
                "invalid_prefab",
            ),
            (
                "place bad orientation",
                lambda: bot.place_building("Path", sx, sy, sz, "bogus"),
                "invalid_param",
            ),
            (
                "find_placement unknown",
                lambda: bot.find_placement("Fake", 0, 0, 10, 10),
                "invalid_prefab",
            ),
            (
                "storage nonexistent",
                lambda: bot.set_storage(999999, good="Water"),
                "not_found",
            ),
            (
                "building_range nonexistent",
                lambda: bot.building_range(999999),
                "not_found",
            ),
            (
                "unlock_building fake",
                lambda: bot.unlock_building("FakeBuilding"),
                "not_found",
            ),
            (
                "set_distribution fake",
                lambda: bot.set_distribution("FakeDistrict", "Water"),
                "not_found",
            ),
        ]

        for name, fn, expected_code in error_cases:
            try:
                fn()
                self.check(f"err {name}", False, "no exception raised")
            except TimberbotError as e:
                ok = e.code == expected_code
                self.check(
                    f"err {name} -> {expected_code}",
                    ok,
                    f"got code={e.code} error={e.error}" if not ok else "",
                )
            except Exception as e:
                self.check(f"err {name}", False, f"unexpected: {type(e).__name__}: {e}")

        # verify non-error calls do NOT raise
        success_cases = [
            ("speed read", lambda: bot.speed()),
            ("ping", lambda: bot.ping()),
            ("weather", lambda: bot.weather()),
            ("time", lambda: bot.time()),
        ]
        for name, fn in success_cases:
            try:
                result = fn()
                self.check(f"ok {name}", isinstance(result, dict), f"got {type(result).__name__}")
            except TimberbotError as e:
                self.check(f"ok {name}", False, f"unexpected error: {e.error}")

    def test_data_accuracy(self):
        """Validate cached API data matches live game components using debug validate."""
        print("\n=== data accuracy (cached vs live) ===\n")

        # validate a sample of buildings against freshly published snapshots
        self.prime_validation_snapshots()
        buildings = self.bot.buildings()
        if isinstance(buildings, list) and buildings:
            for b in buildings[:5]:
                bid = b.get("id")
                if not bid:
                    continue
                result = self.bot.debug(target="validate", id=str(bid))
                if isinstance(result, dict) and "mismatches" in result:
                    mm = result["mismatches"]
                    total = result["total"]
                    name = result.get("name", "?")
                    if mm > 0:
                        # show which fields mismatched
                        bad = [
                            f"{k}: cached={v['cached']} live={v['live']}"
                            for k, v in result.get("fields", {}).items()
                            if isinstance(v, dict) and not v.get("match")
                        ]
                        self.check(
                            f"building {name} ({bid})",
                            False,
                            f"{mm}/{total} mismatched: {'; '.join(bad)}",
                        )
                    else:
                        self.check(f"building {name} ({bid}) {total} fields", True)
                elif isinstance(result, dict) and "error" in result:
                    self.skip(f"building {bid}", result["error"])
                else:
                    self.skip(f"building {bid}", "unexpected response")
        else:
            self.skip("building validation", "no buildings")

        # validate a sample of beavers
        beavers = self.bot.beavers()
        if isinstance(beavers, list) and beavers:
            for bv in beavers[:5]:
                bid = bv.get("id")
                if not bid:
                    continue
                result = self.bot.debug(target="validate", id=str(bid))
                if isinstance(result, dict) and "mismatches" in result:
                    mm = result["mismatches"]
                    total = result["total"]
                    name = result.get("name", "?")
                    if mm > 0:
                        bad = [
                            f"{k}: cached={v['cached']} live={v['live']}"
                            for k, v in result.get("fields", {}).items()
                            if isinstance(v, dict) and not v.get("match")
                        ]
                        self.check(
                            f"beaver {name} ({bid})",
                            False,
                            f"{mm}/{total} mismatched: {'; '.join(bad)}",
                        )
                    else:
                        self.check(f"beaver {name} ({bid}) {total} fields", True)
                elif isinstance(result, dict) and "error" in result:
                    self.skip(f"beaver {bid}", result["error"])
                else:
                    self.skip(f"beaver {bid}", "unexpected response")
        else:
            self.skip("beaver validation", "no beavers")

        # validate_all summary
        result = self.bot.debug(target="validate_all")
        if isinstance(result, dict) and "mismatches" in result:
            self.check(
                f"validate_all: {result['entities']} entities, {result['fields']} fields",
                result["mismatches"] == 0,
                f"{result['mismatches']} total mismatches across {result['entities']} entities",
            )

    def test_json_schema(self):
        """Validate JSON structure of every endpoint in both toon and json formats."""
        print("\n=== json schema ===\n")
        self._audit_doc_contracts()

        def validate(data, schema, path="root"):
            errors = []
            if isinstance(schema, type):
                # json deserializes all numbers as int or float; accept int where float expected
                if schema == float and isinstance(data, (int, float)):
                    return errors
                if not isinstance(data, schema):
                    errors.append(
                        f"{path}: expected {schema.__name__}, got {type(data).__name__}"
                    )
            elif isinstance(schema, dict):
                if not isinstance(data, dict):
                    errors.append(f"{path}: expected dict, got {type(data).__name__}")
                else:
                    for key, expected in schema.items():
                        if key not in data:
                            errors.append(f"{path}.{key}: missing")
                        else:
                            errors.extend(
                                validate(data[key], expected, f"{path}.{key}")
                            )
            elif isinstance(schema, list) and len(schema) == 1:
                if not isinstance(data, list):
                    errors.append(f"{path}: expected list, got {type(data).__name__}")
                elif len(data) > 0:
                    errors.extend(validate(data[0], schema[0], f"{path}[0]"))
            return errors

        # json-mode bot for format=json endpoints
        jbot = self.bot

        # --- format=json schemas ---
        summary = jbot.summary()
        errs = validate(
            summary,
            {
                "time": {
                    "dayNumber": int,
                    "dayProgress": float,
                    "partialDayNumber": float,
                    "speed": int,
                },
                "weather": {"cycle": int, "cycleDay": int, "isHazardous": bool},
                "districts": [
                    {
                        "name": str,
                        "population": {"adults": int, "children": int, "bots": int},
                        "resources": dict,
                        "housing": {
                            "occupiedBeds": int,
                            "totalBeds": int,
                            "homeless": int,
                        },
                        "employment": {
                            "assigned": int,
                            "vacancies": int,
                            "unemployed": int,
                        },
                    }
                ],
                "trees": {
                    "markedGrown": int,
                    "markedSeedling": int,
                    "unmarkedGrown": int,
                },
                "crops": {"ready": int, "growing": int},
                "wellbeing": {"average": float, "miserable": int, "critical": int},
                "science": int,
                "alerts": dict,
            },
        )
        self.check("schema: summary (json)", len(errs) == 0, "; ".join(errs[:5]))

        # verify new summary sub-fields: species breakdowns and wellbeing categories
        trees_obj = summary.get("trees", {})
        has_species = isinstance(trees_obj.get("species"), list)
        self.check(
            "schema: summary trees.species",
            has_species,
            f"got {type(trees_obj.get('species'))}",
        )
        if has_species and trees_obj["species"]:
            errs = validate(
                trees_obj["species"],
                [
                    {
                        "name": str,
                        "markedGrown": int,
                        "unmarkedGrown": int,
                        "seedling": int,
                    }
                ],
            )
            self.check(
                "schema: trees.species[] fields", len(errs) == 0, "; ".join(errs[:5])
            )

        crops_obj = summary.get("crops", {})
        has_cspecies = isinstance(crops_obj.get("species"), list)
        self.check(
            "schema: summary crops.species",
            has_cspecies,
            f"got {type(crops_obj.get('species'))}",
        )
        if has_cspecies and crops_obj["species"]:
            errs = validate(
                crops_obj["species"], [{"name": str, "ready": int, "growing": int}]
            )
            self.check(
                "schema: crops.species[] fields", len(errs) == 0, "; ".join(errs[:5])
            )

        wb_obj = summary.get("wellbeing", {})
        has_cats = isinstance(wb_obj.get("categories"), list)
        self.check(
            "schema: summary wellbeing.categories",
            has_cats,
            f"got {type(wb_obj.get('categories'))}",
        )
        if has_cats and wb_obj["categories"]:
            errs = validate(
                wb_obj["categories"], [{"group": str, "current": float, "max": float}]
            )
            self.check(
                "schema: wellbeing.categories[] fields",
                len(errs) == 0,
                "; ".join(errs[:5]),
            )

        buildings = jbot.buildings()
        errs = validate(
            buildings,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "orientation": str,
                    "finished": int,
                    "paused": int,
                    "priority": str,
                    "workers": str,
                }
            ],
        )
        self.check(
            "schema: buildings basic (json)", len(errs) == 0, "; ".join(errs[:5])
        )

        buildings_full = jbot.buildings(detail="full")
        errs = validate(
            buildings_full,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "orientation": str,
                    "finished": int,
                    "paused": int,
                }
            ],
        )
        self.check("schema: buildings full (json)", len(errs) == 0, "; ".join(errs[:5]))

        trees = jbot.trees()
        errs = validate(
            trees,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "alive": int,
                    "grown": int,
                    "growth": float,
                }
            ],
        )
        self.check("schema: trees (json)", len(errs) == 0, "; ".join(errs[:5]))

        beavers = jbot.beavers()
        errs = validate(
            beavers,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "wellbeing": float,
                    "isBot": int,
                    "tier": str,
                    "workplace": str,
                    "critical": str,
                    "unmet": str,
                }
            ],
        )
        self.check("schema: beavers basic (json)", len(errs) == 0, "; ".join(errs[:5]))

        beavers_full = jbot.beavers(detail="full")
        errs = validate(
            beavers_full,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "wellbeing": float,
                    "isBot": int,
                    "anyCritical": int,
                    "hasHome": int,
                    "contaminated": int,
                    "needs": [
                        {
                            "id": str,
                            "points": float,
                            "wellbeing": int,
                            "favorable": int,
                            "critical": int,
                            "group": str,
                        }
                    ],
                }
            ],
        )
        self.check("schema: beavers full (json)", len(errs) == 0, "; ".join(errs[:5]))

        time_data = jbot.time()
        errs = validate(time_data, {"dayNumber": int, "dayProgress": float})
        self.check("schema: time (json)", len(errs) == 0, "; ".join(errs[:5]))

        weather = jbot.weather()
        errs = validate(weather, {"cycle": int, "cycleDay": int, "isHazardous": bool})
        self.check("schema: weather (json)", len(errs) == 0, "; ".join(errs[:5]))

        speed = jbot.speed()
        errs = validate(speed, {"speed": int})
        self.check("schema: speed (json)", len(errs) == 0, "; ".join(errs[:5]))

        science = jbot.science()
        errs = validate(science, {"points": int})
        self.check("schema: science (json)", len(errs) == 0, "; ".join(errs[:5]))

        workhours = jbot.workhours()
        errs = validate(workhours, {"endHours": float, "areWorkingHours": bool})
        self.check("schema: workhours (json)", len(errs) == 0, "; ".join(errs[:5]))

        prefabs = jbot.prefabs()
        errs = validate(prefabs, [{"name": str}])
        self.check("schema: prefabs (json)", len(errs) == 0, "; ".join(errs[:5]))

        gatherables = jbot.gatherables()
        errs = validate(
            gatherables,
            [{"id": int, "name": str, "x": int, "y": int, "z": int, "alive": int}],
        )
        self.check("schema: gatherables (json)", len(errs) == 0, "; ".join(errs[:5]))

        crops = jbot.crops()
        errs = validate(
            crops,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "marked": int,
                    "alive": int,
                    "grown": int,
                    "growth": float,
                }
            ],
        )
        self.check("schema: crops (json)", len(errs) == 0, "; ".join(errs[:5]))

        power = jbot.power()
        errs = validate(
            power,
            [
                {
                    "id": int,
                    "supply": int,
                    "demand": int,
                    "buildings": [
                        {
                            "name": str,
                            "id": int,
                            "isGenerator": int,
                            "nominalOutput": int,
                            "nominalInput": int,
                        }
                    ],
                }
            ],
        )
        self.check("schema: power (json)", len(errs) == 0, "; ".join(errs[:5]))

        districts = jbot.districts()
        errs = validate(
            districts,
            [
                {
                    "name": str,
                    "population": {"adults": int, "children": int, "bots": int},
                    "resources": dict,
                }
            ],
        )
        self.check("schema: districts (json)", len(errs) == 0, "; ".join(errs[:5]))

        resources = jbot.resources()
        self.check(
            "schema: resources (json)",
            isinstance(resources, dict),
            f"got {type(resources).__name__}",
        )
        if isinstance(resources, dict):
            # nested: {"District 1": {"Water": {"available": N, "all": N}}}
            for dname, goods in resources.items():
                if isinstance(goods, dict):
                    for gname, val in goods.items():
                        errs = validate(val, {"available": int, "all": int})
                        if errs:
                            self.check(
                                f"schema: resources.{dname}.{gname}",
                                False,
                                "; ".join(errs[:3]),
                            )
                            break
                    else:
                        continue
                    break
            else:
                self.check("schema: resources nested (json)", True, "")

        population = jbot.population()
        errs = validate(
            population, [{"district": str, "adults": int, "children": int, "bots": int}]
        )
        self.check("schema: population (json)", len(errs) == 0, "; ".join(errs[:5]))

        alerts = jbot.alerts()
        self.check(
            "schema: alerts (json)",
            isinstance(alerts, list),
            f"got {type(alerts).__name__}",
        )
        if isinstance(alerts, list) and alerts:
            errs = validate(alerts, [{"type": str, "id": int, "name": str}])
            self.check("schema: alerts[] (json)", len(errs) == 0, "; ".join(errs[:5]))

        wellbeing = jbot.wellbeing()
        errs = validate(wellbeing, {"beavers": int, "categories": list})
        self.check("schema: wellbeing (json)", len(errs) == 0, "; ".join(errs[:5]))

        notifications = jbot.notifications()
        errs = validate(
            notifications,
            [{"subject": str, "description": str, "cycle": int, "cycleDay": int}],
        )
        self.check("schema: notifications (json)", len(errs) == 0, "; ".join(errs[:5]))

        distribution = jbot.distribution()
        errs = validate(distribution, [{"district": str, "goods": list}])
        self.check("schema: distribution (json)", len(errs) == 0, "; ".join(errs[:5]))

        tree_clusters = jbot.tree_clusters()
        errs = validate(
            tree_clusters, [{"x": int, "y": int, "z": int, "grown": int, "total": int}]
        )
        self.check("schema: tree_clusters (json)", len(errs) == 0, "; ".join(errs[:5]))

        tiles = jbot.tiles(
            self.center_x, self.center_y, self.center_x + 3, self.center_y + 3
        )
        errs = validate(
            tiles,
            {
                "mapSize": dict,
                "region": dict,
                "tiles": [{"x": int, "y": int, "terrain": int}],
            },
        )
        self.check("schema: tiles (json)", len(errs) == 0, "; ".join(errs[:5]))

        fp = jbot.find_placement(
            "Path", self.center_x, self.center_y, self.center_x + 5, self.center_y + 5
        )
        errs = validate(
            fp, {"prefab": str, "sizeX": int, "sizeY": int, "placements": list}
        )
        self.check("schema: find_placement (json)", len(errs) == 0, "; ".join(errs[:5]))
        if isinstance(fp, dict) and fp.get("placements"):
            errs = validate(
                fp["placements"],
                [
                    {
                        "x": int,
                        "y": int,
                        "z": int,
                        "orientation": str,
                        "pathAccess": int,
                        "reachable": int,
                        "distance": float,
                        "nearPower": int,
                        "flooded": int,
                        "entranceX": int,
                        "entranceY": int,
                    }
                ],
            )
            # waterDepth is optional (only on water buildings)
            self.check(
                "schema: placement[] (json)", len(errs) == 0, "; ".join(errs[:5])
            )

        webhooks = jbot.list_webhooks()
        self.check(
            "schema: webhooks (json)",
            isinstance(webhooks, list),
            f"got {type(webhooks).__name__}",
        )

        # --- toon format schema validation ---
        tbot = self.toon_bot

        ts = tbot.summary()
        errs = validate(
            ts,
            {
                "day": int,
                "dayProgress": float,
                "speed": int,
                "cycle": int,
                "cycleDay": int,
                "isHazardous": bool,
                "adults": int,
                "beds": str,
                "workers": str,
                "wellbeing": float,
                "science": int,
                "alerts": str,
            },
        )
        self.check("schema: summary (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tb = tbot.buildings()
        errs = validate(
            tb,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "orientation": str,
                    "finished": int,
                    "paused": int,
                    "priority": str,
                    "workers": str,
                }
            ],
        )
        self.check(
            "schema: buildings basic (toon)", len(errs) == 0, "; ".join(errs[:5])
        )

        tbf = tbot.buildings(detail="full")
        errs = validate(
            tbf,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "orientation": str,
                    "finished": int,
                    "paused": int,
                }
            ],
        )
        self.check("schema: buildings full (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tt = tbot.trees()
        errs = validate(
            tt,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "marked": int,
                    "alive": int,
                    "grown": int,
                    "growth": float,
                }
            ],
        )
        self.check("schema: trees (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tc = tbot.crops()
        errs = validate(
            tc,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "marked": int,
                    "alive": int,
                    "grown": int,
                    "growth": float,
                }
            ],
        )
        self.check("schema: crops (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tg = tbot.gatherables()
        errs = validate(
            tg, [{"id": int, "name": str, "x": int, "y": int, "z": int, "alive": int}]
        )
        self.check("schema: gatherables (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tbv = tbot.beavers()
        errs = validate(
            tbv,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "wellbeing": float,
                    "isBot": int,
                    "tier": str,
                    "workplace": str,
                    "critical": str,
                    "unmet": str,
                }
            ],
        )
        self.check("schema: beavers basic (toon)", len(errs) == 0, "; ".join(errs[:5]))

        tbvf = tbot.beavers(detail="full")
        errs = validate(
            tbvf,
            [
                {
                    "id": int,
                    "name": str,
                    "x": int,
                    "y": int,
                    "z": int,
                    "wellbeing": float,
                    "isBot": int,
                    "anyCritical": int,
                    "hasHome": int,
                    "contaminated": int,
                    "needs": [
                        {
                            "id": str,
                            "points": float,
                            "wellbeing": int,
                            "favorable": int,
                            "critical": int,
                            "group": str,
                        }
                    ],
                }
            ],
        )
        self.check("schema: beavers full (toon)", len(errs) == 0, "; ".join(errs[:5]))

        td = tbot.districts()
        errs = validate(
            td, [{"name": str, "adults": int, "children": int, "bots": int}]
        )
        self.check("schema: districts (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_time = tbot.time()
        errs = validate(
            t_time, {"dayNumber": int, "dayProgress": float, "partialDayNumber": float}
        )
        self.check("schema: time (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_weather = tbot.weather()
        errs = validate(t_weather, {"cycle": int, "cycleDay": int, "isHazardous": bool})
        self.check("schema: weather (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_speed = tbot.speed()
        errs = validate(t_speed, {"speed": int})
        self.check("schema: speed (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_science = tbot.science()
        errs = validate(t_science, {"points": int})
        self.check("schema: science (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_workhours = tbot.workhours()
        errs = validate(t_workhours, {"endHours": float, "areWorkingHours": bool})
        self.check("schema: workhours (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_prefabs = tbot.prefabs()
        errs = validate(t_prefabs, [{"name": str}])
        self.check("schema: prefabs (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_power = tbot.power()
        errs = validate(
            t_power,
            [
                {
                    "id": int,
                    "supply": int,
                    "demand": int,
                    "buildings": [{"name": str, "id": int, "isGenerator": int}],
                }
            ],
        )
        self.check("schema: power (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_resources = tbot.resources()
        errs = validate(
            t_resources, [{"district": str, "good": str, "available": int, "all": int}]
        )
        self.check("schema: resources (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_population = tbot.population()
        errs = validate(
            t_population,
            [{"district": str, "adults": int, "children": int, "bots": int}],
        )
        self.check("schema: population (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_alerts = tbot.alerts()
        self.check(
            "schema: alerts (toon)",
            isinstance(t_alerts, list),
            f"got {type(t_alerts).__name__}",
        )
        if isinstance(t_alerts, list) and t_alerts:
            errs = validate(t_alerts, [{"type": str, "id": int, "name": str}])
            self.check("schema: alerts[] (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_wellbeing = tbot.wellbeing()
        errs = validate(t_wellbeing, {"beavers": int, "categories": list})
        self.check("schema: wellbeing (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_notifications = tbot.notifications()
        errs = validate(
            t_notifications,
            [{"subject": str, "description": str, "cycle": int, "cycleDay": int}],
        )
        self.check("schema: notifications (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_distribution = tbot.distribution()
        errs = validate(t_distribution, [{"district": str, "goods": list}])
        self.check("schema: distribution (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_tree_clusters = tbot.tree_clusters()
        errs = validate(
            t_tree_clusters,
            [{"x": int, "y": int, "z": int, "grown": int, "total": int}],
        )
        self.check("schema: tree_clusters (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_tiles = tbot.tiles(
            self.center_x, self.center_y, self.center_x + 3, self.center_y + 3
        )
        errs = validate(
            t_tiles,
            {
                "mapSize": dict,
                "region": dict,
                "tiles": [{"x": int, "y": int, "terrain": int}],
            },
        )
        self.check("schema: tiles (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_fp = tbot.find_placement(
            "Path", self.center_x, self.center_y, self.center_x + 5, self.center_y + 5
        )
        errs = validate(t_fp, [{"x": int, "y": int, "z": int, "orientation": str}])
        self.check("schema: find_placement (toon)", len(errs) == 0, "; ".join(errs[:5]))

        t_webhooks = tbot.list_webhooks()
        self.check(
            "schema: webhooks (toon)",
            isinstance(t_webhooks, list),
            f"got {type(t_webhooks).__name__}",
        )

        # --- cross-validate cached data vs live game state via debug endpoint ---
        self.prime_validation_snapshots()
        result = self.bot.debug(target="validate_all")
        if isinstance(result, dict) and "mismatches" in result:
            entities = result.get("entities", 0)
            fields = result.get("fields", 0)
            mismatches = result.get("mismatches", 0)
            failures = result.get("failures", [])
            self.check(
                f"cache vs live: {entities} entities, {fields} fields, {mismatches} mismatches",
                mismatches == 0,
                "; ".join(
                    f"{f.get('name', '?')}:{f.get('type', '?')}" for f in failures[:5]
                )
                if failures
                else "",
            )
        else:
            self.skip(
                "cache vs live", "debug endpoint not available or unexpected response"
            )

    def test_district_accuracy(self):
        """Cross-validate current summary population/employment/building-category data against buildings+beavers."""
        print("\n=== district accuracy ===\n")

        jbot = self.bot
        summary = jbot.summary()
        districts = summary.get("districts", [])
        if not districts:
            self.skip("district accuracy", "no districts in summary")
            return

        # get all buildings with full detail to check dwellings/workplaces
        buildings = jbot.buildings(limit=0, detail="full")
        items = (
            buildings.get("items", buildings)
            if isinstance(buildings, dict)
            else buildings
        )
        items = items if isinstance(items, list) else []

        # get all beavers for population cross-check (detail=full for district field)
        beavers = jbot.beavers(limit=0, detail="full")
        bvr_items = (
            beavers.get("items", beavers) if isinstance(beavers, dict) else beavers
        )
        bvr_items = bvr_items if isinstance(bvr_items, list) else []

        # --- verify population sums match beaver count ---
        sum_adults = sum(d["population"]["adults"] for d in districts)
        sum_children = sum(d["population"]["children"] for d in districts)
        sum_bots = sum(d["population"]["bots"] for d in districts)
        sum_pop = sum_adults + sum_children + sum_bots
        actual_beavers = len(bvr_items)
        self.check(
            f"district pop total ({sum_pop}) matches beavers ({actual_beavers})",
            sum_pop == actual_beavers,
            f"summary districts sum={sum_pop}, beavers endpoint={actual_beavers}",
        )

        # --- verify per-district beaver counts ---
        beaver_by_district = {}
        for b in bvr_items:
            d = b.get("district", "_unknown")
            beaver_by_district[d] = beaver_by_district.get(d, 0) + 1
        for d in districts:
            dname = d["name"]
            dpop = (
                d["population"]["adults"]
                + d["population"]["children"]
                + d["population"]["bots"]
            )
            bcount = beaver_by_district.get(dname, 0)
            self.check(
                f"district '{dname}' pop ({dpop}) matches beavers ({bcount})",
                dpop == bcount,
                f"population={dpop}, beavers in district={bcount}",
            )

        # --- verify current building-category summary matches building data ---
        # summary now exposes housing as a building-category count, not district bed capacity
        housing_buildings = 0
        workers_by_district = {}  # {district: [assigned, desired]}
        for b in items:
            # buildings in full detail have maxDwellers, dwellers, assignedWorkers, desiredWorkers
            bname = b.get("name", "")
            # skip paths. they have no district relevance for housing/employment
            if bname == "Path":
                continue
            max_d = b.get("maxDwellers", 0)
            if max_d and max_d > 0:
                housing_buildings += 1
            desired = b.get("desiredWorkers", 0)
            if desired and desired > 0:
                assigned = b.get("assignedWorkers", 0)
                workers_by_district["_all"] = workers_by_district.get("_all", [0, 0])
                workers_by_district["_all"][0] += assigned
                workers_by_district["_all"][1] += desired

        building_roles = summary.get("buildings", {})
        summary_housing = building_roles.get("housing", 0)
        self.check(
            f"housing building count: summary ({summary_housing}) vs buildings ({housing_buildings})",
            summary_housing == housing_buildings,
            f"summary={summary_housing}, buildings={housing_buildings}",
        )

        sum_assigned = sum(d["employment"]["assigned"] for d in districts)
        sum_vacancies = sum(d["employment"]["vacancies"] for d in districts)
        bld_assigned = workers_by_district.get("_all", [0, 0])[0]
        bld_vacancies = workers_by_district.get("_all", [0, 0])[1]
        self.check(
            f"employment assigned: summary ({sum_assigned}) vs buildings ({bld_assigned})",
            abs(sum_assigned - bld_assigned) <= 1,
            f"summary={sum_assigned}, buildings={bld_assigned}",
        )
        self.check(
            f"employment vacancies: summary ({sum_vacancies}) vs buildings ({bld_vacancies})",
            abs(sum_vacancies - bld_vacancies) <= 1,
            f"summary={sum_vacancies}, buildings={bld_vacancies}",
        )

        # verify unemployed = adults - assigned (per district)
        for d in districts:
            dname = d["name"]
            expected_unemployed = max(
                0, d["population"]["adults"] - d["employment"]["assigned"]
            )
            actual_unemployed = d["employment"]["unemployed"]
            self.check(
                f"district '{dname}' unemployed ({actual_unemployed}) = adults-assigned ({expected_unemployed})",
                actual_unemployed == expected_unemployed,
            )

        # --- verify species breakdowns match aggregate totals ---
        trees_obj = summary.get("trees", {})
        species = trees_obj.get("species", [])
        if species:
            sp_marked = sum(s.get("markedGrown", 0) for s in species)
            sp_unmarked = sum(s.get("unmarkedGrown", 0) for s in species)
            _sp_seedling = sum(s.get("seedling", 0) for s in species)
            self.check(
                f"tree species markedGrown sum ({sp_marked}) matches aggregate ({trees_obj.get('markedGrown')})",
                sp_marked == trees_obj.get("markedGrown", -1),
            )
            self.check(
                f"tree species unmarkedGrown sum ({sp_unmarked}) matches aggregate ({trees_obj.get('unmarkedGrown')})",
                sp_unmarked == trees_obj.get("unmarkedGrown", -1),
            )

        crops_obj = summary.get("crops", {})
        cspecies = crops_obj.get("species", [])
        if cspecies:
            cs_ready = sum(s.get("ready", 0) for s in cspecies)
            cs_growing = sum(s.get("growing", 0) for s in cspecies)
            self.check(
                f"crop species ready sum ({cs_ready}) matches aggregate ({crops_obj.get('ready')})",
                cs_ready == crops_obj.get("ready", -1),
            )
            self.check(
                f"crop species growing sum ({cs_growing}) matches aggregate ({crops_obj.get('growing')})",
                cs_growing == crops_obj.get("growing", -1),
            )

        # --- verify toon format population is aggregated correctly ---
        tbot = self.toon_bot
        toon_summary = tbot.summary()
        toon_adults = (
            toon_summary.get("adults", -1) if isinstance(toon_summary, dict) else -1
        )
        self.check(
            f"toon adults ({toon_adults}) matches json total ({sum_adults})",
            toon_adults == sum_adults,
            f"toon={toon_adults}, json districts sum={sum_adults}",
        )

    def _subprocess_time(self, cmd_args):
        """Run `python -m tbot.cli` as subprocess, return wall-clock ms plus error details."""
        t0 = time.perf_counter()
        try:
            from urllib.parse import urlparse

            p = urlparse(self.bot.url)
            extra = []
            if p.hostname:
                extra.append(f"--host={p.hostname}")
            if p.port:
                extra.append(f"--port={p.port}")
            r = subprocess.run(
                [sys.executable, "-m", "tbot.cli"] + extra + cmd_args,
                capture_output=True,
                text=True,
                timeout=30,
            )
            ms = (time.perf_counter() - t0) * 1000
            err = None
            if r.returncode != 0:
                err = {
                    "type": "returncode",
                    "code": r.returncode,
                    "stdout": (r.stdout or "")[-400:],
                    "stderr": (r.stderr or "")[-400:],
                }
            return ms, r.returncode == 0, err
        except subprocess.TimeoutExpired as ex:
            ms = (time.perf_counter() - t0) * 1000
            return (
                ms,
                False,
                {
                    "type": "timeout",
                    "code": None,
                    "stdout": ex.stdout[-400:] if isinstance(ex.stdout, str) else "",
                    "stderr": ex.stderr[-400:] if isinstance(ex.stderr, str) else "",
                },
            )

    def _bench_subprocess(self, _name, cmd_args, iterations):
        """Benchmark a CLI command as fresh subprocess. Returns timings, ok count, failures."""
        times = []
        ok = 0
        failures = []
        for _ in range(iterations):
            ms, success, err = self._subprocess_time(cmd_args)
            times.append(ms)
            if success:
                ok += 1
            else:
                failures.append(
                    err or {"type": "unknown", "code": None, "stdout": "", "stderr": ""}
                )
        return times, ok, failures

    def test_performance(self):
        print("\n=== performance (subprocess, real-world latency) ===\n")

        endpoints = [
            ("ping", ["ping"]),
            ("summary", ["summary"]),
            ("brain", ["brain"]),
            ("buildings", ["buildings"]),
            ("buildings full", ["buildings", "detail:full"]),
            ("buildings_v2", ["buildings_v2"]),
            ("buildings_v2 full", ["buildings_v2", "detail:full"]),
            ("trees", ["trees"]),
            ("gatherables", ["gatherables"]),
            ("beavers", ["beavers"]),
            ("alerts", ["alerts"]),
            ("resources", ["resources"]),
            ("weather", ["weather"]),
            ("time", ["time"]),
            ("districts", ["districts"]),
            ("distribution", ["distribution"]),
            ("science", ["science"]),
            ("notifications", ["notifications"]),
            ("workhours", ["workhours"]),
            ("speed", ["speed"]),
            ("prefabs", ["prefabs"]),
            ("wellbeing", ["wellbeing"]),
            ("tree_clusters", ["tree_clusters"]),
            ("food_clusters", ["food_clusters"]),
        ]

        iterations = getattr(self, "perf_iterations", 10)

        print(
            f"  timing {len(endpoints)} endpoints x {iterations} iterations (subprocess per call)\n"
        )
        print(f"  {'endpoint':<25} {'avg ms':>8} {'min ms':>8} {'max ms':>8} {'ok':>4}")
        print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 4}")

        total_ok = 0
        total_bad = 0
        for name, cmd in endpoints:
            times, ok, failures = self._bench_subprocess(name, cmd, iterations)
            total_ok += ok
            total_bad += iterations - ok
            avg = sum(times) / len(times)
            print(
                f"  {name:<25} {avg:>8.0f} {min(times):>8.0f} {max(times):>8.0f} {ok:>4}"
            )
            if failures:
                first = failures[0]
                print(f"    first failure: type={first['type']} code={first['code']}")
                if first["stdout"]:
                    print(f"    stdout: {first['stdout'].replace(chr(10), ' | ')}")
                if first["stderr"]:
                    print(f"    stderr: {first['stderr'].replace(chr(10), ' | ')}")

        print()
        self.check(
            f"all {total_ok + total_bad} responses valid",
            total_bad == 0,
            f"{total_bad} bad responses out of {total_ok + total_bad}",
        )

        # cache consistency (in-process, not subprocess)
        print("\n  cache consistency: verifying repeated calls return same data...")
        b1 = self.bot.buildings()
        b2 = self.bot.buildings()
        self.check(
            "buildings cache consistent",
            isinstance(b1, list) and isinstance(b2, list) and len(b1) == len(b2),
            f"first={len(b1) if isinstance(b1, list) else '?'}, second={len(b2) if isinstance(b2, list) else '?'}",
        )

        v21 = self.bot.buildings_v2()
        v22 = self.bot.buildings_v2()
        self.check(
            "buildings_v2 fresh reads consistent",
            isinstance(v21, list) and isinstance(v22, list) and len(v21) == len(v22),
            f"first={len(v21) if isinstance(v21, list) else '?'}, second={len(v22) if isinstance(v22, list) else '?'}",
        )

    def test_buildings_v2_parity(self):
        print("\n=== buildings v2 parity ===\n")

        legacy_basic = self.bot.buildings()
        v2_basic = self.bot.buildings_v2()
        basic_ok, basic_detail = self._compare_building_lists(legacy_basic, v2_basic)
        self.check("buildings_v2 basic matches buildings", basic_ok, basic_detail)

        legacy_full = self.bot.buildings(detail="full")
        v2_full = self.bot.buildings_v2(detail="full")
        full_ok, full_detail = self._compare_building_lists(legacy_full, v2_full)
        self.check("buildings_v2 full matches buildings", full_ok, full_detail)

        if isinstance(legacy_basic, list) and legacy_basic:
            _sample: Any = [
                item.get("id")
                for item in legacy_basic[:10]
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
            sample_ids: list[int] = _sample
            for bid in sample_ids:
                legacy_one = self._as_list(self.bot.buildings(id=int(bid)))
                v2_one = self._as_list(self.bot.buildings_v2(id=int(bid)))
                self.check(
                    f"buildings_v2 id:{bid} matches buildings",
                    legacy_one == v2_one,
                    self._compare_compact(legacy_one, v2_one),
                )
        else:
            self.skip("buildings_v2 id parity", "no buildings")

    def test_building_endpoint_perf(self):
        print("\n=== building endpoint perf ===\n")

        iterations = getattr(self, "perf_iterations", 100)
        endpoints = [
            ("buildings", ["buildings"]),
            ("buildings full", ["buildings", "detail:full"]),
            ("buildings_v2", ["buildings_v2"]),
            ("buildings_v2 full", ["buildings_v2", "detail:full"]),
        ]

        print(
            f"  timing {len(endpoints)} building endpoints x {iterations} iterations (subprocess per call)\n"
        )
        print(f"  {'endpoint':<20} {'avg ms':>8} {'min ms':>8} {'max ms':>8} {'ok':>4}")
        print(f"  {'-' * 20} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 4}")

        results = {}
        total_bad = 0
        for name, cmd in endpoints:
            times, ok, failures = self._bench_subprocess(name, cmd, iterations)
            results[name] = times
            total_bad += iterations - ok
            avg = sum(times) / len(times)
            print(
                f"  {name:<20} {avg:>8.0f} {min(times):>8.0f} {max(times):>8.0f} {ok:>4}"
            )
            if failures:
                first = failures[0]
                print(f"    first failure: type={first['type']} code={first['code']}")
                if first["stdout"]:
                    print(f"    stdout: {first['stdout'].replace(chr(10), ' | ')}")
                if first["stderr"]:
                    print(f"    stderr: {first['stderr'].replace(chr(10), ' | ')}")

        print()
        self.check(
            f"all {len(endpoints) * iterations} building perf responses valid",
            total_bad == 0,
            f"{total_bad} bad responses out of {len(endpoints) * iterations}",
        )

        basic_avg = sum(results["buildings"]) / len(results["buildings"])
        basic_v2_avg = sum(results["buildings_v2"]) / len(results["buildings_v2"])
        full_avg = sum(results["buildings full"]) / len(results["buildings full"])
        full_v2_avg = sum(results["buildings_v2 full"]) / len(
            results["buildings_v2 full"]
        )

        print("  deltas vs legacy:")
        print(
            f"  basic: {basic_v2_avg - basic_avg:+.0f}ms avg ({basic_v2_avg / basic_avg:.2f}x)"
        )
        print(
            f"  full:  {full_v2_avg - full_avg:+.0f}ms avg ({full_v2_avg / full_avg:.2f}x)"
        )

    def test_brain_perf(self):
        print("\n=== brain perf (subprocess, real-world latency) ===\n")
        import tbot as _tb

        iterations = getattr(self, "perf_iterations", 10)

        # ensure brain exists first
        self._subprocess_time(["brain"])

        # ping (baseline. python startup + minimal HTTP)
        ping_times, _, _ = self._bench_subprocess("ping", ["ping"], iterations)
        # summary
        summary_times, _, _ = self._bench_subprocess("summary", ["summary"], iterations)
        # brain cached
        cached_times, _, _ = self._bench_subprocess(
            "brain (cached)", ["brain"], iterations
        )
        # brain fresh x5. wipe settlement dir contents
        # _MEMORY_DIR may be base or settlement-specific; find the actual settlement dir
        settlement_dir = _tb._memory_dir
        if settlement_dir == _tb._MEMORY_BASE:
            # brain hasn't set it yet, find first settlement subdir
            for d in os.listdir(settlement_dir):
                p = os.path.join(settlement_dir, d)
                if os.path.isdir(p) and os.path.exists(os.path.join(p, "brain.toon")):
                    settlement_dir = p
                    break
        fresh_times = []
        for _ in range(5):
            if os.path.isdir(settlement_dir):
                for f in os.listdir(settlement_dir):
                    fp = os.path.join(settlement_dir, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
            ms, _, _ = self._subprocess_time(["brain"])
            fresh_times.append(ms)

        print(f"  {'method':<25} {'avg ms':>8} {'min ms':>8} {'max ms':>8} {'n':>4}")
        print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 4}")
        for name, times in [
            ("ping (baseline)", ping_times),
            ("summary", summary_times),
            ("brain (cached)", cached_times),
            ("brain (fresh)", fresh_times),
        ]:
            print(
                f"  {name:<25} {sum(times) / len(times):>8.0f} {min(times):>8.0f} {max(times):>8.0f} {len(times):>4}"
            )
        overhead = sum(cached_times) / len(cached_times) - sum(summary_times) / len(
            summary_times
        )
        print(f"\n  brain overhead vs summary: {overhead:+.0f}ms avg")
        print(
            f"  python startup (ping baseline): {sum(ping_times) / len(ping_times):.0f}ms avg"
        )
        self.check(
            "brain < 1000ms",
            sum(cached_times) / len(cached_times) < 1000,
            f"{sum(cached_times) / len(cached_times):.0f}ms",
        )


class TeeWriter:
    """Write to both terminal and a log file."""

    def __init__(self, terminal, logfile):
        self.terminal = terminal
        self.logfile = logfile

    def write(self, message):
        self.terminal.write(message)
        self.logfile.write(message)
        self.logfile.flush()

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Timberbot API test suite")
    parser.add_argument(
        "tests",
        nargs="*",
        help="specific test names to run (e.g. speed priority webhooks). omit for all",
    )
    parser.add_argument(
        "--exclude",
        "-x",
        nargs="+",
        default=[],
        help="test names to exclude (e.g. -x performance brain_perf)",
    )
    parser.add_argument(
        "--perf", action="store_true", help="run only the performance test"
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="call the in-game /api/benchmark endpoint",
    )
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=100,
        help="iterations for perf/benchmark (default: 100)",
    )
    parser.add_argument(
        "--list", action="store_true", help="list all available test names"
    )
    parser.add_argument(
        "--fails-only",
        "-f",
        action="store_true",
        help="only show failures (suppress PASS/SKIP output)",
    )
    parser.add_argument("--host", help="remote host to test")
    parser.add_argument("--port", type=int, help="remote port to test")
    parser.add_argument(
        "--callback-host", help="explicit local IP used for webhooks callback"
    )
    args = parser.parse_args()

    runner = TestRunner()
    if args.host or args.port:
        runner.bot = Timberbot(
            host=args.host, port=args.port, json_mode=True, write_timeout=300
        )
        runner.bot._check = lambda data: data
        runner.strict_bot = Timberbot(
            host=args.host, port=args.port, json_mode=True, write_timeout=300
        )
        runner.toon_bot = Timberbot(host=args.host, port=args.port, write_timeout=300)

    runner.fails_only = args.fails_only
    runner.callback_host = args.callback_host

    # collect all test methods
    all_tests = [
        (name.replace("test_", ""), getattr(runner, name))
        for name in dir(runner)
        if name.startswith("test_")
    ]

    if args.list:
        print("Groups:")
        for group in list(runner.GROUPS.keys()):
            tests = runner.GROUPS[group]
            print(f"  {group:12s} ({len(tests)}) {', '.join(tests)}")
        print(f"\nDefault run: {', '.join(runner.DEFAULT_GROUPS)}")
        print(
            f"Excluded from default: {', '.join(k for k in runner.GROUPS if k not in runner.DEFAULT_GROUPS)}"
        )
        return

    if not runner.bot.ping():
        print("error: game not reachable")
        sys.exit(1)

    # tee output to timestamped results file
    from datetime import datetime

    # File lives at python/tests/integration/validation_runner.py; repo root is four dirname()s up.
    results_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "test-results",
    )
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    test_names = "-".join(args.tests) if args.tests else "all"
    if len(test_names) > 80:
        test_names = test_names[:77] + "..."
    logpath = os.path.join(results_dir, f"{timestamp}-{test_names}.txt")
    logfile = open(logpath, "w")
    print(f"results: {logpath}")
    sys.stdout = TeeWriter(sys.__stdout__, logfile)
    sys.stderr = TeeWriter(sys.__stderr__, logfile)

    if args.benchmark:
        # call the in-game benchmark endpoint directly
        import requests

        print(f"Running in-game benchmark ({args.iterations} iterations)...\n")
        benchmark_timeout = 300
        r = requests.post(
            f"{runner.bot.url}/api/benchmark",
            json={"iterations": args.iterations},
            timeout=benchmark_timeout,
        )
        data = r.json()
        benchmarks = data.get("benchmarks", [])
        print(
            f"  {'test':<35} {'ms/call':>8} {'total ms':>10} {'gc0':>5} {'items':>6} {'pass':>5}"
        )
        print(f"  {'-' * 35} {'-' * 8} {'-' * 10} {'-' * 5} {'-' * 6} {'-' * 5}")
        for b in benchmarks:
            if b.get("test") == "_meta":
                print(
                    f"  META: {b.get('buildings', 0)} buildings, {b.get('beavers', 0)} beavers, {b.get('trees', 0)} trees"
                )
                continue
            pc = b.get("perCallMs", b.get("foreachMs", 0))
            tot = b.get("totalMs", 0)
            gc = b.get("gc0", b.get("foreachGC0", 0))
            items = b.get("items", b.get("count", ""))
            passed = b.get("pass", "")
            print(
                f"  {b['test']:<35} {pc:>8.3f} {tot:>10.1f} {gc:>5} {str(items):>6} {str(passed):>5}"
            )
        return

    group_names = set(runner.GROUPS.keys())
    test_names_set = {name for name, _ in all_tests}

    # resolve args: group names expand to their tests, individual test names pass through
    def resolve_names(names):
        resolved = []
        for name in names:
            if name in group_names:
                resolved.extend(runner.GROUPS[name])
            elif name in test_names_set:
                resolved.append(name)
            else:
                print(f"  unknown test or group: {name}")
                print(f"  groups: {', '.join(sorted(group_names))}")
                print(f"  tests: {', '.join(sorted(test_names_set))}")
                sys.exit(1)
        return resolved

    exclude = set(resolve_names(args.exclude))

    if args.perf:
        runner.discover()
        runner.perf_iterations = args.iterations
        runner.test_performance()
    elif args.tests:
        runner.discover()
        runner.perf_iterations = args.iterations
        test_map = dict(all_tests)
        for name in resolve_names(args.tests):
            if name in exclude:
                continue
            test_map[name]()
    else:
        runner.perf_iterations = args.iterations
        if exclude:
            runner.discover()
            # run default groups, skipping excluded tests
            test_map = dict(all_tests)
            for group in runner.DEFAULT_GROUPS:
                for name in runner.GROUPS[group]:
                    if name not in exclude and name in test_map:
                        test_map[name]()
        else:
            runner.run()

    summary = f"\n=== {runner.passed} passed, {runner.failed} failed"
    if runner.skipped:
        summary += f", {runner.skipped} skipped"
    summary += " ===\n"
    print(summary)
    print(f"results: {logpath}")
    logfile.close()
    sys.stdout = sys.__stdout__
    sys.exit(1 if runner.failed else 0)


if __name__ == "__main__":
    main()
