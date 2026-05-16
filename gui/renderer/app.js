(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  let running = false;
  let downloadRows = {};
  let totalDownloads = 0;
  let completedDownloads = 0;
  let failedDownloads = 0;
  let logEntries = [];
  let hostFilter = "all";
  let lastOutputRoot = "";
  let lastDownloadRoot = "";
  let lastFailedUrls = [];
  let failureReasons = {};
  let logFilter = "all";
  let logChipButtons = [];
  let lastResolveLogMs = 0;
  let lastPreviewUrl = "";

  const PIPE_STAGES = ["crawl", "discover", "resolve", "download", "finalize"];
  const PREVIEW_STATE_CLASSES = ["idle", "loading", "ready", "live", "error", "warning", "done"];

  function pickNum(obj, ...keys) {
    for (const k of keys) {
      const v = obj[k];
      if (typeof v === "number" && !Number.isNaN(v)) return v;
    }
    return 0;
  }

  function findStageEl(stage) {
    return (
      document.querySelector(`.stage[data-stage="${stage}"]`) ||
      document.querySelector(`.pipe-card[data-stage="${stage}"]`)
    );
  }

  function resetPipelineCards() {
    PIPE_STAGES.forEach((s) => {
      const el = findStageEl(s);
      if (el) el.classList.remove("active", "done", "error");
      const st = document.getElementById(`pipe-${s}`);
      if (st) st.textContent = "idle";
    });
  }

  function setPipe(stage, state) {
    const el = findStageEl(stage);
    if (!el) return;
    el.classList.remove("active", "done", "error");
    const st = document.getElementById(`pipe-${stage}`);
    if (state === "active") {
      el.classList.add("active");
      if (st) st.textContent = "running";
    } else if (state === "done") {
      el.classList.add("done");
      if (st) st.textContent = "done";
    } else if (state === "error") {
      el.classList.add("error");
      if (st) st.textContent = "error";
    } else if (st) st.textContent = "idle";
  }

  function mapStageFromEvent(event) {
    const stage = (event.stage || "").toLowerCase();
    const type = (event.type || "").toLowerCase();
    if (stage === "crawl" || type.startsWith("crawl")) return "crawl";
    if (stage === "discovery" || type === "discovery") return "discover";
    if (stage === "resolve" || type === "resolved" || type === "resolve_progress") return "resolve";
    if (stage === "download" || type === "download_plan" || type === "download_progress") return "download";
    if (stage === "done" || type === "finished" || stage === "error" || type === "error") return "finalize";
    return null;
  }

  function isAccessChallengeMessage(message) {
    return /access challenge detected|ddos-guard|browser check/i.test(String(message || ""));
  }

  function formatChallengeHint(url) {
    const target = url || "simpcity.cr";
    return `DDoS-Guard blocked ${shorten(target, 72)}. Open https://simpcity.cr/ in a visible browser, wait for the check to clear, then refresh simpcity-cr-state.json.`;
  }

  function markStagesUpTo(current) {
    const order = ["crawl", "discover", "resolve", "download", "finalize"];
    const idx = order.indexOf(current);
    if (idx < 0) return;
    for (let i = 0; i < idx; i++) setPipe(order[i], "done");
    setPipe(current, "active");
  }

  // ─── URL TEXTAREA AUTO-GROW ───
  const urlsInput = $("#urls-input");
  if (urlsInput) {
    const autoGrow = () => {
      urlsInput.style.height = "auto";
      const next = Math.min(120, Math.max(34, urlsInput.scrollHeight));
      urlsInput.style.height = next + "px";
    };
    urlsInput.addEventListener("input", autoGrow);
    setTimeout(autoGrow, 0);
  }

  let cookiesPathChosenExplicitly = false;
  let cookiesPromptSkippedThisSession = false;

  window.api.getDefaults().then((defaults) => {
    $("#inp-crawl-jobs").value = defaults.crawlJobs;
    $("#inp-download-workers").value = defaults.downloadWorkers;
    $("#inp-resolve-workers").value = defaults.resolveWorkers;
    if ($("#inp-storage-state") && defaults.storageState) {
      $("#inp-storage-state").value = defaults.storageState;
    }
    if ($("#inp-cookies-path") && defaults.cookiesPath) {
      $("#inp-cookies-path").value = defaults.cookiesPath;
    }
    if ($("#inp-chrome-cdp-url") && defaults.chromeCdpUrl) {
      $("#inp-chrome-cdp-url").value = defaults.chromeCdpUrl;
    }
    if ($("#inp-chrome-user-data-dir") && defaults.chromeUserDataDir) {
      $("#inp-chrome-user-data-dir").value = defaults.chromeUserDataDir;
    }
    cookiesPathChosenExplicitly = Boolean(defaults.cookiesPathSavedExplicitly);
    if (!cookiesPathChosenExplicitly) {
      // First launch (or no remembered cookies file) — prompt up front so the
      // user can pick before they hit Run.
      openCookiesModal({ pendingRun: false });
    } else {
      syncCookiesModalPath();
    }
  });

  // ─── COOKIES MODAL ───
  let cookiesModalRunPending = false;

  function syncCookiesModalPath() {
    const value = ($("#inp-cookies-path")?.value || "").trim();
    const display = $("#cookies-modal-path");
    const cont = $("#cookies-modal-continue");
    if (display) {
      if (value) {
        display.textContent = value;
        display.classList.add("has-file");
      } else {
        display.textContent = "No file selected";
        display.classList.remove("has-file");
      }
    }
    if (cont) {
      cont.disabled = !value;
    }
  }

  function openCookiesModal(opts) {
    cookiesModalRunPending = Boolean(opts && opts.pendingRun);
    syncCookiesModalPath();
    const modal = $("#cookies-modal");
    if (modal) modal.hidden = false;
  }

  function closeCookiesModal() {
    const modal = $("#cookies-modal");
    if (modal) modal.hidden = true;
    const wasPending = cookiesModalRunPending;
    cookiesModalRunPending = false;
    return wasPending;
  }

  $("#cookies-modal-pick")?.addEventListener("click", async () => {
    const result = await window.api.pickCookiesFile();
    if (result && result.ok && result.path) {
      const input = $("#inp-cookies-path");
      if (input) input.value = result.path;
      cookiesPathChosenExplicitly = true;
      cookiesPromptSkippedThisSession = false;
      syncCookiesModalPath();
    }
  });

  $("#cookies-modal-continue")?.addEventListener("click", () => {
    const value = ($("#inp-cookies-path")?.value || "").trim();
    if (!value) return;
    cookiesPathChosenExplicitly = true;
    const wasPending = closeCookiesModal();
    if (wasPending) startRunInternal();
  });

  $("#cookies-modal-skip")?.addEventListener("click", () => {
    cookiesPromptSkippedThisSession = true;
    const wasPending = closeCookiesModal();
    if (wasPending) startRunInternal();
  });

  $("#cookies-modal-close")?.addEventListener("click", () => {
    cookiesPromptSkippedThisSession = true;
    closeCookiesModal();
  });

  $("#cookies-modal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      cookiesPromptSkippedThisSession = true;
      closeCookiesModal();
    }
  });

  function collectConfig() {
    const urlsRaw = ($("#urls-input").value || "").trim();
    const urls = urlsRaw
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && l.startsWith("http"));
    const performer =
      ($("#inp-performer").value || "").trim() ||
      inferPerformer(urls[0] || "");

    return {
      urls,
      performer,
      captureProfile: ($("#sel-profile").value || "balanced").toLowerCase(),
      delay: parseInt($("#inp-delay").value) || 250,
      maxPages: $("#inp-max-pages").value
        ? parseInt($("#inp-max-pages").value)
        : null,
      crawlJobs: parseInt($("#inp-crawl-jobs").value) || 2,
      downloadWorkers: parseInt($("#inp-download-workers").value) || 8,
      attempts: parseInt($("#inp-attempts").value) || 3,
      retryDelay: parseFloat($("#inp-retry-delay").value) || 3.0,
      resolveWorkers: parseInt($("#inp-resolve-workers").value) || 12,
      headless: $("#chk-headless").checked,
      resolveLinks: $("#chk-resolve").checked,
      includeSourceHosts: $("#chk-source-hosts").checked,
      useChrome: $("#chk-use-chrome")?.checked || false,
      chromeCdpUrl: ($("#inp-chrome-cdp-url")?.value || "").trim(),
      chromeUserDataDir: ($("#inp-chrome-user-data-dir")?.value || "").trim(),
      chromeProfileDirectory: ($("#inp-chrome-profile-directory")?.value || "").trim(),
      storageState: ($("#inp-storage-state")?.value || "").trim(),
      cookies: ($("#inp-cookies-path")?.value || "").trim(),
    };
  }

  function inferPerformer(url) {
    if (!url) return "performer";
    try {
      const parts = new URL(url).pathname.replace(/\/$/, "").split("/");
      let slug = parts[parts.length - 1] || "performer";
      slug = slug.replace(/\.\d+$/, "").replace(/[-_]/g, " ").trim();
      return slug || "performer";
    } catch {
      return "performer";
    }
  }

  function startRun() {
    const config = collectConfig();
    if (!config.urls.length) {
      $("#urls-input")?.focus();
      $$(".nav-item").forEach((b) => b.classList.remove("active"));
      const navCapture = document.querySelector(`.nav-item[data-view="capture"]`);
      if (navCapture) navCapture.classList.add("active");
      $$(".view").forEach((v) => v.classList.remove("active"));
      $("#view-capture")?.classList.add("active");
      return;
    }
    const cookiesValue = (config.cookies || "").trim();
    if (!cookiesValue && !cookiesPromptSkippedThisSession) {
      openCookiesModal({ pendingRun: true });
      return;
    }
    startRunInternal();
  }

  function startRunInternal() {
    const config = collectConfig();
    if (!config.urls.length) return;
    running = true;
    lastResolveLogMs = 0;
    downloadRows = {};
    totalDownloads = 0;
    completedDownloads = 0;
    failedDownloads = 0;
    failureReasons = {};
    lastFailedUrls = [];
    logEntries = [];
    $("#download-rows").innerHTML = "";
    $("#fail-summary").textContent = "";
    renderHosts({});
    updateStats();
    setBtnRunning(true);
    resetPipelineCards();
    resetPreview();
    setPipe("crawl", "active");
    $("#activity-feed").innerHTML = "";
    appendLog("system", "info", "Starting pipeline...");
    window.api.startPipeline(config);
  }

  function stopRun() {
    window.api.stopPipeline();
    appendLog("system", "warning", "Stop requested.");
  }

  function setBtnRunning(isRunning) {
    ["#btn-start", "#btn-start-capture"].forEach((sel) => {
      const b = $(sel);
      if (!b) return;
      if (isRunning) {
        b.textContent = "Running...";
        b.classList.add("running");
      } else {
        b.textContent = "Start Run";
        b.classList.remove("running");
      }
    });
  }

  $("#btn-start").addEventListener("click", startRun);
  $("#btn-start-capture").addEventListener("click", startRun);
  $("#btn-stop").addEventListener("click", stopRun);
  $("#btn-cookies")?.addEventListener("click", () => openCookiesModal({ pendingRun: false }));

  $("#btn-open-output").addEventListener("click", async () => {
    const p = lastOutputRoot || lastDownloadRoot;
    if (p && window.api.openPath) await window.api.openPath(p);
  });

  $("#btn-retry-failed").addEventListener("click", () => {
    if (!lastFailedUrls.length) return;
    const input = $("#urls-input");
    if (input) {
      input.value = lastFailedUrls.join("\n");
      input.dispatchEvent(new Event("input"));
      input.focus();
    }
    appendLog("system", "info", `Loaded ${lastFailedUrls.length} failed URL(s) for retry.`);
  });

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = ".txt,text/plain";
  fileInput.style.display = "none";
  document.body.appendChild(fileInput);
  $("#btn-import").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      const lines = text
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l && !l.startsWith("#"));
      $("#urls-input").value = lines.join("\n");
    };
    reader.readAsText(f);
    fileInput.value = "";
  });

  // ─── LOG DOCK ───
  logChipButtons = Array.from($$("#log-chips .chip"));
  logChipButtons.forEach((chip) => {
    chip.addEventListener("click", () => {
      logChipButtons.forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      logFilter = chip.dataset.filter || "all";
      rebuildFeed();
    });
  });

  $("#log-search").addEventListener("input", rebuildFeed);
  $("#log-follow").addEventListener("change", () => {
    if ($("#log-follow").checked) scrollFeedToEnd();
  });
  $("#log-clear").addEventListener("click", () => {
    logEntries = [];
    $("#activity-feed").innerHTML = "";
  });
  $("#log-copy").addEventListener("click", async () => {
    const text = logEntries.map((e) => `[${e.ts}] [${e.stage}] ${e.message}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      appendLog("system", "warning", "Copy failed.");
    }
  });
  $("#log-export").addEventListener("click", () => {
    const text = logEntries.map((e) => `[${e.ts}] [${e.stage}] ${e.message}`).join("\n");
    const blob = new Blob([text], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `simpscrape-activity-${Date.now()}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
  });

  $("#btn-dock-toggle")?.addEventListener("click", () => {
    $("#main-split")?.classList.toggle("dock-bottom");
  });

  $("#chk-compact")?.addEventListener("change", () => {
    const on = $("#chk-compact").checked;
    document.querySelectorAll(".feed-row, .download-row").forEach((r) =>
      r.classList.toggle("compact", on)
    );
  });

  // ─── SETTINGS MODAL ───
  const settingsModal = $("#settings-modal");
  const openSettings = () => { if (settingsModal) settingsModal.hidden = false; };
  const closeSettings = () => { if (settingsModal) settingsModal.hidden = true; };
  $("#btn-settings")?.addEventListener("click", openSettings);
  $("#settings-modal-close")?.addEventListener("click", closeSettings);
  $("#settings-modal-done")?.addEventListener("click", closeSettings);
  settingsModal?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeSettings();
  });

  // ─── RESULTS DRAWER ───
  const drawer = $("#results-drawer");
  const setDrawerOpen = (open) => {
    if (!drawer) return;
    drawer.classList.toggle("open", Boolean(open));
    const btn = $("#drawer-toggle");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  $("#drawer-toggle")?.addEventListener("click", () => {
    setDrawerOpen(!drawer?.classList.contains("open"));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (settingsModal && !settingsModal.hidden) { closeSettings(); return; }
      const cookies = $("#cookies-modal");
      if (cookies && !cookies.hidden) { cookies.hidden = true; }
    }
  });

  resetPreview();

  // ─── PIPELINE EVENTS ───
  window.api.onPipelineEvent((event) => {
    const type = event.type || "";
    const mapped = mapStageFromEvent(event);
    if (mapped && type !== "finished" && type !== "error" && type !== "done") {
      markStagesUpTo(mapped);
    }

    if (type === "run_paths") {
      lastOutputRoot = event.workspace || "";
      lastDownloadRoot = event.download_root || event.downloadRoot || "";
      const show = lastOutputRoot || lastDownloadRoot;
      $("#path-display").textContent = show || "—";
      appendLog("system", "info", `Run folder: ${show}`);
      return;
    }

    if (type === "phase") {
      $("#stat-phase").textContent = event.message || "Working...";
      appendLog("system", "info", event.message);
    } else if (type === "url_input_summary") {
      const n = pickNum(event, "url_count", "urlCount");
      appendLog("crawl", "info", `Queued ${n} URL(s) for crawl.`);
    } else if (type === "crawl_page") {
      const pe = event.event || "";
      const page = pickNum(event, "page", "pageNum");
      const total = pickNum(event, "total_pages", "totalPages") || null;
      const pageUrl = event.url || "";
      if (pe === "page_start" && page <= 1) {
        appendLog("crawl", "info", `Opening thread: ${shorten(pageUrl, 72)}`);
        updatePreviewShell("Loading first page", pageUrl, "loading", hostLabel(pageUrl));
      } else if (pe === "page_start") {
        updatePreviewShell(formatPreviewPage(page, total, "Loading"), pageUrl, "loading", hostLabel(pageUrl));
      } else if (pe === "page_preview") {
        updatePreviewFrame(event, page, total, pageUrl);
      } else if (pe === "page_complete") {
        const records = pickNum(event, "total_records", "totalRecords");
        const onPage = pickNum(event, "records_on_page", "recordsOnPage");
        $("#stat-records").textContent = String(records);
        setStageBar(25);
        if (total) {
          $("#stat-phase").textContent = `Scraped page ${page}/${total}`;
        }
        updatePreviewShell(
          total
            ? `Scraped page ${page}/${total} · ${onPage} posts`
            : `Scraped page ${page} · ${onPage} posts`,
          pageUrl,
          "ready",
          `${records} total`
        );
        appendLog(
          "crawl",
          "info",
          total
            ? `Page ${page}/${total} done — +${onPage} posts (${records} total)`
            : `Page ${page} done — +${onPage} posts (${records} total)`
        );
      } else if (pe === "page_error") {
        updatePreviewShell(formatPreviewPage(page, total, "Page error"), pageUrl, "error", "error");
        appendLog("crawl", "error", `Page ${page}: ${event.error || "navigation failed"}`);
      } else if (pe === "interstitial_dismissed") {
        const savedTo = event.storage_state_saved || event.storageStateSaved;
        const suffix = savedTo ? ` Session saved.` : "";
        appendLog("crawl", "info", `Auto-dismissed click-to-verify gate.${suffix}`);
        updatePreviewShell("Cleared consent gate", pageUrl, "ready", hostLabel(pageUrl));
      }
    } else if (type === "crawl_update") {
      const records = pickNum(event, "records", "recordCount");
      const count = pickNum(event, "count", "batchCount");
      const url = event.url || "";
      $("#stat-records").textContent = String(records);
      appendLog(
        "crawl",
        "info",
        url
          ? `+${count} record(s) from thread — ${records} total (${shorten(url, 56)})`
          : `+${count} record(s) — ${records} total`
      );
    } else if (type === "crawl_failure") {
      const message = String(event.message || "Crawl failed");
      const blocked = isAccessChallengeMessage(message);
      const previewTitle = blocked ? "Blocked by DDoS-Guard" : "Crawl stopped";
      const previewState = blocked ? "blocked" : "error";
      updatePreviewShell(previewTitle, event.url || lastPreviewUrl, "error", previewState);
      appendLog(
        "crawl",
        "error",
        blocked
          ? `${formatChallengeHint(event.url || lastPreviewUrl)} Retry with headless disabled if it persists.`
          : `${message} ${event.url ? "(" + shorten(event.url, 64) + ")" : ""}`
      );
    } else if (type === "resolve_progress") {
      const p = event.payload || event;
      const now = Date.now();
      if (now - lastResolveLogMs > 350) {
        lastResolveLogMs = now;
        const bit =
          p.status ||
          p.host ||
          (p.url ? shorten(p.url, 80) : "") ||
          "Resolving…";
        appendLog("resolve", "info", String(bit));
      }
    } else if (type === "discovery") {
      const hosts = event.hosts || 0;
      const unique = event.unique_urls || 0;
      $("#stat-hosts").textContent = hosts;
      setStageBar(50);
      setPipe("crawl", "done");
      setPipe("discover", "active");
      appendLog("resolve", "info", `Discovered ${unique} links across ${hosts} hosts`);
    } else if (type === "resolved") {
      setPipe("discover", "done");
      setPipe("resolve", "done");
      setPipe("download", "active");
      setStageBar(65);
      const ru = pickNum(event, "resolved_unique_urls", "resolvedUniqueUrls");
      const rc = pickNum(event, "resolved_count", "resolvedCount");
      appendLog("resolve", "info", `Resolved ${rc} link(s), ${ru} unique URLs.`);
    } else if (type === "download_plan") {
      const items = event.items || [];
      totalDownloads = items.length;
      completedDownloads = 0;
      failedDownloads = 0;
      failureReasons = {};
      updateStats();
      setStageBar(70);
      const hostMap = {};
      items.forEach(([url, host]) => {
        hostMap[host] = (hostMap[host] || 0) + 1;
      });
      renderHosts(hostMap);
      const drawerEl = $("#results-drawer");
      if (drawerEl && !drawerEl.classList.contains("open")) {
        drawerEl.classList.add("open");
        $("#drawer-toggle")?.setAttribute("aria-expanded", "true");
      }
      appendLog("download", "info", `Downloading ${totalDownloads} links...`);
    } else if (type === "download_progress") {
      handleDownloadProgress(event.payload || event);
    } else if (type === "finished") {
      const sc = event.successCount || event.success_count || 0;
      const fc = event.failureCount || event.failure_count || 0;
      lastFailedUrls = event.failed_urls || event.failedUrls || [];
      lastOutputRoot = event.outputRoot || event.output_root || lastOutputRoot;
      lastDownloadRoot = event.downloadRoot || event.download_root || lastDownloadRoot;
      if (lastOutputRoot) $("#path-display").textContent = lastOutputRoot;
      setPipe("download", "done");
      setPipe("finalize", fc > 0 ? "error" : "done");
      setStageBar(100);
      setOverallBar(100);
      if (fc === 0) {
        updatePreviewState("done", "complete");
        appendLog("system", "info", `Done! All ${sc} downloads succeeded.`);
      } else {
        updatePreviewState("warning", "partial");
        appendLog("system", "warning", `Done. ${sc} succeeded, ${fc} failed.`);
      }
      updateFailureSummary();
    } else if (type === "error") {
      setPipe("finalize", "error");
      updatePreviewState("error", "error");
      const message = String(event.message || "Unknown error");
      appendLog(
        "error",
        "error",
        isAccessChallengeMessage(message) ? formatChallengeHint(lastPreviewUrl || lastOutputRoot || "simpcity.cr") : message
      );
    } else if (type === "done") {
      running = false;
      setBtnRunning(false);
      appendLog("system", "info", "Pipeline subprocess finished.");
    } else if (type === "log") {
      appendLog(event.stage || "system", event.level || "info", event.message || "");
    }
  });

  function phaseToStep(phase, eventName) {
    const p = (phase || "").toLowerCase();
    const ev = (eventName || "").toLowerCase();
    if (ev === "failure") return -1;
    if (ev === "success" || ev === "skipped") return 5;
    if (p === "queued") return 0;
    if (p === "resolving" || p === "retrying") return 1;
    if (p === "fetching" || p === "running") return 2;
    if (p === "verifying") return 3;
    if (p === "complete") return 4;
    return 2;
  }

  function updateSegments(row, phase, eventName) {
    if (!row.segs || !row.segs.length) return;
    const step = phaseToStep(phase, eventName);
    if (step < 0) {
      row.segs.forEach((s) => {
        s.className = "dl-seg fail";
      });
      return;
    }
    if (step >= 5) {
      row.segs.forEach((s) => {
        s.className = "dl-seg done";
      });
      return;
    }
    row.segs.forEach((s, i) => {
      if (i < step) s.className = "dl-seg done";
      else if (i === step) s.className = "dl-seg on";
      else s.className = "dl-seg";
    });
  }

  function formatSpeedEta(payload) {
    const br = payload.bytes_read;
    const bt = payload.bytes_total;
    const parts = [];
    if (typeof br === "number" && br > 0) {
      const mb = br / (1024 * 1024);
      parts.push(`${mb.toFixed(2)} MB`);
      if (typeof bt === "number" && bt > 0) {
        const pct = Math.min(100, (br / bt) * 100);
        parts.push(`${pct.toFixed(0)}%`);
      }
    }
    if (payload.attempt) parts.push(`try ${payload.attempt}`);
    return parts.join(" · ");
  }

  function handleDownloadProgress(payload) {
    const url = payload.url || "";
    if (!url) return;
    const host = payload.host || "unknown";
    const eventName = payload.event || "";
    const method = payload.method || "";
    const attempt = payload.attempt || "";
    const reportedPercent = typeof payload.percent === "number" ? payload.percent : NaN;
    const status = payload.status || eventName;
    const phase = payload.phase || "";

    let row = downloadRows[url];
    if (!row) {
      row = createDownloadRow(url, host);
      downloadRows[url] = row;
    }

    const compact = $("#chk-compact")?.checked;
    row.el.classList.toggle("compact", Boolean(compact));

    // Backend only reports byte-accurate percent for direct-http downloads.
    // For gallery-dl / yt-dlp the bar would otherwise stay at 0% until
    // success — fall back to phase-derived progress so it advances visibly.
    const step = phaseToStep(phase, eventName);
    const phasePercent = step < 0 ? 0 : Math.min(100, step * 20);
    const inferredPercent =
      Number.isFinite(reportedPercent) && reportedPercent > 0
        ? reportedPercent
        : phasePercent;
    const currentWidth = parseFloat(row.barFill.style.width) || 0;
    row.barFill.style.width =
      Math.min(100, Math.max(currentWidth, inferredPercent)) + "%";

    updateSegments(row, phase, eventName);

    const prefix = method ? `[${method}]` : "";
    const tryStr = attempt ? `(try ${attempt})` : "";
    row.statusEl.textContent = `${prefix} ${tryStr} ${status}`.trim();
    const sub = formatSpeedEta(payload);
    row.subEl.textContent = sub;

    if (eventName === "success") {
      row.barFill.style.width = "100%";
      row.barFill.className = "dl-row-bar-fill success";
      row.statusEl.className = "dl-row-status success";
      row.statusEl.textContent = "Done";
      if (!row.finalized) {
        row.finalized = true;
        completedDownloads++;
        updateStats();
      }
    } else if (eventName === "skipped") {
      row.barFill.style.width = "100%";
      row.barFill.className = "dl-row-bar-fill success";
      row.statusEl.className = "dl-row-status skipped";
      row.statusEl.textContent = "Skipped";
      if (!row.finalized) {
        row.finalized = true;
        completedDownloads++;
        updateStats();
      }
    } else if (eventName === "failure") {
      row.barFill.style.width = "100%";
      row.barFill.className = "dl-row-bar-fill failure";
      row.statusEl.className = "dl-row-status failure";
      row.statusEl.textContent = status || "Failed";
      const reason = (status || "unknown").slice(0, 120);
      failureReasons[reason] = (failureReasons[reason] || 0) + 1;
      if (!row.finalized) {
        row.finalized = true;
        completedDownloads++;
        failedDownloads++;
        updateStats();
      }
      updateSegments(row, phase, "failure");
      appendLog("download", "error", `Failed: ${shorten(url, 80)}`);
    } else if (eventName === "retry") {
      row.statusEl.className = "dl-row-status retry";
    } else {
      row.statusEl.className = "dl-row-status pending";
    }
  }

  function createDownloadRow(url, host) {
    const container = $("#download-rows");
    const el = document.createElement("div");
    el.className = "dl-row";
    el.dataset.host = host;
    el.innerHTML = `
      <div class="dl-row-header">
        <span class="dl-row-title">${shorten(url, 72)}</span>
        <span class="dl-row-status pending">queued</span>
      </div>
      <div class="dl-segments">
        <div class="dl-seg"></div><div class="dl-seg"></div><div class="dl-seg"></div><div class="dl-seg"></div><div class="dl-seg"></div>
      </div>
      <div class="dl-row-bar"><div class="dl-row-bar-fill" style="width:0%"></div></div>
      <div class="dl-row-sub"></div>
      <div class="dl-row-meta">${host}</div>
    `;
    container.appendChild(el);
    const segs = Array.from(el.querySelectorAll(".dl-seg"));
    return {
      el,
      host,
      barFill: el.querySelector(".dl-row-bar-fill"),
      statusEl: el.querySelector(".dl-row-status"),
      subEl: el.querySelector(".dl-row-sub"),
      segs,
      finalized: false,
    };
  }

  function updateFailureSummary() {
    const el = $("#fail-summary");
    const keys = Object.keys(failureReasons);
    if (!keys.length) {
      el.textContent = "";
      return;
    }
    const parts = keys.slice(0, 5).map((k) => `${shorten(k, 48)} ×${failureReasons[k]}`);
    el.textContent = parts.join(" · ");
  }

  function renderHosts(hostMap) {
    const container = $("#hosts-list");
    container.innerHTML = '<div class="host-item active" data-host="all">All hosts</div>';
    Object.keys(hostMap)
      .sort()
      .forEach((host) => {
        const hel = document.createElement("div");
        hel.className = "host-item";
        hel.dataset.host = host;
        hel.textContent = `${host} (${hostMap[host]})`;
        container.appendChild(hel);
      });
    container.querySelectorAll(".host-item").forEach((item) => {
      item.addEventListener("click", () => {
        container.querySelectorAll(".host-item").forEach((i) => i.classList.remove("active"));
        item.classList.add("active");
        hostFilter = item.dataset.host;
        applyHostFilter();
      });
    });
  }

  function applyHostFilter() {
    document.querySelectorAll(".dl-row").forEach((row) => {
      row.style.display =
        hostFilter === "all" || row.dataset.host === hostFilter ? "" : "none";
    });
  }

  function appendLog(stage, level, message) {
    if (!message) return;
    if ($("#log-pause")?.checked) return;

    const now = Date.now();
    const last = logEntries[logEntries.length - 1];
    if (last && last.message === message && last.stage === stage && now - last.t < 2000) {
      last.repeat = (last.repeat || 1) + 1;
      const feed = $("#activity-feed");
      if (feed && feed.lastChild) {
        const msgEl = feed.lastChild.querySelector(".feed-msg");
        if (msgEl) msgEl.textContent = `${message} (×${last.repeat})`;
      }
      return;
    }

    const d = new Date();
    const ts = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    logEntries.push({ ts, t: now, stage, level, message, repeat: 1 });
    if (logEntries.length > 2500) logEntries = logEntries.slice(-2500);
    renderFeedItem({ ts, stage, level, message, repeat: 1 });
  }

  function renderFeedItem(entry) {
    const filter = logFilter.toLowerCase();
    const search = ($("#log-search")?.value || "").toLowerCase();
    if (filter !== "all" && entry.stage !== filter && entry.level !== filter) return;
    if (search && !entry.message.toLowerCase().includes(search)) return;

    const feed = $("#activity-feed");
    if (!feed) return;
    const item = document.createElement("div");
    const levelClass =
      entry.level === "error" ? "error" :
      entry.level === "warning" ? "warning" :
      "info";
    item.className = `feed-row ${levelClass}`;
    if ($("#chk-compact")?.checked) item.classList.add("compact");
    item.innerHTML = `
      <div class="feed-time">${entry.ts}</div>
      <div class="feed-stage">${escapeHtml(entry.stage || "")}</div>
      <div class="feed-msg">${escapeHtml(entry.message)}</div>
    `;
    feed.appendChild(item);
    if ($("#log-follow")?.checked) scrollFeedToEnd();
  }

  function scrollFeedToEnd() {
    const feed = $("#activity-feed");
    feed.scrollTop = feed.scrollHeight;
  }

  function rebuildFeed() {
    const feed = $("#activity-feed");
    feed.innerHTML = "";
    logEntries.forEach((e) => renderFeedItem(e));
  }

  function updateStats() {
    const text = `${completedDownloads} / ${totalDownloads}${
      failedDownloads ? ` (${failedDownloads} failed)` : ""
    }`;
    const dl = $("#stat-downloads");
    if (dl) dl.textContent = text;
    const drawerStat = $("#drawer-stat");
    if (drawerStat) drawerStat.textContent = text;
    if (totalDownloads > 0) {
      setOverallBar((completedDownloads / totalDownloads) * 100);
    }
  }

  function setStageBar(pct) {
    const el = $("#stage-bar");
    if (el) el.style.width = Math.min(100, pct) + "%";
  }
  function setOverallBar(pct) {
    const el = $("#overall-bar");
    if (el) el.style.width = Math.min(100, pct) + "%";
  }

  function resetPreview() {
    lastPreviewUrl = "";
    updatePreviewState("idle", "standby");
    updatePreviewMeta("Waiting for crawl", "No page loaded yet.");
    const img = $("#preview-image");
    const empty = $("#preview-empty");
    if (img) {
      img.removeAttribute("src");
      img.style.display = "none";
    }
    if (empty) {
      empty.textContent = "Preview frames will appear here during crawl.";
      empty.style.display = "flex";
    }
  }

  function updatePreviewFrame(event, page, total, pageUrl) {
    const imageBase64 = event.image_base64 || event.imageBase64 || "";
    if (!imageBase64) return;
    const mime = event.image_mime || event.imageMime || "image/jpeg";
    const img = $("#preview-image");
    const empty = $("#preview-empty");
    if (!img || !empty) return;
    lastPreviewUrl = pageUrl || lastPreviewUrl;
    img.src = `data:${mime};base64,${imageBase64}`;
    img.style.display = "block";
    empty.style.display = "none";
    updatePreviewShell(formatPreviewPage(page, total, "Viewing"), pageUrl, "live", hostLabel(pageUrl));
  }

  function updatePreviewShell(pageLabel, pageUrl, state, chip) {
    if (pageUrl) lastPreviewUrl = pageUrl;
    updatePreviewState(state, chip);
    updatePreviewMeta(pageLabel, pageUrl || lastPreviewUrl || "No page loaded yet.");
  }

  function updatePreviewState(state, chip) {
    const stateEl = $("#preview-state");
    const chipEl = $("#preview-chip");
    const value = (state || "idle").toLowerCase();
    if (stateEl) {
      stateEl.textContent = value;
      PREVIEW_STATE_CLASSES.forEach((c) => stateEl.classList.remove(c));
      stateEl.classList.add(value);
    }
    if (chipEl) chipEl.textContent = chip || "standby";
  }

  function updatePreviewMeta(pageLabel, pageUrl) {
    const pageEl = $("#preview-page");
    const urlEl = $("#preview-url");
    if (pageEl) pageEl.textContent = pageLabel || "Waiting for crawl";
    if (urlEl) urlEl.textContent = pageUrl || "No page loaded yet.";
  }

  function formatPreviewPage(page, total, prefix) {
    if (page > 0 && total) return `${prefix} page ${page}/${total}`;
    if (page > 0) return `${prefix} page ${page}`;
    return prefix;
  }

  function hostLabel(rawUrl) {
    if (!rawUrl) return "standby";
    try {
      return new URL(rawUrl).host;
    } catch {
      return shorten(rawUrl, 28);
    }
  }

  function shorten(text, max) {
    return text.length <= max ? text : text.slice(0, max - 1) + "\u2026";
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
})();
