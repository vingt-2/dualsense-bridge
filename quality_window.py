"""Connection quality window: live Bluetooth link stats, for finding a good spot for the
controller and the Bluetooth adapter.

Lives on the shared UI thread (see ui.py).
"""
import threading
import time
import tkinter as tk

import ui
from link_stats import DROPOUT, rating
from ui import GRID, MUTED, PANEL, SURFACE, TEXT, TEXT_2, label

CHART_SECONDS = 60
RECENT_SECONDS = 10

SERIES = ui.ACCENT
# Status colors are reserved for the rating, which always carries its label too.
STATUS = {"Excellent": ("#0ca30c", "●"), "Good": ("#0ca30c", "●"),
          "Fair": ("#fab219", "▲"), "Poor": ("#d03b3b", "■")}

TIPS = ("Tips: keep a clear line of sight between the controller and the Bluetooth adapter. "
        "A USB adapter works best on a short extension cable on your desk, away from the PC "
        "case and USB 3 ports, which interfere with 2.4 GHz. Move the Wi-Fi router away too, "
        "or use 5 GHz Wi-Fi. Press Reset after each change and give it 30 seconds.")

_window = None


def show(bt):
    """Open the window, or bring it to the front if it is already open."""
    ui.call(lambda: _show(bt))


def _show(bt):
    global _window
    if _window is not None and _window.root.winfo_exists():
        ui.bring_to_front(_window.root)
    else:
        _window = QualityWindow(bt)


def _fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"


