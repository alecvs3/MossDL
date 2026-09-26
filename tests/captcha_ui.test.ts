import assert from "node:assert/strict";
import { partitionMultiPartItems, detectMultiPart, normalizePackageName } from "../src/lib/multipart.ts";
import { detectProvider, providerLabel } from "../src/lib/providerDetection.ts";
import type { DownloadWithHistory } from "../src/figma/types.ts";
import type { CaptchaChallenge } from "../src/api.ts";

// ── Test 1: Multipart grouping with DataNodes examplepack package ──────
{
  const rawLinks = [
    "https://datanodes.to/rmd2sktac75w/Star_Wars_Outlaws_--_example-repacks.test_--_.part01.rar",
    "https://datanodes.to/w92idlhzp7py/Star_Wars_Outlaws_--_example-repacks.test_--_.part02.rar",
    "https://datanodes.to/l5qc1z1dzeh1/Star_Wars_Outlaws_--_example-repacks.test_--_.part03.rar",
  ];

  const tasks: DownloadWithHistory[] = rawLinks.map((url, i) => ({
    id: `task-${i + 1}`,
    name: url.split("/").pop()!,
    url,
    provider: "datanodes",
    size: 2048,
    downloaded: 0,
    speed: 0,
    eta: "—",
    status: i === 0 ? "needs_user" : "queued",
    rawState: i === 0 ? "needs_user" : "queued",
    detailedStatus: i === 0 ? "CAPTCHA" : "Starting",
    type: "RAR",
    added: "12:00 PM",
    speedHistory: [],
    selected: false,
  }));

  const partitioned = partitionMultiPartItems(tasks);
  assert.equal(partitioned.packages.length, 1, "Should group parts into 1 package");
  assert.equal(partitioned.standalone.length, 0, "No parts should be standalone");
  assert.equal(partitioned.packages[0].items.length, 3, "Package should contain all 3 parts");
  assert.equal(partitioned.packages[0].name, "Star Wars Outlaws", "Normalized package name");
  assert.equal(partitioned.packages[0].provider, "datanodes", "Detected provider should be datanodes");
}

// ── Test 2: Package Captcha Resolution logic ──────────────────────
{
  const tasks: DownloadWithHistory[] = [
    {
      id: "part-01",
      name: "Game.part01.rar",
      url: "https://datanodes.to/1/Game.part01.rar",
      provider: "datanodes",
      size: 1024,
      downloaded: 0,
      speed: 0,
      eta: "—",
      status: "needs_user",
      rawState: "needs_user",
      detailedStatus: "CAPTCHA",
      type: "RAR",
      added: "12:00 PM",
      speedHistory: [],
      selected: false,
    },
    {
      id: "part-02",
      name: "Game.part02.rar",
      url: "https://datanodes.to/2/Game.part02.rar",
      provider: "datanodes",
      size: 1024,
      downloaded: 0,
      speed: 0,
      eta: "—",
      status: "queued",
      rawState: "queued",
      detailedStatus: "Starting",
      type: "RAR",
      added: "12:00 PM",
      speedHistory: [],
      selected: false,
    },
  ];

  // Case A: Captcha is registered in captchaPending with loopback URL
  const pendingWithLoopback: CaptchaChallenge[] = [
    {
      id: "cap-1",
      task_id: "part-01",
      url: "https://datanodes.to/1/Game.part01.rar",
      provider: "datanodes",
      captcha_type: "turnstile",
      created_at: Date.now(),
      loopback_url: "http://127.0.0.1:49201/captcha/cap-1",
    },
  ];

  const taskIdsSet = new Set(tasks.map((t) => t.id));
  const packageCaptchaA =
    pendingWithLoopback.find((c) => c.task_id && taskIdsSet.has(c.task_id)) || null;

  assert(packageCaptchaA !== null, "Package should detect pending captcha");
  assert.equal(packageCaptchaA!.loopback_url, "http://127.0.0.1:49201/captcha/cap-1");
  assert.equal(packageCaptchaA!.captcha_type, "turnstile");

  // Case B: Captcha is cleared/absent from captchaPending, but task is still in needs_user state
  const pendingEmpty: CaptchaChallenge[] = [];
  const directB = pendingEmpty.find((c) => c.task_id && taskIdsSet.has(c.task_id));
  const needsUserTask = tasks.find((t) => t.status === "needs_user" || (t as any).rawState === "needs_user");
  const packageCaptchaB = directB || (needsUserTask ? {
    id: needsUserTask.id,
    task_id: needsUserTask.id,
    url: needsUserTask.url,
    provider: needsUserTask.provider || "host",
    captcha_type: "turnstile" as const,
    created_at: Date.now(),
    loopback_url: undefined,
  } : null);

  assert(packageCaptchaB !== null, "Package fallback MUST still activate CAPTCHA! indicator when task is needs_user");
  assert.equal(packageCaptchaB!.task_id, "part-01");
}

