Integration tests
=================

This directory contains tests that drive the **live `/api/*` surface of a
running Timberborn game**. They were originally the standalone scripts
`timberbot/script/test_v2.py` and `test_validation.py`; they now live here as
pytest-shaped modules so the rest of the suite can pick them up alongside the
fast unit tests.

Two test files, one shared fixture layer:

- `test_v2_modes.py` — five tests, one per V2Runner mode (`smoke`,
  `freshness`, `write_to_read`, `performance`, `concurrency`). Performance +
  concurrency carry an extra `@pytest.mark.slow`.
- `test_validation_methods.py` — every `TestRunner.test_*` method becomes its
  own parametrized pytest case (~70 cases). The method's group (from
  `TestRunner.GROUPS`) is applied as a marker so you can filter:
  `-m "integration and write"`, `-m "integration and not perf"`, etc.

Default behaviour
-----------------

The package-level `addopts = "-m 'not integration'"` in `pyproject.toml`
means **`pytest python/tests/` skips these by default**. CI runs only the fast
unit tests. The integration tests are opt-in.

Running
-------

Launch Timberborn with the Timberbot mod loaded, open a save game, then:

```bash
# everything (slow modes included)
pytest python/tests/integration -m integration

# fast integration tests only (skip perf + concurrency)
pytest python/tests/integration -m "integration and not slow"

# one validation group
pytest python/tests/integration -m "integration and write"

# point at a remote game
pytest python/tests/integration -m integration --tbot-host 192.168.1.10 --tbot-port 9090

# one specific case
pytest python/tests/integration::test_validation_method[priority] -m integration
```

When the mod isn't reachable, the session-scoped `live_game` fixture calls
`pytest.skip(...)` and every integration test is reported as **skipped**, not
failed — so a run from a machine without the game stays green.

Layout
------

```
python/tests/integration/
  conftest.py                  shared fixtures: live_game, v2_runner, validation_runner
  v2_runner.py                 the V2Runner class (was timberbot/script/test_v2.py)
  validation_runner.py         the TestRunner class (was test_validation.py)
  v2_specs.py                  ENDPOINT_SPECS data (was test_v2_specs.py)
  test_v2_modes.py             pytest wrappers — one test per V2Runner mode
  test_validation_methods.py   pytest wrappers — one test per TestRunner.test_*
```

The two `*_runner.py` files **still expose their original `main()`** so the
legacy `python -m python.tests.integration.v2_runner smoke` style invocation
keeps working. The pytest wrappers are the recommended interface going forward.

Future work
-----------

The runner classes were custom test harnesses (custom `check()`/`skip()`
methods, manual assertion accounting, stateful `discover()`). Migrating each
individual assertion into a flat pytest function would let us drop the runner
state entirely and use real `assert` statements. That's not in scope here —
this PR's goal is to *expose* the existing tests to pytest so they can be
filtered, parametrized, and reported uniformly. Method-by-method conversion
is a follow-up task.
