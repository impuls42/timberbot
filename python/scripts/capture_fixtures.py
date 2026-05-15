"""Capture golden JSON fixtures from a running Timberbot mod.

Hits every GET endpoint that returns structured data and saves the raw
response to `python/tests/fixtures/openapi/<operationId>.json`. Run this
against a save with reasonable diversity (some buildings, beavers, an active
drought, automation links, a tree-cutting marker) so the fixtures exercise
the breadth of each schema.

Usage:
    python python/scripts/capture_fixtures.py
    python python/scripts/capture_fixtures.py --host 127.0.0.1 --port 8085
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import requests

from tbot.api.client import TimberbotClient

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "openapi"

GET_OPS: list[tuple[str, str, dict]] = [
    ("ping",            "/api/ping",            {}),
    ("settlement",      "/api/settlement",      {}),
    ("summary",         "/api/summary",         {}),
    ("time",            "/api/time",            {}),
    ("weather",         "/api/weather",         {}),
    ("population",      "/api/population",      {}),
    ("resources",       "/api/resources",       {}),
    ("districts",       "/api/districts",       {}),
    ("distribution",    "/api/distribution",    {}),
    ("science",         "/api/science",         {}),
    ("wellbeing",       "/api/wellbeing",       {}),
    ("workhours",       "/api/workhours",       {}),
    ("speed",           "/api/speed",           {}),
    ("prefabs",         "/api/prefabs",         {}),
    ("power",           "/api/power",           {}),
    ("tiles",           "/api/tiles",           {}),
    ("tree_clusters",   "/api/tree_clusters",   {}),
    ("food_clusters",   "/api/food_clusters",   {}),
    ("alerts",          "/api/alerts",          {}),
    ("notifications",   "/api/notifications",   {}),
    ("buildings",       "/api/buildings",       {"detail": "full"}),
    ("beavers",         "/api/beavers",         {"detail": "full"}),
    ("trees",           "/api/trees",           {}),
    ("crops",           "/api/crops",           {}),
    ("gatherables",     "/api/gatherables",     {}),
    ("list_webhooks",   "/api/webhooks",        {}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    parser.add_argument(
        "--seed-webhook",
        action="store_true",
        default=True,
        help="Register a dummy webhook before capturing list_webhooks (default: on).",
    )
    parser.add_argument(
        "--no-seed-webhook",
        dest="seed_webhook",
        action="store_false",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    bot = TimberbotClient(host=args.host, port=args.port, json_mode=True)
    base = bot.url
    if not bot.ping():
        print(f"error: mod not reachable at {base}", file=sys.stderr)
        return 2

    seeded_webhook_id: int | None = None
    if args.seed_webhook:
        try:
            r = bot.register_webhook(
                "http://127.0.0.1:9/timberbot-fixture-seed",
                events=["day.start"],
            )
            seeded_webhook_id = r.get("id") if isinstance(r, dict) else None
        except Exception as e:
            print(f"  warn: could not seed webhook: {e}", file=sys.stderr)

    written: list[str] = []
    failed: list[tuple[str, str]] = []
    try:
        for op_id, path, params in GET_OPS:
            q = {"format": "json", **params}
            try:
                resp = requests.get(f"{base}{path}", params=q, timeout=10)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                failed.append((op_id, str(e)))
                print(f"  FAIL {op_id} ({path}): {e}", file=sys.stderr)
                continue
            (args.out / f"{op_id}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n"
            )
            written.append(op_id)
            print(f"  wrote {op_id}.json")
    finally:
        if seeded_webhook_id is not None:
            try:
                bot.unregister_webhook(seeded_webhook_id)
            except Exception as e:
                print(f"  warn: could not unregister seed webhook: {e}", file=sys.stderr)

    print(f"\ncaptured {len(written)}/{len(GET_OPS)} fixtures under {args.out}")
    if failed:
        print(f"failed: {[f[0] for f in failed]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
