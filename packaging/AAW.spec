# PyInstaller spec for the AAW portable app (one-folder build: AAW/AAW.exe + AAW/_internal).
# Build with:  python packaging/build_portable.py
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
WINDOWS = sys.platform.startswith("win")
sys.path.insert(0, str(ROOT))
import product_version  # noqa: E402


def _version_resource() -> str | None:
    """Windows file-version resource (Company/Product/Description). An exe without one looks anonymous
    to Defender's ML heuristics (Trojan:Win32/Wacatac.*!ml false positives on unsigned PyInstaller apps)."""
    if not WINDOWS:
        return None
    import re
    nums = [int(x) for x in re.findall(r"\d+", product_version.RELEASE.split("-")[0])][:3]
    nums += [0] * (3 - len(nums))
    quad = tuple(nums + [0])
    fields = [("CompanyName", "AAW (open source, Apache-2.0)"),
              ("FileDescription", "AAW - Adaptive AI Work Engine"),
              ("FileVersion", product_version.RELEASE), ("InternalName", "AAW"),
              ("LegalCopyright", "Apache License 2.0"), ("OriginalFilename", "AAW.exe"),
              ("ProductName", "AAW - Adaptive AI Work Engine"), ("ProductVersion", product_version.RELEASE)]
    strings = ", ".join(f"StringStruct({k!r}, {v!r})" for k, v in fields)
    text = (f"VSVersionInfo(ffi=FixedFileInfo(filevers={quad}, prodvers={quad}, mask=0x3f, flags=0x0, "
            f"OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), "
            f"kids=[StringFileInfo([StringTable('040904B0', [{strings}])]), "
            f"VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")
    target = ROOT / "build" / "aaw_version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)

datas = [(str(ROOT / name), ".") for name in (
    "IMPLEMENTER_PROFILES.json", "MODEL_CATALOG.json", "MODEL_REGISTRY.json", "AUTONOMY_ROLES.json",
    "MODEL_RECOMMENDATIONS.json", "LICENSE")]
datas += [(str(path), "PRODUCT_UI") for path in (ROOT / "PRODUCT_UI").iterdir() if path.is_file()]

# Engine and product modules are plain top-level modules; several are imported
# lazily inside functions, so they are listed explicitly.
hidden = ["aaw_paths", "autonomy_adapters", "autonomy_contract", "autonomy_controller", "autonomy_policy",
          "autonomy_run_lock", "execution_contract", "execution_ledger", "local_preprocess", "model_catalog", "model_router", "repair_escalation", "provider_adapters",
          "process_observation", "routing_contract", "run_cancellation", "run_recovery", "workflow_runner",
          "workflow_schema", "product_home", "product_providers", "product_recommendations", "product_runs",
          "product_server", "product_view", "product_version", "tkinter", "tkinter.filedialog"]

a = Analysis([str(ROOT / "AAW.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=["pytest", "playwright", "test_autonomy", "product_fake_cli", "aaw_autonomy_fake_cli"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AAW", console=not WINDOWS,
          icon=None, upx=False, version=_version_resource())
coll = COLLECT(exe, a.binaries, a.datas, name="AAW", upx=False)
