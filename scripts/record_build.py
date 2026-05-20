#!/usr/bin/env python3
"""Record per-build APK filenames into manifest entries (one entry per built APK).

Used inside the build-apps matrix job: after a successful build, run this with
APP_NAME, SOURCE, ARCH, APK_PATH set so we can write a tiny per-build JSON that
the release job will merge into the final manifest.json.

Output file: ./build_records/<app>__<source>__<arch>.json
Content:    {"key": "app|source|arch", "apk": "<filename>"}
"""
import os
import sys
import json
from pathlib import Path

REC_DIR = Path("build_records")


def main() -> int:
    app = os.environ.get("APP_NAME", "").strip()
    src = os.environ.get("SOURCE", "").strip()
    arch = os.environ.get("ARCH", "universal").strip() or "universal"
    apk_path = os.environ.get("APK_PATH", "").strip()

    if not app or not src:
        print("APP_NAME / SOURCE missing; skipping manifest record")
        return 0
    if not apk_path:
        # No APK produced; record empty so we can still see this attempt
        apk_name = ""
    else:
        apk_name = Path(apk_path).name

    REC_DIR.mkdir(parents=True, exist_ok=True)
    record = {"key": f"{app}|{src}|{arch}", "apk": apk_name,
              "app_name": app, "source": src, "arch": arch}

    safe = f"{app}__{src}__{arch}".replace("/", "_")
    fp = REC_DIR / f"{safe}.json"
    with fp.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"Recorded build: {fp} -> {record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
