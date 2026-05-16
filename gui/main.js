const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

let mainWindow;
let pythonProcess;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 900,
    minWidth: 1040,
    minHeight: 720,
    titleBarStyle: "hiddenInset",
    vibrancy: "under-window",
    backgroundColor: "#00000000",
    transparent: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("closed", () => {
    mainWindow = null;
    killPython();
  });
}

function findPython() {
  const root = path.resolve(__dirname, "..");
  const venvPython = path.join(root, ".venv", "bin", "python");
  try {
    require("fs").accessSync(venvPython);
    return venvPython;
  } catch {
    return "python3";
  }
}

const DEBUG_BRIDGE = process.env.SIMPSCRAPE_DEBUG === "1";

function spawnPython(config) {
  killPython();
  const root = path.resolve(__dirname, "..");
  const pythonBin = findPython();
  if (DEBUG_BRIDGE) {
    console.error(`[bridge] spawning ${pythonBin} -u gui_bridge.py (cwd=${root})`);
  }
  pythonProcess = spawn(
    pythonBin,
    ["-u", path.join(root, "gui_bridge.py")],
    {
      cwd: root,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    }
  );
  if (DEBUG_BRIDGE) {
    console.error(`[bridge] pid=${pythonProcess.pid}`);
  }

  let buffer = "";
  let parseFailures = 0;
  let parseSuccesses = 0;
  pythonProcess.stdout.on("data", (data) => {
    buffer += data.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        parseSuccesses++;
        if (DEBUG_BRIDGE) {
          const preview = JSON.stringify(msg).slice(0, 120);
          console.error(`[bridge] ev #${parseSuccesses} type=${msg.type} stage=${msg.stage || "-"} ${preview}`);
        }
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("pipeline-event", msg);
        }
      } catch (err) {
        parseFailures++;
        if (DEBUG_BRIDGE) {
          console.error(`[bridge] parse FAIL #${parseFailures}: ${String(err).slice(0, 80)} | line[0..120]=${line.slice(0, 120)}`);
        }
      }
    }
  });

  pythonProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (DEBUG_BRIDGE && text) {
      console.error(`[bridge stderr] ${text.slice(0, 400)}`);
    }
    if (!text) return;
    // Filter Node/Electron noise that the user can't act on so the activity feed stays clean.
    const isNodeNoise =
      text.includes("DeprecationWarning") ||
      text.includes("--trace-deprecation") ||
      /\(node:\d+\)\s*\[DEP\d+\]/.test(text);
    if (isNodeNoise) return;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("pipeline-event", {
        type: "log",
        stage: "system",
        level: "error",
        message: text,
      });
    }
  });

  pythonProcess.on("exit", (code) => {
    if (DEBUG_BRIDGE) {
      console.error(`[bridge] exited code=${code} parsed=${parseSuccesses} failed=${parseFailures}`);
    }
    pythonProcess = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("pipeline-event", {
        type: "done",
        code: code,
      });
    }
  });

  pythonProcess.stdin.write(JSON.stringify(config) + "\n");
  if (DEBUG_BRIDGE) {
    console.error(`[bridge] stdin written (${JSON.stringify(config).length} bytes)`);
  }
}

function killPython() {
  if (pythonProcess) {
    try {
      pythonProcess.kill("SIGTERM");
    } catch {}
    pythonProcess = null;
  }
}

ipcMain.handle("start-pipeline", (_event, config) => {
  spawnPython(config);
  return { ok: true };
});

ipcMain.handle("stop-pipeline", () => {
  killPython();
  return { ok: true };
});

