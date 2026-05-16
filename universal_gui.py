#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import json
import queue
import re
import shutil
import threading
import tkinter as tk
import os
from pathlib import Path
import subprocess
import sys
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional
from urllib.parse import urlparse

from core.pipeline import (
    PipelineConfig,
    PipelineError,
    PipelineEvent,
    load_cookies,
    _safe_run_segment,
    parse_cli_args,
    run_universal_pipeline,
)
from core.chrome_bridge import normalize_origin, write_storage_state


_DOWNLOADS_AUTH_COOKIES = Path.home() / "Downloads" / "e9ca2c61-8d73-4fdf-81a4-190a29bcaf36.txt"


def _default_cookies_path() -> Path:
    if _DOWNLOADS_AUTH_COOKIES.exists():
        return _DOWNLOADS_AUTH_COOKIES
    return Path(__file__).resolve().parent / "cooks.txt"


def _primary_origin(urls: list[str]) -> str:
    for url in urls:
        try:
            return normalize_origin(url)
        except Exception:
            continue
    return "https://simpcity.cr"


def _storage_state_from_cookies_path(cookies: Optional[Path], urls: list[str]) -> Optional[Path]:
    if not cookies or cookies.suffix.lower() != ".txt":
        return None
    try:
        parsed = load_cookies(cookies)
    except Exception:
        return None
    if not parsed:
        return None
    try:
        return write_storage_state(_primary_origin(urls), parsed, base_dir=Path.home())
    except Exception:
        return None