class QualityWindow:
    def __init__(self, bt):
        self.bt = bt
        self._bars = []    # (x0, x1, text) for hover
        self._mouse_x = None
        self._build()
        self._tick()

    # -- layout ---------------------------------------------------------------
    def _build(self):
        self.root = root = ui.window("Connection quality")
        self.s = s = ui.scale
        pad = int(16 * s)

        top = tk.Frame(root, bg=SURFACE)
        top.pack(fill="x", padx=pad, pady=(pad, 0))
        self.badge = label(top, size=20, bold=True)
        self.badge.pack(side="left")
        self.badge_note = label(top, f"last {RECENT_SECONDS} seconds", color=MUTED)
        self.badge_note.pack(side="left", padx=(int(10 * s), 0), pady=(int(8 * s), 0))

        # stat tiles: last 10 s
        tiles = tk.Frame(root, bg=SURFACE)
        tiles.pack(fill="x", padx=pad, pady=(int(12 * s), 0))
        self.tiles = {}
        for i, (key, title) in enumerate([("rate", "Reports / s"),
                                          ("dropouts", f"Dropouts / min (> {DROPOUT * 1000:.0f} ms)"),
                                          ("worst", "Longest gap")]):
            f = tk.Frame(tiles, bg=PANEL, padx=int(12 * s), pady=int(8 * s))
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else int(8 * s), 0))
            tiles.columnconfigure(i, weight=1, uniform="tile")
            label(f, title, size=9, color=TEXT_2, anchor="w").pack(fill="x")
            self.tiles[key] = label(f, "–", size=18, bold=True, anchor="w")
            self.tiles[key].pack(fill="x")

        # chart
        label(root, f"Longest gap between reports, each second (ms, last {CHART_SECONDS} s)",
              color=TEXT_2, anchor="w").pack(fill="x", padx=pad, pady=(int(16 * s), int(4 * s)))
        self.chart = tk.Canvas(root, bg=SURFACE, height=int(180 * s), highlightthickness=0)
        self.chart.pack(fill="x", padx=pad)
        self.chart.bind("<Configure>", lambda e: self._draw_chart())
        self.chart.bind("<Motion>", lambda e: self._hover(e.x))
        self.chart.bind("<Leave>", lambda e: self._hover(None))

        # since reset
        since = tk.Frame(root, bg=SURFACE)
        since.pack(fill="x", padx=pad, pady=(int(12 * s), 0))
        self.since_text = label(since, color=TEXT_2, anchor="w", justify="left")
        self.since_text.pack(side="left", fill="x", expand=True)
        ui.button(since, "Reset", self.bt.link.reset).pack(side="right")

        label(root, TIPS, size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(12 * s), pad))
        root.bind("<Configure>", lambda e: e.widget is root and self._rewrap())
        ui.bring_to_front(root)

    def _rewrap(self):
        for w in self.root.pack_slaves():
            if isinstance(w, tk.Label) and w.cget("wraplength"):
                w.configure(wraplength=max(200, self.root.winfo_width() - int(32 * self.s)))

    # -- updates --------------------------------------------------------------
    def _tick(self):
        if not self.root.winfo_exists():
            return
        self.history, total, elapsed = self.bt.link.snapshot()
        connected = self.bt.link.connected
        recent = self.history[-RECENT_SECONDS:]

        r = rating(recent) if connected else None
        if r:
            color, glyph = STATUS[r]
            self.badge.configure(text=f"{glyph} {r}", fg=color)
        else:
            self.badge.configure(text="Controller not connected" if not connected else "Measuring…",
                                 fg=TEXT_2)

        live = [x for x in recent if x.reports]
        if connected and live:
            n = len(live)
            self.tiles["rate"].configure(text=f"{sum(x.reports for x in live) / n:.0f}")
            self.tiles["dropouts"].configure(text=f"{sum(x.dropouts for x in live) * 60 / n:.0f}")
            self.tiles["worst"].configure(text=f"{max(x.worst for x in live) * 1000:.0f} ms")
        else:
            for t in self.tiles.values():
                t.configure(text="–")

        if total.reports and elapsed >= 1:
            since_rating = rating(self.history)
            self.since_text.configure(text=(
                f"Since reset ({_fmt_duration(elapsed)}): {since_rating or '–'} · "
                f"{total.reports / elapsed:.0f} reports/s · "
                f"{total.dropouts * 60 / elapsed:.0f} dropouts/min · "
                f"longest gap {total.worst * 1000:.0f} ms · "
                f"{self.bt.link.disconnects} disconnect{'s' if self.bt.link.disconnects != 1 else ''}"))
        else:
            self.since_text.configure(text="Since reset: waiting for the controller…")

        self._draw_chart()
        self.root.after(500, self._tick)

    def _draw_chart(self):
        c, s = self.chart, self.s
        c.delete("all")
        self._bars = []
        w, h = c.winfo_width(), c.winfo_height()
        left, bottom, top = int(40 * s), int(20 * s), int(8 * s)
        plot_w, plot_h = w - left - int(4 * s), h - bottom - top
        if plot_w < 50:
            return
        seconds = getattr(self, "history", [])[-CHART_SECONDS:]
        buffer_ms = self.bt.haptics_depth   # ~ms of haptics the controller buffers

        worst = max([x.worst * 1000 for x in seconds] + [buffer_ms * 1.25, 20])
        y_max = next(v for v in (25, 50, 100, 200, 500, 1000, float("inf")) if v >= worst)
        y_max = min(y_max, 1000)
        y = lambda ms: top + plot_h - min(ms, y_max) / y_max * plot_h

        # recessive grid + axis labels
        for i in range(5):
            v = y_max * i / 4
            c.create_line(left, y(v), left + plot_w, y(v), fill=GRID)
            c.create_text(left - int(6 * s), y(v), text=f"{v:.0f}", anchor="e", fill=MUTED,
                          font=("Segoe UI", 8))
        c.create_text(left, top + plot_h + int(12 * s), text=f"{CHART_SECONDS} s ago",
                      anchor="w", fill=MUTED, font=("Segoe UI", 8))
        c.create_text(left + plot_w, top + plot_h + int(12 * s), text="now", anchor="e",
                      fill=MUTED, font=("Segoe UI", 8))

        slot = plot_w / CHART_SECONDS
        gap = max(1, int(2 * s))
        now = len(seconds)
        for i, sec in enumerate(seconds):
            x0 = left + (CHART_SECONDS - now + i) * slot
            x1 = x0 + slot - gap
            ago = now - i
            if sec.disconnected and not sec.reports:
                c.create_rectangle(x0, top, x1, top + plot_h, fill=GRID, outline="",
                                   stipple="gray50")
                self._bars.append((x0, x1 + gap, f"{ago} s ago: disconnected"))
                continue
            if not sec.reports:
                continue
            ms = sec.worst * 1000
            c.create_rectangle(x0, y(ms), x1, top + plot_h, fill=SERIES, outline="")
            self._bars.append((x0, x1 + gap, f"{ago} s ago: longest gap {ms:.0f} ms, "
                                             f"{sec.reports} reports, {sec.dropouts} dropouts"))

        # haptics buffer reference line, labelled directly
        yb = y(buffer_ms)
        c.create_line(left, yb, left + plot_w, yb, fill=TEXT_2, dash=(4, 3))
        c.create_text(left + int(4 * s), yb - int(3 * s), anchor="sw", fill=TEXT_2,
                      font=("Segoe UI", 8),
                      text=f"haptics buffer (~{buffer_ms} ms): longer gaps can make haptics stutter")
        self._hover(self._mouse_x)

    def _hover(self, mouse_x):
        c = self.chart
        c.delete("tip")
        self._mouse_x = mouse_x
        if mouse_x is None:
            return
        for x0, x1, text in self._bars:
            if x0 <= mouse_x < x1:
                tx = min(mouse_x + 12, c.winfo_width() - 4)
                t = c.create_text(tx, 4, text=text, anchor="ne" if tx > c.winfo_width() / 2 else "nw",
                                  fill=TEXT, font=("Segoe UI", 9), tags="tip")
                bx0, by0, bx1, by1 = c.bbox(t)
                c.create_rectangle(bx0 - 6, by0 - 3, bx1 + 6, by1 + 3, fill=PANEL, outline=GRID,
                                   tags="tip")
                c.create_line(mouse_x, 0, mouse_x, c.winfo_height(), fill=MUTED, tags="tip")
                c.tag_raise(t)
                return


if __name__ == "__main__":
    # Preview with simulated data:  python quality_window.py
    import random
    from link_stats import LinkStats

    class FakeBt:
        haptics_depth = 0x20
        link = LinkStats()

    def feed():
        FakeBt.link.connection(True)
        while True:
            time.sleep(random.choice([0.001] * 200 + [0.015, 0.03, 0.06]))
            FakeBt.link.input_report()

    threading.Thread(target=feed, daemon=True).start()
    show(FakeBt)
    while ui.root is None or _window is None or _window.root.winfo_exists():
        time.sleep(0.5)
