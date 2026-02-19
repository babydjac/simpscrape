#!/usr/bin/env python3
from __future__ import annotations

import queue
import re
import shutil
import threading
import tkinter as tk
import os
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Optional

from core.pipeline import PipelineConfig, PipelineError, PipelineEvent, parse_cli_args, run_universal_pipeline


class UniversalGui:
    BG = "#050816"
    CARD = "#0f172a"
    SURFACE = "#020617"
    INK = "#e5e7eb"
    MUTED = "#9ca3af"
    ACCENT = "#22c55e"
    ACCENT_SOFT = "#064e3b"
    WARN = "#f59e0b"
    DANGER = "#f97373"
    SUCCESS = "#4ade80"
    DOWNLOAD_UI_MIN_DELTA = 1.5

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

        self.performer_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")
        self.phase_var = tk.StringVar(value="Waiting to start")
        self.output_path_var = tk.StringVar(value="Output: ~/Downloads/<auto performer>")
        self.total_var = tk.StringVar(value="0 / 0 complete")
        self.crawl_var = tk.StringVar(value="Scraped records: 0")
        self.host_var = tk.StringVar(value="Hosts: 0")
        self.result_var = tk.StringVar(value="")

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
        self.cookies_var = tk.StringVar(value=str(Path(__file__).resolve().parent / "cooks.txt"))
        self.storage_state_var = tk.StringVar(value="")
        self.gallery_args_var = tk.StringVar(value="--no-colors")
        self.yt_dlp_args_var = tk.StringVar(value="--no-warnings --ignore-errors")
        self.resolve_links_var = tk.BooleanVar(value=True)
        self.resolve_workers_var = tk.StringVar(value=str(default_resolve_workers))

        self._build_styles()
        self._build_ui()
        self._bind_updates()
        self.root.after(120, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_styles(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("Root.TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure(
            "Title.TLabel",
            background=self.BG,
            foreground=self.INK,
            font=("SF Pro Display", 26, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=self.BG,
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
            "Chip.TLabel",
            background=self.ACCENT_SOFT,
            foreground=self.ACCENT,
            font=("SF Pro Text", 9, "bold"),
            padding=(10, 4),
        )
        style.configure(
            "Primary.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground="#0b1120",
            background=self.ACCENT,
            borderwidth=0,
            padding=(16, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#16a34a"), ("disabled", "#4b5563")],
            foreground=[("disabled", "#9ca3af")],
        )
        style.configure(
            "Secondary.TButton",
            font=("SF Pro Text", 10, "bold"),
            foreground=self.INK,
            background="#111827",
            borderwidth=1,
            relief="flat",
            padding=(14, 8),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#1f2937"), ("disabled", "#020617")],
            foreground=[("disabled", "#4b5563")],
            bordercolor=[("!disabled", "#374151")],
        )
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#020617",
            background=self.ACCENT,
            borderwidth=0,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=14,
        )
        style.configure(
            "Download.Pending.Horizontal.TProgressbar",
            troughcolor="#020617",
            background="#38bdf8",
            borderwidth=0,
            lightcolor="#38bdf8",
            darkcolor="#38bdf8",
            thickness=10,
        )
        style.configure(
            "Download.Success.Horizontal.TProgressbar",
            troughcolor="#020617",
            background=self.SUCCESS,
            borderwidth=0,
            lightcolor=self.SUCCESS,
            darkcolor=self.SUCCESS,
            thickness=10,
        )
        style.configure(
            "Download.Failure.Horizontal.TProgressbar",
            troughcolor="#020617",
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
            bordercolor="#1f2937",
            lightcolor="#1f2937",
            darkcolor="#000000",
            padding=6,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", self.ACCENT)],
            foreground=[("disabled", self.MUTED)],
        )

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, style="Root.TFrame", padding=18)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=2)
        root.columnconfigure(1, weight=3)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root, style="Root.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Universal Performer Downloader", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Scrape complete forum threads, classify every host, and track each download live.",
            style="Sub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, style="Chip.TLabel").grid(row=0, column=1, rowspan=2, sticky="e")

        control_card = ttk.Frame(root, style="Card.TFrame", padding=14)
        control_card.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))
        control_card.columnconfigure(1, weight=1)
        control_card.columnconfigure(3, weight=1)

        ttk.Label(control_card, text="Performer Folder Name", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        performer_entry = ttk.Entry(control_card, textvariable=self.performer_var)
        performer_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 10), pady=(4, 8))

        ttk.Label(control_card, text="Delay (ms)", style="Field.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Entry(control_card, textvariable=self.delay_var, width=10).grid(row=1, column=2, sticky="ew", padx=(0, 10), pady=(4, 8))

        ttk.Label(control_card, text="Max Pages (blank = all)", style="Field.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Entry(control_card, textvariable=self.max_pages_var, width=10).grid(row=1, column=3, sticky="ew", pady=(4, 8))

        ttk.Label(control_card, text="URLs (one per line)", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.urls_text = tk.Text(
            control_card,
            height=9,
            wrap=tk.WORD,
            font=("SF Pro Text", 10),
            bg=self.SURFACE,
            fg=self.INK,
            insertbackground=self.INK,
            relief=tk.FLAT,
            padx=9,
            pady=9,
            highlightthickness=1,
            highlightbackground="#d6dde6",
            highlightcolor=self.ACCENT,
        )
        self.urls_text.grid(row=3, column=0, columnspan=4, sticky="nsew", pady=(4, 10))
        control_card.rowconfigure(3, weight=1)

        ttk.Label(control_card, text="Crawl Jobs", style="Field.TLabel").grid(row=4, column=0, sticky="w")
        ttk.Entry(control_card, textvariable=self.crawl_jobs_var, width=8).grid(row=5, column=0, sticky="w", pady=(4, 8))

        ttk.Label(control_card, text="Download Workers", style="Field.TLabel").grid(row=4, column=1, sticky="w")
        ttk.Entry(control_card, textvariable=self.download_workers_var, width=8).grid(row=5, column=1, sticky="w", pady=(4, 8))

        ttk.Label(control_card, text="Attempts", style="Field.TLabel").grid(row=4, column=2, sticky="w")
        ttk.Entry(control_card, textvariable=self.attempts_var, width=8).grid(row=5, column=2, sticky="w", pady=(4, 8))

        ttk.Label(control_card, text="Retry Delay (sec)", style="Field.TLabel").grid(row=4, column=3, sticky="w")
        ttk.Entry(control_card, textvariable=self.retry_delay_var, width=8).grid(row=5, column=3, sticky="w", pady=(4, 8))

        ttk.Label(control_card, text="Cookies Path", style="Field.TLabel").grid(row=6, column=0, sticky="w")
        ttk.Entry(control_card, textvariable=self.cookies_var).grid(row=7, column=0, columnspan=2, sticky="ew", padx=(0, 10), pady=(4, 8))

        ttk.Label(control_card, text="Storage State Path", style="Field.TLabel").grid(row=6, column=2, sticky="w")
        ttk.Entry(control_card, textvariable=self.storage_state_var).grid(row=7, column=2, columnspan=2, sticky="ew", pady=(4, 8))

        ttk.Label(control_card, text="gallery-dl args", style="Field.TLabel").grid(row=8, column=0, sticky="w")
        ttk.Entry(control_card, textvariable=self.gallery_args_var).grid(row=9, column=0, columnspan=2, sticky="ew", padx=(0, 10), pady=(4, 8))

        ttk.Label(control_card, text="yt-dlp args", style="Field.TLabel").grid(row=8, column=2, sticky="w")
        ttk.Entry(control_card, textvariable=self.yt_dlp_args_var).grid(row=9, column=2, columnspan=2, sticky="ew", pady=(4, 8))

        checks = ttk.Frame(control_card, style="Card.TFrame")
        checks.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(4, 8))
        ttk.Checkbutton(checks, text="Headless Browser", variable=self.headless_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(checks, text="Download Same-Host Source Links", variable=self.include_source_hosts_var).grid(
            row=0, column=1, sticky="w", padx=(14, 0)
        )
        ttk.Checkbutton(checks, text="Use Host Resolvers", variable=self.resolve_links_var).grid(
            row=0, column=2, sticky="w", padx=(14, 0)
        )
        ttk.Label(checks, text="Resolver Workers", style="Body.TLabel").grid(row=0, column=3, sticky="w", padx=(14, 4))
        ttk.Entry(checks, textvariable=self.resolve_workers_var, width=6).grid(row=0, column=4, sticky="w")

        actions = ttk.Frame(control_card, style="Card.TFrame")
        actions.grid(row=11, column=0, columnspan=4, sticky="ew")
        actions.columnconfigure(3, weight=1)
        self.start_btn = ttk.Button(actions, text="Start Run", style="Primary.TButton", command=self._start_run)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.stop_btn = ttk.Button(actions, text="Stop", style="Secondary.TButton", command=self._stop_run, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(actions, text="Clear List", style="Secondary.TButton", command=self._clear_download_rows).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        ttk.Label(actions, textvariable=self.output_path_var, style="Body.TLabel").grid(row=0, column=3, sticky="e")

        right_col = ttk.Frame(root, style="Root.TFrame")
        right_col.grid(row=1, column=1, rowspan=2, sticky="nsew")
        right_col.rowconfigure(1, weight=1)
        right_col.columnconfigure(0, weight=1)

        summary = ttk.Frame(right_col, style="Card.TFrame", padding=14)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, textvariable=self.phase_var, style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.stage_bar = ttk.Progressbar(summary, style="Accent.Horizontal.TProgressbar", maximum=100, value=0)
        self.stage_bar.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.overall_bar = ttk.Progressbar(summary, style="Accent.Horizontal.TProgressbar", maximum=100, value=0)
        self.overall_bar.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(summary, textvariable=self.total_var, style="Body.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.crawl_var, style="Body.TLabel").grid(row=4, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.host_var, style="Body.TLabel").grid(row=5, column=0, sticky="w")
        self.result_label = ttk.Label(summary, textvariable=self.result_var, style="Body.TLabel")
        self.result_label.grid(row=6, column=0, sticky="w", pady=(3, 0))

        downloads_card = ttk.Frame(right_col, style="Card.TFrame", padding=10)
        downloads_card.grid(row=1, column=0, sticky="nsew")
        downloads_card.columnconfigure(0, weight=1)
        downloads_card.rowconfigure(1, weight=1)
        ttk.Label(downloads_card, text="Individual Downloads", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        downloads_content = ttk.Frame(downloads_card, style="Card.TFrame")
        downloads_content.grid(row=1, column=0, sticky="nsew")
        downloads_content.columnconfigure(0, weight=0)
        downloads_content.columnconfigure(1, weight=1)
        downloads_content.rowconfigure(0, weight=1)

        hosts_panel = tk.Frame(downloads_content, bg=self.CARD, highlightthickness=1, highlightbackground="#1f2937")
        hosts_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        tk.Label(
            hosts_panel,
            text="Downloads by Host",
            bg=self.CARD,
            fg=self.INK,
            anchor="w",
            font=("SF Pro Text", 9, "bold"),
        ).pack(fill=tk.X, padx=8, pady=(8, 6))
        self.host_listbox = tk.Listbox(
            hosts_panel,
            activestyle="none",
            bg=self.SURFACE,
            fg=self.INK,
            selectbackground=self.ACCENT_SOFT,
            selectforeground=self.INK,
            highlightthickness=1,
            highlightbackground="#1f2937",
            highlightcolor=self.ACCENT,
            relief=tk.FLAT,
            width=30,
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

    def _bind_updates(self) -> None:
        self.performer_var.trace_add("write", lambda *_: self._refresh_output_preview())
        self.urls_text.bind("<<Modified>>", self._on_urls_modified)

    def _on_urls_modified(self, _event=None) -> None:
        self.urls_text.edit_modified(False)
        self._refresh_output_preview()

    def _refresh_output_preview(self) -> None:
        performer = self.performer_var.get().strip()
        if not performer:
            urls = self._collect_urls()
            performer = _infer_performer_name([], urls[0] if urls else "")
        folder = _sanitize_folder_name(performer)
        self.output_path_var.set(f"Output: {self.base_download_dir / folder}")

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
        self.phase_var.set("Starting crawl...")
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
        urls = self._collect_urls()
        if not urls:
            raise ValueError("Add at least one URL.")

        performer = self.performer_var.get().strip()
        if not performer:
            performer = _infer_performer_name([], urls[0])
        performer = _sanitize_folder_name(performer)
        if not performer:
            raise ValueError("Performer name is required.")
        output_root = self.base_download_dir / performer

        max_pages_value = self.max_pages_var.get().strip()
        max_pages = None
        if max_pages_value:
            max_pages = int(max_pages_value)
            if max_pages < 1:
                raise ValueError("Max Pages must be >= 1.")

        cookies_path = self.cookies_var.get().strip()
        cookies = Path(cookies_path) if cookies_path else None
        if cookies and not cookies.exists():
            raise ValueError(f"Cookies file not found: {cookies}")

        storage_state_path = self.storage_state_var.get().strip()
        storage_state = Path(storage_state_path) if storage_state_path else None
        if storage_state and not storage_state.exists():
            raise ValueError(f"Storage state not found: {storage_state}")

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
            include_source_hosts=self.include_source_hosts_var.get(),
            no_download=False,
            headless=self.headless_var.get(),
            delay_ms=int(self.delay_var.get()),
            max_pages=max_pages,
            crawl_jobs=max(1, int(self.crawl_jobs_var.get())),
            nav_timeout_ms=max(1, int(self.nav_timeout_var.get())),
            idle_timeout_ms=max(1, int(self.idle_timeout_var.get())),
            download_workers=max(1, int(self.download_workers_var.get())),
            attempts=max(1, int(self.attempts_var.get())),
            retry_delay=max(0.0, float(self.retry_delay_var.get())),
            storage_state=storage_state,
            cookies=cookies,
            gallery_dl_path=shutil.which("gallery-dl"),
            yt_dlp_path=shutil.which("yt-dlp"),
            gallery_args=list(gallery_args),
            yt_dlp_args=list(yt_dlp_args),
            resolve_links=self.resolve_links_var.get(),
            resolve_workers=max(1, int(self.resolve_workers_var.get())),
            emit_resolve_progress=True,
            emit_download_progress=True,
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
        if event.kind == "phase":
            return {"type": "phase", "text": event.message}
        if event.kind == "crawl_update":
            return {
                "type": "crawl_update",
                "records": int(event.data.get("records", 0)),
                "url": str(event.data.get("url", "")),
                "count": int(event.data.get("count", 0)),
            }
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
            return {
                "type": "finished",
                "success": bool(event.data.get("success", False)),
                "success_count": int(event.data.get("success_count", 0)),
                "failure_count": int(event.data.get("failure_count", 0)),
                "total": int(event.data.get("total", 0)),
                "output_root": str(event.data.get("output_root", "")),
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
        if etype == "phase":
            text = str(event.get("text", ""))
            self.phase_var.set(text)
            self._update_stage_from_phase(text)
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
            else:
                self.result_var.set(
                    f"{success_count}/{total} succeeded, {failure_count} failed. Check _meta/download_results.json."
                )
                self.result_label.configure(foreground=self.DANGER)
            return
        if etype == "error":
            self.phase_var.set("Failed")
            self.result_var.set(str(event.get("message", "Unknown error")))
            self.result_label.configure(foreground=self.DANGER)
            self._set_activity_mode("determinate")
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
            highlightbackground="#1f2937",
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

        row = {
            "frame": frame,
            "title": title,
            "status": status,
            "bar": bar,
            "percent": 0.0,
            "last_ui_percent": -1.0,
            "host": host,
            "order": self.row_order_counter,
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
        glyph = {
            "queued": "●",
            "start": "●",
            "attempt": "●",
            "method": "●",
            "progress": "●",
            "retry": "↻",
            "success": "✓",
            "failure": "✕",
        }.get(event_name, "●")
        prefix = f"[{method}] " if method else ""
        if attempt:
            prefix += f"(try {attempt}) "
        row["status"].configure(text=f"{glyph} {prefix}{status_text or event_name}")

        if event_name == "success":
            row["status"].configure(fg=self.SUCCESS)
            row["bar"].configure(style="Download.Success.Horizontal.TProgressbar", value=100)
            row["percent"] = 100.0
            row["frame"].configure(highlightbackground="#14532d")
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
            row["frame"].configure(highlightbackground="#374151")

        if self.total_downloads > 0:
            percent = (self.completed_downloads / self.total_downloads) * 100.0
            self.overall_bar.configure(value=percent)
            self.total_var.set(
                f"{self.completed_downloads} / {self.total_downloads} complete"
                + (f" ({self.failed_downloads} failed)" if self.failed_downloads else "")
            )

    def _shorten(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "..."

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Run in progress",
                "Downloads are still running in the background.\nClose anyway?",
            ):
                return
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
