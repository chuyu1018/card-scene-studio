#!/usr/bin/env python3
"""Import the portable layout attached alongside the user's copied prompt."""
import argparse
import json
from pathlib import Path

from server import MAX_BODY, store_project


def import_layout(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path.stat().st_size > MAX_BODY:
        raise ValueError("排版文件超过 100 MB")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != "card-scene-studio-layout" or payload.get("version") != 1:
        raise ValueError("请提供卡景工坊导出的排版 JSON 文件")
    # Portable files are data only. Never activate an old automatic handoff.
    payload.pop("handoff_id", None)
    root.mkdir(parents=True, exist_ok=True)
    return store_project(root, payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(import_layout(args.layout, args.root), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
