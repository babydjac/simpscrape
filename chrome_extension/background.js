const HOST_NAME = "com.simpscrape.auth_bridge";
const TRACKED_ORIGINS_KEY = "trackedOrigins";
const STATUS_BY_ORIGIN_KEY = "statusByOrigin";
const LAST_HOST_STATUS_KEY = "lastHostStatus";
const syncTimers = new Map();

function normalizeOrigin(raw) {
  const value = String(raw || "").trim();
  if (!value) {
    throw new Error("Origin is required.");
  }
  const candidate = value.includes("://") ? value : `https://${value}`;
  const parsed = new URL(candidate);
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error(`Unsupported origin scheme: ${parsed.protocol}`);
  }
  const port = parsed.port ? `:${parsed.port}` : "";
  return `${parsed.protocol}//${parsed.hostname.toLowerCase()}${port}`;
}

function originPattern(origin) {
  const parsed = new URL(normalizeOrigin(origin));
  return `${parsed.protocol}//${parsed.host}/*`;
}

function sameSiteMessage(error) {
  if (chrome.runtime.lastError?.message) {
    return chrome.runtime.lastError.message;
  }
  return error?.message || String(error || "Unknown error");
}

async function getTrackedOrigins() {
  const payload = await chrome.storage.local.get(TRACKED_ORIGINS_KEY);
  const origins = payload[TRACKED_ORIGINS_KEY];
  return Array.isArray(origins) ? origins : [];
}

async function setTrackedOrigins(origins) {
  const unique = [...new Set(origins.map((origin) => normalizeOrigin(origin)))].sort();
  await chrome.storage.local.set({ [TRACKED_ORIGINS_KEY]: unique });
  return unique;
}

async function getStatusByOrigin() {
  const payload = await chrome.storage.local.get(STATUS_BY_ORIGIN_KEY);
  const status = payload[STATUS_BY_ORIGIN_KEY];
  return status && typeof status === "object" ? status : {};
}

async function setStatus(origin, patch) {
  const normalized = normalizeOrigin(origin);
  const status = await getStatusByOrigin();
  status[normalized] = {
    ...(status[normalized] || {}),
    ...patch,
    origin: normalized,
  };
  await chrome.storage.local.set({ [STATUS_BY_ORIGIN_KEY]: status });
  return status[normalized];
}

async function clearStatus(origin) {
  const normalized = normalizeOrigin(origin);
  const status = await getStatusByOrigin();
  delete status[normalized];
  await chrome.storage.local.set({ [STATUS_BY_ORIGIN_KEY]: status });
}

function cookieMatchesOrigin(cookie, origin) {
  const parsed = new URL(normalizeOrigin(origin));
  const host = parsed.hostname.toLowerCase();
  const cookieDomain = String(cookie?.domain || "").replace(/^\./, "").toLowerCase();
  if (!cookieDomain) return false;
  return cookieDomain === host || host.endsWith(`.${cookieDomain}`);
}

async function sendNativeMessage(payload) {
  try {
    const response = await chrome.runtime.sendNativeMessage(HOST_NAME, payload);
    const result = response && typeof response === "object" ? response : {};
    if (!result.ok) {
      throw new Error(String(result.error || "Native host rejected the request."));
    }
    const hostStatus = {
      ok: true,
      checkedAt: new Date().toISOString(),
      message: "Native host connected.",
      repoRoot: String(result.repoRoot || ""),
    };
    await chrome.storage.local.set({ [LAST_HOST_STATUS_KEY]: hostStatus });
    return result;
  } catch (error) {
    const message = sameSiteMessage(error);
    const hostStatus = {
      ok: false,
      checkedAt: new Date().toISOString(),
      message,
    };
    await chrome.storage.local.set({ [LAST_HOST_STATUS_KEY]: hostStatus });
    throw new Error(message);
  }
}

async function ensureOriginAccess(origin) {
  const normalized = normalizeOrigin(origin);
  const permissions = { origins: [originPattern(normalized)] };
  const hasAccess = await chrome.permissions.contains(permissions);
  if (!hasAccess) {
    throw new Error(`Chrome access has not been granted for ${normalized}`);
  }
  return true;
}

async function pingNativeHost() {
  return sendNativeMessage({ type: "ping" });
}

