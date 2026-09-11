"""Small dependency-free UI helpers for AAW Control Center."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable


class ScrollablePage(ttk.Frame):
    """A page-scoped vertical scroller with mouse-wheel support."""

    def __init__(self, master: tk.Misc, *, background: str = "#f5f6f8") -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=background)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(28, 22, 32, 32), style="Page.TFrame")
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.content.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)

    def _sync_region(self, _event: tk.Event[Any] | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.window_id, width=max(1, event.width))

    def activate(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def deactivate(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event[Any]) -> str:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"


class CollapsibleSection(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str, *, open_by_default: bool = False) -> None:
        super().__init__(master, style="Card.TFrame", padding=1)
        self.expanded = tk.BooleanVar(value=open_by_default)
        self.button = ttk.Checkbutton(
            self, text=title, variable=self.expanded, command=self._toggle,
            style="Disclosure.Toolbutton",
        )
        self.button.grid(row=0, column=0, sticky="w", padx=14, pady=10)
        self.body = ttk.Frame(self, style="Card.TFrame", padding=(14, 0, 14, 14))
        self.body.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        if open_by_default:
            self.body.grid(row=1, column=0, sticky="ew")

    def _toggle(self) -> None:
        if self.expanded.get():
            self.body.grid(row=1, column=0, sticky="ew")
        else:
            self.body.grid_remove()


def load_ui_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def save_ui_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def section_header(master: tk.Misc, title: str, description: str = "") -> ttk.Frame:
    frame = ttk.Frame(master, style="Page.TFrame")
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text=title, style="Section.TLabel").grid(row=0, column=0, sticky="w")
    if description:
        ttk.Label(frame, text=description, style="Muted.TLabel", wraplength=780).grid(row=1, column=0, sticky="w", pady=(3, 0))
    return frame


def action_button(master: tk.Misc, text: str, command: Callable[[], None], *, primary: bool = False) -> ttk.Button:
    return ttk.Button(master, text=text, command=command, style="Primary.TButton" if primary else "TButton")
