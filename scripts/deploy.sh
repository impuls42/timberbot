#!/usr/bin/env bash
#
# Build and deploy the Timberbot C# mod into the right Timberborn Mods/ folder
# for the current OS.
#
# Why this exists: `dotnet build` already runs a post-build Deploy target that
# copies the DLL into `$(ModDir)`. On Windows and macOS that path is correct;
# on Linux/Proton the csproj defaults to the canonical compatdata prefix for
# Steam AppID 1062090, which covers the standard case. This script handles the
# rest:
#
#   * non-standard Wine prefixes (custom Steam library, alternate AppID, beta)
#   * non-`steamuser` Wine usernames
#   * Steam Deck installs that mount compatdata under a different parent
#
# The runtime `tbot` CLI no longer touches the game's Documents tree
# (impuls42/timberbot#43 PR 4), so the resolver lives at
# `scripts/_paths.py` rather than under `python/src/timberbot/` — this
# script is its only caller. `TBOT_DOCUMENTS_DIR` / `TBOT_MOD_DIR` env vars
# still work the same way.
#
# Usage:
#   scripts/deploy.sh                # native or auto-detected Proton path
#   scripts/deploy.sh /custom/mods   # explicit ModDir override
#   TBOT_DOCUMENTS_DIR=... scripts/deploy.sh   # via the env var the resolver honours

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CSPROJ="$REPO_ROOT/timberbot/src/Timberbot.csproj"

if [[ ! -f "$CSPROJ" ]]; then
    echo "deploy: cannot find $CSPROJ" >&2
    exit 1
fi

# Resolve the mod-dir target. Order of precedence:
#   1. $1 (explicit positional arg).
#   2. The standalone Proton/Documents resolver at scripts/_paths.py
#      (handles env-var override + Proton compatdata scan).
#   3. Fall back to the csproj default (which is the native ~/Documents path).
MOD_DIR="${1:-}"
if [[ -z "$MOD_DIR" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        MOD_DIR="$(python3 "$REPO_ROOT/scripts/_paths.py" 2>/dev/null || true)"
    fi
fi

echo "deploy: target mod dir = ${MOD_DIR:-(csproj default)}"

if [[ -n "$MOD_DIR" ]]; then
    dotnet build "$CSPROJ" -c Release -p:ModDir="$MOD_DIR"
else
    dotnet build "$CSPROJ" -c Release
fi

echo "deploy: done."
