# PyInstaller spec for the AAW portable app (one-folder build: AAW/AAW.exe + AAW/_internal).
# Build with:  python packaging/build_portable.py
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
WINDOWS = sys.platform.startswith("win")

datas = [(str(ROOT / name), ".") for name in (
    "IMPLEMENTER_PROFILES.json", "MODEL_CATALOG.json", "MODEL_REGISTRY.json", "AUTONOMY_ROLES.json",
    "MODEL_RECOMMENDATIONS.json", "LICENSE")]
datas += [(str(path), "PRODUCT_UI") for path in (ROOT / "PRODUCT_UI").iterdir() if path.is_file()]

# Engine and product modules are plain top-level modules; several are imported
# lazily inside functions, so they are listed explicitly.
hidden = ["aaw_paths", "autonomy_adapters", "autonomy_contract", "autonomy_controller", "autonomy_policy",
          "autonomy_run_lock", "execution_contract", "execution_ledger", "local_preprocess", "model_catalog",
          "process_observation", "routing_contract", "run_cancellation", "run_recovery", "workflow_runner",
          "workflow_schema", "product_home", "product_providers", "product_recommendations", "product_runs",
          "product_server", "product_view", "tkinter", "tkinter.filedialog"]

a = Analysis([str(ROOT / "AAW.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=["pytest", "playwright", "test_autonomy", "product_fake_cli", "aaw_autonomy_fake_cli"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AAW", console=not WINDOWS,
          icon=None, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="AAW", upx=False)
