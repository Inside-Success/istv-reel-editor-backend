"use strict";

/**
 * Pure reel-edit model — NO DOM. Loadable both as a browser classic script
 * (attaches to window.ReelModel) and via require() in Node tests. Keeping the
 * cut/extend math and subtitle logic here makes the non-destructive editing
 * model unit-testable without a GUI.
 */
(function (root) {
  // Conservative filler set for the editor's display aid. The real, audio-level
  // filler cut happens server-side at export via the engine's cutFillersFromVideo.
  const FILLERS = new Set(["um", "uh", "umm", "uhh", "er", "erm", "ah", "hmm", "mhm", "mm"]);

  const MIN_SPAN = 0.3; // never collapse a span below this many seconds

  function normTok(w) {
    return String(w || "").toLowerCase().replace(/[^a-z']/g, "");
  }
  function isFiller(w) {
    return FILLERS.has(normTok(w));
  }
  function editedText(reel, i, fallback) {
    const e = reel.settings.subtitleEdits[i];
    return e != null ? e : fallback;
  }

  function recomputeReel(reel) {
    reel.inSec = reel.segments[0].startSec;
    reel.outSec = reel.segments[reel.segments.length - 1].endSec;
    reel.durationSec =
      Math.round(reel.segments.reduce((a, s) => a + (s.endSec - s.startSec), 0) * 10) / 10;
    return reel;
  }

  // Cut (move inward) / extend (move outward) the in-point: first span start.
  function setReelIn(reel, t) {
    const first = reel.segments[0];
    first.startSec = Math.max(0, Math.min(t, first.endSec - MIN_SPAN));
    return recomputeReel(reel);
  }
  // Cut/extend the out-point: last span end, bounded by the master duration.
  function setReelOut(reel, t, masterDur) {
    const last = reel.segments[reel.segments.length - 1];
    const hi = masterDur > 0 ? masterDur : t;
    last.endSec = Math.min(hi, Math.max(t, last.startSec + MIN_SPAN));
    return recomputeReel(reel);
  }

  // Words shown as subtitles: edits applied, fillers dropped when the toggle is
  // on, and words the editor blanked out (edited to empty/whitespace) removed.
  function visibleWords(reel) {
    return reel.words
      .map((w, pos) => {
        const key = w.index != null ? w.index : pos; // stable global index
        return {
          i: key,
          start: w.start,
          end: w.end,
          text: editedText(reel, key, w.word),
          filler: isFiller(w.word),
        };
      })
      .filter((w) => !(reel.settings.removeFillers && w.filler))
      .filter((w) => String(w.text).trim() !== "");
  }

  const api = {
    FILLERS,
    MIN_SPAN,
    normTok,
    isFiller,
    editedText,
    recomputeReel,
    setReelIn,
    setReelOut,
    visibleWords,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ReelModel = api;
})(typeof window !== "undefined" ? window : globalThis);
