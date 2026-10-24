"""Cloud runner for the BillBot Market Updater.

Runs the same updater code the Android app embeds, under plain CPython in
GitHub Actions. The Android app is no longer required for daily updates.

Usage:
    python updater/run_in_cloud.py --self-test
    python updater/run_in_cloud.py --publish
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import updater_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLOUD = ROOT / ".cloud"


def main() -> int:
    if "--self-test" in sys.argv:
        result = json.loads(updater_app.self_test())
    else:
        publish = "--publish" in sys.argv
        config = {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "token": os.environ.get("GH_TOKEN", ""),
        }
        result = json.loads(
            updater_app.run_update(
                str(CLOUD / "billbot-daily.db"),
                str(CLOUD / "work"),
                str(CLOUD / "status.json"),
                str(CLOUD / "updater.log"),
                json.dumps(config),
                publish,
            )
        )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
