"use strict";

/**
 * Resolve a working Python 3 interpreter for spawning the repo's CLI scripts.
 *
 * A bare "python" (or "python3") guess frequently fails with `spawn ... ENOENT`
 * inside a packaged/GUI-launched Electron app: Explorer-launched processes can
 * inherit a stale PATH snapshot that predates a later `pip`/Python install, and
 * on Windows the WindowsApps "App execution alias" stub shadows `python`/
 * `python3` on PATH but isn't a real interpreter (it just prints a Microsoft
 * Store prompt and exits non-zero). So instead of trusting a single guess, we
 * probe a list of candidates and verify each with `--version` before using it.
 */

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

let cached = null;

function venvCandidates() {
  return [
    path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
    path.join(REPO_ROOT, ".venv", "bin", "python"),
  ];
}

/** Per-user Python installs under %LOCALAPPDATA%\Programs\Python\Python3xx\ —
 * present even when that directory was added to PATH after this app's process
 * (or its launcher shortcut) last picked up an environment snapshot. */
function windowsUserInstallCandidates() {
  const localAppData = process.env.LOCALAPPDATA;
  if (!localAppData) return [];
  const pyRoot = path.join(localAppData, "Programs", "Python");
  try {
    return fs
      .readdirSync(pyRoot)
      .filter((d) => /^Python3(\d+)/i.test(d))
      .sort((a, b) => Number(b.match(/^Python3(\d+)/i)[1]) - Number(a.match(/^Python3(\d+)/i)[1])) // newest first
      .map((d) => path.join(pyRoot, d, "python.exe"));
  } catch (_e) {
    return [];
  }
}

function candidateList() {
  const candidates = [...venvCandidates()];
  if (process.env.PYTHON) candidates.push(process.env.PYTHON);
  if (process.platform === "win32") {
    candidates.push(...windowsUserInstallCandidates());
    // "py" is the official Windows launcher, installed to C:\Windows (always
    // on PATH) rather than a location that depends on per-user PATH edits —
    // a much more reliable bare-name guess than "python"/"python3" here.
    candidates.push("py", "python3", "python");
  } else {
    candidates.push("python3", "python");
  }
  return candidates;
}

/** True if `cmd --version` actually runs a real Python interpreter. */
function worksAsPython(cmd) {
  try {
    const res = spawnSync(cmd, ["--version"], { windowsHide: true, timeout: 5000 });
    return !res.error && res.status === 0;
  } catch (_e) {
    return false;
  }
}

/** Resolve and cache a working Python interpreter path/command. */
function resolvePython() {
  if (cached) return cached;
  for (const candidate of candidateList()) {
    if (!candidate) continue;
    const looksLikePath = candidate.includes(path.sep);
    if (looksLikePath && !fs.existsSync(candidate)) continue;
    if (!worksAsPython(candidate)) continue;
    cached = candidate;
    return candidate;
  }
  // Nothing verified — fall back to the historical guess so the resulting
  // spawn error is still the familiar "Is Python available?" prompt.
  return process.platform === "win32" ? "python" : "python3";
}

module.exports = { resolvePython, REPO_ROOT };
