"use strict";

const { app } = require("electron");

// Packaged builds (what gets handed to other users) talk to the hosted
// backend by default; running from source (`npm start`) still defaults to
// local dev, since that's almost always a developer iterating against a
// backend they're running themselves. Either way, ISTV_BACKEND_URL overrides.
const DEFAULT_BACKEND_URL = app.isPackaged
  ? "https://istv-reel-editor-backend.onrender.com"
  : "http://127.0.0.1:8722";

const BACKEND_URL = (process.env.ISTV_BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/$/, "");

module.exports = { BACKEND_URL };