async function syncOrigin(origin, reason = "manual") {
  const normalized = normalizeOrigin(origin);
  await ensureOriginAccess(normalized);
  const cookies = await chrome.cookies.getAll({ url: `${normalized}/` });
  const response = await sendNativeMessage({
    type: "syncCookies",
    origin: normalized,
    cookies,
    reason,
    syncedAt: new Date().toISOString(),
  });
  await setStatus(normalized, {
    ok: true,
    syncedAt: new Date().toISOString(),
    cookieCount: Number(response.cookieCount || cookies.length || 0),
    statePath: String(response.statePath || ""),
    message: "Synced to SimpScrape.",
  });
  return response;
}

async function trackOrigin(origin) {
  const normalized = normalizeOrigin(origin);
  await ensureOriginAccess(normalized);
  const trackedOrigins = await getTrackedOrigins();
  trackedOrigins.push(normalized);
  await setTrackedOrigins(trackedOrigins);
  return syncOrigin(normalized, "track");
}

async function removeOrigin(origin) {
  const normalized = normalizeOrigin(origin);
  const trackedOrigins = await getTrackedOrigins();
  await setTrackedOrigins(trackedOrigins.filter((item) => item !== normalized));
  await clearStatus(normalized);
  try {
    await sendNativeMessage({ type: "removeOrigin", origin: normalized });
  } catch {
    return { ok: true, origin: normalized, removed: false };
  }
  return { ok: true, origin: normalized, removed: true };
}

async function syncAllTrackedOrigins(reason = "batch") {
  const trackedOrigins = await getTrackedOrigins();
  for (const origin of trackedOrigins) {
    try {
      await syncOrigin(origin, reason);
    } catch (error) {
      await setStatus(origin, {
        ok: false,
        syncedAt: new Date().toISOString(),
        message: sameSiteMessage(error),
      });
    }
  }
}

function scheduleSync(origin, reason) {
  const normalized = normalizeOrigin(origin);
  if (syncTimers.has(normalized)) {
    clearTimeout(syncTimers.get(normalized));
  }
  const handle = setTimeout(async () => {
    syncTimers.delete(normalized);
    try {
      await syncOrigin(normalized, reason);
    } catch (error) {
      await setStatus(normalized, {
        ok: false,
        syncedAt: new Date().toISOString(),
        message: sameSiteMessage(error),
      });
    }
  }, 800);
  syncTimers.set(normalized, handle);
}

chrome.cookies.onChanged.addListener(async (changeInfo) => {
  const trackedOrigins = await getTrackedOrigins();
  for (const origin of trackedOrigins) {
    if (cookieMatchesOrigin(changeInfo.cookie, origin)) {
      scheduleSync(origin, `cookie:${changeInfo.cause}`);
    }
  }
});

chrome.runtime.onInstalled.addListener(() => {
  syncAllTrackedOrigins("install").catch(() => {});
});

chrome.runtime.onStartup.addListener(() => {
  syncAllTrackedOrigins("startup").catch(() => {});
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    const type = String(message?.type || "").trim();
    if (type === "getState") {
      const [trackedOrigins, statusByOrigin, hostStatus] = await Promise.all([
        getTrackedOrigins(),
        getStatusByOrigin(),
        chrome.storage.local.get(LAST_HOST_STATUS_KEY).then((payload) => payload[LAST_HOST_STATUS_KEY] || null),
      ]);
      sendResponse({ ok: true, trackedOrigins, statusByOrigin, hostStatus });
      return;
    }
    if (type === "pingNativeHost") {
      const response = await pingNativeHost();
      sendResponse({ ok: true, response });
      return;
    }
    if (type === "trackOrigin") {
      const response = await trackOrigin(String(message.origin || ""));
      sendResponse({ ok: true, response });
      return;
    }
    if (type === "syncOrigin") {
      const response = await syncOrigin(String(message.origin || ""), "manual");
      sendResponse({ ok: true, response });
      return;
    }
    if (type === "removeOrigin") {
      const response = await removeOrigin(String(message.origin || ""));
      sendResponse({ ok: true, response });
      return;
    }
    throw new Error(`Unsupported message type: ${type || "(missing)"}`);
  })().catch((error) => {
    sendResponse({ ok: false, error: sameSiteMessage(error) });
  });
  return true;
});
