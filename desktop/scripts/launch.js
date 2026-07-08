"use strict";

/**
 * Launches Electron with a clean environment.
 *
 * Some hosts (e.g. VS Code's integrated terminal / extension host) export
 * ELECTRON_RUN_AS_NODE=1, which makes the `electron` binary behave like plain
 * Node — `require('electron')` then returns a path string and the app's main
 * process crashes (ipcMain/app undefined). We strip that var before spawning so
 * `npm start` works regardless of where it's run. Cross-platform: resolves the
 * electron binary via the `electron` module, no shell assumptions.
 */

const { spawn } = require("child_process");
const electronPath = require("electron"); // absolute path to the electron binary

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const args = [".", ...process.argv.slice(2)];
const child = spawn(electronPath, args, { stdio: "inherit", env });

child.on("close", (code) => process.exit(code == null ? 0 : code));
child.on("error", (err) => {
  console.error("Failed to launch Electron:", err.message);
  process.exit(1);
});
