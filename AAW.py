#!/usr/bin/env python3
"""AAW — Adaptive AI Work Engine, product entry point (`AAW.exe` in the portable build).

    AAW                       start the local app and open it in the browser
    AAW --no-browser          start without opening a browser (prints the URL)
    AAW --detect              print provider detection as JSON and exit
    AAW --self-test           quick packaging self-test (imports, data files) and exit
    AAW --run-worker RUN_ID --mode start|resume [--reconcile-lock TOKEN]
                              internal: background worker that drives one run

The app listens on 127.0.0.1 only. A second launch re-opens the running app
instead of starting another server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _prepare_environment() -> None:
    """Before any AAW module is imported: point runtime output to the user's data folder."""
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        os.environ.setdefault("AAW_ROOT", str(bundle))
        if str(bundle) not in sys.path:
            sys.path.insert(0, str(bundle))
    import product_home
    data = product_home.home()
    os.environ.setdefault("AAW_STATS_ROOT", str(data / "runs"))
    os.environ.setdefault("AAW_ROUTING_ROOT", str(data / "routing"))
    os.environ.setdefault("AAW_CONTROL_CENTER_STATE", str(data / "control_center_state"))
    if os.name == "nt":
        _extend_windows_path()


def _extend_windows_path() -> None:
    """Explorer-started apps often lack user PATH entries where AI CLIs live."""
    candidates = []
    home = Path.home()
    candidates += [home / ".local" / "bin", home / "AppData" / "Roaming" / "npm"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        codex_root = Path(local) / "OpenAI" / "Codex" / "bin"
        if codex_root.is_dir():
            found = sorted(codex_root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            if found:
                candidates.append(found[0].parent)
    current = os.environ.get("PATH", "")
    extra = [str(p) for p in candidates if p.is_dir() and str(p) not in current]
    if extra:
        os.environ["PATH"] = os.pathsep.join([current, *extra])


def self_test() -> int:
    import autonomy_adapters  # noqa: F401  (engine importable)
    import autonomy_controller  # noqa: F401
    import product_recommendations as pr
    import product_server
    import workflow_runner as wr
    profiles = wr.load_implementer_profiles()
    catalog = pr.builtin_catalog()
    ui = product_server.UI_ROOT / "index.html"
    report = {"status": "PASS" if profiles and catalog and ui.is_file() else "FAIL",
              "profiles": len(profiles), "recommendations_catalog": catalog["catalog_version"],
              "ui": str(ui), "frozen": bool(getattr(sys, "frozen", False)),
              "data_home": os.environ.get("AAW_STATS_ROOT")}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


def _ensure_streams() -> None:
    """A windowed (no-console) build has no stdout/stderr; keep a log instead."""
    if sys.stdout is None or sys.stderr is None:
        import product_home
        log = open(product_home.home() / "aaw.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def main(argv: list[str] | None = None) -> int:
    _prepare_environment()
    _ensure_streams()
    parser = argparse.ArgumentParser(prog="AAW", description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-worker")
    parser.add_argument("--mode", choices=("start", "resume"), default="start")
    parser.add_argument("--reconcile-lock")
    parser.add_argument("--detect", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.run_worker:
        import product_runs
        return product_runs.worker_main(args.run_worker, args.mode, reconcile_lock=args.reconcile_lock)
    if args.detect:
        import product_providers
        print(json.dumps(product_providers.detect_all(), indent=2, ensure_ascii=False))
        return 0
    if args.self_test:
        return self_test()
    import product_server
    return product_server.serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
