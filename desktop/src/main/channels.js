"use strict";

/** IPC channel names shared between main and preload. Keep in one place. */
module.exports = {
  OPEN_PROJECT_DIALOG: "project:open-dialog",
  PROBE_MEDIA: "media:probe",
  GENERATE_PROXY: "media:generate-proxy",
  PROXY_PROGRESS: "media:proxy-progress",
  TO_FILE_URL: "media:to-file-url",
  GENERATE_REELS: "pipeline:generate-reels",
  SELECT_REELS_ONLY: "pipeline:select-reels-only",
  PIPELINE_EVENT: "pipeline:event",
  PICK_AUDIO: "media:pick-audio",
  SAVE_PROJECT: "project:save",
  LOAD_PROJECT: "project:load",
  PICK_EXPORT_DIR: "export:pick-dir",
  EXPORT_REELS: "export:run",
  EXPORT_EVENT: "export:event",

  PICK_REFERENCE_AUDIO: "cameras:pick-reference-audio",
  ADD_CAMERA_DIALOG: "cameras:add-camera-dialog",
  SYNC_CAMERAS: "cameras:sync",
  SYNC_EVENT: "cameras:sync-event",
};