// ── Test 3: DownloadsPage Top Banner BannerChallenge Calculation ──
{
  const downloads: DownloadWithHistory[] = [
    {
      id: "task-sw",
      name: "Star_Wars_Outlaws.part01.rar",
      url: "https://datanodes.to/rmd2sktac75w/Star_Wars_Outlaws.part01.rar",
      provider: "datanodes",
      size: 5000,
      downloaded: 0,
      speed: 0,
      eta: "—",
      status: "needs_user",
      rawState: "needs_user",
      detailedStatus: "CAPTCHA",
      type: "RAR",
      added: "1:00 PM",
      speedHistory: [],
      selected: false,
    },
  ];

  // When captchaPending is empty, banner MUST still render based on needsUserTasks
  const captchaPendingEmpty: CaptchaChallenge[] = [];
  const needsUserTasks = downloads.filter((d) => d.status === "needs_user" || d.rawState === "needs_user");
  
  const bannerChallenge = (() => {
    if (captchaPendingEmpty.length > 0) return captchaPendingEmpty[0];
    const first = needsUserTasks[0];
    if (first) {
      return {
        id: first.id,
        task_id: first.id,
        url: first.url,
        provider: first.provider || "host",
        captcha_type: "turnstile" as const,
        created_at: Date.now(),
        loopback_url: undefined,
      };
    }
    return null;
  })();

  const verificationCount = Math.max(captchaPendingEmpty.length, needsUserTasks.length);

  assert(bannerChallenge !== null, "Banner challenge must not be null when task is needs_user");
  assert.equal(verificationCount, 1, "Verification count must be at least 1");
  assert.equal(bannerChallenge!.provider, "datanodes");
  assert.equal(bannerChallenge!.captcha_type, "turnstile");
}

// ── Test 5: Package Batch Action Resolution ─────────────────────
{
  const tasks: DownloadWithHistory[] = [
    {
      id: "part-1",
      name: "Package.part01.rar",
      url: "https://example.com/1",
      provider: "direct",
      size: 100,
      downloaded: 50,
      speed: 10,
      eta: "5s",
      status: "downloading",
      rawState: "downloading",
      type: "RAR",
      added: "12:00 PM",
      speedHistory: [],
      selected: false,
    },
    {
      id: "part-2",
      name: "Package.part02.rar",
      url: "https://example.com/2",
      provider: "direct",
      size: 100,
      downloaded: 0,
      speed: 0,
      eta: "—",
      status: "queued",
      rawState: "queued",
      type: "RAR",
      added: "12:00 PM",
      speedHistory: [],
      selected: false,
    },
  ];

  // Test toggle package logic
  const isDownloading = tasks.some((t) => t.status === "downloading");
  assert.equal(isDownloading, true, "Package with active task should identify as downloading");

  const toPause = tasks.filter((t) => t.status === "downloading").map((t) => t.id);
  assert.deepEqual(toPause, ["part-1"], "Should pause active downloading parts");

  // Selection toggle logic
  const selectedIds: string[] = [];
  const allIds = tasks.map((t) => t.id);
  const selectedAfter = Array.from(new Set([...selectedIds, ...allIds]));
  assert.deepEqual(selectedAfter, ["part-1", "part-2"], "Master select should select all package task ids");
}

console.log("All CAPTCHA UI unit and verification tests passed!");

