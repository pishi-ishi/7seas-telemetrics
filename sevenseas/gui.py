"""7seas-telemetrics desktop GUI (tkinter, dark single-window editor).

Layout: left control panel (video / data / sync+trim / gauges+maneuvers /
export), center live preview canvas with a maneuver-marked timeline.
Gauges are dragged directly on the preview; right-click for size/stretch.
All ffmpeg work happens in worker threads; results arrive via a queue.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import APP_NAME, __version__, gauges, project, telemetry, videoio

# palette (DESIGN.md)
BG = "#000000"
PANEL = "#121212"
PANEL2 = "#1C1C1C"
PANEL3 = "#242424"
TEXT = "#FAFAFA"
MUTED = "#A7A7A7"
DIM = "#6F6F6F"
ACCENT = "#DC143C"
ACCENT_HOVER = "#F04060"
OK = "#22C55E"
WARN = "#F5B544"
ERR = "#EF4444"

F_UI = ("Segoe UI", 9)
F_UI_B = ("Segoe UI Semibold", 9)
F_SECTION = ("Segoe UI Semibold", 8)
F_MONO = ("Consolas", 9)
F_MONO_B = ("Consolas", 10, "bold")

ENCODER_NAMES = {
    "h264_qsv": "Intel QuickSync",
    "h264_mf": "MediaFoundation (HW)",
    "libx264": "Software x264",
}
VIDEO_TYPES = [("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv *.mts *.m2ts *.wmv"),
               ("All files", "*.*")]
DATA_TYPES = [("Telemetry (GPX / CSV / VKX)", "*.gpx *.csv *.vkx"),
              ("All files", "*.*")]
PROJECT_TYPES = [("7seas project", "*.7seas.json"), ("All files", "*.*")]


def _btn(parent, text, cmd, accent=False, **kw):
    b = tk.Button(parent, text=text, command=cmd, bd=0, relief="flat",
                  font=F_UI_B if accent else F_UI, cursor="hand2",
                  bg=ACCENT if accent else PANEL2,
                  fg=TEXT, activeforeground=TEXT,
                  activebackground=ACCENT_HOVER if accent else PANEL3,
                  disabledforeground=DIM, padx=10, pady=4, **kw)
    return b


def _scroll_area(parent, **kw):
    """Vertically scrollable region. Returns (canvas, inner frame): pack the
    content into the inner frame, scroll the canvas."""
    canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0, bd=0, **kw)
    sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview,
                       style="Dark.Vertical.TScrollbar")
    canvas.configure(yscrollcommand=sb.set)
    # scrollbar first: the canvas's requested width can exceed a fixed-width
    # parent, and then the packer has nothing left to give the scrollbar
    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=PANEL)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")

    def fit(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(win, width=canvas.winfo_width())
    inner.bind("<Configure>", fit)
    canvas.bind("<Configure>", fit)
    return canvas, inner


def _bind_wheel(widget, canvas):
    """Scroll `canvas` when the wheel turns over `widget` or its children.
    Bound per widget (not bind_all) so the preview keeps its own wheel-zoom."""
    def on_wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"
    stack = [widget]
    while stack:
        w = stack.pop()
        w.bind("<MouseWheel>", on_wheel)
        stack.extend(w.winfo_children())


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} {__version__}")
        root.configure(bg=BG)
        root.geometry("1340x860")
        root.minsize(1080, 720)
        self._set_icon()

        # ---- state ----
        self.video_path = None
        self.vinfo = None
        self.data_path = None
        self.tele_full = None        # complete parsed telemetry
        self.tele = None             # trimmed view used everywhere
        self.trim = [None, None]     # absolute epoch [in, out] or None
        self.maneuvers = []          # shared list, rendered by TrackMap
        self.t_rng = [65.0, 120.0]   # tack magnitude range (deg)
        self.g_rng = [15.0, 60.0]    # gybe magnitude range (deg)
        self.gauges = []
        self.user_off = 0.0          # seconds into (trimmed) data at video t=0
        self.cur_t = 0.0
        self.frame_t = 0.0
        self.preview_frame = None
        self.placeholder = None
        self.encoder = None
        self.quality = videoio.QUALITY_DEFAULT
        self.overlay_fps = videoio.get_quality(self.quality).overlay_fps
        self.exporting = False
        self.cancel_evt = threading.Event()
        self._pending_project = None
        self._drag = None
        self._preview_job = None
        self._preview_busy = False
        self._preview_want = None
        self._photo = None
        self._geom = None
        self.playing = False
        self._play_proc = None
        self._play_gen = 0
        self.q = queue.Queue()

        self._build_ui()
        self._update_quality_ui()
        self._draw_placeholder_text()
        threading.Thread(target=self._detect_encoder, daemon=True).start()
        root.after(80, self._poll)

    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(__file__)))
            ico = os.path.join(base, "icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    # ================= UI construction =================
    def _section(self, parent, title):
        lbl = tk.Label(parent, text=title.upper(), font=F_SECTION, bg=PANEL,
                       fg=DIM, anchor="w")
        lbl.pack(fill="x", padx=12, pady=(10, 2))
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=12, pady=(0, 2))
        return f

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=PANEL3,
                        background=ACCENT, bordercolor=PANEL, lightcolor=ACCENT,
                        darkcolor=ACCENT)
        style.configure("Dark.Vertical.TScrollbar", troughcolor=PANEL2,
                        background=DIM, bordercolor=PANEL, arrowcolor=MUTED,
                        lightcolor=DIM, darkcolor=DIM)
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", MUTED)])

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        # left panel scrolls: on short screens the export controls would
        # otherwise sit below the window edge with no way to reach them
        self.left = tk.Frame(outer, bg=PANEL, width=320)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)
        self.lscroll, pane = _scroll_area(self.left)

        center = tk.Frame(outer, bg=BG)
        center.pack(side="left", fill="both", expand=True, padx=(8, 0))

        # ---- left panel ----
        head = tk.Frame(pane, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(12, 0))
        tk.Label(head, text="7SEAS", font=("Segoe UI Semibold", 12), bg=PANEL,
                 fg=TEXT).pack(side="left")
        tk.Label(head, text="TELEMETRICS", font=("Segoe UI", 12), bg=PANEL,
                 fg=ACCENT).pack(side="left", padx=(4, 0))

        f = self._section(pane, "Video")
        _btn(f, "Open video…", self.open_video).pack(fill="x")
        self.lbl_video = tk.Label(f, text="no video loaded", font=F_MONO,
                                  bg=PANEL, fg=DIM, anchor="w", justify="left",
                                  wraplength=270)
        self.lbl_video.pack(fill="x", pady=(3, 0))

        f = self._section(pane, "Telemetry data")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x")
        _btn(row, "Open GPX / CSV / VKX…", self.open_data).pack(
            side="left", fill="x", expand=True)
        self.btn_map = _btn(row, "Map…", self.open_mapper)
        self.btn_map.pack(side="left", padx=(6, 0))
        self.btn_map.configure(state="disabled")
        self.lbl_data = tk.Label(f, text="no data loaded", font=F_MONO, bg=PANEL,
                                 fg=DIM, anchor="w", justify="left", wraplength=270)
        self.lbl_data.pack(fill="x", pady=(3, 0))

        f = self._section(pane, "Sync · offset (s) · trim")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x")
        self.var_off = tk.StringVar(value="0.0")
        e = tk.Entry(row, textvariable=self.var_off, width=10, bg=PANEL2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=F_MONO,
                     justify="center")
        e.pack(side="left", ipady=3)
        e.bind("<Return>", lambda _e: self._offset_from_entry())
        e.bind("<FocusOut>", lambda _e: self._offset_from_entry())
        _btn(row, "Align starts", self.sync_align).pack(side="left", padx=(6, 0))
        _btn(row, "From metadata", self.sync_meta).pack(side="left", padx=(6, 0))
        grid = tk.Frame(f, bg=PANEL)
        grid.pack(fill="x", pady=(5, 0))
        for r, deltas in enumerate([(-3600, -60, -10, -1, -0.1),
                                    (3600, 60, 10, 1, 0.1)]):
            for c, d in enumerate(deltas):
                txt = ("-1h" if d == -3600 else "+1h" if d == 3600
                       else f"{d:+g}")
                b = _btn(grid, txt, lambda dd=d: self.nudge_offset(dd))
                b.configure(padx=2, pady=2, font=("Consolas", 8), width=5)
                b.grid(row=r, column=c, padx=2, pady=1, sticky="ew")
            grid.columnconfigure(c, weight=1)
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(5, 0))
        _btn(row, "IN = cursor", self.trim_in).pack(side="left")
        _btn(row, "OUT = cursor", self.trim_out).pack(side="left", padx=(6, 0))
        _btn(row, "Clear trim", self.trim_clear).pack(side="left", padx=(6, 0))
        self.lbl_clock = tk.Label(f, text="", font=F_MONO, bg=PANEL, fg=DIM,
                                  anchor="w", justify="left", wraplength=270)
        self.lbl_clock.pack(fill="x", pady=(4, 0))

        f = self._section(pane, "Gauges · drag on preview")
        self.gauge_box = tk.Frame(f, bg=PANEL)
        self.gauge_box.pack(fill="x")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(4, 0))
        _btn(row, "Add readout…", self.add_readout).pack(side="left")
        _btn(row, "Reset layout", self.reset_layout).pack(side="left", padx=(6, 0))
        f = self._section(pane, "Maneuvers · tacks & gybes")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x")
        _btn(row, "T @ cursor", lambda: self.man_add("T")).pack(side="left")
        _btn(row, "G @ cursor", lambda: self.man_add("G")).pack(
            side="left", padx=(6, 0))
        _btn(row, "Edit…", self.open_maneuvers).pack(side="left", padx=(6, 0))
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(4, 0))
        _btn(row, "Auto-detect", self.man_autodetect).pack(side="left")
        self.lbl_man = tk.Label(f, text="", font=F_MONO, bg=PANEL, fg=DIM,
                                anchor="w")
        self.lbl_man.pack(fill="x", pady=(3, 0))

        f = self._section(pane, "Export")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x")
        self.var_out = tk.StringVar()
        tk.Entry(row, textvariable=self.var_out, bg=PANEL2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=F_MONO).pack(
            side="left", fill="x", expand=True, ipady=3)
        _btn(row, "…", self.browse_out).pack(side="left", padx=(6, 0))
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(5, 0))
        tk.Label(row, text="QUALITY", font=F_SECTION, bg=PANEL, fg=DIM).pack(
            side="left", padx=(0, 6))
        self.btn_quality = {}
        for key in ("high", "medium", "low"):
            b = _btn(row, videoio.QUALITY_PRESETS[key].label,
                     lambda k=key: self.set_quality(k))
            b.pack(side="left", fill="x", expand=True, padx=(0, 4))
            self.btn_quality[key] = b
        self.var_audio = tk.BooleanVar(value=True)
        self.chk_audio = tk.Checkbutton(
            f, text="Include audio", variable=self.var_audio,
            command=self._on_audio_toggle, bg=PANEL, fg=TEXT,
            activebackground=PANEL, activeforeground=TEXT, selectcolor=PANEL2,
            font=F_UI, anchor="w", cursor="hand2", disabledforeground=DIM)
        self.chk_audio.pack(fill="x", pady=(4, 0))
        self.lbl_quality = tk.Label(f, text="", font=F_MONO, bg=PANEL, fg=DIM,
                                    anchor="w")
        self.lbl_quality.pack(fill="x", pady=(3, 0))
        self.lbl_enc = tk.Label(f, text="encoder: detecting…", font=F_MONO,
                                bg=PANEL, fg=DIM, anchor="w")
        self.lbl_enc.pack(fill="x", pady=(1, 0))
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(5, 0))
        self.btn_render = _btn(row, "RENDER VIDEO", self.start_export, accent=True)
        self.btn_render.pack(side="left", fill="x", expand=True)
        self.btn_cancel = _btn(row, "Cancel", self.cancel_export)
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.btn_cancel.configure(state="disabled")
        self.pbar = ttk.Progressbar(f, style="Accent.Horizontal.TProgressbar",
                                    maximum=1000)
        self.pbar.pack(fill="x", pady=(5, 0))
        self.lbl_status = tk.Label(f, text="", font=F_MONO, bg=PANEL, fg=DIM,
                                   anchor="w", justify="left", wraplength=270)
        self.lbl_status.pack(fill="x", pady=(3, 0))
        self.btn_folder = _btn(f, "Open output folder", self.open_out_folder)

        f = self._section(pane, "Project")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(0, 10))
        _btn(row, "Save project…", self.save_project).pack(side="left")
        _btn(row, "Load project…", self.load_project).pack(side="left",
                                                                padx=(6, 0))
        _bind_wheel(self.lscroll, self.lscroll)

        # ---- center: preview canvas + maneuver marks + scrub bar ----
        self.canvas = tk.Canvas(center, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._refresh_overlay())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Button-3>", self._on_context)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.marks = tk.Canvas(center, bg=PANEL, height=16, highlightthickness=0,
                               cursor="hand2")
        self.marks.pack(fill="x", pady=(8, 0))
        self.marks.bind("<Button-1>", self._on_mark_click)
        self.marks.bind("<Configure>", lambda _e: self._redraw_marks())

        bar = tk.Frame(center, bg=PANEL)
        bar.pack(fill="x")
        self.btn_play = _btn(bar, "▶", self.play_toggle)
        self.btn_play.configure(width=3, font=("Segoe UI", 10))
        self.btn_play.pack(side="left", padx=(8, 0), pady=6)
        self.lbl_time = tk.Label(bar, text="00:00.0", font=F_MONO_B, bg=PANEL,
                                 fg=TEXT, width=9)
        self.lbl_time.pack(side="left", padx=(6, 0))
        self._scale_guard = False
        self.scale = tk.Scale(bar, from_=0, to=100, orient="horizontal",
                              resolution=0.05, showvalue=0, bg=PANEL,
                              troughcolor=PANEL3, activebackground=ACCENT,
                              highlightthickness=0, sliderrelief="flat",
                              bd=0, command=self._on_scrub, cursor="hand2")
        self.scale.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        self.lbl_dur = tk.Label(bar, text="--:--", font=F_MONO, bg=PANEL,
                                fg=DIM, width=8)
        self.lbl_dur.pack(side="left", padx=(0, 10))

    # ================= helpers =================
    @property
    def offset(self):
        """Data epoch seconds at video t=0 (relative to trimmed view)."""
        return (self.tele.t_start + self.user_off) if self.tele else 0.0

    def _fmt_t(self, t):
        m, s = divmod(max(0.0, t), 60.0)
        return f"{int(m):02d}:{s:04.1f}"

    def status(self, msg, color=DIM):
        self.lbl_status.configure(text=msg, fg=color)

    # ================= file loading =================
    def open_video(self):
        p = filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if p:
            self._load_video(p)

    def _load_video(self, p):
        self.play_stop()
        self.lbl_video.configure(text="probing…", fg=MUTED)

        def work():
            try:
                info = videoio.probe(p)
                self.q.put(("video", p, info))
            except Exception as e:
                self.q.put(("error", f"Could not open video:\n{e}"))
        threading.Thread(target=work, daemon=True).start()

    def open_data(self):
        p = filedialog.askopenfilename(title="Open telemetry (GPX / CSV / VKX)",
                                       filetypes=DATA_TYPES)
        if p:
            self._load_data(p)

    def _load_data(self, p, mapping=None, units=None):
        self.lbl_data.configure(text="parsing…", fg=MUTED)

        def work():
            try:
                tele = telemetry.load(p, mapping=mapping, speed_units=units)
                self.q.put(("tele", p, tele))
            except Exception as e:
                self.q.put(("error", f"Could not parse telemetry:\n{e}"))
        threading.Thread(target=work, daemon=True).start()

    def _detect_encoder(self):
        self.q.put(("encoder", videoio.pick_encoder()))

    # ================= queue pump =================
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "video":
                    self._on_video_loaded(msg[1], msg[2])
                elif kind == "tele":
                    self._on_tele_loaded(msg[1], msg[2])
                elif kind == "frame":
                    self._on_frame(msg[1], msg[2])
                elif kind == "pframe":
                    self._on_play_frame(msg[1], msg[2], msg[3])
                elif kind == "pdone":
                    if msg[1] == self._play_gen:
                        self.play_stop()
                elif kind == "map_ready":
                    self._refresh_overlay()
                elif kind == "status":
                    self.status(msg[1], MUTED)
                elif kind == "encoder":
                    self.encoder = msg[1]
                    self.lbl_enc.configure(
                        text=f"encoder: {ENCODER_NAMES.get(msg[1], msg[1])}")
                elif kind == "progress":
                    self._on_progress(*msg[1:])
                elif kind == "export_done":
                    self._on_export_done(*msg[1:])
                elif kind == "export_err":
                    self._on_export_err(msg[1])
                elif kind == "export_cancelled":
                    self.exporting = False
                    self._export_buttons(False)
                    self.status("export cancelled", WARN)
                elif kind == "error":
                    self.status("error", ERR)
                    self.lbl_video.configure(fg=DIM)
                    self.lbl_data.configure(fg=DIM)
                    messagebox.showerror(APP_NAME, msg[1])
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _on_video_loaded(self, path, info):
        self.video_path = path
        self.vinfo = info
        base = os.path.basename(path)
        ct = ("" if not info.creation_time else
              "\nstart " + datetime.fromtimestamp(
                  info.creation_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"))
        self.lbl_video.configure(
            text=f"{base}\n{info.width}x{info.height} @ {info.fps:.4g} fps  "
                 f"{self._fmt_t(info.duration)}{ct}", fg=MUTED)
        self._scale_guard = True
        self.scale.configure(to=max(0.1, info.duration))
        self._scale_guard = False
        self.lbl_dur.configure(text=self._fmt_t(info.duration))
        self._update_quality_ui()
        if not self.var_out.get():
            self.var_out.set(videoio.default_output_path(path))
        if not self._pending_project and self.tele and self.vinfo.creation_time:
            self.sync_meta(quiet=True)
        self._redraw_marks()
        self.request_preview(self.cur_t, force=True)

    def _on_tele_loaded(self, path, tele):
        remap = (path == self.data_path)   # re-parse via the mapper dialog
        self.data_path = path
        self.tele_full = tele
        pend = self._pending_project
        if pend:
            self._pending_project = None
            self.trim = list(pend.get("trim") or [None, None])
            self.maneuvers = list(pend.get("maneuvers") or [])
            self.gauges = pend.get("gauge_objects") or gauges.default_gauges(tele)
            self.user_off = float(pend.get("user_offset", 0.0))
            self.set_quality(pend.get("quality") or self.quality)
            self.var_audio.set(bool(pend.get("audio", True)))
            self._update_quality_ui()
            self._apply_view()
        else:
            if not remap:
                self.trim = [None, None]
                # the maneuver list stays empty and is the user's to fill —
                # by hand at the cursor, or with Auto-detect once the angle
                # windows suit the boat
                self.maneuvers = []
            self._apply_view()
            if not self.gauges:
                self.gauges = gauges.default_gauges(self.tele)
            if self.vinfo and self.vinfo.creation_time and not remap:
                self.sync_meta(quiet=True)
        n = len(tele.streams)
        rng = tele.t_end - tele.t_start
        self.lbl_data.configure(
            text=f"{os.path.basename(path)}  [{tele.kind.upper()}]\n"
                 f"{n} streams · {self._fmt_t(rng)} · "
                 + ("track ✓" if tele.track else "no lat/lon"), fg=MUTED)
        self.btn_map.configure(
            state="normal" if tele.kind == "csv" else "disabled")
        self.var_off.set(f"{self.user_off:.1f}")
        self._rebuild_gauge_list()
        self._sync_ui()

    # ================= trimmed view =================
    def _apply_view(self, keep_abs=None):
        """Rebuild the trimmed view; keep_abs preserves absolute sync."""
        if not self.tele_full:
            return
        t = self.tele_full
        a, b = self.trim
        if a is not None or b is not None:
            tv = t.trimmed(a, b)
            if tv.streams or tv.track:
                t = tv
        t.maneuvers = self.maneuvers
        self.tele = t
        if keep_abs is not None:
            self.user_off = keep_abs - t.t_start
            self.var_off.set(f"{self.user_off:.1f}")

    def _sync_ui(self):
        self._update_clock()
        self._update_man_label()
        self._redraw_marks()
        self._refresh_overlay()

    def trim_in(self):
        if not self.tele:
            return
        cut = self.offset + self.frame_t
        if self.trim[1] is not None and cut >= self.trim[1]:
            messagebox.showinfo(APP_NAME, "IN point must be before OUT point.")
            return
        keep = self.offset
        self.trim[0] = cut
        self._apply_view(keep_abs=keep)
        self._sync_ui()

    def trim_out(self):
        if not self.tele:
            return
        cut = self.offset + self.frame_t
        if self.trim[0] is not None and cut <= self.trim[0]:
            messagebox.showinfo(APP_NAME, "OUT point must be after IN point.")
            return
        keep = self.offset
        self.trim[1] = cut
        self._apply_view(keep_abs=keep)
        self._sync_ui()

    def trim_clear(self):
        if not self.tele_full:
            return
        keep = self.offset
        self.trim = [None, None]
        self._apply_view(keep_abs=keep)
        self._sync_ui()

    # ================= preview =================
    def request_preview(self, t, force=False):
        self.cur_t = t
        self.lbl_time.configure(text=self._fmt_t(t))
        self._update_clock()
        if not self.video_path:
            self._refresh_overlay()
            return
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        delay = 10 if force else 160
        self._preview_job = self.root.after(delay, self._start_extract)

    def _start_extract(self):
        self._preview_job = None
        if self._preview_busy:
            self._preview_want = self.cur_t
            return
        self._preview_busy = True
        t = self.cur_t

        def work():
            img = videoio.extract_frame(self.video_path, t, max_w=1280)
            self.q.put(("frame", t, img))
        threading.Thread(target=work, daemon=True).start()

    def _on_frame(self, t, img):
        self._preview_busy = False
        if self.playing:
            return
        if img is not None:
            self.preview_frame = img
            self.frame_t = t
            self._refresh_overlay()
        if self._preview_want is not None and self._preview_want != t:
            self._preview_want = None
            self._start_extract()

    def _base_image(self):
        if self.preview_frame is not None:
            return self.preview_frame
        if self.placeholder is None:
            self.placeholder = Image.new("RGB", (1280, 720), (8, 8, 8))
        return self.placeholder

    def _refresh_overlay(self):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 40 or ch < 40:
            return
        if not self.video_path and not self.tele:
            self._draw_placeholder_text()
            return
        base = self._base_image().convert("RGBA")
        pw, ph = base.size
        if self.tele and self.gauges:
            ov = gauges.compose(pw, ph, self.gauges, self.tele,
                                self.offset + self.frame_t)
            base.alpha_composite(ov)
        scale = min(cw / pw, ch / ph)
        dw, dh = max(1, int(pw * scale)), max(1, int(ph * scale))
        disp = base.convert("RGB").resize((dw, dh), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(disp)
        offx, offy = (cw - dw) // 2, (ch - dh) // 2
        self._geom = (offx, offy, scale, pw, ph)
        self.canvas.delete("all")
        self.canvas.create_image(offx, offy, image=self._photo, anchor="nw")
        if self.preview_frame is None:
            self.canvas.create_text(
                cw // 2, offy + 24, fill=DIM, font=F_UI,
                text="layout preview — open a video to see footage")

    def _draw_placeholder_text(self):
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 300)
        lines = [
            ("7SEAS-TELEMETRICS", ("Segoe UI Semibold", 16), TEXT),
            ("", F_UI, DIM),
            ("1   Open a video file", F_UI, MUTED),
            ("2   Open a GPX, CSV, or Vakaros VKX log", F_UI, MUTED),
            ("3   Sync offset · trim data · drag gauges", F_UI, MUTED),
            ("4   Play to check sync, then Render", F_UI, MUTED),
        ]
        y = ch // 2 - 60
        for txt, fnt, col in lines:
            self.canvas.create_text(cw // 2, y, text=txt, font=fnt, fill=col)
            y += 26

    def _on_scrub(self, val):
        if self._scale_guard:
            return
        v = float(val)
        # tk.Scale fires its command at idle time even for programmatic
        # set() calls — ignore echoes of the position we just set ourselves
        if abs(v - self.cur_t) < 0.06:
            return
        if self.playing:
            self.play_stop()
        self.request_preview(v)

    # ================= play preview =================
    def play_toggle(self):
        if self.playing:
            self.play_stop()
        else:
            self.play_start()

    def play_start(self):
        if not (self.video_path and self.vinfo) or self.playing:
            return
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
            self._preview_job = None
        t0 = min(self.cur_t, max(0.0, self.vinfo.duration - 0.3))
        sw, sh = videoio.play_dims(self.vinfo, max_w=960)
        try:
            proc = videoio.open_play_stream(self.video_path, t0, sw, sh)
        except OSError as e:
            self.status(f"play failed: {e}", ERR)
            return
        self.playing = True
        self._play_proc = proc
        self._play_gen += 1
        gen = self._play_gen
        self.btn_play.configure(text="⏸")
        fsz = sw * sh * 3

        def reader():
            n = 0
            while True:
                try:
                    buf = proc.stdout.read(fsz)
                except (OSError, ValueError):
                    break
                if not buf or len(buf) < fsz or gen != self._play_gen:
                    break
                img = Image.frombytes("RGB", (sw, sh), buf)
                self.q.put(("pframe", gen, t0 + n / videoio.PLAY_FPS, img))
                n += 1
            self.q.put(("pdone", gen))
        threading.Thread(target=reader, daemon=True).start()

    def play_stop(self):
        if not self.playing and self._play_proc is None:
            return
        self.playing = False
        self._play_gen += 1
        self.btn_play.configure(text="▶")
        if self._play_proc is not None:
            try:
                self._play_proc.kill()
            except OSError:
                pass
            self._play_proc = None

    def _on_play_frame(self, gen, t, img):
        if not self.playing or gen != self._play_gen:
            return
        if t > (self.vinfo.duration if self.vinfo else 1e12):
            self.play_stop()
            return
        self.preview_frame = img
        self.frame_t = t
        self.cur_t = t
        self._scale_guard = True
        self.scale.set(t)
        self._scale_guard = False
        self.lbl_time.configure(text=self._fmt_t(t))
        self._update_clock()
        self._refresh_overlay()

    # ================= sync =================
    def _offset_from_entry(self):
        try:
            self.user_off = float(self.var_off.get().replace(",", "."))
        except ValueError:
            self.var_off.set(f"{self.user_off:.1f}")
            return
        self._sync_ui()

    def nudge_offset(self, d):
        self.user_off += d
        self.var_off.set(f"{self.user_off:.1f}")
        self._sync_ui()

    def sync_align(self):
        self.user_off = 0.0
        self.var_off.set("0.0")
        self._sync_ui()

    def sync_meta(self, quiet=False):
        if not (self.tele and self.vinfo):
            if not quiet:
                messagebox.showinfo(APP_NAME, "Load a video and data first.")
            return
        if self.vinfo.creation_time is None:
            if not quiet:
                messagebox.showinfo(
                    APP_NAME, "This video has no creation-time metadata.\n"
                    "Sync manually with the offset buttons.")
            return
        self.user_off = self.vinfo.creation_time - self.tele.t_start
        self.var_off.set(f"{self.user_off:.1f}")
        self._sync_ui()

    def _update_clock(self):
        if not self.tele:
            self.lbl_clock.configure(text="")
            return
        t_data = self.offset + self.frame_t
        clock = datetime.fromtimestamp(t_data, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ")
        inside = self.tele.t_start - 3 <= t_data <= self.tele.t_end + 3
        parts = [f"data @ cursor: {clock}"]
        if self.vinfo:
            a = max(0.0, self.tele.t_start - self.offset)
            b = min(self.vinfo.duration, self.tele.t_end - self.offset)
            parts.append(f"data covers video {self._fmt_t(a)}–{self._fmt_t(b)}"
                         if b > a else "data does not overlap video!")
        if (self.trim[0] is not None or self.trim[1] is not None) and self.tele_full:
            f0 = self.tele_full.t_start
            a = "start" if self.trim[0] is None else self._fmt_t(self.trim[0] - f0)
            b = "end" if self.trim[1] is None else self._fmt_t(self.trim[1] - f0)
            parts.append(f"trim: {a} → {b} of data")
        self.lbl_clock.configure(text="\n".join(parts),
                                 fg=MUTED if inside else WARN)

    # ================= maneuvers =================
    def _update_man_label(self):
        n_t = sum(1 for m in self.maneuvers
                  if m["kind"] == "T" and m.get("enabled", True))
        n_g = sum(1 for m in self.maneuvers
                  if m["kind"] == "G" and m.get("enabled", True))
        self.lbl_man.configure(text=f"{n_t} tacks · {n_g} gybes"
                               if self.maneuvers else "")

    def _redraw_marks(self):
        self.marks.delete("all")
        w = self.marks.winfo_width()
        if w < 40 or not (self.vinfo and self.tele):
            return
        dur = self.vinfo.duration
        pad = 58  # roughly aligns with the scale's start
        span = max(1, w - pad - 60)
        for m, lbl in telemetry.numbered_maneuvers(self.maneuvers):
            tv = m["t"] - self.offset
            if not (0 <= tv <= dur):
                continue
            x = pad + tv / dur * span
            col = ACCENT if m["kind"] == "T" else OK
            self.marks.create_rectangle(x - 1.5, 3, x + 1.5, 13, fill=col,
                                        outline="")
            self.marks.create_text(x + 4, 8, text=lbl, anchor="w",
                                   font=("Consolas", 7), fill=MUTED)

    def _on_mark_click(self, e):
        if not self.vinfo:
            return
        w = self.marks.winfo_width()
        pad = 58
        span = max(1, w - pad - 60)
        tv = (e.x - pad) / span * self.vinfo.duration
        tv = max(0.0, min(self.vinfo.duration, tv))
        if self.playing:
            self.play_stop()
        self._scale_guard = True
        self.scale.set(tv)
        self._scale_guard = False
        self.request_preview(tv, force=True)

    def _maneuvers_changed(self):
        if self.tele:
            self.tele.maneuvers = self.maneuvers
        self._update_man_label()
        self._redraw_marks()
        self._refresh_overlay()

    def _set_maneuvers(self, mans):
        self.maneuvers = mans
        self._maneuvers_changed()

    def _confirm_replace(self, parent=None):
        """Auto-detect overwrites the list — ask first if it has entries."""
        if not self.maneuvers:
            return True
        return messagebox.askyesno(
            APP_NAME, f"Replace the {len(self.maneuvers)} maneuvers in the "
            "list with auto-detected ones?", parent=parent or self.root)

    def man_autodetect(self):
        if not self.tele:
            messagebox.showinfo(APP_NAME, "Load telemetry data first.")
            return
        if not self._confirm_replace():
            return
        found = telemetry.detect_maneuvers(self.tele, tuple(self.t_rng),
                                           tuple(self.g_rng))
        self._set_maneuvers(found)
        if not found:
            self.status("no maneuvers found — adjust ranges in Edit…", WARN)

    def man_add(self, kind):
        if not self.tele:
            messagebox.showinfo(APP_NAME, "Load telemetry data first.")
            return
        self.maneuvers.append({"t": self.offset + self.frame_t, "kind": kind,
                               "mag": 0.0, "enabled": True})
        self._maneuvers_changed()

    def _map_ready(self):
        """Called from a tile-fetch thread when a mosaic becomes available."""
        self.q.put(("map_ready",))

    def open_maneuvers(self):
        if not self.tele:
            messagebox.showinfo(APP_NAME, "Load telemetry data first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Maneuvers — tacks & gybes")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 380,
                                 self.root.winfo_rooty() + 120))

        top = tk.Frame(dlg, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(12, 4))
        vars_ = {}
        for i, (name, val) in enumerate((("T min", self.t_rng[0]),
                                         ("T max", self.t_rng[1]),
                                         ("G min", self.g_rng[0]),
                                         ("G max", self.g_rng[1]))):
            tk.Label(top, text=name + "°", bg=PANEL, fg=MUTED,
                     font=F_UI).grid(row=0, column=2 * i, padx=(0, 3))
            v = tk.StringVar(value=f"{val:g}")
            tk.Entry(top, textvariable=v, width=5, bg=PANEL2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=F_MONO,
                     justify="center").grid(row=0, column=2 * i + 1,
                                            padx=(0, 8))
            vars_[name] = v

        # the list scrolls so the buttons below stay reachable no matter how
        # many maneuvers are in it
        listwrap = tk.Frame(dlg, bg=PANEL)
        listwrap.pack(fill="both", expand=True, padx=14, pady=4)
        lscroll, listf = _scroll_area(listwrap, height=250, width=330)
        hint = tk.Label(dlg, text="", font=F_MONO, bg=PANEL, fg=DIM,
                        anchor="w", justify="left")
        hint.pack(fill="x", padx=14)

        def rebuild_rows():
            for wdg in listf.winfo_children():
                wdg.destroy()
            hint.configure(
                text="" if self.maneuvers else
                "no maneuvers yet — add them at the cursor, or set\n"
                "the angle windows above and press Auto-detect")
            numbered = telemetry.numbered_maneuvers(self.maneuvers)
            lbl_by_id = {id(m): lbl for m, lbl in numbered}
            for m in sorted(self.maneuvers, key=lambda m: m["t"]):
                row = tk.Frame(listf, bg=PANEL)
                row.pack(fill="x")
                var = tk.BooleanVar(value=m.get("enabled", True))

                def toggle(mm=m, vv=var):
                    mm["enabled"] = vv.get()
                    self._maneuvers_changed()
                    rebuild_rows()
                tv = m["t"] - self.offset
                name = lbl_by_id.get(id(m), "—")
                txt = (f"{name:>3s}  video {self._fmt_t(tv)}  "
                       f"{m.get('mag', 0):5.1f}°")
                tk.Checkbutton(row, text=txt, variable=var, command=toggle,
                               bg=PANEL, fg=TEXT, activebackground=PANEL,
                               activeforeground=TEXT, selectcolor=PANEL2,
                               font=F_MONO, anchor="w",
                               cursor="hand2").pack(side="left", fill="x",
                                                    expand=True)

                def remove(mm=m):
                    self.maneuvers.remove(mm)
                    self._maneuvers_changed()
                    rebuild_rows()
                rb = _btn(row, "✕", remove)
                rb.configure(padx=4, pady=0, font=("Segoe UI", 8))
                rb.pack(side="right")
            _bind_wheel(lscroll, lscroll)

        def get_ranges():
            try:
                self.t_rng = [float(vars_["T min"].get()),
                              float(vars_["T max"].get())]
                self.g_rng = [float(vars_["G min"].get()),
                              float(vars_["G max"].get())]
                return True
            except ValueError:
                messagebox.showerror(APP_NAME, "Angle limits must be numbers.",
                                     parent=dlg)
                return False

        def detect():
            if not get_ranges() or not self._confirm_replace(parent=dlg):
                return
            self.maneuvers = telemetry.detect_maneuvers(
                self.tele, tuple(self.t_rng), tuple(self.g_rng))
            self._maneuvers_changed()
            rebuild_rows()

        def add(kind):
            self.maneuvers.append({"t": self.offset + self.frame_t,
                                   "kind": kind, "mag": 0.0, "enabled": True})
            self._maneuvers_changed()
            rebuild_rows()

        def clear():
            self.maneuvers = []
            self._maneuvers_changed()
            rebuild_rows()

        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(fill="x", padx=14, pady=(4, 12))
        _btn(btns, "Auto-detect", detect, accent=True).pack(side="left")
        _btn(btns, "Add T at cursor", lambda: add("T")).pack(side="left",
                                                             padx=(6, 0))
        _btn(btns, "Add G at cursor", lambda: add("G")).pack(side="left",
                                                             padx=(6, 0))
        _btn(btns, "Clear", clear).pack(side="left", padx=(6, 0))
        _btn(btns, "Close", dlg.destroy).pack(side="right")
        rebuild_rows()

    # ================= gauges =================
    def _rebuild_gauge_list(self):
        for g in self.gauges:
            if g.KIND == "track_map":
                g.on_map_ready = self._map_ready
        for w in self.gauge_box.winfo_children():
            w.destroy()
        for g in self.gauges:
            avail = g.available(self.tele)
            var = tk.BooleanVar(value=g.enabled and avail)

            def toggle(gg=g, vv=var):
                gg.enabled = vv.get()
                self._refresh_overlay()
            name = g.label or g.stream
            suffix = "" if avail else "  (no data)"
            cb = tk.Checkbutton(
                self.gauge_box, text=f"{name}{suffix}", variable=var,
                command=toggle, bg=PANEL, fg=TEXT if avail else DIM,
                activebackground=PANEL, activeforeground=TEXT,
                selectcolor=PANEL2, font=F_UI, anchor="w", cursor="hand2",
                state="normal" if avail else "disabled",
                disabledforeground=DIM)
            cb.pack(fill="x")
        _bind_wheel(self.gauge_box, self.lscroll)

    def reset_layout(self):
        if self.tele:
            self.gauges = gauges.default_gauges(self.tele)
            self._rebuild_gauge_list()
            self._refresh_overlay()

    def add_readout(self):
        if not self.tele:
            messagebox.showinfo(APP_NAME, "Load telemetry data first.")
            return
        if len(self.gauges) >= gauges.MAX_GAUGES:
            messagebox.showinfo(APP_NAME,
                                f"Maximum {gauges.MAX_GAUGES} gauges.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Add readout")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text="Choose a data stream", bg=PANEL, fg=MUTED,
                 font=F_UI).pack(padx=14, pady=(12, 6))
        lb = tk.Listbox(dlg, bg=PANEL2, fg=TEXT, selectbackground=ACCENT,
                        relief="flat", font=F_MONO, height=12, width=34)
        streams = sorted(self.tele.streams)
        for s in streams:
            st = self.tele.streams[s]
            unit = f" [{st.unit}]" if st.unit else ""
            lb.insert("end", f"{st.label:<10s} {s}{unit}")
        lb.pack(padx=14, pady=4)

        def ok():
            sel = lb.curselection()
            if sel:
                s = streams[sel[0]]
                st = self.tele.streams[s]
                n = sum(1 for g in self.gauges if g.KIND == "digits")
                g = gauges.DigitsBox(stream=s, label=st.label,
                                     x=0.35 + 0.02 * n, y=0.82 - 0.03 * n)
                self.gauges.append(g)
                self._rebuild_gauge_list()
                self._refresh_overlay()
            dlg.destroy()
        _btn(dlg, "Add", ok, accent=True).pack(pady=(6, 12))
        lb.bind("<Double-Button-1>", lambda _e: ok())

    # ================= canvas drag =================
    def _canvas_to_frame(self, cx, cy):
        if not self._geom:
            return None
        offx, offy, scale, pw, ph = self._geom
        fx = (cx - offx) / scale
        fy = (cy - offy) / scale
        if 0 <= fx <= pw and 0 <= fy <= ph:
            return fx, fy, pw, ph
        return None

    def _hit(self, cx, cy):
        pos = self._canvas_to_frame(cx, cy)
        if not pos or not self.tele:
            return None
        fx, fy, pw, ph = pos
        for g in reversed(self.gauges):
            if not g.enabled or not g.available(self.tele):
                continue
            px, py, w, h = g.pixel_rect(pw, ph)
            px = max(0, min(pw - w, px))
            py = max(0, min(ph - h, py))
            if px <= fx <= px + w and py <= fy <= py + h:
                return g, fx - px, fy - py, pw, ph
        return None

    def _on_press(self, e):
        self._drag = self._hit(e.x, e.y)

    def _on_drag(self, e):
        if not self._drag:
            return
        g, dx, dy, pw, ph = self._drag
        pos = self._canvas_to_frame(e.x, e.y)
        if not pos:
            return
        fx, fy, _, _ = pos
        w, h = g.pixel_wh(ph)
        g.x = max(0.0, min(1.0 - w / pw, (fx - dx) / pw))
        g.y = max(0.0, min(1.0 - h / ph, (fy - dy) / ph))
        self._refresh_overlay()

    def _on_release(self, _e):
        self._drag = None

    def _on_hover(self, e):
        if self._drag:
            return
        self.canvas.configure(cursor="fleur" if self._hit(e.x, e.y) else "")

    def _on_wheel(self, e):
        """Mouse wheel over a gauge zooms it (uniform scale)."""
        hit = self._hit(e.x, e.y)
        if not hit:
            return
        g = hit[0]
        factor = 1.1 if e.delta > 0 else 1 / 1.1
        g.size = max(0.5, min(3.0, g.size * factor))
        self._refresh_overlay()

    def _on_context(self, e):
        hit = self._hit(e.x, e.y)
        menu = tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=TEXT,
                       activebackground=ACCENT, activeforeground=TEXT)
        if hit:
            g = hit[0]

            def hide():
                if g.KIND == "digits":
                    self.gauges.remove(g)
                else:
                    g.enabled = False
                self._rebuild_gauge_list()
                self._refresh_overlay()

            def adj(attr, factor, lo, hi):
                setattr(g, attr,
                        max(lo, min(hi, getattr(g, attr) * factor)))
                self._refresh_overlay()
            label = g.label or g.stream
            menu.add_command(label=f"Remove {label}", command=hide)
            menu.add_command(label="Larger",
                             command=lambda: adj("size", 1.15, 0.5, 2.5))
            menu.add_command(label="Smaller",
                             command=lambda: adj("size", 1 / 1.15, 0.5, 2.5))
            if g.STRETCH:
                menu.add_separator()
                menu.add_command(label="Wider",
                                 command=lambda: adj("sx", 1.2, 0.6, 3.5))
                menu.add_command(label="Narrower",
                                 command=lambda: adj("sx", 1 / 1.2, 0.6, 3.5))
                menu.add_command(label="Taller",
                                 command=lambda: adj("sy", 1.2, 0.6, 2.5))
                menu.add_command(label="Shorter",
                                 command=lambda: adj("sy", 1 / 1.2, 0.6, 2.5))
            if g.KIND == "track_map":
                def set_map(style, gg=g):
                    gg.map_style = style
                    gg.on_map_ready = self._map_ready
                    if style != "none" and self.tele and self.tele.track:
                        self.status("fetching map tiles…", MUTED)
                        gg.mosaic(self.tele)  # kicks async fetch
                    self._refresh_overlay()

                def set_view(mode, meters, gg=g):
                    gg.view_mode = mode
                    gg.follow_m = meters
                    if gg.map_style != "none":
                        gg.mosaic(self.tele)
                    self._refresh_overlay()
                mmap = tk.Menu(menu, tearoff=0, bg=PANEL2, fg=TEXT,
                               activebackground=ACCENT, activeforeground=TEXT)
                for lbl2, st in (("None", "none"), ("Street (OSM)", "street"),
                                 ("Satellite (Esri)", "satellite")):
                    mark = "✓ " if g.map_style == st else "   "
                    mmap.add_command(label=mark + lbl2,
                                     command=lambda s=st: set_map(s))
                menu.add_cascade(label="Map background", menu=mmap)
                mview = tk.Menu(menu, tearoff=0, bg=PANEL2, fg=TEXT,
                                activebackground=ACCENT, activeforeground=TEXT)
                opts = [("Whole route", "route", g.follow_m),
                        ("Follow boat · 500 m", "follow", 500.0),
                        ("Follow boat · 1 km", "follow", 1000.0),
                        ("Follow boat · 2 km", "follow", 2000.0)]
                for lbl2, mode, meters in opts:
                    cur = (g.view_mode == mode and
                           (mode == "route" or abs(g.follow_m - meters) < 1))
                    mark = "✓ " if cur else "   "
                    mview.add_command(
                        label=mark + lbl2,
                        command=lambda m=mode, mm=meters: set_view(m, mm))
                menu.add_cascade(label="Map view", menu=mview)
            menu.add_separator()
        menu.add_command(label="Reset layout", command=self.reset_layout)
        menu.tk_popup(e.x_root, e.y_root)

    # ================= export =================
    def browse_out(self):
        p = filedialog.asksaveasfilename(
            title="Output video", defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
            initialfile=os.path.basename(self.var_out.get() or "overlay.mp4"))
        if p:
            self.var_out.set(p)

    def set_quality(self, key):
        if self.exporting:
            return
        self.quality = key
        self.overlay_fps = videoio.get_quality(key).overlay_fps
        self._update_quality_ui()

    def _on_audio_toggle(self):
        self._update_quality_ui()

    def _update_quality_ui(self):
        for key, b in self.btn_quality.items():
            on = key == self.quality
            b.configure(bg=ACCENT if on else PANEL2, font=F_UI_B if on else F_UI,
                        activebackground=ACCENT_HOVER if on else PANEL3)
        q = videoio.get_quality(self.quality)
        if self.vinfo:
            w, h = videoio.target_dims(self.vinfo, q)
            mb = videoio.estimate_size_mb(self.vinfo, q, self.var_audio.get())
            size = f"{mb:,.1f}" if mb < 100 else f"{mb:,.0f}"
            self.lbl_quality.configure(
                text=f"{w}x{h} · {videoio.target_fps(self.vinfo, q):.4g} fps · "
                     f"≈{size} MB")
        else:
            self.lbl_quality.configure(
                text=f"≤{q.max_h}p · {q.fps} fps · gauges at {q.overlay_fps} fps")

    def _export_buttons(self, running):
        self.btn_render.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        for b in self.btn_quality.values():
            b.configure(state="disabled" if running else "normal")
        self.chk_audio.configure(state="disabled" if running else "normal")

    def start_export(self):
        if self.exporting:
            return
        self.play_stop()
        if not (self.video_path and self.vinfo):
            messagebox.showinfo(APP_NAME, "Open a video first.")
            return
        if not self.tele:
            messagebox.showinfo(APP_NAME,
                                "Open a GPX/CSV/VKX telemetry file first.")
            return
        out = self.var_out.get().strip()
        if not out:
            out = videoio.default_output_path(self.video_path)
            self.var_out.set(out)
        if os.path.abspath(out) == os.path.abspath(self.video_path):
            messagebox.showerror(APP_NAME, "Output path equals the input video.")
            return
        if os.path.exists(out) and not messagebox.askyesno(
                APP_NAME, f"Overwrite existing file?\n{out}"):
            return
        self.exporting = True
        self.cancel_evt.clear()
        self._export_buttons(True)
        self.btn_folder.pack_forget()
        self.pbar.configure(value=0)
        self.status("starting export…", MUTED)
        info, tele = self.vinfo, self.tele
        tele.maneuvers = self.maneuvers
        glist = list(self.gauges)
        offset, qual, enc = self.offset, self.quality, self.encoder
        want_audio = self.var_audio.get()
        start = [0.0]

        def prog(done, total):
            now = time.time()
            if start[0] == 0.0:
                start[0] = now
            if done == total or now - getattr(prog, "_last", 0) > 0.25:
                prog._last = now
                self.q.put(("progress", done, total, now - start[0]))

        def work():
            try:
                # make sure map tiles are on hand before frames start flowing
                from . import maptiles
                for g in glist:
                    if (g.KIND == "track_map" and g.enabled
                            and g.map_style != "none" and tele.track):
                        self.q.put(("status", "fetching map tiles…"))
                        maptiles.service.prepare(tele.track, g.map_style,
                                                 g.map_pad())
                elapsed = videoio.export(info, tele, glist, offset, out,
                                         quality=qual, audio=want_audio,
                                         encoder=enc, progress=prog,
                                         cancel=self.cancel_evt)
                self.q.put(("export_done", out, elapsed))
            except videoio.ExportCancelled:
                try:
                    os.remove(out)
                except OSError:
                    pass
                self.q.put(("export_cancelled",))
            except Exception as e:
                self.q.put(("export_err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def cancel_export(self):
        self.cancel_evt.set()

    def _on_progress(self, done, total, elapsed):
        self.pbar.configure(value=1000.0 * done / total)
        video_done = done / self.overlay_fps
        speed = video_done / max(1e-9, elapsed)
        eta = (total - done) / self.overlay_fps / max(1e-9, speed)
        self.status(f"rendering  {100 * done / total:5.1f}%   "
                    f"{speed:.2f}x realtime   ETA {self._fmt_t(eta)}", MUTED)

    def _on_export_done(self, out, elapsed):
        self.exporting = False
        self._export_buttons(False)
        self.pbar.configure(value=1000)
        try:
            size = f", {os.path.getsize(out) / (1024 * 1024):,.0f} MB"
        except OSError:
            size = ""
        self.status(f"saved ({self._fmt_t(elapsed)}{size}): {out}", OK)
        self.btn_folder.pack(fill="x", pady=(6, 0))

    def _on_export_err(self, msg):
        self.exporting = False
        self._export_buttons(False)
        self.status("export failed", ERR)
        messagebox.showerror(APP_NAME, f"Export failed:\n{msg[:1200]}")

    def open_out_folder(self):
        out = self.var_out.get()
        folder = os.path.dirname(os.path.abspath(out)) if out else None
        if folder and os.path.isdir(folder):
            os.startfile(folder)

    # ================= project =================
    def save_project(self):
        if not self.tele and not self.video_path:
            messagebox.showinfo(APP_NAME, "Nothing to save yet.")
            return
        p = filedialog.asksaveasfilename(
            title="Save project", defaultextension=".7seas.json",
            filetypes=PROJECT_TYPES)
        if not p:
            return
        full = self.tele_full
        project.save_project(
            p, self.video_path, self.data_path,
            full.mapping if (full and full.kind == "csv") else None,
            full.speed_units if full else {},
            self.user_off, self.overlay_fps, self.gauges,
            trim=self.trim, maneuvers=self.maneuvers, quality=self.quality,
            audio=self.var_audio.get())
        self.status(f"project saved: {os.path.basename(p)}", OK)

    def load_project(self):
        p = filedialog.askopenfilename(title="Load project",
                                       filetypes=PROJECT_TYPES)
        if not p:
            return
        try:
            doc = project.load_project(p)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not load project:\n{e}")
            return
        self._pending_project = doc
        if doc.get("video") and os.path.exists(doc["video"]):
            self._load_video(doc["video"])
        if doc.get("data") and os.path.exists(doc["data"]):
            self._load_data(doc["data"], mapping=doc.get("mapping"),
                            units=doc.get("speed_units"))
        else:
            self._pending_project = None
            messagebox.showwarning(APP_NAME, "Project's data file not found.")

    # ================= CSV column mapper =================
    def open_mapper(self):
        if not (self.tele_full and self.tele_full.kind == "csv"):
            return
        tele = self.tele_full
        dlg = tk.Toplevel(self.root)
        dlg.title("Map CSV columns")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = tk.Frame(dlg, bg=PANEL)
        frm.pack(padx=16, pady=12)
        rows = [("time", "Timestamp *"), ("lat", "Latitude"), ("lon", "Longitude"),
                ("sog", "Speed (SOG)"), ("heading", "Heading"),
                ("cog", "Course (COG)"), ("roll", "Roll / heel / tilt"),
                ("pitch", "Pitch"), ("twd", "True wind direction"),
                ("tws", "True wind speed"), ("awa", "Apparent wind angle"),
                ("awd", "Apparent wind dir"), ("aws", "Apparent wind speed")]
        choices = ["(none)"] + list(tele.columns)
        auto = telemetry.Telemetry.auto_map(tele.columns)
        combos = {}
        for i, (stream, label) in enumerate(rows):
            tk.Label(frm, text=label, bg=PANEL, fg=MUTED, font=F_UI,
                     anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            cb = ttk.Combobox(frm, values=choices, state="readonly", width=24)
            cur = tele.mapping.get(stream) or auto.get(stream)
            cb.set(cur if cur in tele.columns else "(none)")
            cb.grid(row=i, column=1, padx=(10, 0), pady=2)
            combos[stream] = cb
        units = {}
        for j, stream in enumerate(("sog", "tws", "aws")):
            tk.Label(frm, text=f"{stream.upper()} unit", bg=PANEL, fg=MUTED,
                     font=F_UI, anchor="w").grid(row=len(rows) + j, column=0,
                                                 sticky="w", pady=2)
            cb = ttk.Combobox(frm, values=list(telemetry.SPEED_UNIT_FACTORS),
                              state="readonly", width=8)
            cb.set(tele.speed_units.get(stream, "kn"))
            cb.grid(row=len(rows) + j, column=1, sticky="w", padx=(10, 0), pady=2)
            units[stream] = cb

        def ok():
            mapping = {s: c.get() for s, c in combos.items()
                       if c.get() and c.get() != "(none)"}
            uu = {s: c.get() for s, c in units.items()}
            dlg.destroy()
            self._load_data(self.data_path, mapping=mapping, units=uu)
        _btn(dlg, "Apply", ok, accent=True).pack(pady=(4, 14))


def run():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()
