#!/usr/bin/env python3
"""Build the AAW portable app and zip it.

    python packaging/build_portable.py            -> dist/AAW-<OS>-x64.zip

Requires `pip install pyinstaller` on the BUILD machine only. A Windows zip
must be built on Windows (CI: .github/workflows/aaw-portable.yml); PyInstaller
does not cross-compile. The built app is smoke-tested with `AAW --self-test`.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OS_NAME = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}[platform.system()]


def main() -> int:
    build = ROOT / "build" / "pyinstaller"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
                    "--workpath", str(build), str(ROOT / "packaging" / "AAW.spec")], check=True, cwd=ROOT)
    app = DIST / "AAW"
    shutil.copy2(ROOT / "packaging" / "README_PORTABLE.txt", app / "README.txt")
    exe = app / ("AAW.exe" if OS_NAME == "Windows" else "AAW")
    env = dict(os.environ, AAW_PRODUCT_HOME=str(ROOT / "build" / "selftest_home"))
    result = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True, env=env, timeout=120)
    print(result.stdout, result.stderr)
    if result.returncode != 0:
        print("self-test of the built app FAILED", file=sys.stderr)
        return 1
    archive = DIST / f"AAW-{OS_NAME}-x64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app.rglob("*")):
            zf.write(path, Path("AAW") / path.relative_to(app))
    print(f"built {archive} ({archive.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
