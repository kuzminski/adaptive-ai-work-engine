from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import product_home
import product_server


@pytest.fixture
def static_server(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    ui_dir = tmp_path / "PRODUCT_UI"
    ui_dir.mkdir()

    # Create root index
    (ui_dir / "index.html").write_text("<html>Token: __AAW_TOKEN__</html>", encoding="utf-8")

    # Nested assets directory
    assets_dir = ui_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "index.js").write_text("console.log('nested-js');", encoding="utf-8")

    # Sub-nested directory
    nested_dir = assets_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "deep.css").write_text("body { color: red; }", encoding="utf-8")

    # Font woff2
    fonts_dir = ui_dir / "fonts"
    fonts_dir.mkdir()
    (fonts_dir / "font.woff2").write_bytes(b"\x00\x01\x00\x00woff2data")

    # JSON file
    data_dir = ui_dir / "data"
    data_dir.mkdir()
    (data_dir / "manifest.json").write_text('{"name": "test"}', encoding="utf-8")

    # Images
    img_dir = ui_dir / "images"
    img_dir.mkdir()
    (img_dir / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
    (img_dir / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    # Root-level static files
    (ui_dir / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    (ui_dir / "sample.bin").write_bytes(b"binary data")

    # File outside UI_ROOT to test directory traversal
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret_content", encoding="utf-8")

    monkeypatch.setenv("AAW_PRODUCT_HOME", str(home))
    monkeypatch.setattr(product_server, "UI_ROOT", ui_dir)

    port = product_server.free_port()
    ready = threading.Event()
    thread = threading.Thread(
        target=product_server.serve,
        kwargs={"port": port, "open_browser": False, "ready": lambda url: ready.set()},
        daemon=True,
    )
    thread.start()
    assert ready.wait(10), "Server did not start in time"

    info = product_home.read_json(home / "ui.json")
    token = info["token"]
    base_url = f"http://127.0.0.1:{port}"

    yield {
        "base_url": base_url,
        "token": token,
        "port": port,
        "ui_dir": ui_dir,
        "outside_file": outside_file,
    }

    try:
        req = urllib.request.Request(
            f"{base_url}/api/quit",
            data=b"{}",
            headers={"X-AAW-Token": token, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    thread.join(timeout=5)


def _get(base_url: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, str | None, bytes]:
    req = urllib.request.Request(f"{base_url}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_nested_static_assets_and_mime_types(static_server):
    base = static_server["base_url"]

    # Nested JS
    status, ctype, body = _get(base, "/static/assets/index.js")
    assert status == 200
    assert ctype == "text/javascript; charset=utf-8"
    assert body == b"console.log('nested-js');"

    # Sub-nested CSS
    status, ctype, body = _get(base, "/static/assets/nested/deep.css")
    assert status == 200
    assert ctype == "text/css; charset=utf-8"
    assert body == b"body { color: red; }"

    # Font woff2
    status, ctype, body = _get(base, "/static/fonts/font.woff2")
    assert status == 200
    assert ctype == "font/woff2"
    assert body == b"\x00\x01\x00\x00woff2data"

    # JSON
    status, ctype, body = _get(base, "/static/data/manifest.json")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"name": "test"}

    # SVG
    status, ctype, body = _get(base, "/static/images/logo.svg")
    assert status == 200
    assert ctype == "image/svg+xml"
    assert body == b"<svg></svg>"

    # PNG
    status, ctype, body = _get(base, "/static/images/photo.png")
    assert status == 200
    assert ctype == "image/png"
    assert body == b"\x89PNG\r\n\x1a\n"

    # ICO
    status, ctype, body = _get(base, "/static/favicon.ico")
    assert status == 200
    assert ctype == "image/x-icon"
    assert body == b"\x00\x00\x01\x00"

    # Fallback octet-stream
    status, ctype, body = _get(base, "/static/sample.bin")
    assert status == 200
    assert ctype == "application/octet-stream"
    assert body == b"binary data"


def test_directory_traversal_and_not_found(static_server):
    base = static_server["base_url"]

    # Traversal attempts
    status, _, _ = _get(base, "/static/../secret.txt")
    assert status == 404

    status, _, _ = _get(base, "/static/assets/../../secret.txt")
    assert status == 404

    status, _, _ = _get(base, "/static/..")
    assert status == 404

    # Directory access (should not serve directory listing or crash)
    status, _, _ = _get(base, "/static/")
    assert status == 404

    status, _, _ = _get(base, "/static/assets")
    assert status == 404

    status, _, _ = _get(base, "/static/assets/")
    assert status == 404

    # Nonexistent file
    status, _, _ = _get(base, "/static/does_not_exist.js")
    assert status == 404


def test_index_page_token_substitution_and_forbidden(static_server):
    base = static_server["base_url"]
    token = static_server["token"]

    # Root path
    status, ctype, body = _get(base, "/")
    assert status == 200
    assert ctype == "text/html; charset=utf-8"
    assert f"Token: {token}".encode("utf-8") in body

    # /index.html
    status, ctype, body = _get(base, "/index.html")
    assert status == 200
    assert ctype == "text/html; charset=utf-8"
    assert f"Token: {token}".encode("utf-8") in body

    # Invalid host returns 403 Forbidden
    status, _, _ = _get(base, "/", headers={"Host": "evil.com"})
    assert status == 403


def test_packaging_spec_datas_collects_nested_files(tmp_path):
    ui_root = tmp_path / "PRODUCT_UI"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("html", encoding="utf-8")
    (ui_root / "app.js").write_text("js", encoding="utf-8")

    assets = ui_root / "assets"
    assets.mkdir()
    (assets / "vendor.js").write_text("vendor", encoding="utf-8")

    nested = assets / "sub"
    nested.mkdir()
    (nested / "style.css").write_text("css", encoding="utf-8")

    datas = [
        (str(path), (Path("PRODUCT_UI") / path.relative_to(ui_root).parent).as_posix())
        for path in ui_root.rglob("*")
        if path.is_file()
    ]

    dest_map = {Path(src).name: dest for src, dest in datas}
    assert dest_map["index.html"] == "PRODUCT_UI"
    assert dest_map["app.js"] == "PRODUCT_UI"
    assert dest_map["vendor.js"] == "PRODUCT_UI/assets"
    assert dest_map["style.css"] == "PRODUCT_UI/assets/sub"


def test_build_portable_frontend_handling(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_portable", ROOT / "packaging" / "build_portable.py")
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)

    called_cmds = []

    def fake_run(cmd, cwd=None, check=True):
        called_cmds.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    frontend_dir = tmp_path / "frontend"
    monkeypatch.setattr(bp, "ROOT", tmp_path)

    # 1. When frontend does not exist
    called_cmds.clear()
    assert not frontend_dir.exists()

    # 2. When frontend exists and npm is present
    frontend_dir.mkdir()
    monkeypatch.setattr(bp.shutil, "which", lambda cmd: "/usr/bin/npm" if cmd == "npm" else None)
    res = bp.build_frontend_if_present(tmp_path)
    assert res is True
    assert len(called_cmds) == 1
    assert called_cmds[0] == (["/usr/bin/npm", "run", "build"], frontend_dir)

    # 3. When frontend exists and npm is NOT present
    called_cmds.clear()
    monkeypatch.setattr(bp.shutil, "which", lambda cmd: None)
    res = bp.build_frontend_if_present(tmp_path)
    assert res is False
    assert len(called_cmds) == 0
    err = capsys.readouterr().err
    assert "skipping frontend build" in err


def test_decision_continue_endpoint_mode_validation(static_server, monkeypatch):
    called = []

    def fake_continue(run_id, *, mode):
        called.append((run_id, mode))
        return {'ok': True, 'prefill': {'goal': 'next-step'}}

    monkeypatch.setattr(product_server.prun, 'prepare_continuation', fake_continue)
    base = static_server['base_url']
    token = static_server['token']

    # 1. POST /api/runs/run-101/decision with action=CONTINUE, mode='' (missing/fallback to direction)
    req = urllib.request.Request(
        f'{base}/api/runs/run-101/decision',
        data=json.dumps({'action': 'CONTINUE', 'feedback': 'Some feedback'}).encode('utf-8'),
        headers={'X-AAW-Token': token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode('utf-8'))
        assert data['ok'] is True
    assert called[-1] == ('run-101', 'direction')

    # 2. POST /api/runs/run-101/decision with action=CONTINUE, mode='new_goal'
    req = urllib.request.Request(
        f'{base}/api/runs/run-101/decision',
        data=json.dumps({'action': 'CONTINUE', 'mode': 'new_goal'}).encode('utf-8'),
        headers={'X-AAW-Token': token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
    assert called[-1] == ('run-101', 'new_goal')

    # 3. POST /api/runs/run-101/continue with invalid mode -> defaults to direction
    req = urllib.request.Request(
        f'{base}/api/runs/run-101/continue',
        data=json.dumps({'mode': 'invalid_mode_value'}).encode('utf-8'),
        headers={'X-AAW-Token': token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
    assert called[-1] == ('run-101', 'direction')
