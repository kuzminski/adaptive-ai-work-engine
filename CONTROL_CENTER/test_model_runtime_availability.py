import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import aaw_control_center as cc


def available_runtime(catalog):
    runtime = {
        "snapshot_schema_version": cc.MODEL_RUNTIME_SCHEMA_VERSION,
        "profile_catalog_version": cc.profile_catalog_version(),
        "checked_at": "2026-09-05T10:00:00+02:00",
        "harnesses": {
            "codex": {"state": "AVAILABLE", "reason": "Codex CLI responds"},
            "claude": {"state": "UNAVAILABLE", "reason": "Claude CLI not configured"},
        },
    }
    runtime["profiles"] = {profile_id: cc.get_runtime_profile_state(profile_id, catalog, runtime) for profile_id in catalog}
    return runtime


class ModelRuntimeAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = cc.load_implementer_profiles_for_ui()
        cls.runtime = available_runtime(cls.catalog)

    def display(self, profile_id, runtime=None):
        app = cc.ControlCenter.__new__(cc.ControlCenter)
        app.profile_catalog = self.catalog
        app.model_runtime = self.runtime if runtime is None else runtime
        return app._profile_display(self.catalog[profile_id])

    def test_01_verified_terra_renders_without_unavailable(self):
        self.assertEqual(self.display("TERRA_HIGH"), "Terra / high")

    def test_02_verified_sol_medium_renders_without_unavailable(self):
        self.assertEqual(self.display("SOL_MEDIUM"), "Sol / medium")

    def test_03_verified_sol_high_renders_without_unavailable(self):
        self.assertEqual(self.display("SOL_HIGH"), "Sol / high")

    def test_04_unavailable_sonnet_renders_unavailable(self):
        self.assertEqual(self.display("SONNET_HIGH"), "Sonnet / high — unavailable")

    def test_05_unknown_renders_not_checked(self):
        runtime = {"checked_at": "now", "harnesses": {"codex": {"state": "UNKNOWN", "reason": "Probe incomplete"}}}
        self.assertEqual(self.display("TERRA_HIGH", runtime), "Terra / high — not checked")

    def test_06_missing_snapshot_is_not_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(cc.load_model_runtime_snapshot(Path(temp) / "missing.json", self.catalog))
        self.assertEqual(cc.get_runtime_profile_state("TERRA_HIGH", self.catalog, None)["state"], "NOT_CHECKED")

    def test_07_preflight_completion_refreshes_selectors(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter)
        app.preflight_button = mock.Mock(); app.preflight_result = {}; app.model_runtime = None
        app.preflight = mock.Mock(); app.preflight.get_children.return_value = []
        app.version_warning_var = mock.Mock(); app.system_status_var = mock.Mock()
        app.profile_catalog = self.catalog
        app._refresh_runtime_dependent_views = mock.Mock(); app._refresh_system_status = mock.Mock()
        result = {"model_runtime": self.runtime, "launcher": "OK", "playbook_root": "OK", "model_registry": "OK"}
        with mock.patch.object(cc, "write_json_atomic") as write:
            app._show_preflight(result)
        write.assert_called_once(); app._refresh_runtime_dependent_views.assert_called_once()

    def start_state(self, selected):
        root = tk.Tk(); root.withdraw()
        try:
            app = cc.ControlCenter.__new__(cc.ControlCenter); app.profile_catalog = self.catalog; app.model_runtime = self.runtime
            app.binding_display_to_id = {app._profile_display(profile): pid for pid, profile in self.catalog.items()}
            app.node_binding_vars = {"N01": tk.StringVar(root, value=app._profile_display(self.catalog[selected]))}
            app.workflow_button = mock.Mock()
            app._refresh_start_state()
            return app.workflow_button.configure.call_args.kwargs["state"]
        finally:
            root.destroy()

    def test_08_selected_available_profile_enables_start(self):
        self.assertEqual(self.start_state("TERRA_HIGH"), "normal")

    def test_09_selected_unavailable_sonnet_blocks_start(self):
        self.assertEqual(self.start_state("SONNET_HIGH"), "disabled")

    def test_10_nonselected_sonnet_does_not_block_start(self):
        self.assertEqual(self.start_state("SOL_HIGH"), "normal")

    def test_11_warning_counter_updates_after_preflight(self):
        root = tk.Tk(); root.withdraw()
        try:
            app = cc.ControlCenter.__new__(cc.ControlCenter); app.profile_catalog = self.catalog; app.model_runtime = self.runtime
            app.binding_display_to_id = {app._profile_display(profile): pid for pid, profile in self.catalog.items()}
            app.node_binding_vars = {"N01": tk.StringVar(root, value="Terra / high")}; app.execution_mode_var = tk.StringVar(root, value="direct")
            app.preflight_result = {"launcher": "OK", "playbook_root": "OK", "model_registry": "OK", "orca_runtime": "NOT READY"}; app.system_status_var = tk.StringVar(root)
            app._refresh_system_status(); self.assertEqual(app.system_status_var.get(), "System ready")
        finally:
            root.destroy()

    def test_12_gui_restart_loads_persisted_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime.json"; cc.write_json_atomic(path, self.runtime)
            loaded = cc.load_model_runtime_snapshot(path, self.catalog)
        self.assertEqual(loaded["profiles"]["SOL_HIGH"]["state"], "VERIFIED")

    def test_13_queue_editor_uses_same_runtime_state(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter); app.profile_catalog = self.catalog; app.model_runtime = self.runtime
        label = app._task_profiles_label({"mode": "WORKFLOW", "bindings": {"N01": "TERRA_HIGH", "N03": "SOL_HIGH"}})
        self.assertEqual(label, "Terra / high → Sol / high")

    def test_14_new_task_workflow_uses_same_runtime_state(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter); app.profile_catalog = self.catalog; app.model_runtime = self.runtime
        choices = app._profile_choices()
        self.assertIn("Terra / high", choices)
        self.assertIn("Sonnet / high — unavailable", choices)
        self.assertEqual(choices[0], "Terra / high")
        self.assertEqual(app.get_runtime_profile_state("SONNET_HIGH")["state"], "UNAVAILABLE")

    def test_15_rendering_dropdowns_does_not_invoke_cli(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter); app.profile_catalog = self.catalog; app.model_runtime = self.runtime
        with mock.patch.object(cc.subprocess, "run", side_effect=AssertionError("CLI invoked during render")):
            self.assertIn("Terra / high", app._profile_choices())


if __name__ == "__main__":
    unittest.main()
