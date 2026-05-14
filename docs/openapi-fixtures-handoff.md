---
title: OpenAPI fixtures handoff
status: draft
audience: agent
---

This note is the handoff for the **next development session** that picks up
where PR 3 (OpenAPI spec + contract tests) left off. It is intended for a
Claude Code instance that has **access to a real Timberborn install with the
Timberbot mod loaded and a running save**, because the work that remains is
specifically about capturing real game responses and validating them against
the spec.

If you're reading this without a game running, **stop and ping the user** for
a session with live-game access.

## What's already done (in master after PR 3 merges)

- `openapi.yaml` at the repo root documents all 59 operations across 55
  paths. Request-body schemas are complete and verified by the C# contract
  test against `req.Body?.Value<T>("...")` extractions.
- `OPENAPI_VERSION` constant on both sides (Python `tbot.OPENAPI_VERSION` /
  C# `TimberbotPure.OPENAPI_VERSION`), surfaced via `/api/ping`.
- C# `OpenApiContractTests.cs` (9 tests): route coverage both directions,
  operationId uniqueness, request-body field coverage, version match.
- Python `tests/test_openapi_spec.py` (8 tests): spec validates against
  OpenAPI 3.1, every operationId has a `TimberbotClient` method, every POST
  has a JSON request body, every operation has a 200 response.
- **Response schemas are intentionally skeletal** (`additionalProperties:
  true` on most). That's your job.

## What you need to do

### 1. Record golden response fixtures (1-2 hours of in-game time)

For every GET endpoint and every POST endpoint that returns structured data,
capture a real JSON response from a running game and save it under:

```
python/tests/fixtures/openapi/<operationId>.json
```

A small helper script is the cleanest way:

```python
# python/scripts/capture_fixtures.py  (new)
import json, pathlib
from tbot.api.client import TimberbotClient

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "openapi"
OUT.mkdir(parents=True, exist_ok=True)
bot = TimberbotClient(json_mode=True)

GET_OPS = [
    "ping", "settlement", "summary", "time", "weather", "population",
    "resources", "districts", "distribution", "science", "wellbeing",
    "workhours", "speed", "prefabs", "power", "tiles", "tree_clusters",
    "food_clusters", "alerts", "notifications", "buildings", "beavers",
    "trees", "crops", "gatherables", "list_webhooks",
]
for op in GET_OPS:
    response = getattr(bot, op)()
    (OUT / f"{op}.json").write_text(json.dumps(response, indent=2))
    print(f"wrote {op}.json")
```

Run it against a save that has decent diversity (some buildings, some
beavers, an active drought, a few automation links, a tree-cutting marker
set). The wirer/scout subagent prompts under `tbot.agent_prompts` describe
the kind of scene you want.

Don't bother capturing fixtures for mutating POSTs (`set_speed`,
`place_building`, etc.) — they need before/after pairs and live testing is
a different exercise (see step 4).

### 2. Generate Pydantic v2 models from the spec

```bash
pip install datamodel-code-generator
python -m datamodel_code_generator \
    --input openapi.yaml \
    --output python/src/tbot/api/models/ \
    --output-model-type pydantic_v2.BaseModel \
    --use-default-kwarg \
    --target-python-version 3.10
```

Commit the generated files. Add a `python/scripts/regen_models.py` wrapper
so future regenerations stay reproducible.

### 3. Replace skeletal `additionalProperties: true` response schemas

For each GET fixture you captured in step 1:

- Look at the actual response shape.
- Replace the `additionalProperties: true` placeholder in `openapi.yaml`
  with a real schema declaring every observed top-level field.
- Use `allOf` + `additionalProperties: true` if you want forward-compatibility
  (server can add fields without breaking clients).
- Re-run `regen_models.py` to refresh the Pydantic models.

Keep `additionalProperties: true` only for genuinely free-form responses
(`/api/debug`, `/api/benchmark`).

### 4. Add the response-validation test

```
python/tests/contract/test_openapi_responses.py  (new)
```

For each operationId with a fixture, parse the fixture through the
corresponding Pydantic model and assert no validation errors. This catches
the case where someone changes a response shape without updating the spec.

If you have time, also add a "hits a live server" variant gated behind an
env var (`TBOT_OPENAPI_LIVE=1`) that calls the real mod and validates each
response, so the test is opt-in for developers with the game open.

### 5. Wire the client to use the models for response parsing

Right now `TimberbotClient.summary()` returns `dict[str, Any]`. After step 2:

```python
from tbot.api.models import Summary

def summary(self) -> Summary:
    return Summary.model_validate(self._get("/api/summary"))
```

This is mechanical for stable shapes (scalars, single-object responses) and
trickier for list/paginated ones where the legacy code returns `list | dict`
depending on `?format=`. The simplest path: keep the existing
`dict`-returning methods and add `model_validated` counterparts (e.g.
`summary_model()` returning `Summary`). Callers can opt in.

Decide with the user whether to break the existing return types or layer
the typed API on top.

### 6. Update the C# contract test to validate response shape

Add a new `OpenApiResponseShapeTests.cs` that:

- Loads each fixture in `python/tests/fixtures/openapi/`.
- For each, walks the corresponding spec operation's response schema and
  asserts every documented `required` field is present in the fixture.

This is the C# half of the round-trip — Python validates that the *spec*
describes the *fixture*; C# can additionally validate that the *fixture* (a
real server response) satisfies the spec's `required` constraints.

## Out of scope for the follow-up

- Adding `/api/agent/start`, `/api/agent/stop`, `/api/agent/status` routes
  to the C# server. These are referenced in `TimberbotClient.agent_status`
  / `agent_stop` and in PR 2's `tbot agent run` flow but the actual HTTP
  routing doesn't exist yet. Track separately in #2; not blocking the
  contract work.
- Authentication / authorization. Mod listens on localhost by default;
  spec-level security is a separate refactor.

## How to verify your session is complete

1. `cd python && pytest` is green, including any new fixture-based tests.
2. `cd timberbot/test && dotnet test` is green.
3. `python -m tbot.dev.spec_audit` (if you add one) reports zero drift.
4. `tbot summary --json | python -c 'import json, sys, pydantic, tbot.api.models;
   tbot.api.models.Summary.model_validate(json.load(sys.stdin))'` works on a
   live mod without errors.

When all four are green, push a commit on top of PR 3's branch (or open a
follow-up PR), update issue #2 with the fixture coverage stats, and ping
the user.
