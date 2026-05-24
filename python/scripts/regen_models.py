"""Regenerate Pydantic v2 models from `openapi.yaml`.

Wraps `datamodel-code-generator` with the canonical flags. Commit the
generated files so they're reviewable.

Usage:
    python python/scripts/regen_models.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "openapi.yaml"
OUT_DIR = ROOT / "python" / "src" / "timberbot" / "api" / "models"
OUT_FILE = OUT_DIR / "_generated.py"
INIT_FILE = OUT_DIR / "__init__.py"

INIT_TEMPLATE = '''"""Pydantic v2 models for Timberbot HTTP responses.

Regenerated from `openapi.yaml` via `python/scripts/regen_models.py`. Do not
edit `_generated.py` by hand; the file is fully overwritten on regen.
"""
from timberbot.api.models._generated import *  # noqa: F401,F403
'''


def main() -> int:
    if not SPEC.exists():
        print(f"error: spec not found at {SPEC}", file=sys.stderr)
        return 2
    if shutil.which("datamodel-codegen") is None:
        print(
            "error: datamodel-codegen not installed. "
            "Run: pip install datamodel-code-generator",
            file=sys.stderr,
        )
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_FILE.exists():
        OUT_FILE.unlink()

    cmd = [
        "datamodel-codegen",
        "--input", str(SPEC),
        "--input-file-type", "openapi",
        "--output", str(OUT_FILE),
        "--output-model-type", "pydantic_v2.BaseModel",
        "--use-default-kwarg",
        "--use-double-quotes",
        "--target-python-version", "3.10",
        "--field-constraints",
        "--use-schema-description",
    ]
    print("  > " + " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    INIT_FILE.write_text(INIT_TEMPLATE)
    print(f"  wrote {INIT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