class UniversalGui:
    BG = "#180427"
    BG_MID = "#4a1272"
    BG_HI = "#ff3fab"
    CARD = "#21123a"
    SURFACE = "#1a0f31"
    INK = "#fff5ff"
    MUTED = "#d9b6df"
    ACCENT = "#ff4ecd"
    ACCENT_SOFT = "#4a1e58"
    WARN = "#ffd166"
    DANGER = "#ff6b9e"
    SUCCESS = "#4cf2b3"
    GLASS_BG = "#261843"
    GLASS_FG = "#f7e8ff"
    DOWNLOAD_UI_MIN_DELTA = 1.5
    LOG_MAX_ENTRIES = 5000
    SETTINGS_FILE = Path.home() / ".simpscrape_gui_settings.json"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Universal Performer Downloader")
        self.root.geometry("1220x860")
        self.root.minsize(1040, 720)
        self.root.configure(bg=self.BG)

        self.base_download_dir = Path.home() / "Downloads"
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.stop_requested = False
        self.download_rows: dict[str, dict[str, Any]] = {}
        self.finalized_urls: set[str] = set()
        self.total_downloads = 0
        self.completed_downloads = 0
        self.failed_downloads = 0
        self.row_order_counter = 0
        self.host_stats: dict[str, dict[str, int]] = {}
        self.host_filter_keys: list[Optional[str]] = []
        self.selected_host_filter: Optional[str] = None
        self.log_entries: list[dict[str, Any]] = []
        self.log_visible_cache = ""
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.performer_entry: Optional[ttk.Entry] = None
        self.delay_entry: Optional[ttk.Entry] = None
        self._bg_canvas: Optional[tk.Canvas] = None
        self._bg_window = None

        self.performer_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")
        self.phase_var = tk.StringVar(value="Waiting to start")
        self.output_path_var = tk.StringVar(value="Output: ~/Downloads/<auto performer>")
        self.total_var = tk.StringVar(value="0 / 0 complete")
        self.crawl_var = tk.StringVar(value="Scraped records: 0")
        self.host_var = tk.StringVar(value="Hosts: 0")
        self.result_var = tk.StringVar(value="")
        self.log_filter_var = tk.StringVar(value="all")
        self.log_search_var = tk.StringVar(value="")
        self.log_follow_var = tk.BooleanVar(value=True)

        cpu_count = max(1, os.cpu_count() or 1)
        default_crawl_jobs = max(1, min(8, cpu_count // 2 if cpu_count > 1 else 1))
        default_download_workers = max(4, min(16, cpu_count))
        default_resolve_workers = max(6, min(24, cpu_count * 2))

        self.headless_var = tk.BooleanVar(value=True)
        self.include_source_hosts_var = tk.BooleanVar(value=False)
        self.max_pages_var = tk.StringVar(value="")
        self.delay_var = tk.StringVar(value="250")
        self.crawl_jobs_var = tk.StringVar(value=str(default_crawl_jobs))
        self.nav_timeout_var = tk.StringVar(value="60000")
        self.idle_timeout_var = tk.StringVar(value="5000")
        self.download_workers_var = tk.StringVar(value=str(default_download_workers))
        self.attempts_var = tk.StringVar(value="3")
        self.retry_delay_var = tk.StringVar(value="3.0")
        self.cookies_var = tk.StringVar(value=str(_default_cookies_path()))
        self.storage_state_var = tk.StringVar(value="")
        self.gallery_args_var = tk.StringVar(value="--no-colors")
        self.yt_dlp_args_var = tk.StringVar(value="--no-warnings --ignore-errors")
        self.resolve_links_var = tk.BooleanVar(value=True)
        self.resolve_workers_var = tk.StringVar(value=str(default_resolve_workers))
        self.capture_profile_var = tk.StringVar(value="balanced")
        self.last_failed_urls: list[str] = []
        self._input_url_note = ""

        self._build_styles()
        self._build_ui()
        self._restore_settings()
        self._bind_updates()
        self.root.bind("<Control-Return>", lambda _event: self._start_run())
        self.root.bind("<Control-1>", lambda _event: self._on_nav_selected("Home"))
        self.root.bind("<Control-2>", lambda _event: self._on_nav_selected("Downloads"))
        self.root.bind("<Control-3>", lambda _event: self._on_nav_selected("Activity"))
        self.root.after(120, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_styles(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("Root.TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("Glass.TFrame", background=self.GLASS_BG)
        style.configure("Soft.TFrame", background="#341651")
        style.configure(
            "Title.TLabel",
            background=self.CARD,
            foreground=self.INK,
            font=("SF Pro Display", 26, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=self.CARD,
            foreground=self.MUTED,
            font=("SF Pro Text", 11),
        )
        style.configure(
            "Field.TLabel",
            background=self.CARD,
            foreground=self.INK,
            font=("SF Pro Text", 10, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background=self.CARD,
            foreground=self.MUTED,
            font=("SF Pro Text", 10),
        )
        style.configure(
            "GlassBody.TLabel",
            background=self.GLASS_BG,
            foreground=self.GLASS_FG,
            font=("SF Pro Text", 9),
        )
        style.configure(
            "HeroTitle.TLabel",
            background=self.CARD,
            foreground=self.INK,
            font=("SF Pro Display", 32, "bold"),
        )
        style.configure(
            "HeroSub.TLabel",
            background=self.CARD,
            foreground=self.MUTED,
            font=("SF Pro Text", 13),
        )
        style.configure(
            "Chip.TLabel",
            background=self.ACCENT_SOFT,
            foreground=self.ACCENT,
            font=("SF Pro Text", 9, "bold"),
            padding=(12, 6),
        )
        style.configure(
            "Primary.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground="#fff5ff",
            background="#c32686",
            borderwidth=0,
            padding=(20, 10),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#eb48b0"), ("disabled", "#5f2b56")],
            foreground=[("disabled", "#c5a3c8")],
        )
        style.configure(
            "PrimaryHover.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground="#fff5ff",
            background="#f35abc",
            borderwidth=0,
            padding=(20, 10),
        )
        style.configure(
            "Secondary.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground=self.INK,
            background="#37205a",
            borderwidth=1,
            relief="flat",
            padding=(16, 9),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#4a2e75"), ("disabled", "#2a1a45")],
            foreground=[("disabled", "#9b7ca8")],
            bordercolor=[("!disabled", "#6f4a8e")],
        )
        style.configure(
            "Nav.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground="#eacfff",
            background="#341651",
            borderwidth=0,
            padding=(12, 10),
        )
        style.configure(
            "NavActive.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground="#fff3ff",
            background="#612265",
            borderwidth=0,
            padding=(12, 10),
        )
        style.map(
            "Nav.TButton",
            background=[("active", "#5a2770")],
            foreground=[("active", "#fff3ff")],
        )
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=self.SURFACE,
            background=self.ACCENT,
            borderwidth=0,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=14,
        )
        style.configure(
            "Download.Pending.Horizontal.TProgressbar",
            troughcolor=self.SURFACE,
            background="#7cc6ff",
            borderwidth=0,
            lightcolor="#7cc6ff",
            darkcolor="#7cc6ff",
            thickness=10,
        )
        style.configure(
            "Download.Success.Horizontal.TProgressbar",
            troughcolor=self.SURFACE,
            background=self.SUCCESS,
            borderwidth=0,
            lightcolor=self.SUCCESS,
            darkcolor=self.SUCCESS,
            thickness=10,
        )
        style.configure(
            "Download.Failure.Horizontal.TProgressbar",
            troughcolor=self.SURFACE,
            background=self.DANGER,
            borderwidth=0,
            lightcolor=self.DANGER,
            darkcolor=self.DANGER,
            thickness=10,
        )
        style.configure(
            "TCheckbutton",
            background=self.CARD,
            foreground=self.INK,
            font=("SF Pro Text", 9),
        )
        style.configure(
            "TEntry",
            fieldbackground=self.SURFACE,
            foreground=self.INK,
            bordercolor="#2b416d",
            lightcolor="#2b416d",
            darkcolor="#000000",
            padding=6,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", self.ACCENT)],
            foreground=[("disabled", self.MUTED)],
        )

    def _build_ui(self) -> None:
        self._bg_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, relief=tk.FLAT)
        self._bg_canvas.pack(fill=tk.BOTH, expand=True)
        self._bg_canvas.bind("<Configure>", self._on_background_resize)

        root = ttk.Frame(self._bg_canvas, style="Root.TFrame", padding=12)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)
        self._bg_window = self._bg_canvas.create_window((0, 0), window=root, anchor="nw")

        topbar = ttk.Frame(root, style="Card.TFrame", padding=12)
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        topbar.columnconfigure(0, weight=1)
        ttk.Label(topbar, text="SimpScrape Studio", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            topbar,
            text="Professional desktop crawler control center",
            style="Sub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.start_btn = ttk.Button(topbar, text="Start Run", style="Primary.TButton", command=self._start_run, cursor="hand2")
        self.start_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.stop_btn = ttk.Button(topbar, text="Stop", style="Secondary.TButton", command=self._stop_run, state=tk.DISABLED, cursor="hand2")
        self.stop_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Button(topbar, text="Open Folder", style="Secondary.TButton", command=self._open_output_folder, cursor="hand2").grid(
            row=0, column=3, sticky="e", padx=(8, 0)
        )
        ttk.Button(topbar, text="Clear List", style="Secondary.TButton", command=self._clear_download_rows, cursor="hand2").grid(
            row=0, column=4, sticky="e", padx=(8, 0)
        )
        ttk.Label(topbar, textvariable=self.status_var, style="Chip.TLabel").grid(row=1, column=1, columnspan=4, sticky="e")
        ttk.Label(topbar, textvariable=self.output_path_var, style="Body.TLabel").grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))

        shell = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        shell.grid(row=1, column=0, sticky="nsew")

        sidebar = ttk.Frame(shell, style="Card.TFrame", padding=12)
        sidebar.columnconfigure(0, weight=1)
        shell.add(sidebar, weight=1)

        nav = ttk.Frame(sidebar, style="Soft.TFrame", padding=8)
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        nav.columnconfigure(0, weight=1)
        ttk.Label(nav, text="Workspace", style="GlassBody.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        nav_items = ["Home", "Capture", "Performance", "Downloads", "Activity"]
        for idx, item in enumerate(nav_items, start=1):
            btn = ttk.Button(
                nav,
                text=f"  {item}",
                style="Nav.TButton",
                command=lambda name=item: self._on_nav_selected(name),
                cursor="hand2",
            )
            btn.grid(row=idx, column=0, sticky="ew", pady=2)
            self.nav_buttons[item] = btn

        ttk.Label(sidebar, text="Pipeline", style="Field.TLabel").grid(row=20, column=0, sticky="w")
        ttk.Label(sidebar, textvariable=self.phase_var, style="Body.TLabel").grid(row=21, column=0, sticky="w", pady=(4, 8))
        self.stage_bar = ttk.Progressbar(sidebar, style="Accent.Horizontal.TProgressbar", maximum=100, value=0)
        self.stage_bar.grid(row=22, column=0, sticky="ew")
        self.overall_bar = ttk.Progressbar(sidebar, style="Accent.Horizontal.TProgressbar", maximum=100, value=0)
        self.overall_bar.grid(row=23, column=0, sticky="ew", pady=(6, 10))
        ttk.Label(sidebar, textvariable=self.total_var, style="Body.TLabel").grid(row=24, column=0, sticky="w")
        ttk.Label(sidebar, textvariable=self.crawl_var, style="Body.TLabel").grid(row=25, column=0, sticky="w")
        ttk.Label(sidebar, textvariable=self.host_var, style="Body.TLabel").grid(row=26, column=0, sticky="w")
        self.result_label = ttk.Label(sidebar, textvariable=self.result_var, style="Body.TLabel")
        self.result_label.grid(row=27, column=0, sticky="w", pady=(6, 0))

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).grid(row=28, column=0, sticky="ew", pady=10)
        ttk.Label(sidebar, text="Quick Toggles", style="Field.TLabel").grid(row=29, column=0, sticky="w")
        ttk.Checkbutton(sidebar, text="Headless Browser", variable=self.headless_var).grid(row=30, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(sidebar, text="Include Same-Host Links", variable=self.include_source_hosts_var).grid(row=31, column=0, sticky="w")
        ttk.Checkbutton(sidebar, text="Use Host Resolvers", variable=self.resolve_links_var).grid(row=32, column=0, sticky="w")

        workspace = ttk.Frame(shell, style="Root.TFrame")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(0, weight=1)
        shell.add(workspace, weight=4)

        self.workspace_notebook = ttk.Notebook(workspace)
        self.workspace_notebook.grid(row=0, column=0, sticky="nsew")

        setup_tab = ttk.Frame(self.workspace_notebook, style="Card.TFrame", padding=12)
        setup_tab.columnconfigure(0, weight=1)
        setup_tab.rowconfigure(3, weight=1)
        self.workspace_notebook.add(setup_tab, text="Setup")

        hero = ttk.Frame(setup_tab, style="Card.TFrame", padding=18)
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text="Performance", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            hero,
            text="Run intelligent crawl + download routines to optimize your collection speed.",
            style="HeroSub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))
        ttk.Button(hero, text="Quick Start", style="Primary.TButton", command=self._start_run).grid(row=0, column=1, rowspan=2, sticky="e")

        identity = ttk.Frame(setup_tab, style="Card.TFrame")
        identity.grid(row=1, column=0, sticky="ew")
        for col in range(4):
            identity.columnconfigure(col, weight=1)
        ttk.Label(identity, text="Performer", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.performer_entry = ttk.Entry(identity, textvariable=self.performer_var)
        self.performer_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(identity, text="Delay (ms)", style="Field.TLabel").grid(row=0, column=1, sticky="w")
        self.delay_entry = ttk.Entry(identity, textvariable=self.delay_var)
        self.delay_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(identity, text="Max Pages", style="Field.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Entry(identity, textvariable=self.max_pages_var).grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(identity, text="Resolver Workers", style="Field.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Entry(identity, textvariable=self.resolve_workers_var).grid(row=1, column=3, sticky="ew", pady=(4, 8))

        tuning = ttk.Frame(setup_tab, style="Card.TFrame")
        tuning.grid(row=2, column=0, sticky="ew", pady=(8, 8))
        for col in range(4):
            tuning.columnconfigure(col, weight=1)
        ttk.Label(tuning, text="Crawl Jobs", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(tuning, textvariable=self.crawl_jobs_var).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(tuning, text="Download Workers", style="Field.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Entry(tuning, textvariable=self.download_workers_var).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(tuning, text="Attempts", style="Field.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Entry(tuning, textvariable=self.attempts_var).grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(tuning, text="Retry Delay", style="Field.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Entry(tuning, textvariable=self.retry_delay_var).grid(row=1, column=3, sticky="ew", pady=(4, 8))
        ttk.Label(tuning, text="Capture profile", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            tuning,
            textvariable=self.capture_profile_var,
            state="readonly",
            values=("fast", "balanced", "deep"),
            width=14,
        ).grid(row=3, column=0, sticky="w", pady=(4, 8))

        center = ttk.Panedwindow(setup_tab, orient=tk.VERTICAL)
        center.grid(row=3, column=0, sticky="nsew")
        urls_panel = ttk.Frame(center, style="Card.TFrame", padding=8)
        urls_panel.columnconfigure(0, weight=1)
        ttk.Label(urls_panel, text="Input URLs", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(urls_panel, text="Import URLs", style="Secondary.TButton", command=self._import_urls_file).grid(row=0, column=1, sticky="e")
        self.urls_text = tk.Text(
            urls_panel,
            height=10,
            wrap=tk.WORD,
            font=("SF Pro Text", 10),
            bg=self.SURFACE,
            fg=self.INK,
            insertbackground=self.INK,
            relief=tk.FLAT,
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#32476f",
            highlightcolor=self.ACCENT,
        )
        self.urls_text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        urls_panel.rowconfigure(1, weight=1)
        center.add(urls_panel, weight=2)

        paths = ttk.Frame(center, style="Card.TFrame", padding=8)
        paths.columnconfigure(0, weight=1)
        paths.columnconfigure(2, weight=1)
        ttk.Label(paths, text="Cookies Path", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.cookies_var).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Button(paths, text="Browse", style="Secondary.TButton", command=self._browse_cookies_file).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(paths, text="Storage State", style="Field.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Entry(paths, textvariable=self.storage_state_var).grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Button(paths, text="Browse", style="Secondary.TButton", command=self._browse_storage_state_file).grid(row=1, column=3, sticky="ew", pady=(4, 8))
        ttk.Label(paths, text="gallery-dl args", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(paths, textvariable=self.gallery_args_var).grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(4, 8))
        ttk.Label(paths, text="yt-dlp args", style="Field.TLabel").grid(row=2, column=2, sticky="w")
        ttk.Entry(paths, textvariable=self.yt_dlp_args_var).grid(row=3, column=2, columnspan=2, sticky="ew", pady=(4, 8))
        center.add(paths, weight=1)

        downloads_tab = ttk.Frame(self.workspace_notebook, style="Card.TFrame", padding=10)
        downloads_tab.columnconfigure(0, weight=1)
        downloads_tab.rowconfigure(0, weight=1)
        self.workspace_notebook.add(downloads_tab, text="Downloads")

        downloads_content = ttk.Frame(downloads_tab, style="Card.TFrame")
        downloads_content.grid(row=0, column=0, sticky="nsew")
        downloads_content.columnconfigure(0, weight=0)
        downloads_content.columnconfigure(1, weight=1)
        downloads_content.rowconfigure(0, weight=1)

        hosts_panel = tk.Frame(downloads_content, bg=self.CARD, highlightthickness=1, highlightbackground="#2d426b")
        hosts_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        tk.Label(hosts_panel, text="Hosts", bg=self.CARD, fg=self.INK, anchor="w", font=("SF Pro Text", 9, "bold")).pack(fill=tk.X, padx=8, pady=(8, 6))
        self.host_listbox = tk.Listbox(
            hosts_panel,
            activestyle="none",
            bg=self.SURFACE,
            fg=self.INK,
            selectbackground=self.ACCENT_SOFT,
            selectforeground=self.INK,
            highlightthickness=1,
            highlightbackground="#2d426b",
            highlightcolor=self.ACCENT,
            relief=tk.FLAT,
            width=28,
            height=14,
            font=("SF Pro Text", 8),
        )
        self.host_listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.host_listbox.bind("<<ListboxSelect>>", self._on_host_select)

        list_panel = ttk.Frame(downloads_content, style="Card.TFrame")
        list_panel.grid(row=0, column=1, sticky="nsew")
        list_panel.columnconfigure(0, weight=1)
        list_panel.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(list_panel, bg=self.CARD, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_panel, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.rows_frame = ttk.Frame(self.canvas, style="Card.TFrame")
        self.rows_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", self._on_rows_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._refresh_host_panel()

        logs_tab = ttk.Frame(self.workspace_notebook, style="Glass.TFrame", padding=10)
        logs_tab.columnconfigure(0, weight=1)
        logs_tab.rowconfigure(1, weight=1)
        self.workspace_notebook.add(logs_tab, text="Activity")
        logs_card = logs_tab
        logs_card.columnconfigure(0, weight=1)
        logs_card.rowconfigure(1, weight=1)
        header_row = ttk.Frame(logs_card, style="Glass.TFrame")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header_row.columnconfigure(2, weight=1)
        ttk.Label(header_row, text="Activity Feed", style="GlassBody.TLabel").grid(row=0, column=0, sticky="w")
        filter_menu = ttk.Combobox(
            header_row,
            state="readonly",
            width=12,
            textvariable=self.log_filter_var,
            values=("all", "crawl", "resolve", "download", "error", "warning"),
        )
        filter_menu.grid(row=0, column=1, sticky="w", padx=(10, 8))
        filter_menu.bind("<<ComboboxSelected>>", lambda _event: self._refresh_log_view())
        search_entry = ttk.Entry(header_row, textvariable=self.log_search_var)
        search_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.log_search_var.trace_add("write", lambda *_: self._refresh_log_view())
        ttk.Checkbutton(header_row, text="Follow", variable=self.log_follow_var).grid(row=0, column=3, sticky="e")
        ttk.Button(header_row, text="Copy", style="Secondary.TButton", command=self._copy_visible_logs).grid(
            row=0, column=4, sticky="e", padx=(8, 0)
        )
        ttk.Button(header_row, text="Export", style="Secondary.TButton", command=self._export_logs).grid(
            row=0, column=5, sticky="e", padx=(8, 0)
        )

        log_frame = tk.Frame(logs_card, bg=self.GLASS_BG, highlightthickness=0)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            bg="#15223f",
            fg="#dbe8ff",
            insertbackground="#dbe8ff",
            relief=tk.FLAT,
            font=("SF Pro Text", 10),
            padx=14,
            pady=12,
            highlightthickness=0,
            spacing1=2,
            spacing2=1,
            spacing3=6,
            borderwidth=0,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self._set_active_nav("Home")
        self._attach_primary_hover(self.start_btn)

    def _on_background_resize(self, event) -> None:
        if self._bg_canvas is None:
            return
        width = int(event.width)
        height = int(event.height)
        self._bg_canvas.delete("bg")
        steps = 42
        for i in range(steps):
            t = i / max(1, steps - 1)
            if t < 0.5:
                local = t / 0.5
                color = self._mix_hex(self.BG, self.BG_MID, local)
            else:
                local = (t - 0.5) / 0.5
                color = self._mix_hex(self.BG_MID, self.BG_HI, local)
            y0 = int((height * i) / steps)
            y1 = int((height * (i + 1)) / steps) + 1
            self._bg_canvas.create_rectangle(0, y0, width, y1, fill=color, outline="", tags="bg")
        glow_size = max(width, height)
        self._bg_canvas.create_oval(
            int(width * 0.48) - glow_size // 3,
            int(height * 0.2) - glow_size // 3,
            int(width * 0.48) + glow_size // 3,
            int(height * 0.2) + glow_size // 3,
            fill="#f08ad0",
            outline="",
            stipple="gray25",
            tags="bg",
        )
        if self._bg_window is not None:
            self._bg_canvas.coords(self._bg_window, 0, 0)
            self._bg_canvas.itemconfigure(self._bg_window, width=width, height=height)

    def _mix_hex(self, left: str, right: str, ratio: float) -> str:
        t = max(0.0, min(1.0, ratio))
        l = left.lstrip("#")
        r = right.lstrip("#")
        lv = (int(l[0:2], 16), int(l[2:4], 16), int(l[4:6], 16))
        rv = (int(r[0:2], 16), int(r[2:4], 16), int(r[4:6], 16))
        mixed = tuple(int(lv[idx] + ((rv[idx] - lv[idx]) * t)) for idx in range(3))
        return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"

    def _attach_primary_hover(self, button: ttk.Button) -> None:
        button.bind("<Enter>", lambda _event: button.configure(style="PrimaryHover.TButton"))
        button.bind("<Leave>", lambda _event: button.configure(style="Primary.TButton"))

    def _bind_updates(self) -> None:
        self.performer_var.trace_add("write", lambda *_: self._refresh_output_preview())
        self.urls_text.bind("<<Modified>>", self._on_urls_modified)

    def _set_active_nav(self, active: str) -> None:
        for name, button in self.nav_buttons.items():
            marker = "● " if name == active else "  "
            style_name = "NavActive.TButton" if name == active else "Nav.TButton"
            button.configure(text=f"{marker}{name}", style=style_name)

    def _on_nav_selected(self, item: str) -> None:
        target = (item or "").strip().lower()
        if target == "home":
            self.workspace_notebook.select(0)
            self.phase_var.set("Home view ready")
            if self.performer_entry is not None:
                self.performer_entry.focus_set()
        elif target == "capture":
            self.workspace_notebook.select(0)
            self.phase_var.set("Capture input ready")
            self.urls_text.focus_set()
        elif target == "performance":
            self.workspace_notebook.select(0)
            self.phase_var.set("Performance tuning ready")
            if self.delay_entry is not None:
                self.delay_entry.focus_set()
        elif target == "downloads":
            self.workspace_notebook.select(1)
            self.phase_var.set("Downloads view ready")
        elif target == "activity":
            self.workspace_notebook.select(2)
            self.phase_var.set("Activity feed ready")
        self._set_active_nav(item)

    def _restore_settings(self) -> None:
        if not self.SETTINGS_FILE.exists():
            return
        try:
            payload = json.loads(self.SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        for key, var in (
            ("performer", self.performer_var),
            ("delay_ms", self.delay_var),
            ("max_pages", self.max_pages_var),
            ("crawl_jobs", self.crawl_jobs_var),
            ("nav_timeout_ms", self.nav_timeout_var),
            ("idle_timeout_ms", self.idle_timeout_var),
            ("download_workers", self.download_workers_var),
            ("attempts", self.attempts_var),
            ("retry_delay", self.retry_delay_var),
            ("cookies", self.cookies_var),
            ("storage_state", self.storage_state_var),
            ("gallery_args", self.gallery_args_var),
            ("yt_dlp_args", self.yt_dlp_args_var),
            ("resolve_workers", self.resolve_workers_var),
            ("capture_profile", self.capture_profile_var),
        ):
            value = payload.get(key)
            if isinstance(value, str):
                var.set(value)

        for key, var in (
            ("headless", self.headless_var),
            ("include_source_hosts", self.include_source_hosts_var),
            ("resolve_links", self.resolve_links_var),
        ):
            value = payload.get(key)
            if isinstance(value, bool):
                var.set(value)

        urls_blob = payload.get("urls")
        if isinstance(urls_blob, str) and urls_blob.strip():
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", urls_blob.strip() + "\n")
            self.urls_text.edit_modified(False)

        geometry = payload.get("window_geometry")
        if isinstance(geometry, str) and geometry:
            try:
                self.root.geometry(geometry)
            except Exception:
                pass
        self._refresh_output_preview()

    def _save_settings(self) -> None:
        payload = {
            "performer": self.performer_var.get().strip(),
            "delay_ms": self.delay_var.get().strip(),
            "max_pages": self.max_pages_var.get().strip(),
            "crawl_jobs": self.crawl_jobs_var.get().strip(),
            "nav_timeout_ms": self.nav_timeout_var.get().strip(),
            "idle_timeout_ms": self.idle_timeout_var.get().strip(),
            "download_workers": self.download_workers_var.get().strip(),
            "attempts": self.attempts_var.get().strip(),
            "retry_delay": self.retry_delay_var.get().strip(),
            "cookies": self.cookies_var.get().strip(),
            "storage_state": self.storage_state_var.get().strip(),
            "gallery_args": self.gallery_args_var.get().strip(),
            "yt_dlp_args": self.yt_dlp_args_var.get().strip(),
            "resolve_workers": self.resolve_workers_var.get().strip(),
            "capture_profile": self.capture_profile_var.get().strip(),
            "headless": bool(self.headless_var.get()),
            "include_source_hosts": bool(self.include_source_hosts_var.get()),
            "resolve_links": bool(self.resolve_links_var.get()),
            "urls": self.urls_text.get("1.0", tk.END).strip(),
            "window_geometry": self.root.winfo_geometry(),
        }
        try:
            self.SETTINGS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        except Exception:
            return

    def _import_urls_file(self) -> None:
        path = filedialog.askopenfilename(title="Select URL list", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        file_path = Path(path)
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as exc:
            messagebox.showerror("Import failed", f"Could not read file:\n{exc}")
            return
        tokens = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        if not tokens:
            messagebox.showinfo("Import URLs", "No URLs found in the selected file.")
            return
        existing = self._collect_urls()
        combined = existing + tokens
        deduped = []
        seen: set[str] = set()
        for token in combined:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        self.urls_text.delete("1.0", tk.END)
        self.urls_text.insert("1.0", "\n".join(deduped) + "\n")
        self.urls_text.edit_modified(False)
        self._refresh_output_preview()

    def _browse_cookies_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select cookies file",
            initialdir=str(Path(self.cookies_var.get()).expanduser().parent),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookies_var.set(path)

    def _browse_storage_state_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select storage state JSON",
            initialdir=str(Path(self.storage_state_var.get() or ".").expanduser().parent),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.storage_state_var.set(path)

    def _open_output_folder(self) -> None:
        raw = self.output_path_var.get().replace("Output:", "", 1).strip()
        folder = Path(raw).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(folder)])
            elif os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror("Open folder failed", str(exc))

    def _on_urls_modified(self, _event=None) -> None:
        self.urls_text.edit_modified(False)
        self._refresh_output_preview()

    def _refresh_output_preview(self) -> None:
        performer = self.performer_var.get().strip()
        if not performer:
            urls = self._collect_urls()
            performer = _infer_performer_name([], urls[0] if urls else "")
        folder = _safe_run_segment(_sanitize_folder_name(performer))
        self.output_path_var.set(f"Output: {self.base_download_dir / folder} (runs timestamped per start)")

    def _on_rows_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.rows_window, width=event.width)

    def _collect_urls(self) -> list[str]:
        text = self.urls_text.get("1.0", tk.END)
        urls: list[str] = []
        for line in text.splitlines():
            token = line.strip()
            if token:
                urls.append(token)
        return urls

    def _is_http_url(self, value: str) -> bool:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _prepare_input_urls(self, urls: list[str]) -> tuple[list[str], int, int]:
        deduped: list[str] = []
        seen: set[str] = set()
        invalid_count = 0
        duplicate_count = 0
        for value in urls:
            token = value.strip()
            if not token:
                continue
            if not self._is_http_url(token):
                invalid_count += 1
                continue
            if token in seen:
                duplicate_count += 1
                continue
            seen.add(token)
            deduped.append(token)
        return deduped, invalid_count, duplicate_count

    def _parse_int(self, value: str, name: str, minimum: int) -> int:
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer.") from exc
        if parsed < minimum:
            raise ValueError(f"{name} must be >= {minimum}.")
        return parsed

    def _parse_float(self, value: str, name: str, minimum: float) -> float:
        try:
            parsed = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be a number.") from exc
        if parsed < minimum:
            raise ValueError(f"{name} must be >= {minimum}.")
        return parsed

    def _start_run(self) -> None:
        if self.running:
            return
        try:
            config = self._build_run_config()
        except ValueError as exc:
            messagebox.showerror("Invalid Settings", str(exc))
            return

        self.running = True
        self.stop_requested = False
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set("Running")
        self.phase_var.set(self._input_url_note or "Starting crawl...")
        self.result_var.set("")
        self.result_label.configure(foreground=self.MUTED)
        self.download_rows.clear()
        self.finalized_urls.clear()
        self.total_downloads = 0
        self.completed_downloads = 0
        self.failed_downloads = 0
        self.row_order_counter = 0
        self.host_stats.clear()
        self.selected_host_filter = None
        self._refresh_host_panel()
        self._set_stage_progress(0)
        self._set_activity_mode("indeterminate")
        self.total_var.set("0 / 0 complete")
        self.crawl_var.set("Scraped records: 0")
        self.host_var.set("Hosts: 0")
        self._clear_rows_widgets()
        self.log_entries.clear()
        self.log_visible_cache = ""
        self._refresh_log_view()
        self._append_log_entry("system", "info", "Run started.")
        self._save_settings()

        self.worker = threading.Thread(target=self._run_pipeline, args=(config,), daemon=True)
        self.worker.start()

    def _stop_run(self) -> None:
        if not self.running:
            return
        self.stop_requested = True
        self.status_var.set("Stopping")
        self.phase_var.set("Stopping after current operation...")
        self.stop_btn.configure(state=tk.DISABLED)

    def _build_run_config(self) -> PipelineConfig:
        raw_urls = self._collect_urls()
        if not raw_urls:
            raise ValueError("Add at least one URL.")
        urls, invalid_count, duplicate_count = self._prepare_input_urls(raw_urls)
        if invalid_count:
            raise ValueError(f"Found {invalid_count} invalid URL(s). Use full http(s) links.")
        if not urls:
            raise ValueError("No valid URLs remain after validation.")
        self._input_url_note = (
            f"Starting crawl... ({duplicate_count} duplicate URL(s) removed)"
            if duplicate_count
            else "Starting crawl..."
        )

        performer = self.performer_var.get().strip()
        if not performer:
            performer = _infer_performer_name([], urls[0])
        performer = _sanitize_folder_name(performer)
        if not performer:
            raise ValueError("Performer name is required.")
        output_root = self.base_download_dir / _safe_run_segment(performer)

        profile = (self.capture_profile_var.get() or "balanced").strip().lower()
        if profile not in {"fast", "balanced", "deep"}:
            profile = "balanced"

        max_pages_value = self.max_pages_var.get().strip()
        max_pages = None
        if max_pages_value:
            max_pages = self._parse_int(max_pages_value, "Max Pages", 1)

        cookies_path = self.cookies_var.get().strip()
        cookies = Path(cookies_path) if cookies_path else None
        if cookies and not cookies.exists():
            raise ValueError(f"Cookies file not found: {cookies}")

        storage_state_path = self.storage_state_var.get().strip()
        storage_state = Path(storage_state_path) if storage_state_path else None
        if storage_state and not storage_state.exists():
            raise ValueError(f"Storage state not found: {storage_state}")
        if storage_state is None:
            storage_state = _storage_state_from_cookies_path(cookies, urls)
            if storage_state:
                self.storage_state_var.set(str(storage_state))

        try:
            gallery_args = parse_cli_args(self.gallery_args_var.get(), "gallery args")
            yt_dlp_args = parse_cli_args(self.yt_dlp_args_var.get(), "yt-dlp args")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        return PipelineConfig(
            urls=urls,
            workspace=output_root,
            metadata_dir=output_root / "_meta",
            download_root=output_root,
            run_scoped_outputs=True,
            run_label=performer,
            structured_downloads=False,
            capture_profile=profile,
            include_source_hosts=self.include_source_hosts_var.get(),
            no_download=False,
            headless=self.headless_var.get(),
            delay_ms=self._parse_int(self.delay_var.get(), "Delay", 0),
            max_pages=max_pages,
            crawl_jobs=self._parse_int(self.crawl_jobs_var.get(), "Crawl Jobs", 1),
            nav_timeout_ms=self._parse_int(self.nav_timeout_var.get(), "Navigation Timeout", 1),
            idle_timeout_ms=self._parse_int(self.idle_timeout_var.get(), "Idle Timeout", 1),
            download_workers=self._parse_int(self.download_workers_var.get(), "Download Workers", 1),
            attempts=self._parse_int(self.attempts_var.get(), "Attempts", 1),
            retry_delay=self._parse_float(self.retry_delay_var.get(), "Retry Delay", 0.0),
            storage_state=storage_state,
            cookies=cookies,
            gallery_dl_path=shutil.which("gallery-dl"),
            yt_dlp_path=shutil.which("yt-dlp"),
            gallery_args=list(gallery_args),
            yt_dlp_args=list(yt_dlp_args),
            resolve_links=self.resolve_links_var.get(),
            resolve_workers=self._parse_int(self.resolve_workers_var.get(), "Resolver Workers", 1),
            emit_resolve_progress=True,
            emit_download_progress=True,
            skip_existing_downloads=True,
            strict_url_validation=True,
        )

    def _run_pipeline(self, config: PipelineConfig) -> None:
        def on_event(event: PipelineEvent) -> None:
            mapped = self._map_pipeline_event(event)
            if mapped is not None:
                self._emit(mapped)

        def should_stop() -> bool:
            return self.stop_requested

        try:
            run_universal_pipeline(config, on_event=on_event, should_stop=should_stop)
        except PipelineError:
            # Engine already emits structured error events.
            pass
        except Exception as exc:
            self._emit({"type": "error", "message": str(exc)})
        finally:
            self._emit({"type": "done"})

    def _map_pipeline_event(self, event: PipelineEvent) -> Optional[dict[str, Any]]:
        if event.kind == "run_paths":
            return {
                "type": "run_paths",
                "workspace": str(event.data.get("workspace", "")),
                "metadata_dir": str(event.data.get("metadata_dir", "")),
                "download_root": str(event.data.get("download_root", "")),
                "capture_profile": str(event.data.get("capture_profile", "")),
            }
        if event.kind == "url_input_summary":
            return {
                "type": "url_input_summary",
                "url_count": int(event.data.get("url_count", 0)),
                "invalid_count": int(event.data.get("invalid_count", 0)),
                "duplicate_count": int(event.data.get("duplicate_count", 0)),
            }
        if event.kind == "phase":
            return {"type": "phase", "text": event.message}
        if event.kind == "crawl_update":
            return {
                "type": "crawl_update",
                "records": int(event.data.get("records", 0)),
                "url": str(event.data.get("url", "")),
                "count": int(event.data.get("count", 0)),
            }
        if event.kind == "crawl_page":
            return {"type": "crawl_page", "payload": dict(event.data)}
        if event.kind == "crawl_failure":
            return {"type": "phase", "text": event.message}
        if event.kind == "discovery":
            return {
                "type": "discovery",
                "records": int(event.data.get("records", 0)),
                "hosts": int(event.data.get("hosts", 0)),
                "unique_urls": int(event.data.get("unique_urls", 0)),
                "resolved_unique_urls": int(event.data.get("resolved_unique_urls", event.data.get("unique_urls", 0))),
                "output_root": str(event.data.get("output_root", "")),
            }
        if event.kind == "resolve_progress":
            return {"type": "resolve_progress", "payload": dict(event.data.get("payload", {}))}
        if event.kind == "resolved":
            return {
                "type": "phase",
                "text": f"Resolved {int(event.data.get('resolved_unique_urls', 0))} unique links.",
            }
        if event.kind == "download_plan":
            return {
                "type": "download_plan",
                "items": event.data.get("items", []),
                "skipped": int(event.data.get("skipped", 0)),
            }
        if event.kind == "download_progress":
            return {"type": "download_progress", "payload": dict(event.data.get("payload", {}))}
        if event.kind == "finished":
            failed = event.data.get("failed_urls") or []
            if not isinstance(failed, list):
                failed = []
            return {
                "type": "finished",
                "success": bool(event.data.get("success", False)),
                "success_count": int(event.data.get("success_count", 0)),
                "failure_count": int(event.data.get("failure_count", 0)),
                "total": int(event.data.get("total", 0)),
                "output_root": str(event.data.get("output_root", "")),
                "failed_urls": [str(u) for u in failed],
            }
        if event.kind == "error":
            return {"type": "error", "message": event.message}
        return None

    def _emit(self, event: dict[str, Any]) -> None:
        self.event_queue.put(event)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(120, self._drain_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "run_paths":
            ws = str(event.get("workspace", "")).strip()
            if ws:
                self.output_path_var.set(f"Output: {ws}")
                self._append_log_entry("system", "info", f"Run folder: {ws}")
            return
        if etype == "url_input_summary":
            invalid_count = int(event.get("invalid_count", 0))
            duplicate_count = int(event.get("duplicate_count", 0))
            details = []
            if invalid_count:
                details.append(f"{invalid_count} invalid skipped")
            if duplicate_count:
                details.append(f"{duplicate_count} duplicates skipped")
            if details:
                self.phase_var.set("Input normalized: " + ", ".join(details))
                self._append_log_entry("crawl", "info", self.phase_var.get())
            return
        if etype == "phase":
            text = str(event.get("text", ""))
            self.phase_var.set(text)
            self._append_log_entry("system", "info", text)
            self._update_stage_from_phase(text)
            return
        if etype == "crawl_page":
            payload = dict(event.get("payload", {}))
            page_event = str(payload.get("event", "")).strip().lower()
            page = int(payload.get("page", 0) or 0)
            page_url = str(payload.get("url", "")).strip()
            total_pages_raw = payload.get("total_pages")
            total_pages = int(total_pages_raw) if isinstance(total_pages_raw, int) and total_pages_raw > 0 else 0
            if page_event == "page_start":
                if total_pages:
                    self.phase_var.set(f"Scraping page {page}/{total_pages}: {self._shorten(page_url, 72)}")
                else:
                    self.phase_var.set(f"Scraping page {page}: {self._shorten(page_url, 72)}")
            elif page_event == "page_complete":
                records_on_page = int(payload.get("records_on_page", 0) or 0)
                total_records = int(payload.get("total_records", 0) or 0)
                self.crawl_var.set(f"Scraped records: {total_records} (+{records_on_page} on page {page})")
                if total_pages:
                    self.phase_var.set(f"Scraped page {page}/{total_pages}")
                else:
                    self.phase_var.set(f"Scraped page {page}")
            elif page_event == "page_error":
                message = str(payload.get("error", "Unknown navigation error"))
                self.phase_var.set(f"Page {page} failed: {message}")
                self._append_log_entry("crawl", "error", self.phase_var.get())
            self._set_stage_progress(25)
            self._set_activity_mode("indeterminate")
            return
        if etype == "crawl_update":
            records = int(event.get("records", 0))
            url = str(event.get("url", ""))
            count = int(event.get("count", 0))
            self.crawl_var.set(f"Scraped records: {records} (+{count} from {url})")
            self._set_stage_progress(25)
            self._set_activity_mode("indeterminate")
            return
        if etype == "discovery":
            hosts = int(event.get("hosts", 0))
            records = int(event.get("records", 0))
            resolved_unique = int(event.get("resolved_unique_urls", event.get("unique_urls", 0)))
            self.host_var.set(f"Hosts: {hosts}")
            self.crawl_var.set(f"Scraped records: {records}")
            self.output_path_var.set(f"Output: {event.get('output_root', '')}")
            self.phase_var.set(
                f"Discovered {event.get('unique_urls', 0)} unique links "
                f"({resolved_unique} resolved)"
            )
            self._set_stage_progress(50)
            self._set_activity_mode("indeterminate")
            self._append_log_entry("resolve", "info", self.phase_var.get())
            return
        if etype == "resolve_progress":
            payload = dict(event.get("payload", {}))
            status = str(payload.get("status", "")).strip()
            host = str(payload.get("host", "")).strip()
            url = str(payload.get("url", "")).strip()
            if status:
                self.phase_var.set(f"Resolving: {status}")
            elif host:
                self.phase_var.set(f"Resolving: {host}")
            elif url:
                self.phase_var.set(f"Resolving: {self._shorten(url, 88)}")
            self._set_stage_progress(75)
            self._set_activity_mode("indeterminate")
            if status:
                self._append_log_entry("resolve", "info", status)
            return
        if etype == "download_plan":
            items = event.get("items", [])
            self.total_downloads = len(items)
            self.completed_downloads = 0
            self.failed_downloads = 0
            self.finalized_urls.clear()
            self.total_var.set(f"0 / {self.total_downloads} complete")
            self.overall_bar.configure(value=0)
            self._set_stage_progress(75)
            self._set_activity_mode("determinate")
            skipped = int(event.get("skipped", 0))
            self.phase_var.set(
                f"Downloading {self.total_downloads} links"
                + (f" (skipped {skipped} same-host links)" if skipped else "")
            )
            self._append_log_entry("download", "info", self.phase_var.get())
            self._reset_host_stats_from_plan(items)
            return
        if etype == "download_progress":
            payload = dict(event.get("payload", {}))
            self._update_download_row(payload)
            return
        if etype == "finished":
            success = bool(event.get("success", False))
            success_count = int(event.get("success_count", 0))
            failure_count = int(event.get("failure_count", 0))
            total = int(event.get("total", 0))
            raw_failed = event.get("failed_urls") or []
            self.last_failed_urls = [str(u) for u in raw_failed] if isinstance(raw_failed, list) else []
            self.phase_var.set("Done")
            self._set_stage_progress(100)
            self._set_activity_mode("determinate")
            if total > 0:
                self.overall_bar.configure(value=100)
            if success:
                self.result_var.set(
                    f"All {total} downloads succeeded. Folder: {event.get('output_root', '')}"
                )
                self.result_label.configure(foreground=self.SUCCESS)
                self._append_log_entry("system", "info", self.result_var.get())
            else:
                self.result_var.set(
                    f"{success_count}/{total} succeeded, {failure_count} failed. Check _meta/download_results.json."
                )
                self.result_label.configure(foreground=self.DANGER)
                self._append_log_entry("system", "warning", self.result_var.get())
            return
        if etype == "error":
            self.phase_var.set("Failed")
            message = str(event.get("message", "Unknown error"))
            if self._is_access_challenge_message(message):
                self.phase_var.set("Blocked by DDoS-Guard")
                message = self._format_access_challenge_hint()
            self.result_var.set(message)
            self.result_label.configure(foreground=self.DANGER)
            self._set_activity_mode("determinate")
            self._append_log_entry("error", "error", self.result_var.get())
            return
        if etype == "done":
            self.running = False
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self._set_activity_mode("determinate")
            if "Failed" in self.phase_var.get():
                self.status_var.set("Error")
            elif self.phase_var.get() == "Done":
                self.status_var.set("Complete")
            else:
                self.status_var.set("Ready")
            self._append_log_entry("system", "info", f"Run ended with status: {self.status_var.get()}")
            return

    def _set_stage_progress(self, value: int) -> None:
        self.stage_bar.configure(value=max(0, min(100, int(value))))

    def _set_activity_mode(self, mode: str) -> None:
        current_mode = str(self.overall_bar.cget("mode"))
        if current_mode == "indeterminate":
            self.overall_bar.stop()
        if mode == "indeterminate":
            self.overall_bar.configure(mode="indeterminate", value=0, maximum=100)
            self.overall_bar.start(12)
        else:
            self.overall_bar.configure(mode="determinate", maximum=100)

    def _update_stage_from_phase(self, text: str) -> None:
        lowered = text.lower()
        if "scrap" in lowered:
            self._set_stage_progress(25)
            self._set_activity_mode("indeterminate")
            return
        if "resolv" in lowered:
            self._set_stage_progress(75)
            self._set_activity_mode("indeterminate")
            return
        if "download" in lowered:
            if "skipped" in lowered or "nothing to download" in lowered:
                self._set_stage_progress(100)
            else:
                self._set_stage_progress(75)
            self._set_activity_mode("determinate")
            return
        if "done" in lowered:
            self._set_stage_progress(100)
            self._set_activity_mode("determinate")

    def _reset_host_stats_from_plan(self, items: list[Any]) -> None:
        self.host_stats.clear()
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            host = str(item[1] or "unknown")
            stats = self.host_stats.setdefault(host, {"total": 0, "completed": 0, "failed": 0})
            stats["total"] += 1
        self._refresh_host_panel()

    def _refresh_host_panel(self) -> None:
        if not hasattr(self, "host_listbox"):
            return
        self.host_filter_keys = [None]
        total = sum(stats.get("total", 0) for stats in self.host_stats.values())
        completed = sum(stats.get("completed", 0) for stats in self.host_stats.values())
        failed = sum(stats.get("failed", 0) for stats in self.host_stats.values())

        self.host_listbox.delete(0, tk.END)
        self.host_listbox.insert(tk.END, f"All hosts  {completed}/{total}" + (f" ({failed} fail)" if failed else ""))
        self.host_listbox.itemconfig(0, fg=self.INK)

        for host in sorted(self.host_stats):
            stats = self.host_stats[host]
            row_text = (
                f"{host}  {stats.get('completed', 0)}/{stats.get('total', 0)}"
                + (f" ({stats.get('failed', 0)} fail)" if stats.get("failed", 0) else "")
            )
            index = self.host_listbox.size()
            self.host_listbox.insert(tk.END, row_text)
            self.host_listbox.itemconfig(index, fg=self._host_row_color(stats))
            self.host_filter_keys.append(host)

        if self.selected_host_filter in self.host_filter_keys:
            selected_index = self.host_filter_keys.index(self.selected_host_filter)
        else:
            selected_index = 0
            self.selected_host_filter = None
        self.host_listbox.selection_clear(0, tk.END)
        self.host_listbox.selection_set(selected_index)
        self.host_listbox.activate(selected_index)
        self._apply_host_filter()

    def _on_host_select(self, _event=None) -> None:
        if not hasattr(self, "host_listbox"):
            return
        selected = self.host_listbox.curselection()
        if not selected:
            return
        index = int(selected[0])
        if index >= len(self.host_filter_keys):
            return
        self.selected_host_filter = self.host_filter_keys[index]
        self._apply_host_filter()

    def _apply_host_filter(self) -> None:
        rows = sorted(self.download_rows.values(), key=lambda item: int(item.get("order", 0)))
        for row in rows:
            row["frame"].pack_forget()
        for row in rows:
            if self.selected_host_filter and row.get("host") != self.selected_host_filter:
                continue
            row["frame"].pack(fill=tk.X, pady=4, padx=2)
        self._on_rows_configure()

    def _host_row_color(self, stats: dict[str, int]) -> str:
        total = max(0, int(stats.get("total", 0)))
        failed = max(0, int(stats.get("failed", 0)))
        completed = max(0, int(stats.get("completed", 0)))
        if total <= 0:
            return self.MUTED
        ratio = failed / total
        if ratio > 0.20:
            return self.DANGER
        if ratio >= 0.05:
            return self.WARN
        if completed <= 0:
            return self.MUTED
        return self.SUCCESS

    def _clear_rows_widgets(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()

    def _clear_download_rows(self) -> None:
        if self.running:
            return
        self.download_rows.clear()
        self.finalized_urls.clear()
        self.total_downloads = 0
        self.completed_downloads = 0
        self.failed_downloads = 0
        self.row_order_counter = 0
        self.host_stats.clear()
        self.selected_host_filter = None
        self.total_var.set("0 / 0 complete")
        self._set_stage_progress(0)
        self._set_activity_mode("determinate")
        self.overall_bar.configure(value=0)
        self._refresh_host_panel()
        self._clear_rows_widgets()
        self.phase_var.set("Waiting to start")
        self.result_var.set("")
        self.result_label.configure(foreground=self.MUTED)

    def _ensure_row(self, url: str, host: str) -> dict[str, Any]:
        row = self.download_rows.get(url)
        if row:
            return row
        host = host or "unknown"

        frame = tk.Frame(
            self.rows_frame,
            bg=self.CARD,
            highlightthickness=1,
            highlightbackground="#2d426b",
        )
        frame.pack(fill=tk.X, pady=4, padx=2)

        title = tk.Label(
            frame,
            text=f"{host} {self._shorten(url, 96)}",
            bg=self.CARD,
            fg=self.INK,
            anchor="w",
            font=("SF Pro Text", 9, "bold"),
        )
        title.pack(fill=tk.X, padx=8, pady=(6, 2))

        phase_label = tk.Label(
            frame,
            text="queued",
            bg=self.CARD,
            fg="#8cc9ff",
            anchor="w",
            font=("SF Pro Text", 8, "bold"),
        )
        phase_label.pack(fill=tk.X, padx=8, pady=(0, 2))

        status = tk.Label(
            frame,
            text="queued",
            bg=self.ACCENT_SOFT,
            fg=self.MUTED,
            anchor="w",
            font=("SF Pro Text", 8),
            padx=8,
            pady=2,
        )
        status.pack(anchor="w", padx=8, pady=(0, 4))

        bar = ttk.Progressbar(frame, style="Download.Pending.Horizontal.TProgressbar", maximum=100, value=0)
        bar.pack(fill=tk.X, padx=8, pady=(0, 6))

        meta = tk.Label(
            frame,
            text="speed: -- | eta: -- | size: --",
            bg=self.CARD,
            fg=self.MUTED,
            anchor="w",
            font=("SF Pro Text", 8),
        )
        meta.pack(fill=tk.X, padx=8, pady=(0, 6))

        row = {
            "frame": frame,
            "title": title,
            "phase": phase_label,
            "status": status,
            "bar": bar,
            "meta": meta,
            "percent": 0.0,
            "last_ui_percent": -1.0,
            "host": host,
            "order": self.row_order_counter,
            "last_bytes_read": 0,
            "last_timestamp_ms": None,
            "speed_bps": 0.0,
        }
        self.row_order_counter += 1
        self.download_rows[url] = row
        self.host_stats.setdefault(host, {"total": 0, "completed": 0, "failed": 0})
        self._apply_host_filter()
        return row

    def _update_download_row(self, payload: dict[str, Any]) -> None:
        url = str(payload.get("url", ""))
        if not url:
            return
        host = str(payload.get("host", ""))
        row = self._ensure_row(url, host)

        current = float(row["percent"])
        next_percent = payload.get("percent")
        if isinstance(next_percent, (int, float)):
            value = max(current, min(100.0, float(next_percent)))
            row["percent"] = value
            previous_shown = float(row.get("last_ui_percent", -1.0))
            # Throttle tiny UI redraws to keep the app responsive on large queues.
            if value >= 100.0 or previous_shown < 0 or abs(value - previous_shown) >= self.DOWNLOAD_UI_MIN_DELTA:
                row["bar"].configure(value=value)
                row["last_ui_percent"] = value

        method = str(payload.get("method", ""))
        status_text = str(payload.get("status", "")).strip()
        event_name = str(payload.get("event", ""))
        attempt = payload.get("attempt")
        row_phase = self._phase_for_download_event(event_name, method)
        row["phase"].configure(text=f"phase: {row_phase}")
        glyph = {
            "queued": "●",
            "start": "●",
            "attempt": "●",
            "method": "●",
            "progress": "●",
            "retry": "↻",
            "success": "✓",
            "skipped": "↷",
            "failure": "✕",
        }.get(event_name, "●")
        prefix = f"[{method}] " if method else ""
        if attempt:
            prefix += f"(try {attempt}) "
        row["status"].configure(text=f"{glyph} {prefix}{status_text or event_name}")

        bytes_read = int(payload.get("bytes_read", 0) or 0)
        bytes_total = int(payload.get("bytes_total", 0) or 0)
        timestamp_ms = payload.get("timestamp_ms")
        previous_bytes = int(row.get("last_bytes_read", 0) or 0)
        previous_ts = row.get("last_timestamp_ms")
        if isinstance(timestamp_ms, int):
            if isinstance(previous_ts, int) and timestamp_ms > previous_ts and bytes_read >= previous_bytes:
                delta_bytes = bytes_read - previous_bytes
                delta_sec = (timestamp_ms - previous_ts) / 1000.0
                if delta_sec > 0:
                    row["speed_bps"] = float(delta_bytes) / delta_sec
            row["last_timestamp_ms"] = timestamp_ms
        if bytes_read >= previous_bytes:
            row["last_bytes_read"] = bytes_read

        speed_bps = float(row.get("speed_bps", 0.0) or 0.0)
        if speed_bps > 1 and bytes_total > 0 and bytes_read > 0:
            remaining = max(0, bytes_total - bytes_read)
            eta_text = self._format_duration(int(remaining / speed_bps))
        else:
            eta_text = "--"
        size_text = "--"
        if bytes_total > 0:
            size_text = f"{self._format_bytes(bytes_read)} / {self._format_bytes(bytes_total)}"
        elif bytes_read > 0:
            size_text = self._format_bytes(bytes_read)
        row["meta"].configure(
            text=f"speed: {self._format_speed(speed_bps)} | eta: {eta_text} | size: {size_text}"
        )

        if event_name == "success":
            row["status"].configure(fg=self.SUCCESS)
            row["bar"].configure(style="Download.Success.Horizontal.TProgressbar", value=100)
            row["percent"] = 100.0
            row["frame"].configure(highlightbackground="#14532d")
            row["phase"].configure(text="phase: complete", fg=self.SUCCESS)
            row["meta"].configure(text=f"speed: {self._format_speed(speed_bps)} | eta: 00:00 | size: done")
            self._append_log_entry("download", "info", f"Success: {self._shorten(url, 88)}")
            if url not in self.finalized_urls:
                self.finalized_urls.add(url)
                self.completed_downloads += 1
                host_stats = self.host_stats.setdefault(str(row.get("host") or "unknown"), {"total": 0, "completed": 0, "failed": 0})
                host_stats["completed"] += 1
                if host_stats["completed"] > host_stats["total"]:
                    host_stats["total"] = host_stats["completed"]
                self._refresh_host_panel()
        elif event_name == "skipped":
            row["status"].configure(fg=self.ACCENT)
            row["bar"].configure(style="Download.Success.Horizontal.TProgressbar", value=100)
            row["percent"] = 100.0
            row["frame"].configure(highlightbackground="#166534")
            row["phase"].configure(text="phase: skipped", fg=self.ACCENT)
            row["meta"].configure(text="speed: -- | eta: 00:00 | size: already downloaded")
            self._append_log_entry("download", "info", f"Skipped existing: {self._shorten(url, 88)}")
            if url not in self.finalized_urls:
                self.finalized_urls.add(url)
                self.completed_downloads += 1
                host_stats = self.host_stats.setdefault(str(row.get("host") or "unknown"), {"total": 0, "completed": 0, "failed": 0})
                host_stats["completed"] += 1
                if host_stats["completed"] > host_stats["total"]:
                    host_stats["total"] = host_stats["completed"]
                self._refresh_host_panel()
        elif event_name == "failure":
            row["status"].configure(fg=self.DANGER)
            row["bar"].configure(style="Download.Failure.Horizontal.TProgressbar", value=100)
            row["percent"] = 100.0
            row["frame"].configure(highlightbackground="#7f1d1d")
            row["phase"].configure(text="phase: failed", fg=self.DANGER)
            self._append_log_entry("download", "error", f"Failed: {self._shorten(url, 88)} ({status_text or 'unknown'})")
            if url not in self.finalized_urls:
                self.finalized_urls.add(url)
                self.completed_downloads += 1
                self.failed_downloads += 1
                host_stats = self.host_stats.setdefault(str(row.get("host") or "unknown"), {"total": 0, "completed": 0, "failed": 0})
                host_stats["completed"] += 1
                host_stats["failed"] += 1
                if host_stats["completed"] > host_stats["total"]:
                    host_stats["total"] = host_stats["completed"]
                self._refresh_host_panel()
        elif event_name in {"queued", "start", "attempt", "method", "progress", "retry"}:
            row["bar"].configure(style="Download.Pending.Horizontal.TProgressbar")
            row["status"].configure(fg=self.WARN if event_name == "retry" else self.MUTED)
            row["frame"].configure(highlightbackground="#2d426b")
            if event_name == "retry":
                row["phase"].configure(text="phase: retrying", fg=self.WARN)
                self._append_log_entry("download", "warning", f"Retrying: {self._shorten(url, 88)}")
            else:
                row["phase"].configure(text=f"phase: {row_phase}", fg="#8cc9ff")

        if self.total_downloads > 0:
            percent = (self.completed_downloads / self.total_downloads) * 100.0
            self.overall_bar.configure(value=percent)
            self.total_var.set(
                f"{self.completed_downloads} / {self.total_downloads} complete"
                + (f" ({self.failed_downloads} failed)" if self.failed_downloads else "")
            )

    def _phase_for_download_event(self, event_name: str, method: str) -> str:
        if event_name in {"queued", "start"}:
            return "queued"
        if event_name in {"attempt", "method"}:
            if method == "pipeline":
                return "resolving"
            return "fetching"
        if event_name == "progress":
            return "fetching"
        if event_name == "retry":
            return "retrying"
        if event_name == "success":
            return "complete"
        if event_name == "skipped":
            return "skipped"
        if event_name == "failure":
            return "failed"
        return "running"

    def _format_bytes(self, value: int) -> str:
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(max(0, value))
        idx = 0
        while amount >= 1024.0 and idx < len(units) - 1:
            amount /= 1024.0
            idx += 1
        return f"{amount:.1f} {units[idx]}"

    def _format_speed(self, bps: float) -> str:
        if bps <= 1:
            return "--"
        return f"{self._format_bytes(int(bps))}/s"

    def _format_duration(self, seconds: int) -> str:
        if seconds < 0:
            return "--"
        mins, sec = divmod(seconds, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{sec:02d}"
        return f"{mins:02d}:{sec:02d}"

    def _append_log_entry(self, stage: str, level: str, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        entry = {
            "ts": datetime.now().strftime("%I:%M %p").lstrip("0"),
            "stage": stage.strip().lower() or "system",
            "level": level.strip().lower() or "info",
            "message": text,
        }
        if self.log_entries and self.log_entries[-1]["message"] == entry["message"] and self.log_entries[-1]["stage"] == entry["stage"]:
            return
        self.log_entries.append(entry)
        if len(self.log_entries) > self.LOG_MAX_ENTRIES:
            self.log_entries = self.log_entries[-self.LOG_MAX_ENTRIES :]
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        if not hasattr(self, "log_text"):
            return
        stage_filter = self.log_filter_var.get().strip().lower()
        needle = self.log_search_var.get().strip().lower()
        lines: list[str] = []
        for entry in self.log_entries:
            stage = str(entry.get("stage", "system"))
            level = str(entry.get("level", "info"))
            if stage_filter and stage_filter != "all":
                if stage != stage_filter and level != stage_filter:
                    continue
            level_label = {
                "info": "update",
                "warning": "watch",
                "error": "issue",
            }.get(level, level)
            stage_label = stage.replace("_", " ").title()
            line = f"{entry.get('ts', '--:--')}  ·  {stage_label} ({level_label})\n{entry.get('message', '')}"
            if needle and needle not in line.lower():
                continue
            lines.append(line)
        rendered = "\n".join(lines)
        if rendered == self.log_visible_cache:
            return
        self.log_visible_cache = rendered
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", rendered)
        self.log_text.configure(state=tk.DISABLED)
        if self.log_follow_var.get():
            self.log_text.see(tk.END)

    def _copy_visible_logs(self) -> None:
        text = self.log_visible_cache.strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._append_log_entry("system", "info", "Copied visible logs to clipboard.")

    def _export_logs(self) -> None:
        initial = f"simpscrape-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        path = filedialog.asksaveasfilename(
            title="Export logs",
            initialfile=initial,
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_visible_cache, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Export logs failed", str(exc))
            return
        self._append_log_entry("system", "info", f"Exported logs to {path}")

    def _shorten(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "..."

    def _is_access_challenge_message(self, message: str) -> bool:
        lowered = str(message or "").lower()
        return "access challenge detected" in lowered or "ddos-guard" in lowered or "browser check" in lowered

    def _format_access_challenge_hint(self, url: str = "") -> str:
        target = self._shorten(url or "simpcity.cr", 72)
        return (
            f"DDoS-Guard blocked {target}. Open https://simpcity.cr/ in a visible browser, "
            f"wait for the check to clear, then refresh simpcity-cr-state.json."
        )

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Run in progress",
                "Downloads are still running in the background.\nClose anyway?",
            ):
                return
        self._save_settings()
        self.root.destroy()


def _sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] if cleaned else "performer"


def _infer_performer_name(records: list[dict[str, Any]], first_url: str) -> str:
    title = ""
    for record in records:
        value = str(record.get("title") or "").strip()
        if value:
            title = value
            break

    if title:
        if "|" in title:
            parts = [part.strip() for part in title.split("|") if part.strip()]
            if parts:
                return parts[-1]
        if " - " in title:
            parts = [part.strip() for part in title.split(" - ") if part.strip()]
            if parts:
                return parts[0]
        return title

    if first_url:
        parsed = first_url.rstrip("/").split("/")[-1]
        parsed = re.sub(r"\.\d+$", "", parsed)
        parsed = parsed.replace("-", " ").replace("_", " ").strip()
        if parsed:
            return parsed
    return "performer"


def main() -> None:
    root = tk.Tk()
    UniversalGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
