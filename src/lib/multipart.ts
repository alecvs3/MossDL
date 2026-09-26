/**
 * Single source of truth for multi-part archive detection, filename normalization,
 * and generic multi-part collection partitioning across Downloads, History, and Link Grabber.
 */
import { detectProvider } from "./providerDetection.ts";
import { bestPackageTitle, RE_SITE_TAG } from "./titleScorer.ts";

export interface MultiPartInfo {
  base: string;
  part: number;
  ext: string;
}

export interface MultiPartPackage<T> {
  key: string;
  name: string;
  provider: string;
  items: T[];
}

export interface PartitionedItems<T> {
  packages: MultiPartPackage<T>[];
  standalone: T[];
}

/**
 * Extracts and sanitizes valid HTTP and HTTPS URLs from multi-line text, HTML snippets,
 * and comma/semicolon/whitespace-separated lists.
 */
export function extractUrls(text: string): string[] {
  if (!text || typeof text !== "string") return [];
  const seen = new Set<string>();
  const results: string[] = [];

  // Match href="..." or href='...'
  const hrefRegex = /href=["'](https?:\/\/[^"'>]+)["']/gi;
  let match: RegExpExecArray | null;
  while ((match = hrefRegex.exec(text)) !== null) {
    const u = match[1].trim();
    if (!seen.has(u)) {
      seen.add(u);
      results.push(u);
    }
  }

  // Match standard standalone http:// or https:// URLs
  const urlRegex = /https?:\/\/[^\s<>"',;()[\]{}|\\^`]+/gi;
  while ((match = urlRegex.exec(text)) !== null) {
    let u = match[0].trim();
    // Strip trailing punctuation often caught in sentences
    u = u.replace(/[.,:;!?)]+$/, "");
    if (u && !seen.has(u)) {
      seen.add(u);
      results.push(u);
    }
  }

  return results;
}

/**
 * Parses a filename or URL path into base name, part number, and archive extension.
 * Supports .part01.rar, .001, .r00, .z01, and standard web/archive patterns.
 */
export function detectMultiPart(rawNameOrUrl: string): MultiPartInfo | null {
  if (!rawNameOrUrl) return null;

  // Extract clean filename if a full URL or path was passed
  let filename = rawNameOrUrl.trim();
  try {
    if (filename.includes("/") || filename.includes("\\")) {
      const parts = filename.split(/[/\\]/);
      filename = parts[parts.length - 1].split("?")[0].split("#")[0] || filename;
    }
  } catch {
    // Keep original filename
  }

  // standard .part01.rar, .part1.zip, .part002.7z
  let m = filename.match(/^(.+?)[._-]part(\d+)\.(rar|7z|zip)$/i);
  if (m) return { base: m[1], part: parseInt(m[2], 10), ext: m[3].toLowerCase() };

  // split archives .001, .002, etc.
  m = filename.match(/^(.+?)\.(\d{3})$/i);
  if (m) return { base: m[1], part: parseInt(m[2], 10), ext: m[2] };

  // classic RAR volumes .r00, .r01 (.rar is part 1, .r00 is part 2)
  m = filename.match(/^(.+?)\.r(\d{2,})$/i);
  if (m) return { base: m[1], part: parseInt(m[2], 10) + 2, ext: `r${m[2]}`.toLowerCase() };

  // classic 7z / zip split volumes .z01, .z02
  m = filename.match(/^(.+?)\.z(\d{2,})$/i);
  if (m) return { base: m[1], part: parseInt(m[2], 10), ext: `z${m[2]}`.toLowerCase() };

  // trailing part designation before extension: archive_part1.rar, game.part.1.rar
  m = filename.match(/^(.+?)[._-]part[._-]?(\d+)\.([a-z0-9]+)$/i);
  if (m && ["rar", "7z", "zip", "tar", "iso"].includes(m[3].toLowerCase())) {
    return { base: m[1], part: parseInt(m[2], 10), ext: m[3].toLowerCase() };
  }

  return null;
}

/**
 * Strips release group prefixes/suffixes, site URLs, domain tags, unicode hyphens,
 * and normalizes whitespace into a human-readable folder / package name.
 */
export function normalizePackageName(base: string): string {
  if (!base) return "Package";
  let s = base.replace(/[\u2010-\u2015\u2212\ufffd]+/g, " - ");
  s = s.replace(RE_SITE_TAG, " ");
  s = s.replace(/(?:[-_ ]+)?(?:www\.)?[a-z0-9-]+\.(?:com|org|site|net|to|cr|is|ru|su|co)(?:[-_ ]+)?/gi, " ");
  s = s.replace(/[_]+/g, " ");
  s = s.replace(/[-]{2,}/g, " ");
  const cleaned = s.trim().replace(/^[-_ ]+|[-_ ]+$/g, "");
  return cleaned || base;
}

/**
 * Sanitizes a package name for use as a folder name, preventing directory traversal
 * and illegal filesystem characters.
 */
export function sanitizePackageFolderName(name: string): string {
  if (!name) return "Package";
  // Strip null bytes and illegal path characters: / \ : * ? " < > |
  let sanitized = name.replace(/[\x00/\\:*?"<>|]/g, "_");
  // Prevent directory traversal (../ or ..\)
  sanitized = sanitized.replace(/\.{2,}/g, ".");
  sanitized = sanitized.trim().replace(/^[\. ]+|[\. ]+$/g, "");
  return sanitized || "Package";
}

/**
 * Determines whether a URL or filename represents a multipart archive part,
 * returning the sanitized package subfolder name if detected.
 */
export function getMultiPartFolder(rawNameOrUrl: string): string | null {
  const info = detectMultiPart(rawNameOrUrl);
  if (!info) return null;
  const pkgName = normalizePackageName(info.base);
  return sanitizePackageFolderName(pkgName);
}

/**
 * Generic multi-part partitioner: groups items belonging to the same host & multi-part archive
 * into unified packages. Items with only 1 part are preserved as standalone.
 */
export function partitionMultiPartItems<T extends { name: string; url?: string; provider?: string }>(
  items: T[],
  sortItemsFn?: (list: T[]) => T[]
): PartitionedItems<T> {
  const sorted = sortItemsFn ? sortItemsFn(items) : items;
  const packageBuckets = new Map<string, { provider: string; baseName: string; items: T[] }>();
  const standalone: T[] = [];

  for (const item of sorted) {
    const multi = detectMultiPart(item.name) || (item.url ? detectMultiPart(item.url) : null);
    if (multi) {
      const prov = detectProvider(item.url || "", item.provider);
      const pkgName = normalizePackageName(multi.base);
      const pkgKey = `${prov}::${pkgName.toLowerCase()}`;
      const bucket = packageBuckets.get(pkgKey) || { provider: prov, baseName: pkgName, items: [] };
      bucket.items.push(item);
      packageBuckets.set(pkgKey, bucket);
    } else {
      standalone.push(item);
    }
  }

  const packages: MultiPartPackage<T>[] = [];
  for (const [pkgKey, bucket] of packageBuckets.entries()) {
    if (bucket.items.length > 1) {
      const partNames = bucket.items.map((i) => i.name);
      const cleanName = bestPackageTitle(partNames, bucket.baseName);
      packages.push({
        key: pkgKey,
        name: cleanName,
        provider: bucket.provider,
        items: bucket.items,
      });
    } else {
      standalone.push(...bucket.items);
    }
  }

  return { packages, standalone };
}
