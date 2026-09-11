"""Minimal dependency-free charts for the AAW Insights page.

Hand-drawn on a tk.Canvas so the Control Center keeps its stdlib-only guarantee.
Every comparative chart shows its sample size; small samples render an
explicit INSUFFICIENT EVIDENCE state instead of a misleading bar.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Sequence

_BG = "#ffffff"
_AXIS = "#c3cad4"
_GRID = "#edf0f4"
_TEXT = "#27313d"
_MUTED = "#657080"
_SERIES = ("#2f6f4f", "#3d6ea5", "#b4823a", "#8a4b6b", "#5a5f9c", "#7d7d7d")
_EMPTY_NOTE = "Brak danych dla bieżących filtrów"
_INSUFFICIENT = "INSUFFICIENT EVIDENCE"


class Chart(ttk.Frame):
    """A titled canvas with a fixed drawing area and a caption line."""

    def __init__(self, master: tk.Misc, title: str, *, height: int = 210) -> None:
        super().__init__(master, style="Card.TFrame", padding=12)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.caption_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.caption_var, style="CardMuted.TLabel",
                  wraplength=560, justify="left").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.canvas = tk.Canvas(self, height=height, background=_BG, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self._painter = None

    def set_caption(self, text: str) -> None:
        self.caption_var.set(text)

    def _clear(self) -> None:
        self.canvas.delete("all")

    def _note(self, text: str) -> None:
        self._clear()
        width = max(self.canvas.winfo_width(), 1)
        self.canvas.create_text(width / 2, self.canvas.winfo_reqheight() / 2,
                                text=text, fill=_MUTED, font=("Segoe UI", 10))

    def _redraw(self) -> None:
        if self._painter is not None:
            self._painter()

    # -- chart kinds -------------------------------------------------------

    def draw_bars(self, labels: Sequence[str], values: Sequence[float],
                  *, sample_sizes: Sequence[int] | None = None,
                  insufficient: Sequence[bool] | None = None,
                  value_suffix: str = "") -> None:
        labels = list(labels)
        values = [float(v) for v in values]
        sample_sizes = list(sample_sizes) if sample_sizes is not None else [None] * len(labels)
        insufficient = list(insufficient) if insufficient is not None else [False] * len(labels)

        def paint() -> None:
            self._clear()
            if not labels:
                self._note(_EMPTY_NOTE)
                return
            w = max(self.canvas.winfo_width(), 200)
            h = max(self.canvas.winfo_height(), 120)
            left, right, top, bottom = 46, 16, 14, 40
            plot_w, plot_h = w - left - right, h - top - bottom
            top_value = max([v for v, bad in zip(values, insufficient) if not bad] or [1.0]) or 1.0
            for frac in (0.0, 0.5, 1.0):
                y = top + plot_h * (1 - frac)
                self.canvas.create_line(left, y, w - right, y, fill=_GRID)
                self.canvas.create_text(left - 6, y, text=_fmt(top_value * frac), anchor="e",
                                        fill=_MUTED, font=("Segoe UI", 8))
            n = len(labels)
            slot = plot_w / n
            bar_w = min(46, slot * 0.6)
            for i, label in enumerate(labels):
                cx = left + slot * (i + 0.5)
                if insufficient[i]:
                    self.canvas.create_text(cx, top + plot_h / 2, text=_INSUFFICIENT,
                                            fill=_MUTED, font=("Segoe UI", 7), angle=90)
                else:
                    bh = plot_h * (values[i] / top_value) if top_value else 0
                    self.canvas.create_rectangle(cx - bar_w / 2, top + plot_h - bh,
                                                 cx + bar_w / 2, top + plot_h,
                                                 fill=_SERIES[0], outline="")
                    self.canvas.create_text(cx, top + plot_h - bh - 8,
                                            text=_fmt(values[i]) + value_suffix,
                                            fill=_TEXT, font=("Segoe UI", 8))
                cap = _short(label)
                if sample_sizes[i] is not None:
                    cap += f"\nN={sample_sizes[i]}"
                self.canvas.create_text(cx, h - bottom + 6, text=cap, anchor="n",
                                        fill=_MUTED, font=("Segoe UI", 8))
            self.canvas.create_line(left, top, left, top + plot_h, fill=_AXIS)
            self.canvas.create_line(left, top + plot_h, w - right, top + plot_h, fill=_AXIS)

        self._painter = paint
        paint()

    def draw_grouped_bars(self, groups: Sequence[str], series_names: Sequence[str],
                          matrix: Sequence[Sequence[float]],
                          *, sample_sizes: Sequence[int] | None = None) -> None:
        groups = list(groups)
        series_names = list(series_names)
        matrix = [list(map(float, row)) for row in matrix]
        sample_sizes = list(sample_sizes) if sample_sizes is not None else [None] * len(groups)

        def paint() -> None:
            self._clear()
            if not groups:
                self._note(_EMPTY_NOTE)
                return
            w = max(self.canvas.winfo_width(), 220)
            h = max(self.canvas.winfo_height(), 120)
            left, right, top, bottom = 40, 14, 26, 46
            plot_w, plot_h = w - left - right, h - top - bottom
            top_value = max((max(row) for row in matrix if row), default=1.0) or 1.0
            for frac in (0.0, 0.5, 1.0):
                y = top + plot_h * (1 - frac)
                self.canvas.create_line(left, y, w - right, y, fill=_GRID)
                self.canvas.create_text(left - 6, y, text=_fmt(top_value * frac), anchor="e",
                                        fill=_MUTED, font=("Segoe UI", 8))
            slot = plot_w / len(groups)
            k = max(len(series_names), 1)
            bar_w = min(18, (slot * 0.7) / k)
            for gi, group in enumerate(groups):
                base = left + slot * gi + (slot - bar_w * k) / 2
                for si in range(k):
                    value = matrix[gi][si] if si < len(matrix[gi]) else 0.0
                    bh = plot_h * (value / top_value) if top_value else 0
                    x0 = base + bar_w * si
                    self.canvas.create_rectangle(x0, top + plot_h - bh, x0 + bar_w - 2,
                                                 top + plot_h, fill=_SERIES[si % len(_SERIES)], outline="")
                cap = _short(group)
                if sample_sizes[gi] is not None:
                    cap += f"\nN={sample_sizes[gi]}"
                self.canvas.create_text(left + slot * (gi + 0.5), h - bottom + 6, text=cap,
                                        anchor="n", fill=_MUTED, font=("Segoe UI", 8))
            # legend
            for si, name in enumerate(series_names):
                lx = left + si * 96
                self.canvas.create_rectangle(lx, 6, lx + 10, 16,
                                             fill=_SERIES[si % len(_SERIES)], outline="")
                self.canvas.create_text(lx + 14, 11, text=name, anchor="w", fill=_MUTED,
                                        font=("Segoe UI", 8))
            self.canvas.create_line(left, top + plot_h, w - right, top + plot_h, fill=_AXIS)

        self._painter = paint
        paint()

    def draw_lines(self, x_labels: Sequence[str], series: dict[str, Sequence[float]]) -> None:
        x_labels = list(x_labels)
        series = {name: [float(v) for v in values] for name, values in series.items()}

        def paint() -> None:
            self._clear()
            if not x_labels or not series:
                self._note(_EMPTY_NOTE)
                return
            w = max(self.canvas.winfo_width(), 220)
            h = max(self.canvas.winfo_height(), 120)
            left, right, top, bottom = 48, 14, 22, 44
            plot_w, plot_h = w - left - right, h - top - bottom
            top_value = max((max(v) for v in series.values() if v), default=1.0) or 1.0
            for frac in (0.0, 0.5, 1.0):
                y = top + plot_h * (1 - frac)
                self.canvas.create_line(left, y, w - right, y, fill=_GRID)
                self.canvas.create_text(left - 6, y, text=_fmt(top_value * frac), anchor="e",
                                        fill=_MUTED, font=("Segoe UI", 8))
            n = len(x_labels)
            step = plot_w / max(n - 1, 1)
            for si, (name, values) in enumerate(series.items()):
                colour = _SERIES[si % len(_SERIES)]
                points = []
                for i, value in enumerate(values):
                    x = left + step * i
                    y = top + plot_h * (1 - (value / top_value if top_value else 0))
                    points.extend((x, y))
                    self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=colour, outline="")
                if len(points) >= 4:
                    self.canvas.create_line(*points, fill=colour, width=2, smooth=False)
                self.canvas.create_rectangle(left + si * 110, 4, left + si * 110 + 10, 14,
                                             fill=colour, outline="")
                self.canvas.create_text(left + si * 110 + 14, 9, text=name, anchor="w",
                                        fill=_MUTED, font=("Segoe UI", 8))
            for i, label in enumerate(x_labels):
                if n > 8 and i % (n // 8 + 1):
                    continue
                x = left + step * i
                self.canvas.create_text(x, h - bottom + 6, text=_short(label, 10), anchor="n",
                                        fill=_MUTED, font=("Segoe UI", 8))
            self.canvas.create_line(left, top, left, top + plot_h, fill=_AXIS)
            self.canvas.create_line(left, top + plot_h, w - right, top + plot_h, fill=_AXIS)

        self._painter = paint
        paint()

    def show_note(self, text: str) -> None:
        self._painter = lambda: self._note(text)
        self._note(text)


def _fmt(value: float) -> str:
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    if value and abs(value) < 10 and value != int(value):
        return f"{value:.1f}"
    return f"{int(round(value))}"


def _short(text: str, limit: int = 14) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
