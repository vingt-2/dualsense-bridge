"""The tray's windows (Tk), all on one thread of their own: the tray owns the main thread.

Tk must only be touched from that thread, so other threads hand it work with `call`.
"""
import ctypes
import queue
import threading
import tkinter as tk

# Dark surface and text tokens shared by the windows.
SURFACE = "#1a1a19"
PANEL = "#242423"
TEXT = "#f0efec"
TEXT_2 = "#c3c2b7"
MUTED = "#8a897f"
GRID = "#3a3a38"
ACCENT = "#3987e5"

root = None
scale = 1.0          # px per 96-dpi px
_queue = queue.Queue()
_start_lock = threading.Lock()
_started = False


def call(fn):
    """Run fn() on the UI thread (starting it if needed)."""
    global _started
    with _start_lock:
        if not _started:
            _started = True
            ready = threading.Event()
            threading.Thread(target=_run, args=(ready,), daemon=True).start()
            ready.wait()
    _queue.put(fn)


def _run(ready):
    global root, scale
    try:
        # Sharp text on scaled displays, for this thread's windows only.
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.withdraw()
    scale = root.winfo_fpixels("1i") / 96
    ready.set()

    def poll():
        while not _queue.empty():
            try:
                _queue.get_nowait()()
            except Exception as e:  # keep the UI thread alive whatever a window does
                print(f"[ui] {type(e).__name__}: {e}")
        root.after(50, poll)

    poll()
    root.mainloop()


def window(title, min_width=520):
    w = tk.Toplevel(root)
    w.title(f"DualSense Bridge – {title}")
    w.configure(bg=SURFACE)
    w.minsize(int(min_width * scale), 0)
    return w


def bring_to_front(w):
    w.deiconify()
    w.lift()
    w.focus_force()


def label(parent, text="", size=10, color=TEXT, bold=False, **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                    font=("Segoe UI", size, "bold" if bold else "normal"), **kw)


def button(parent, text, command, primary=False, **kw):
    options = dict(bg=ACCENT if primary else PANEL, fg=TEXT,
                   activebackground=GRID, activeforeground=TEXT, relief="flat",
                   font=("Segoe UI", 10), padx=int(14 * scale), pady=int(2 * scale),
                   cursor="hand2")
    options.update(kw)
    return tk.Button(parent, text=text, command=command, **options)


def checkbox(parent, text, variable, **kw):
    return tk.Checkbutton(parent, text=text, variable=variable, bg=parent["bg"], fg=TEXT,
                          selectcolor=PANEL, activebackground=parent["bg"],
                          activeforeground=TEXT, font=("Segoe UI", 10), anchor="w", **kw)
