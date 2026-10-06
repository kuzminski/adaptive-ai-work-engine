#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.2 — local app server (loopback HTTP + static UI).

Transport only, stdlib only (same reasoning as `aaw_bridge_server`): every
route is a thin call into `product_runs` / `product_view` /
`product_providers` / `product_recommendations`. No run logic lives here.

Security posture: bound to 127.0.0.1, single local user. Every API call must
carry the per-launch token that is embedded in the served page
(`X-AAW-Token`), and the Host header must be the loopback address the app
was opened on — so another web page in the user's browser cannot drive AAW
(a cross-origin request cannot read the token, and a custom header forces a
CORS preflight this server never approves). Not for network exposure.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import product_home
import product_providers as pp
import product_recommendations as pr
import product_runs as prun
import product_version
import product_view as pv

UI_ROOT = Path(__file__).resolve().with_name("PRODUCT_UI")
APP_VERSION = f"AAW {product_version.RELEASE}"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon"}


class App:
    def __init__(self, token: str) -> None:
        self.token = token
        self.server: ThreadingHTTPServer | None = None

    # GET ---------------------------------------------------------------------
    def bootstrap(self, _q: dict[str, Any]) -> dict[str, Any]:
        detection = product_home.read_json(product_home.home() / "providers.json")
        catalog = pr.effective_catalog()
        if isinstance(detection, dict) and "providers" in detection:
            detection = pp.apply_states(detection)
        return {"version": APP_VERSION, "release": product_version.describe(), "settings": product_home.load_settings(),
                "choices": pr.choice_options(catalog), "providers": detection,
                "notice": pp.PROVIDER_NOTICE, "setup_notice": pp.SETUP_NOTICE,
                "has_runs": bool(prun.list_run_ids()), "recent_repos": prun.recent_repos(),
                "catalog": {"version": catalog["catalog_version"], "source": catalog["_source"],
                            "last_updated": catalog.get("last_updated")},
                "data_home": str(product_home.home()), "frozen": product_home.is_frozen(),
                "control_center_available": self._control_center_path() is not None,
                "slot_labels": pr.SLOT_LABELS}

    def recommendations(self, _q: dict[str, Any]) -> dict[str, Any]:
        catalog = pr.effective_catalog()
        detection = prun.detection_snapshot()
        runnable = pp.runnable_profiles(detection)
        return {"catalog_version": catalog["catalog_version"], "source": catalog["_source"],
                "last_updated": catalog.get("last_updated"),
                "profiles": [{**row, "runnable_here": row["profile"] in runnable} for row in catalog["profiles"]],
                "choices": pr.choice_options(catalog)}

    # POST --------------------------------------------------------------------
    def detect(self, _body: dict[str, Any]) -> dict[str, Any]:
        return prun.detection_snapshot(refresh=True)

    def verify_models(self, body: dict[str, Any]) -> dict[str, Any]:
        ids = body.get("profile_ids")
        if not ids:
            setup = prun.resolve_setup(body.get("choices") or {}, implementer_chain=body.get("implementer_chain"))
            ids = sorted({p for g in setup["groups"].values() for p in g["checkable"]})
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise prun.ProductError("profile_ids must be a list of profile IDs")
        result = prun.verify_models(ids)
        return {**result, "setup": prun.resolve_setup(body.get("choices") or {}, implementer_chain=body.get("implementer_chain"))}

    def first_run_done(self, _body: dict[str, Any]) -> dict[str, Any]:
        return product_home.save_settings({"first_run_completed": True})

    def pick_folder(self, _body: dict[str, Any]) -> dict[str, Any]:
        """Native folder dialog on the user's machine (the browser cannot reveal paths)."""
        result: dict[str, Any] = {"path": None}

        def ask() -> None:
            try:
                import tkinter
                from tkinter import filedialog
                root = tkinter.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                result["path"] = filedialog.askdirectory(title="Wybierz folder projektu") or None
                root.destroy()
            except Exception as exc:  # no Tk / no display: the user types the path instead
                result["error"] = f"Okno wyboru folderu niedostępne ({type(exc).__name__}); wpisz ścieżkę."
        thread = threading.Thread(target=ask)
        thread.start()
        thread.join(timeout=600)
        return result

    def settings_post(self, body: dict[str, Any]) -> dict[str, Any]:
        return product_home.save_settings(body)

    def update_recommendations(self, _body: dict[str, Any]) -> dict[str, Any]:
        return pr.update_catalog()

    def quit(self, _body: dict[str, Any]) -> dict[str, Any]:
        threading.Timer(0.3, lambda: self.server and self.server.shutdown()).start()
        return {"status": "STOPPING_UI", "note": "Okno AAW zostanie zamknięte. Trwające zadania pracują dalej w tle."}

    @staticmethod
    def _control_center_path() -> Path | None:
        path = Path(__file__).resolve().parent / "CONTROL_CENTER" / "aaw_control_center.py"
        return path if path.is_file() and not product_home.is_frozen() else None

    def open_control_center(self, _body: dict[str, Any]) -> dict[str, Any]:
        path = self._control_center_path()
        if not path:
            raise prun.ProductError("Konsola operatora (Control Center) jest dostępna tylko w wersji źródłowej.")
        subprocess.Popen([sys.executable, str(path)], cwd=str(path.parent.parent))
        return {"status": "LAUNCHED"}


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 2_000_000:
        raise prun.ProductError("żądanie jest za duże")
    raw = handler.rfile.read(length) if length else b"{}"
    value = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(value, dict):
        raise prun.ProductError("oczekiwano obiektu JSON")
    return value


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    get_routes: dict[str, Callable[..., Any]] = {
        "/api/bootstrap": app.bootstrap,
        "/api/home": lambda q: pv.home_view(),
        "/api/settings": lambda q: product_home.load_settings(),
        "/api/recommendations": app.recommendations,
        "/api/ping": lambda q: {"ok": True, "version": APP_VERSION},
        "/api/version": lambda q: product_version.describe(),
    }
    post_routes: dict[str, Callable[[dict[str, Any]], Any]] = {
        "/api/providers/detect": app.detect,
        "/api/models/verify": app.verify_models,
        "/api/setup/resolve": lambda b: prun.resolve_setup(b.get("choices") or {},
                                                          implementer_chain=b.get("implementer_chain")),
        "/api/first-run/done": app.first_run_done,
        "/api/repo/inspect": lambda b: prun.inspect_repo(str(b.get("path") or "")),
        "/api/repo/init": lambda b: prun.init_git_repo(str(b.get("path") or "")),
        "/api/pick-folder": app.pick_folder,
        "/api/tasks/preview": lambda b: prun.public_preview(prun.preview_task(b.get("form") or {})),
        "/api/tasks/start": lambda b: prun.start_task(b.get("form") or {}),
        "/api/settings": app.settings_post,
        "/api/recommendations/update": app.update_recommendations,
        "/api/quit": app.quit,
        "/api/advanced/control-center": app.open_control_center,
    }
    run_get = re.compile(r"^/api/runs/([A-Za-z0-9_\-]+)(/evidence)?$")
    run_post = re.compile(r"^/api/runs/([A-Za-z0-9_\-]+)/(stop|resume|accept|reject|continue)$")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AAW/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console/log quiet
            return

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: Any) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _api(self, fn: Callable[[], Any]) -> None:
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-AAW-Token") or "", app.token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                self._json(HTTPStatus.OK, fn())
            except prun.ProductError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                if not self._host_ok():
                    self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                    return
                page = (UI_ROOT / "index.html").read_text(encoding="utf-8").replace("__AAW_TOKEN__", app.token)
                self._send(HTTPStatus.OK, page.encode("utf-8"), STATIC_TYPES[".html"])
                return
            if url.path.startswith("/static/"):
                name = url.path[len("/static/"):]
                target = (UI_ROOT / name).resolve()
                if UI_ROOT.resolve() not in target.parents or not target.is_file():
                    self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                    return
                self._send(HTTPStatus.OK, target.read_bytes(),
                           STATIC_TYPES.get(target.suffix, "application/octet-stream"))
                return
            if url.path in get_routes:
                self._api(lambda: get_routes[url.path](query))
                return
            match = run_get.match(url.path)
            if match:
                run_id, evidence = match.groups()
                if evidence:
                    self._api(lambda: pv.evidence_view(run_id, query.get("execution_id")))
                else:
                    self._api(lambda: pv.run_view(run_id))
                return
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path in post_routes:
                self._api(lambda: post_routes[url.path](_json_body(self)))
                return
            match = run_post.match(url.path)
            if not match:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            run_id, action = match.groups()

            def act() -> Any:
                body = _json_body(self)
                if action == "stop":
                    return prun.request_stop(run_id, force=bool(body.get("force")))
                if action == "resume":
                    return prun.resume_task(run_id, expected_lock_token=body.get("lock_token"))
                if action == "accept":
                    return prun.accept(run_id, early_end=bool(body.get("early_end")))
                if action == "reject":
                    return prun.reject(run_id, reason=str(body.get("reason") or ""))
                return prun.prepare_continuation(run_id, mode=str(body.get("mode") or ""))
            self._api(act)

    return Handler


def _existing_instance() -> str | None:
    info = product_home.read_json(product_home.home() / "ui.json") or {}
    port, token = info.get("port"), info.get("token")
    if not port or not token:
        return None
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-AAW-Token": token})
        with urllib.request.urlopen(request, timeout=1.5) as response:
            if json.loads(response.read()).get("ok"):
                return f"http://127.0.0.1:{port}/"
    except Exception:
        return None
    return None


def serve(*, port: int = 0, open_browser: bool = True, ready: Callable[[str], None] | None = None) -> int:
    existing = _existing_instance()
    if existing:
        print(f"AAW już działa: {existing}")
        if open_browser:
            webbrowser.open(existing)
        return 0
    token = secrets.token_urlsafe(24)
    app = App(token)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    app.server = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    product_home.write_json(product_home.home() / "ui.json", {"port": server.server_address[1], "token": token,
                                                             "pid": os.getpid()})
    print(f"AAW działa: {url}  (zamknij z poziomu aplikacji: Ustawienia → Zamknij AAW)", flush=True)
    if ready:
        ready(url)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        info = product_home.read_json(product_home.home() / "ui.json") or {}
        if info.get("token") == token:
            (product_home.home() / "ui.json").unlink(missing_ok=True)
    return 0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
