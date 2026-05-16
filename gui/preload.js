const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  startPipeline: (config) => ipcRenderer.invoke("start-pipeline", config),
  stopPipeline: () => ipcRenderer.invoke("stop-pipeline"),
  openPath: (targetPath) => ipcRenderer.invoke("open-path", targetPath),
  getDefaults: () => ipcRenderer.invoke("get-defaults"),
  pickCookiesFile: () => ipcRenderer.invoke("pick-cookies-file"),
  saveGuiSetting: (patch) => ipcRenderer.invoke("save-gui-setting", patch),
  onPipelineEvent: (callback) => {
    ipcRenderer.on("pipeline-event", (_event, data) => callback(data));
  },
});
