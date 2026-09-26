/**
 * Client-side title scoring mirror of engine/title_scorer.py
 * Keeps rows and the intake window showing the cleanest available name.
 */

const MEDIA_EXTENSIONS = new Set([
  "mp4","mkv","avi","mov","wmv","flv","webm","mp3","flac","aac","wav","ogg","m4a",
  "rar","7z","zip","tar","gz","iso","bin","exe","msi","dmg","apk","pdf","epub","cbz","cbr",
]);

const GENERIC_WORDS = new Set([
  "download","file","archive","attachment","stream","get","fetch",
  "resource","index","temp","tmp","data","unknown","unnamed","untitled","media",
]);

const RE_PURE_DIGITS  = /^\d+$/;
const RE_HEX_HASH     = /^[0-9a-f]{16,}$/i;
const RE_UUID         = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RE_VERSION      = /\bv?\d+\.\d+(?:\.\d+)*\b|\b(19|20)\d{2}\b/;
const RE_SITE_SUFFIX  = /(?:\s*[|\-–—]\s*)(?:www\.)?[a-z0-9.-]+\.[a-z]{2,6}\s*$/i;
// A release site's tag in a file name: "Game_--_site.example_--_.part1.rar". Any domain
// between "--" separators; the last label must be letters so "--_v1.2.3" is kept.
export const RE_SITE_TAG = /[-_ ]*--[-_ ]*(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[-_ ]*--)?[-_ ]*/gi;
const RE_REPACK_TAG   = RE_SITE_TAG;

export interface TitleCandidate {
  text: string;
  source: "provider" | "header" | "url_param" | "url_path" | "page_title" | "sibling";
  score?: number;
}

function stem(text: string): string {
  const idx = text.lastIndexOf(".");
  return idx > 0 ? text.slice(0, idx) : text;
}

function ext(text: string): string {
  const idx = text.lastIndexOf(".");
  return idx > 0 ? text.slice(idx + 1).toLowerCase() : "";
}

const SOURCE_BASELINE: Record<string, number> = {
  provider: 30, header: 25, page_title: 20, url_param: 18, sibling: 15, url_path: 8,
};

export function scoreTitleCandidate(cand: TitleCandidate): number {
  const raw = cand.text.trim();
  const s = stem(raw);
  const e = ext(raw);

  let score = SOURCE_BASELINE[cand.source] ?? 5;

  if (RE_PURE_DIGITS.test(s)) score -= 45;
  else if (RE_HEX_HASH.test(s) || RE_UUID.test(s)) score -= 40;
  if (GENERIC_WORDS.has(s.toLowerCase())) score -= 35;
  if (raw.includes("%") || raw.startsWith("?")) score -= 25;

  if (MEDIA_EXTENSIONS.has(e)) score += 25;
  const letters = (s.match(/[a-zA-Z]/g) ?? []).length;
  if (s.length > 0 && letters / s.length >= 0.4) score += 20;
  if (RE_VERSION.test(raw)) score += 15;
  const words = s.split(/[\s_.-]+/);
  if (words.some((w) => w.length > 1 && w[0] === w[0].toUpperCase() && w !== w.toUpperCase())) score += 10;

  return score;
}

export function bestTitle(candidates: TitleCandidate[], fallback = ""): string {
  if (!candidates.length) return fallback;
  let best = candidates[0];
  let bestScore = scoreTitleCandidate(best);
  for (const c of candidates.slice(1)) {
    const s = scoreTitleCandidate(c);
    if (s > bestScore) { best = c; bestScore = s; }
  }
  if (bestScore < 0 && fallback) return fallback;
  return best.text || fallback;
}

export function extractUrlCandidates(url: string): TitleCandidate[] {
  const out: TitleCandidate[] = [];
  try {
    const parsed = new URL(url);
    for (const key of ["filename","file","name","title","fn","fname"]) {
      const val = parsed.searchParams.get(key);
      if (val) out.push({ text: decodeURIComponent(val).trim(), source: "url_param" });
    }
    const segments = parsed.pathname.split("/").filter(Boolean);
    const leaf = decodeURIComponent(segments[segments.length - 1] ?? "").split("?")[0];
    if (leaf && leaf.includes(".")) out.push({ text: leaf, source: "url_path" });
  } catch { /* ignore */ }
  return out;
}

/** Derive a clean package title from the common prefix across sibling filenames. */
export function bestPackageTitle(partNames: string[], providerTitle = ""): string {
  const cleaned = partNames.map((n) => {
    let b = n.replace(/[._-]?(?:part\.?\d+|\d{2,3}|r\d{2,}|z\d{2,})\.[a-z0-9]{1,5}$/i, "");
    b = b.replace(/\.[a-z0-9]{2,5}$/i, "");
    return b.trim();
  });

  let prefix = cleaned[0] ?? "";
  for (const s of cleaned.slice(1)) {
    while (prefix && !s.startsWith(prefix)) prefix = prefix.slice(0, -1);
  }
  prefix = prefix.replace(/[-_. ]+$/, "").trim();

  const candidates: TitleCandidate[] = [];
  if (prefix.length >= 4) candidates.push({ text: prefix, source: "sibling" });
  if (providerTitle)      candidates.push({ text: providerTitle, source: "provider" });
  for (const n of partNames) candidates.push(...extractUrlCandidates(n));

  const raw = bestTitle(candidates, partNames[0] ?? "Package");
  return raw
    .replace(RE_REPACK_TAG, " ")
    .replace(/_+/g, " ")
    .replace(/-{2,}/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[-_ ]+|[-_ ]+$/g, "") || "Package";
}
