const currentOriginEl = document.getElementById("current-origin");
const hostStatusEl = document.getElementById("host-status");
const trackedSitesEl = document.getElementById("tracked-sites");
const manualForm = document.getElementById("manual-form");
const manualOriginEl = document.getElementById("manual-origin");
const btnTrackCurrent = document.getElementById("btn-track-current");
const btnSyncCurrent = document.getElementById("btn-sync-current");
const btnPing = document.getElementById("btn-ping");
const btnRefresh = document.getElementById("btn-refresh");

let currentOrigin = "";

function normalizeOrigin(raw) {
  const value = String(raw || "").trim();
  if (!value) {
    throw new Error("Origin is required.");
  }
  const candidate = value.includes("://") ? value : `https://${value}`;
  const parsed = new URL(candidate);
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("Only http(s) sites are supported.");
  }
  const port = parsed.port ? `:${parsed.port}` : "";
  return `${parsed.protocol}//${parsed.hostname.toLowerCase()}${port}`;
}

function originPattern(origin) {
  const parsed = new URL(normalizeOrigin(origin));
  return `${parsed.protocol}//${parsed.host}/*`;
}

function formatHostStatus(hostStatus) {
  if (!hostStatus) {
    return { text: "Native host not checked yet.", kind: "neutral" };
  }
  return {
    text: hostStatus.message || (hostStatus.ok ? "Native host connected." : "Native host unavailable."),
    kind: hostStatus.ok ? "ok" : "error",
  };
}

function setHostStatus(hostStatus) {
  const payload = formatHostStatus(hostStatus);
  hostStatusEl.textContent = payload.text;
  hostStatusEl.className = `status ${payload.kind}`;
}

function siteMeta(status) {
  if (!status) {
    return "No sync has run yet.";
  }
  if (!status.ok) {
    return status.message || "Last sync failed.";
  }
  const synced = status.syncedAt ? `Synced ${status.syncedAt}` : "Synced";
  const count = Number(status.cookieCount || 0);
  const statePath = status.statePath ? ` -> ${status.statePath}` : "";
  return `${synced} | ${count} cookies${statePath}`;
}

function renderTrackedSites(trackedOrigins, statusByOrigin) {
  if (!trackedOrigins.length) {
    trackedSitesEl.className = "site-list empty";
    trackedSitesEl.textContent = "No tracked sites yet.";
    return;
  }

  trackedSitesEl.className = "site-list";
  trackedSitesEl.innerHTML = "";
  for (const origin of trackedOrigins) {
    const status = statusByOrigin[origin];
    const item = document.createElement("div");
    item.className = "site-item";
    item.innerHTML = `
      <div class="site-header">
        <div class="site-origin">${origin}</div>
        <div class="site-actions">
          <button type="button" data-action="sync" data-origin="${origin}">Sync</button>
          <button type="button" class="ghost" data-action="remove" data-origin="${origin}">Remove</button>
        </div>
      </div>
      <div class="site-meta">${siteMeta(status)}</div>
    `;
    trackedSitesEl.appendChild(item);
  }
}

async function request(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) {
    throw new Error(String(response?.error || "Request failed."));
  }
  return response;
}

async function refreshState() {
  const [tabs, state] = await Promise.all([
    chrome.tabs.query({ active: true, currentWindow: true }),
    request({ type: "getState" }),
  ]);
  const activeTab = Array.isArray(tabs) ? tabs[0] : null;
  try {
    currentOrigin = activeTab?.url ? normalizeOrigin(activeTab.url) : "";
  } catch {
    currentOrigin = "";
  }
  currentOriginEl.textContent = currentOrigin || "No active http(s) tab";
  btnTrackCurrent.disabled = !currentOrigin;
  btnSyncCurrent.disabled = !currentOrigin;
  setHostStatus(state.hostStatus);
  renderTrackedSites(state.trackedOrigins || [], state.statusByOrigin || {});
}

async function trackOrigin(origin) {
  const granted = await chrome.permissions.request({ origins: [originPattern(origin)] });
  if (!granted) {
    throw new Error(`Chrome access was not granted for ${origin}`);
  }
  await request({ type: "trackOrigin", origin });
  await refreshState();
}

async function syncOrigin(origin) {
  await request({ type: "syncOrigin", origin });
  await refreshState();
}

async function removeOrigin(origin) {
  await request({ type: "removeOrigin", origin });
  await refreshState();
}

btnTrackCurrent.addEventListener("click", async () => {
  if (!currentOrigin) return;
  try {
    await trackOrigin(currentOrigin);
  } catch (error) {
    setHostStatus({ ok: false, message: error.message });
  }
});

btnSyncCurrent.addEventListener("click", async () => {
  if (!currentOrigin) return;
  try {
    await syncOrigin(currentOrigin);
  } catch (error) {
    setHostStatus({ ok: false, message: error.message });
  }
});

btnPing.addEventListener("click", async () => {
  try {
    const result = await request({ type: "pingNativeHost" });
    setHostStatus({
      ok: true,
      message: result.response?.repoRoot
        ? `Native host connected at ${result.response.repoRoot}`
        : "Native host connected.",
    });
  } catch (error) {
    setHostStatus({ ok: false, message: error.message });
  }
});

btnRefresh.addEventListener("click", () => {
  refreshState().catch((error) => setHostStatus({ ok: false, message: error.message }));
});

manualForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const origin = normalizeOrigin(manualOriginEl.value);
    await trackOrigin(origin);
    manualOriginEl.value = "";
  } catch (error) {
    setHostStatus({ ok: false, message: error.message });
  }
});

trackedSitesEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const origin = button.dataset.origin;
  if (!origin) return;
  try {
    if (action === "sync") {
      await syncOrigin(origin);
      return;
    }
    if (action === "remove") {
      await removeOrigin(origin);
    }
  } catch (error) {
    setHostStatus({ ok: false, message: error.message });
  }
});

refreshState().catch((error) => setHostStatus({ ok: false, message: error.message }));
