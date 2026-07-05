"use strict";

/** Backend base URL. Override with ISTV_BACKEND_URL; defaults to local dev. */
const BACKEND_URL = (process.env.ISTV_BACKEND_URL || "http://127.0.0.1:8722").replace(/\/$/, "");

module.exports = { BACKEND_URL };
