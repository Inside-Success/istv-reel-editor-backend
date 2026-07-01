"use strict";

const { contextBridge, ipcRenderer } = require("electron");
const C = require("../main/channels");

/**
 * Safe, minimal API exposed to the renderer. No Node globals leak through;
 * everything heavy (ffmpeg, fs, dialogs) runs in the main process.
 */
contextBridge.exposeInMainWorld("api", {
  openProjectDialog: () => ipcRenderer.invoke(C.OPEN_PROJECT_DIALOG),
  probeMedia: (filePath) => ipcRenderer.invoke(C.PROBE_MEDIA, filePath),
  toFileUrl: (filePath) => ipcRenderer.invoke(C.TO_FILE_URL, filePath),
  generateProxy: (srcPath) => ipcRenderer.invoke(C.GENERATE_PROXY, srcPath),
  onProxyProgress: (cb) => {
    const handler = (_evt, p) => cb(p);
    ipcRenderer.on(C.PROXY_PROGRESS, handler);
    return () => ipcRenderer.removeListener(C.PROXY_PROGRESS, handler);
  },

  generateReels: (srcPath, name) => ipcRenderer.invoke(C.GENERATE_REELS, srcPath, name),
  onPipelineEvent: (cb) => {
    const handler = (_evt, e) => cb(e);
    ipcRenderer.on(C.PIPELINE_EVENT, handler);
    return () => ipcRenderer.removeListener(C.PIPELINE_EVENT, handler);
  },

  pickAudio: () => ipcRenderer.invoke(C.PICK_AUDIO),
  saveProject: (project) => ipcRenderer.invoke(C.SAVE_PROJECT, project),
  loadProject: () => ipcRenderer.invoke(C.LOAD_PROJECT),

  pickExportDir: () => ipcRenderer.invoke(C.PICK_EXPORT_DIR),
  exportReels: (args) => ipcRenderer.invoke(C.EXPORT_REELS, args),
  onExportEvent: (cb) => {
    const handler = (_evt, e) => cb(e);
    ipcRenderer.on(C.EXPORT_EVENT, handler);
    return () => ipcRenderer.removeListener(C.EXPORT_EVENT, handler);
  },

  pickReferenceAudio: () => ipcRenderer.invoke(C.PICK_REFERENCE_AUDIO),
  addCameraDialog: () => ipcRenderer.invoke(C.ADD_CAMERA_DIALOG),
  syncCameras: (args) => ipcRenderer.invoke(C.SYNC_CAMERAS, args),
  onSyncEvent: (cb) => {
    const handler = (_evt, e) => cb(e);
    ipcRenderer.on(C.SYNC_EVENT, handler);
    return () => ipcRenderer.removeListener(C.SYNC_EVENT, handler);
  },
});
