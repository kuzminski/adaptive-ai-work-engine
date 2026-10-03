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
REQUIRED_FILES = ("QUICK_START.md", "README.md", "CHANGELOG.md", "LICENSE", "VERSION.txt", "SZYBKI_START.txt",
                  "EXAMPLES/01_expense_tracker.md", "EXAMPLES/02_tests_and_validation.md",
                  "EXAMPLES/03_todo_web_page.md")


def main() -> int:
    build = ROOT / "build" / "pyinstaller"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
                    "--workpath", str(build), str(ROOT / "packaging" / "AAW.spec")], check=True, cwd=ROOT)
    app = DIST / "AAW"
    sys.path.insert(0, str(ROOT))
    import product_version
    # user-facing release documents next to AAW.exe
    shutil.copy2(ROOT / "QUICK_START.md", app / "QUICK_START.md")
    shutil.copy2(ROOT / "release" / "README.md", app / "README.md")
    shutil.copy2(ROOT / "CHANGELOG.md", app / "CHANGELOG.md")
    shutil.copy2(ROOT / "LICENSE", app / "LICENSE")
    shutil.copy2(ROOT / "SZYBKI_START.txt", app / "SZYBKI_START.txt")
    shutil.copytree(ROOT / "release" / "examples", app / "EXAMPLES", dirs_exist_ok=True)
    (app / "VERSION.txt").write_text(
        f"{product_version.RELEASE_NAME}\nrelease: {product_version.RELEASE}\ndate: {product_version.RELEASE_DATE}\n"
        f"engine: {product_version.ENGINE_BASE}\nplatform: {OS_NAME}-x64\n", encoding="utf-8")
    exe = app / ("AAW.exe" if OS_NAME == "Windows" else "AAW")
    env = dict(os.environ, AAW_PRODUCT_HOME=str(ROOT / "build" / "selftest_home"))
    report = ROOT / "build" / "selftest_report.json"
    report.unlink(missing_ok=True)
    result = subprocess.run([str(exe), "--self-test", "--report-file", str(report)], capture_output=True, text=True,
                            env=env, timeout=120)
    print(result.stdout, result.stderr, report.read_text(encoding="utf-8") if report.is_file() else "(no report)")
    if result.returncode != 0 or not report.is_file():
        print("self-test of the built app FAILED", file=sys.stderr)
        return 1
    version_file = ROOT / "build" / "version_report.json"
    version_file.unlink(missing_ok=True)
    subprocess.run([str(exe), "--version", "--report-file", str(version_file)], env=env, timeout=60)
    reported = version_file.read_text(encoding="utf-8") if version_file.is_file() else ""
    if product_version.RELEASE not in reported:
        print(f"--version of the built app FAILED: {reported!r}", file=sys.stderr)
        return 1
    missing = [name for name in REQUIRED_FILES if not (app / name).exists()]
    if missing:
        print(f"release package incomplete, missing: {missing}", file=sys.stderr)
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
