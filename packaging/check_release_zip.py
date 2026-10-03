#!/usr/bin/env python3
"""Check a release ZIP from the user's point of view (no Windows needed).

    python packaging/check_release_zip.py dist/AAW-Windows-x64.zip

- the ZIP root holds exactly one folder, AAW/;
- AAW/ holds AAW.exe, SZYBKI_START.txt, README.md, QUICK_START.md and EXAMPLES/;
- every file or folder name mentioned in the start guides shipped inside the ZIP
  exists in the ZIP, and the guides name no source-repository-only path.
"""
from __future__ import annotations

import re
import sys
import zipfile

REQUIRED = ("AAW.exe", "SZYBKI_START.txt", "README.md", "QUICK_START.md", "EXAMPLES/")
GUIDES = ("SZYBKI_START.txt", "QUICK_START.md", "README.md")
NAME = re.compile(r"(?<![\w/\\.-])([\w-]+\.(?:exe|txt|md))\b|\b(EXAMPLES|_internal)\b")
SOURCE_ONLY = ("packaging/", "packaging\\", "release/", "release\\", ".github", "build_portable", "pip install",
               "python AAW.py", "git clone")


def check(path: str) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        roots = {name.split("/", 1)[0] for name in names}
        if roots != {"AAW"}:
            problems.append(f"ZIP root must be exactly one folder AAW/, found {sorted(roots)}")
        inside = {name.split("/", 1)[1] for name in names if "/" in name}
        top = {entry.split("/", 1)[0] + ("/" if "/" in entry else "") for entry in inside if entry}
        for required in REQUIRED:
            if required not in top:
                problems.append(f"missing AAW/{required}")
        for guide in GUIDES:
            if guide not in top:
                continue
            text = zf.read(f"AAW/{guide}").decode("utf-8")
            for match in NAME.finditer(text):
                mentioned = match.group(1) or match.group(2)
                if mentioned not in top and mentioned + "/" not in top:
                    problems.append(f"{guide} mentions {mentioned!r}, which is not in AAW/")
            for marker in SOURCE_ONLY:
                if marker in text:
                    problems.append(f"{guide} refers to source-repository path {marker!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    problems = check(argv[1])
    for problem in problems:
        print("FAIL:", problem)
    if not problems:
        print(f"OK: {argv[1]} - AAW/ with AAW.exe, SZYBKI_START.txt, README.md, EXAMPLES/; guide paths valid")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