ipcMain.handle("open-path", async (_event, targetPath) => {
  if (!targetPath || typeof targetPath !== "string") return { ok: false };
  try {
    const err = await shell.openPath(targetPath);
    return { ok: !err, error: err || null };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

function existingPath(targetPath) {
  if (!targetPath || typeof targetPath !== "string") return "";
  try {
    fs.accessSync(targetPath);
    return targetPath;
  } catch {
    return "";
  }
}

const SETTINGS_PATH = path.join(os.homedir(), ".simpscrape_gui_settings.json");

function readGuiSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf8"));
  } catch {
    return {};
  }
}

function writeGuiSettings(patch) {
  const current = readGuiSettings();
  const next = { ...current, ...patch };
  try {
    fs.writeFileSync(SETTINGS_PATH, JSON.stringify(next, null, 2), "utf8");
    return true;
  } catch {
    return false;
  }
}

function defaultStorageState(root) {
  const settings = readGuiSettings();
  const configured = existingPath(settings.storage_state || settings.storageState || "");
  if (configured) return configured;

  const candidates = [
    path.join(os.homedir(), "simpcity-cr-state.json"),
    path.join(root, "simpcity-cr-state.json"),
    path.join(os.homedir(), "simpcity-state.json"),
    path.join(root, "simpcity-state.json"),
  ];
  return candidates.map(existingPath).find(Boolean) || "";
}

function defaultChromeUserDataDir() {
  const candidates = [];
  if (process.platform === "darwin") {
    const appSupport = path.join(os.homedir(), "Library", "Application Support");
    candidates.push(path.join(appSupport, "Google", "Chrome"));
    candidates.push(path.join(appSupport, "Google", "Chrome Beta"));
    candidates.push(path.join(appSupport, "Google", "Chrome Dev"));
    candidates.push(path.join(appSupport, "Google", "Chrome Canary"));
    candidates.push(path.join(appSupport, "Chromium"));
  } else if (process.platform === "win32") {
    if (process.env.LOCALAPPDATA) {
      candidates.push(path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "User Data"));
      candidates.push(path.join(process.env.LOCALAPPDATA, "Google", "Chrome Beta", "User Data"));
      candidates.push(path.join(process.env.LOCALAPPDATA, "Google", "Chrome SxS", "User Data"));
      candidates.push(path.join(process.env.LOCALAPPDATA, "Chromium", "User Data"));
    }
  } else {
    candidates.push(path.join(os.homedir(), ".config", "google-chrome"));
    candidates.push(path.join(os.homedir(), ".config", "google-chrome-beta"));
    candidates.push(path.join(os.homedir(), ".config", "google-chrome-unstable"));
    candidates.push(path.join(os.homedir(), ".config", "chromium"));
  }
  return candidates.map(existingPath).find(Boolean) || "";
}

ipcMain.handle("pick-cookies-file", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return { ok: false, canceled: true, path: "" };
  }
  const startDir =
    existingPath(readGuiSettings().cookies_path || readGuiSettings().cookiesPath || "") ||
    path.join(os.homedir(), "Downloads");
  let result;
  try {
    result = await dialog.showOpenDialog(mainWindow, {
      title: "Select cookies file",
      message: "Pick a Netscape cookies.txt or Playwright storage_state.json from your logged-in browser session.",
      defaultPath: startDir,
      properties: ["openFile"],
      filters: [
        { name: "Cookies", extensions: ["txt", "json"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
  } catch (err) {
    return { ok: false, canceled: true, path: "", error: String(err) };
  }
  if (result.canceled || !result.filePaths || !result.filePaths.length) {
    return { ok: false, canceled: true, path: "" };
  }
  const chosen = result.filePaths[0];
  writeGuiSettings({ cookies_path: chosen });
  return { ok: true, canceled: false, path: chosen };
});

ipcMain.handle("save-gui-setting", (_event, patch) => {
  if (!patch || typeof patch !== "object") return { ok: false };
  return { ok: writeGuiSettings(patch) };
});

ipcMain.handle("get-defaults", () => {
  const cpus = Math.max(1, os.cpus().length);
  const root = path.resolve(__dirname, "..");
  const settings = readGuiSettings();
  const savedCookies = existingPath(settings.cookies_path || settings.cookiesPath || "");
  const cookiesPath = savedCookies || existingPath(path.join(root, "cooks.txt"));
  return {
    downloadsDir: path.join(os.homedir(), "Downloads"),
    cookiesPath,
    cookiesPathSavedExplicitly: Boolean(savedCookies),
    storageState: defaultStorageState(root),
    chromeCdpUrl: "http://127.0.0.1:9222",
    chromeUserDataDir: defaultChromeUserDataDir(),
    cpuCount: cpus,
    crawlJobs: Math.max(1, Math.min(8, Math.floor(cpus / 2))),
    downloadWorkers: Math.max(4, Math.min(16, cpus)),
    resolveWorkers: Math.max(6, Math.min(24, cpus * 2)),
  };
});

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  killPython();
  app.quit();
});
