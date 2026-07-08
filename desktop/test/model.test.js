"use strict";

/** Headless tests for the non-destructive reel-edit model (no DOM needed). */

const assert = require("assert");
const M = require("../src/renderer/model");

let pass = 0;
function test(name, fn) {
  fn();
  pass++;
  console.log("  ok -", name);
}

function sampleReel() {
  return {
    id: 1,
    segments: [
      { startSec: 10, endSec: 40, role: "HOOK" },
      { startSec: 60, endSec: 80, role: "PAYOFF" },
    ],
    words: [
      { word: "Um,", start: 10.0, end: 10.3 },
      { word: "I", start: 10.3, end: 10.5 },
      { word: "left", start: 10.5, end: 10.9 },
      { word: "everything", start: 10.9, end: 11.5 },
      { word: "uh", start: 11.5, end: 11.7 },
      { word: "behind.", start: 11.7, end: 12.2 },
    ],
    settings: { subtitles: true, subtitleEdits: {}, removeFillers: true, reframe: { zoom: 1 } },
  };
}

console.log("model: recompute");
test("recompute sums spans and sets in/out", () => {
  const r = M.recomputeReel(sampleReel());
  assert.strictEqual(r.inSec, 10);
  assert.strictEqual(r.outSec, 80);
  assert.strictEqual(r.durationSec, 50); // (40-10)+(80-60)
});

console.log("model: cut/extend in-point");
test("cut in moves first start inward and shortens", () => {
  const r = sampleReel();
  M.setReelIn(r, 25); // cut: later start
  assert.strictEqual(r.segments[0].startSec, 25);
  assert.strictEqual(r.inSec, 25);
  assert.strictEqual(r.durationSec, 35);
});
test("extend in moves first start earlier (pull more from master)", () => {
  const r = sampleReel();
  M.setReelIn(r, 3);
  assert.strictEqual(r.segments[0].startSec, 3);
  assert.strictEqual(r.durationSec, 57);
});
test("in cannot pass first span end (min span enforced)", () => {
  const r = sampleReel();
  M.setReelIn(r, 999);
  assert.ok(Math.abs(r.segments[0].startSec - (40 - M.MIN_SPAN)) < 1e-9);
});
test("in cannot go below 0", () => {
  const r = sampleReel();
  M.setReelIn(r, -50);
  assert.strictEqual(r.segments[0].startSec, 0);
});

console.log("model: cut/extend out-point");
test("extend out moves last end later, clamped to master duration", () => {
  const r = sampleReel();
  M.setReelOut(r, 200, 120); // master is 120s
  assert.strictEqual(r.segments[1].endSec, 120);
  assert.strictEqual(r.outSec, 120);
});
test("cut out moves last end earlier", () => {
  const r = sampleReel();
  M.setReelOut(r, 70, 120);
  assert.strictEqual(r.segments[1].endSec, 70);
  assert.strictEqual(r.durationSec, 40); // (40-10)+(70-60)
});
test("out cannot pass last span start (min span)", () => {
  const r = sampleReel();
  M.setReelOut(r, 0, 120);
  assert.ok(Math.abs(r.segments[1].endSec - (60 + M.MIN_SPAN)) < 1e-9);
});

console.log("model: non-destructive (master words untouched)");
test("editing in/out never mutates word list", () => {
  const r = sampleReel();
  const before = JSON.stringify(r.words);
  M.setReelIn(r, 20);
  M.setReelOut(r, 75, 120);
  assert.strictEqual(JSON.stringify(r.words), before);
});

console.log("model: fillers + subtitle edits");
test("isFiller detects um/uh, not real words", () => {
  assert.ok(M.isFiller("Um,"));
  assert.ok(M.isFiller("uh"));
  assert.ok(!M.isFiller("left"));
  assert.ok(!M.isFiller("everything"));
});
test("visibleWords drops fillers when removeFillers on", () => {
  const r = sampleReel();
  const vis = M.visibleWords(r);
  assert.deepStrictEqual(vis.map((w) => w.text), ["I", "left", "everything", "behind."]);
});
test("visibleWords keeps fillers when removeFillers off", () => {
  const r = sampleReel();
  r.settings.removeFillers = false;
  assert.strictEqual(M.visibleWords(r).length, 6);
});
test("subtitle edit overrides displayed text", () => {
  const r = sampleReel();
  r.settings.subtitleEdits[2] = "departed";
  assert.strictEqual(M.editedText(r, 2, r.words[2].word), "departed");
  const vis = M.visibleWords(r);
  assert.ok(vis.some((w) => w.text === "departed"));
});

console.log(`\nALL ${pass} MODEL TESTS PASSED`);
