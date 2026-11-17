#!/usr/bin/env python3
"""Check the hand-maintained version references used by CI and releases."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    version_bytes = (ROOT / "VERSION").read_bytes()
    try:
        version_file = version_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise SystemExit("VERSION must contain an ASCII semantic version") from None
    version = version_file.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not version_file.endswith("\n"):
        raise SystemExit("VERSION must be x.y.z followed by a newline")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## v" + re.escape(version) + r"(?:\s|$)", changelog, re.M)
    if len(headings) != 1 or not changelog.startswith(f"# 更新记录\n\n## v{version}"):
        raise SystemExit(f"CHANGELOG.md must start with exactly one ## v{version} section")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        f"releases/tag/v{version}",
        f"archive/refs/tags/v{version}.tar.gz",
        f"git checkout v{version}",
    )
    missing = [reference for reference in required if reference not in readme]
    if missing:
        raise SystemExit("README.md is missing current release references: " + ", ".join(missing))
    if not readme.startswith(f"# qqrobot\n\n当前版本：[v{version}]"):
        raise SystemExit("README.md current-version banner does not match VERSION")
    print(f"Release metadata is consistent for v{version}.")


if __name__ == "__main__":
    main()
