"""Build, package, and optionally release the Timberbot Steam Workshop mod.

The PR 2 rework removed `timberbot.py` and the agent prompts from the mod ZIP.
The Python client is now an independent package (`pip install tbot`); the mod
ZIP carries only the DLL, manifest, thumbnail, settings.json, and docs.

Usage:
    python release.py            build + package the mod ZIP
    python release.py --release  build + package + tag + GitHub release
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
SRC_DIR = os.path.join(ROOT, "timberbot", "src")
DIST_DIR = os.path.join(ROOT, "dist")
MOD_DIR = os.path.join(str(Path.home()), "Documents", "Timberborn", "Mods", "Timberbot")
MANIFEST = os.path.join(SRC_DIR, "manifest.json")
DLL_PATH = os.path.join(SRC_DIR, "bin", "Release", "netstandard2.1", "Timberbot.dll")


def run(cmd, **kwargs):
    print(f"  > {cmd}")
    subprocess.check_call(cmd, shell=True, **kwargs)


PRESERVE_MOD_FILES = {"workshop_data.json", "autoload.json"}


def remove_path(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except PermissionError as ex:
        print(f"  ! warning: could not remove {path}: {ex}")


def clean_mod_dir():
    if not os.path.isdir(MOD_DIR):
        return

    for name in os.listdir(MOD_DIR):
        if name in PRESERVE_MOD_FILES:
            continue
        remove_path(os.path.join(MOD_DIR, name))


def main():
    release = "--release" in sys.argv

    with open(MANIFEST) as f:
        version = json.load(f)["Version"]

    print(f"building Timberbot API v{version}")

    # clean deployed mod folder but preserve Workshop linkage and local settings
    clean_mod_dir()

    # ensure settings.json ships with correct defaults
    settings_path = os.path.join(SRC_DIR, "settings.json")
    with open(settings_path) as f:
        settings = json.load(f)
    defaults = {"terminal": "wt -d {cwd} --", "debugEndpointEnabled": False}
    changed = False
    for key, val in defaults.items():
        if settings.get(key) != val:
            settings[key] = val
            changed = True
    if changed:
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        print("  fixed settings.json defaults")

    # build
    run("dotnet build -c Release", cwd=SRC_DIR)

    # package
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(DIST_DIR)

    # mod zip: DLL + manifest + thumbnail + settings + docs
    zip_name = f"TimberbotAPI-v{version}.zip"
    zip_path = os.path.join(DIST_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(DLL_PATH, "Timberbot.dll")
        zf.write(MANIFEST, "manifest.json")
        thumb = os.path.join(SRC_DIR, "thumbnail.png")
        if os.path.exists(thumb):
            zf.write(thumb, "thumbnail.png")
        zf.write(settings_path, "settings.json")
        docs_dir = os.path.join(ROOT, "docs")
        if os.path.isdir(docs_dir):
            for doc in os.listdir(docs_dir):
                if doc.endswith((".md", ".txt")):
                    zf.write(os.path.join(docs_dir, doc), f"docs/{doc}")

    print(f"packaged: dist/{zip_name}")

    # release
    if release:
        tag = f"v{version}"
        run(f"git tag {tag}")
        run(f"git push origin {tag}")
        run(
            f'gh release create {tag} "{zip_path}"'
            f" --repo abix-/TimberbornMods"
            f' --title "Timberbot API {tag}"'
            f' --notes "HTTP API for AI agents to read and control Timberborn."'
        )
        print(f"released: {tag}")


if __name__ == "__main__":
    main()
