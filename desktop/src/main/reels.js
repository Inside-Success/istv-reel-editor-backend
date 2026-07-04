"use strict";

/**
 * Convert a backend analysis into the app's non-destructive reel model.
 *
 * A reel is NOT a rendered file — it is a reference into the master clip: an
 * ordered list of {startSec, endSec} spans (from the analyzer's editor_cut_sheet)
 * plus its metadata and the word timings used for subtitles. "Cut" and "extend"
 * later just move these span boundaries; nothing is rendered until export.
 */

function num(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function toReferenceReels(analysis) {
  const reels = Array.isArray(analysis && analysis.reels) ? analysis.reels : [];
  return reels.map((r, i) => {
    const sheet = Array.isArray(r.editor_cut_sheet) ? r.editor_cut_sheet : [];
    let segments = sheet
      .map((row) => ({
        startSec: num(row.start_time_seconds),
        endSec: num(row.end_time_seconds),
        role: String(row.role || "BODY").toUpperCase(),
      }))
      .filter((s) => s.endSec > s.startSec)
      .sort((a, b) => a.startSec - b.startSec);

    // Fallback to the reel's overall span if no cut sheet rows exist.
    if (!segments.length) {
      const a = num(r.start_time_seconds);
      const b = num(r.end_time_seconds, a + 30);
      segments = [{ startSec: a, endSec: b, role: "HOOK" }];
    }

    const inSec = segments[0].startSec;
    const outSec = segments[segments.length - 1].endSec;
    const durationSec = segments.reduce((t, s) => t + (s.endSec - s.startSec), 0);

    const words = Array.isArray(r.timestamped_words)
      ? r.timestamped_words.map((w) => ({
          word: String(w.word || ""),
          start: num(w.start != null ? w.start : w.time),
          end: num(w.end != null ? w.end : w.start),
        }))
      : [];

    return {
      id: num(r.id, i + 1),
      rank: num(r.rank, i + 1),
      title: String(r.title || `Reel ${i + 1}`),
      caption: String(r.caption || ""),
      seoTitle: String(r.seo_title || ""),
      hashtags: Array.isArray(r.hashtags) ? r.hashtags : [],
      contentType: String(r.content_type || ""),
      isBrandReel: Boolean(r.is_brand_reel),
      score: num(r.score),
      whyItWorks: String(r.why_it_works || r.theme || ""),
      spokenHook: String(r.spoken_hook || ""),
      segments,
      inSec,
      outSec,
      durationSec: Math.round(durationSec * 10) / 10,
      words,
      // Locked-option edit settings (defaults; edited in Phase 4).
      settings: {
        subtitles: true,
        subtitleEdits: {}, // wordIndex -> replacement text
        removeFillers: true,
        removeSilences: false,
        reframe: { cropX: 0.5, cropY: 0.5, panX: 0, panY: 0, zoom: 1 },
        music: null, // { path, volume }
      },
    };
  });
}

module.exports = { toReferenceReels };
