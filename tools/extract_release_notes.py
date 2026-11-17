#!/usr/bin/env python3
"""Extract only the current VERSION section from CHANGELOG.md."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^##\s+", changelog)
    for section in sections[1:]:
        heading, separator, body = section.partition("\n")
        if re.match(r"v" + re.escape(version) + r"(?:\s|$)", heading):
            notes = body.strip()
            if not separator or not notes:
                raise SystemExit(f"CHANGELOG.md has an empty v{version} section")
            args.output.write_text(notes + "\n", encoding="utf-8")
            return
    raise SystemExit(f"CHANGELOG.md has no ## v{version} release section")


if __name__ == "__main__":
    main()
