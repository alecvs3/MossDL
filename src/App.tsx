import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  dismissCaptureBatches,
  acknowledgeEvent,
  addLinkGrabber,
  addTask,
  browserImportCandidate,
  bulkTransferAction,
  clearCompletedTasks,
  clearLinkGrabber,
  deleteTask,
  deleteTasks,
  deleteLinkGrabber,
  deleteLinkGrabberMany,
  bulkUpdateLinks,
  enqueueArchiveExtract,
  enqueueLinkGrabber,
  getDownloadDirectory,
  getEventsSince,
  getUiSnapshot,
  importCaptureCandidate,
  listCaptureBatches,
  listCollectionPlans,
  getCollection,
  selectCollectionItems,
  enqueueCollection,
  cancelCollection,
  continueCollection,
  inspectLink,
  pickDirectory,
  putSecret,
  planMedia,
  retryLinkGrabber,
  setBandwidthRate,
  setAppAutostart,
  setClipboardWatcher,
  setTaskOptions,
  startTask,
  saveCaptchaConfig,
  configureFlareSolverr,
  openPath,
  solveCaptcha,
  skipCaptcha,
  updateLinkGrabber,
  updateUiSettings,
  type AppPage,
  type LinkGrabberEntry,
  type SettingsTab,
  type RouteProfile,
  type Task,
  type TaskAction,
  type UiSettings,
  type ProviderMetadata,
  type CaptchaChallenge,
  type CaptureBatch,
  type CaptureCandidate,
  type CaptureImportResult,
  type FileCategory,
  type CollectionPlan,
  emitLog,
  clearHistory,
  getHistory,
  windowAction,
} from "./api";
import { performTaskAction } from "./lib/transferActions";
import { applyEngineEvents, extractArchiveChoices, type ArchiveChoice } from "./lib/engineEvents";
import { isActiveStage } from "./figma/types";
import FigmaAppSurface, { type AppSettings as FigmaAppSettings } from "./figma/FigmaApp";
import { tasksToFigmaDownloads, tasksToFigmaHistory, resolveDestinationWithPackage } from "./figma/liveAdapters";
import { useSpeedHistory } from "./hooks/useSpeedHistory";
import { settingDefinition, validateSetting } from "./figma/settingsSchema";
import { PythonSetupModal } from "./figma/components/PythonSetupModal";
import { WelcomeWizard } from "./figma/components/WelcomeWizard";
import { ArchiveChoicePrompt } from "./components/ArchiveChoicePrompt";
import { checkPythonEnvironment } from "./api";
import CollectionPlanReview from "./components/CollectionPlanReview";
import { DevLogConsole } from "./components/DevLogConsole";
import { cacheAppearance, cachedAppearance } from "./figma/themes";

const isDevLogsMode = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("mode") === "dev-logs";

const emptySettings: UiSettings = {
  general: {}, network: {}, appearance: {}, notifications: {}, captcha: {}, routes: {}, accounts: {},
};

// Until the engine answers, render with the appearance saved at the last launch.
const launchSettings = (): UiSettings => ({ ...emptySettings, appearance: cachedAppearance() });

function readSetting(settings: UiSettings, section: keyof UiSettings, key: string, fallback: unknown): unknown {
  return (settings?.[section] as Record<string, unknown> | undefined)?.[key] ?? fallback;
}

function taskActionMessage(action: TaskAction): string {
  return action === "stop" ? "Transfer stopped" : action === "restart" ? "Transfer restarted" : action === "pause" ? "Transfer paused" : "Transfer resumed";
}

export default function App() {
  if (isDevLogsMode) {
    return (
      <div style={{ height: "100vh", width: "100vw", overflow: "hidden", backgroundColor: "#090d16" }}>
        <DevLogConsole standalone={true} />
      </div>
    );
  }

  const [page, setPage] = useState<AppPage>("downloads");
  const [tasks, setTasks] = useState<Task[]>([]);
  // Engine-recorded history: survives removing downloads and relaunches.
  const [historyRecords, setHistoryRecords] = useState<Task[]>([]);
  const [linkEntries, setLinkEntries] = useState<LinkGrabberEntry[]>([]);
  const [routes, setRoutes] = useState<RouteProfile[]>([]);
  const [providers, setProviders] = useState<ProviderMetadata[]>([]);
  const [captchaPending, setCaptchaPending] = useState<CaptchaChallenge[]>([]);
  const [captureBatches, setCaptureBatches] = useState<CaptureBatch[]>([]);
  const [collectionPlans, setCollectionPlans] = useState<CollectionPlan[]>([]);
  const [activeCollection, setActiveCollection] = useState<CollectionPlan | null>(null);
  const [uiSettings, setUiSettings] = useState<UiSettings>(launchSettings);
  const [destination, setDestination] = useState("downloads");
  const [message, setMessage] = useState("Connecting to transfer engine…");
  const [busy, setBusy] = useState(false);
  const [pythonSetupRequired, setPythonSetupRequired] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [archiveChoices, setArchiveChoices] = useState<ArchiveChoice[]>([]);
  const speedHistory = useSpeedHistory(tasks);

  const lastEventIdRef = useRef<number>(0);
  const destinationLoadedRef = useRef(false);
  const lastRefreshRef = useRef(0);
  const autoOpenedCaptchaIdsRef = useRef<Set<string>>(new Set());
  const isRefreshingRef = useRef(false);
  const pendingRefreshRef = useRef(false);
  const shownRef = useRef(false);

  const refresh = useCallback(async () => {
    if (isRefreshingRef.current) {
      pendingRefreshRef.current = true;
      return;
    }
    isRefreshingRef.current = true;
    try {
      do {
        pendingRefreshRef.current = false;
        const [snapshot, events, batches] = await Promise.all([
          getUiSnapshot(),
          getEventsSince(lastEventIdRef.current, 50).catch((eventsError) => {
            console.warn("[App] Engine event fetch failed; relying on snapshot resync:", eventsError);
            return [];
          }),
          listCaptureBatches(50).catch(() => []),
        ]);
        getHistory().then(setHistoryRecords, (error: unknown) => console.warn("[App] History unavailable:", error));
        setTasks(snapshot.tasks);
        if (events.length > 0) {
          const maxId = Math.max(...events.map((e) => e.id));
          lastEventIdRef.current = Math.max(lastEventIdRef.current, maxId);
          // Apply durable deltas on top of the snapshot before acknowledging;
          // stale payloads are revision-guarded inside applyEngineEvents and the
          // next snapshot remains the resync authority.
          setTasks((prev) => applyEngineEvents(prev, events));
          const choices = extractArchiveChoices(events);
          if (choices.length > 0) {
            setArchiveChoices((prev) => {
              const seen = new Set(prev.map((choice) => `${choice.taskId}|${choice.packageKey}`));
              const fresh = choices.filter((choice) => !seen.has(`${choice.taskId}|${choice.packageKey}`));
              return fresh.length > 0 ? [...prev, ...fresh] : prev;
            });
          }
          const eventIdsToAck = events.map((e) => e.id);
          void acknowledgeEvent(eventIdsToAck).catch((err) => {
            console.warn("[App] Failed to acknowledge engine events; they will replay on next refresh:", err);
          });
        }
        setLinkEntries(snapshot.linkgrabber);
        setRoutes(snapshot.routes);
        setProviders(snapshot.providers ?? []);
        const pendingCaptchas = snapshot.captcha_pending ?? [];
        setCaptchaPending(pendingCaptchas);
        setCaptureBatches(batches);

        // Auto-open helper in default browser ONLY if explicitly enabled by user
        const autoOpenManual = Boolean(readSetting(snapshot.settings, "captcha", "captchaAutoOpenManual", false));
        if (autoOpenManual) {
          for (const c of pendingCaptchas) {
            // Open page_url if Turnstile (Cloudflare requires origin domain for widget rendering)
            const targetUrl = (c.captcha_type === "turnstile" && (c.params?.page_url || c.params?.url))
              ? (c.params?.page_url || c.params?.url)
              : (c.loopback_url || c.params?.page_url || c.params?.url);

            if (targetUrl && !autoOpenedCaptchaIdsRef.current.has(c.id)) {
              autoOpenedCaptchaIdsRef.current.add(c.id);
              void openPath(targetUrl).catch((err) => {
                console.warn(`[App] Failed to auto-open captcha URL ${targetUrl}:`, err);
              });
            }
          }
        }
        const activeIds = new Set(pendingCaptchas.map((c) => c.id));
        for (const id of Array.from(autoOpenedCaptchaIdsRef.current)) {
          if (!activeIds.has(id)) {
            autoOpenedCaptchaIdsRef.current.delete(id);
          }
        }

        setUiSettings(snapshot.settings);
        cacheAppearance(snapshot.settings.appearance);
        setPythonSetupRequired(false);

        // Trigger first-run guide if not previously dismissed
        const onboardingDone = Boolean(readSetting(snapshot.settings, "general", "onboardingCompleted", false));
        if (!onboardingDone && !onboardingOpen && typeof window !== "undefined" && !localStorage.getItem("onboarding_dismissed")) {
          setOnboardingOpen(true);
        }

        // The save folder only changes through Settings, which updates it directly.
        if (!destinationLoadedRef.current) {
          destinationLoadedRef.current = true;
          const saved = await getDownloadDirectory().catch(() => null);
          if (saved) setDestination(saved);
        }
        setMessage(`${snapshot.tasks.length} transfer${snapshot.tasks.length === 1 ? "" : "s"} loaded`);

      } while (pendingRefreshRef.current);
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : String(error);
      setMessage(errMsg || "Transfer engine unavailable");
      // Check if failure is due to Python missing on host
      void checkPythonEnvironment()
        .then((status) => {
          if (!status.installed && !status.sidecar_available) {
            setPythonSetupRequired(true);
          }
        })
        .catch((checkError) => {
          console.warn("[App] Python environment check failed:", checkError);
        });
    } finally {
      isRefreshingRef.current = false;
      if (!shownRef.current) {
        try {
          await windowAction("show");
          shownRef.current = true;
        } catch (error) {
          console.error("[App] Could not reveal main window", error);
        }
      }
    }
  }, [onboardingOpen]);

  async function openCollection(id: string): Promise<CollectionPlan> {
    const result = await getCollection(id);
    setActiveCollection(result);
    return result;
  }

  async function enqueueCollectionItems(id: string, itemIds: string[], target: string) {
    const result = await enqueueCollection(id, itemIds, target);
    await refresh();
    setActiveCollection(await getCollection(id));
    setMessage(`${result.queued.length} collection item${result.queued.length === 1 ? "" : "s"} queued`);
    return result;
  }

  const hasActiveTransfers = useMemo(() =>
    tasks.some(
      (t) =>
        ["downloading", "resolving", "preflight", "verifying", "postprocessing"].includes(t.state) ||
        isActiveStage(t.stage)
    ),
    [tasks]
  );

  useEffect(() => {
    void refresh();
    const interval = hasActiveTransfers ? 500 : 1000;
    const timer = window.setInterval(() => {
      const now = Date.now();
      if (document.hidden && now - lastRefreshRef.current < 5000) return;
      lastRefreshRef.current = now;
      void refresh();
    }, interval);
    const onVisible = () => {
      if (!document.hidden) {
        lastRefreshRef.current = Date.now();
        void refresh();
      }
    };
    let resizeTimer: number | undefined;
    const onResize = () => {
      if (resizeTimer) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        lastRefreshRef.current = Date.now();
        void refresh();
      }, 120);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("resize", onResize);
    return () => {
      window.clearInterval(timer);
      if (resizeTimer) window.clearTimeout(resizeTimer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("resize", onResize);
    };
  }, [refresh, hasActiveTransfers]);

  const figmaDownloads = useMemo(() => tasksToFigmaDownloads(tasks, speedHistory), [tasks, speedHistory]);
  // Live finished tasks (fresh) over the recorded history (lasting), one row per download.
  const figmaHistory = useMemo(() => {
    const live = new Map(tasks.map((t) => [t.id, t]));
    const merged = [...tasks, ...historyRecords.filter((h) => !live.has(h.id))];
    return tasksToFigmaHistory(merged);
  }, [tasks, historyRecords]);
  const figmaSettings = useMemo<FigmaAppSettings>(() => ({
    autoStart: Boolean(readSetting(uiSettings, "general", "autoStart", true)),
    systemTray: Boolean(readSetting(uiSettings, "general", "systemTray", true)),
    startWithWindows: Boolean(readSetting(uiSettings, "general", "startWithWindows", false)),
    clearOnExit: Boolean(readSetting(uiSettings, "general", "clearOnExit", false)),
    confirmClear: Boolean(readSetting(uiSettings, "general", "confirmClear", true)),
    hotkeys: String(readSetting(uiSettings, "general", "hotkeys", "")),
    clipboardWatcher: Boolean(readSetting(uiSettings, "general", "clipboardWatcher", false)),
    maxConcurrent: String(readSetting(uiSettings, "general", "maxConcurrent", "3")),
    speedLimit: Boolean(readSetting(uiSettings, "network", "speedLimit", false)),
    speedLimitVal: String(readSetting(uiSettings, "network", "speedLimitVal", "0")),
    proxy: Boolean(readSetting(uiSettings, "network", "proxy", false)),
    proxyAddr: String(readSetting(uiSettings, "network", "proxyAddr", "")),
    retryFailed: Boolean(readSetting(uiSettings, "general", "retryFailed", true)),
    retryCount: String(readSetting(uiSettings, "general", "retryCount", "3")),
    acrylic: Boolean(readSetting(uiSettings, "appearance", "acrylic", true)),
    rowSize: (String(readSetting(uiSettings, "appearance", "rowSize", "medium")) as "small" | "medium" | "large"),
    showSpeeds: Boolean(readSetting(uiSettings, "appearance", "showSpeeds", true)),
    colorAccent: String(readSetting(uiSettings, "appearance", "colorAccent", "var(--accent)")),
    notifComplete: Boolean(readSetting(uiSettings, "notifications", "notifComplete", true)),
    notifFailed: Boolean(readSetting(uiSettings, "notifications", "notifFailed", true)),
    notifPause: Boolean(readSetting(uiSettings, "notifications", "notifPause", false)),
    notifSound: Boolean(readSetting(uiSettings, "notifications", "notifSound", true)),
    savePath: destination,
    captchaAcknowledged: Boolean(readSetting(uiSettings, "captcha", "captchaAcknowledged", false)),
    captchaMaster: Boolean(readSetting(uiSettings, "captcha", "captchaMaster", false)),
    captcha2captcha: Boolean(readSetting(uiSettings, "captcha", "captcha2captcha", false)),
    captcha2captchaKey: "",
    captchaFlare: Boolean(readSetting(uiSettings, "captcha", "captchaFlare", false)),
    captchaFlareEndpoint: String(readSetting(uiSettings, "captcha", "captchaFlareEndpoint", "http://127.0.0.1:8191")),
    captchaAudio: Boolean(readSetting(uiSettings, "captcha", "captchaAudio", true)),
    captchaHcaptcha: Boolean(readSetting(uiSettings, "captcha", "captchaHcaptcha", true)),
    captchaRecaptcha: Boolean(readSetting(uiSettings, "captcha", "captchaRecaptcha", true)),
    captchaPositional: Boolean(readSetting(uiSettings, "captcha", "captchaPositional", false)),
    captchaAutoSkip: Boolean(readSetting(uiSettings, "captcha", "captchaAutoSkip", true)),
    captchaAutoSkipSecs: String(readSetting(uiSettings, "captcha", "captchaAutoSkipSecs", "30")),
    captchaAutoOpenManual: Boolean(readSetting(uiSettings, "captcha", "captchaAutoOpenManual", false)),
    captchaShowAutoBanner: Boolean(readSetting(uiSettings, "captcha", "captchaShowAutoBanner", true)),
    captchaAutoSolve: Boolean(readSetting(uiSettings, "captcha", "captchaAutoSolve", false)),
    captchaSound: Boolean(readSetting(uiSettings, "captcha", "captchaSound", false)),
    routeType: String(readSetting(uiSettings, "routes", "routeType", "Direct")),
    activeLocation: String(readSetting(uiSettings, "routes", "activeLocation", "auto")),
    autoSwitchOnQuota: Boolean(readSetting(uiSettings, "routes", "autoSwitchOnQuota", true)),
    switchIncludesProxies: Boolean(readSetting(uiSettings, "routes", "switchIncludesProxies", true)),
    allowDirectFallback: Boolean(readSetting(uiSettings, "routes", "allowDirectFallback", true)),
    connectionsPerFile: String(readSetting(uiSettings, "network", "connectionsPerFile", "8")),
    timeoutSeconds: String(readSetting(uiSettings, "network", "timeoutSeconds", "30")),
    adblock: Boolean(readSetting(uiSettings, "network", "adblock", true)),
    adblockFullLists: Boolean(readSetting(uiSettings, "network", "adblockFullLists", true)),
    colorMode: String(readSetting(uiSettings, "appearance", "colorMode", "Dark")),
    language: String(readSetting(uiSettings, "appearance", "language", "English (US)")),
    soundPreset: String(readSetting(uiSettings, "notifications", "soundPreset", "Windows Notify")),
    themeName: String(readSetting(uiSettings, "appearance", "themeName", "midnight")),
    uiScale: String(readSetting(uiSettings, "appearance", "uiScale", "Auto")),
    autoFullscreenScale: Boolean(readSetting(uiSettings, "appearance", "autoFullscreenScale", true)),
  }), [destination, uiSettings]);

  async function addUrl(
    url: string,
    overrideDestination = destination,
    selectedItemIds?: string[],
    duplicateStrategy?: "skip" | "overwrite" | "rename" | "prompt",
    displayName?: string,
    autoExtract?: boolean,
    shouldRefresh = true
  ) {
    setBusy(true);
    try {
      const effectiveDest = resolveDestinationWithPackage(overrideDestination, url, displayName);
      const task = await addTask(url, effectiveDest, selectedItemIds, duplicateStrategy, displayName, autoExtract);
      await startTask(task);
      setMessage("Transfer added");
      if (shouldRefresh) await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not add transfer");
    } finally {
      setBusy(false);
    }
  }

  async function importBrowserCapture(params: {
    batchId: string;
    candidateId?: string;
    candidateIndex: number;
    destination: string;
    category?: FileCategory;
    queueId: string;
    addAnyway?: boolean;
  }): Promise<CaptureImportResult> {
    try {
      const result = await importCaptureCandidate(params);
      setMessage(result.status === "duplicate" ? "Duplicate capture suppressed" : "Browser capture added");
      await refresh();
      return result;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not import browser capture");
      throw error;
    }
  }

  async function importMediaCapture(candidate: CaptureCandidate, url: string): Promise<CaptureImportResult | Task> {
    try {
      // Planning is performed before task creation. The task then enters the
      // normal engine path, where media_assemble owns I/O and progress.
      await planMedia(url);
      const task = await browserImportCandidate({
        url,
        destination,
        displayName: candidate.filename,
        headers: candidate.headers,
        referrer: candidate.referrer,
        pageContext: { page_url: candidate.page_url, page_origin: candidate.page_origin, session_ref: candidate.session_ref },
      });
      await refresh();
      setMessage("Media capture added");
      return task;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not import media capture");
      throw error;
    }
  }

  async function taskAction(viewTask: { id: string }, action: TaskAction, shouldRefresh: boolean = true) {
    const task = tasks.find((item) => item.id === viewTask.id);
    if (!task) return;
    try {
      await performTaskAction(task, action);
      setMessage(taskActionMessage(action));
      if (shouldRefresh) await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `Could not ${action} transfer`);
      if (shouldRefresh) await refresh();
    }
  }

  async function bulkAction(action: TaskAction, ids: string[]) {
    if (!ids.length) return;
    setBusy(true);
    try {
      if (ids.length === tasks.length && action !== "restart") await bulkTransferAction(action);
      else await Promise.all(ids.map((id) => taskAction({ id }, action, false)));
      setMessage(`${action} requested for ${ids.length} transfer${ids.length === 1 ? "" : "s"}`);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `Could not ${action} transfers`);
    } finally {
      setBusy(false);
    }
  }

  async function updateSetting(tab: SettingsTab, key: string, value: string | boolean) {
    try {
      const definition = settingDefinition(tab, key);
      const normalized = definition?.type === "number" ? Number(value) : value;
      if (definition) {
        const validation = validateSetting(definition, normalized);
        if (!validation.ok) throw new Error(`${key}: ${validation.reason}`);
      }
      const saved = await updateUiSettings({ [tab]: { [key]: normalized } });
      setUiSettings(saved.settings);
      cacheAppearance(saved.settings.appearance);
      if (tab === "general" && key === "savePath" && typeof value === "string") setDestination(value);
      if (tab === "network" && (key === "speedLimit" || key === "speedLimitVal")) {
        const enabled = Boolean(key === "speedLimit" ? value : saved.settings.network.speedLimit);
        const limit = Number(key === "speedLimitVal" ? value : saved.settings.network.speedLimitVal) || 0;
        await setBandwidthRate(enabled ? Math.round(limit * 1024 * 1024) : 0);
      }
      if (tab === "general" && key === "clipboardWatcher") await setClipboardWatcher(Boolean(normalized));
      if (tab === "general" && key === "startWithWindows") await setAppAutostart(Boolean(normalized));
      if (tab === "captcha") {
        const solverId = key === "captchaAudio" ? "audio_speech" : key === "captchaFlare" ? "flaresolverr" : key === "captcha2captcha" ? "twocaptcha" : null;
        if (solverId) await saveCaptchaConfig({ solvers: [{ id: solverId, enabled: Boolean(normalized) }] });
        if (key === "captchaSound") await saveCaptchaConfig({ sound_enabled: Boolean(normalized) });
        if (key === "captchaAutoSkip") await saveCaptchaConfig({ auto_skip_timeout: Boolean(normalized) });
        if (key === "captchaFlareEndpoint" && typeof normalized === "string") await configureFlareSolverr(normalized);
      }
      setMessage("Setting saved");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save setting");
    }
  }

  async function removeTask(id: string, shouldRefresh: boolean = true, deleteFiles: boolean = false) {
    setTasks((prev) => prev.filter((t) => t.id !== id));
    try {
      await deleteTask(id, deleteFiles);
      if (shouldRefresh) await refresh();
      setMessage("Transfer removed");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not remove transfer");
      if (shouldRefresh) await refresh();
    }
  }

  async function removeTasks(ids: string[], deleteFiles: boolean = false) {
    if (!ids.length) return;
    const idSet = new Set(ids);
    setTasks((prev) => prev.filter((t) => !idSet.has(t.id)));
    try {
      await deleteTasks(ids, deleteFiles);
      await refresh();
      setMessage(`${ids.length} transfer${ids.length === 1 ? "" : "s"} removed`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not remove transfers");
      await refresh();
    }
  }

  async function clearCompleted() {
    try { await clearCompletedTasks(); await refresh(); setMessage("Completed transfers cleared"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not clear completed transfers"); }
  }

  async function addLinks(text: string) {
    try { await addLinkGrabber(text); await refresh(); setMessage("Links captured"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not capture links"); }
  }

  async function updateLink(id: string, selected: boolean) {
    try { await updateLinkGrabber(id, { selected }); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not update link"); }
  }

  async function bulkUpdateLinksHandler(ids: string[], selected: boolean) {
    try { await bulkUpdateLinks(ids, { selected }); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not update links"); }
  }

  async function deleteLink(id: string) {
    try { await deleteLinkGrabber(id); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not delete link"); }
  }

  async function bulkDeleteLinksHandler(ids: string[]) {
    try { await deleteLinkGrabberMany(ids); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not delete links"); }
  }

  async function retryLinks(ids: string[]) {
    try { await retryLinkGrabber(ids); await refresh(); setMessage("Retrying captured links"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not retry links"); }
  }

  async function inspectCapturedLink(id: string, resolve: boolean) {
    try { const result = await inspectLink(id, resolve); await refresh(); setMessage(resolve ? "Link resolved" : "Link inspected"); return result; }
    catch (error) { const result = { error: error instanceof Error ? error.message : "Could not inspect link" }; setMessage(result.error); return result; }
  }

  async function enqueueLinks(ids: string[]) {
    try { await enqueueLinkGrabber(ids, destination); await refresh(); setPage("downloads"); setMessage("Captured links queued"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not queue links"); }
  }

  async function clearLinks() {
    try { await clearLinkGrabber(); await refresh(); setMessage("Captured links cleared"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not clear links"); }
  }

  async function saveSecret(name: string, value: string) {
    try {
      await putSecret(name, value, "captcha");
      setMessage("Credential saved securely");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save credential");
    }
  }

  function dismissArchiveChoice(choice: ArchiveChoice) {
    setArchiveChoices((prev) =>
      prev.filter((entry) => !(entry.taskId === choice.taskId && entry.packageKey === choice.packageKey))
    );
  }

  async function extractArchiveChoice(choice: ArchiveChoice) {
    try {
      await enqueueArchiveExtract(choice.inputPath, choice.taskId, choice.outputDirectory || undefined);
      dismissArchiveChoice(choice);
      setMessage("Archive extraction queued");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start archive extraction");
      throw error;
    }
  }

  async function prioritizeTask(id: string) {
    const task = tasks.find((item) => item.id === id);
    if (!task) return;
    try {
      await setTaskOptions(task, { priority: 100 });
      setMessage("Transfer moved to the top");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not prioritize transfer");
    }
  }

  const handlePageChange = useCallback((newPage: AppPage) => {
    emitLog("INFO", "ui:navigation", `Navigated to tab: ${newPage}`, { page: newPage });
    setPage(newPage);
  }, []);

  return <div className="app-root" data-engine-message={message} data-engine-busy={busy ? "true" : "false"}>
    <FigmaAppSurface
      page={page}
      onPageChange={handlePageChange}
      downloads={figmaDownloads}
      history={figmaHistory}
      settings={figmaSettings}
      routes={routes}
      providers={providers}
      captureBatches={captureBatches}
      collectionPlans={collectionPlans}
      activeCollection={activeCollection}
      onCollectionOpen={openCollection}
      onCollectionSelect={selectCollectionItems}
      onCollectionEnqueue={enqueueCollectionItems}
      onCollectionCancel={async (id) => { const result = await cancelCollection(id); setActiveCollection(result); await refresh(); return result; }}
      onCollectionContinue={async (id) => { const result = await continueCollection(id); setActiveCollection(result); await refresh(); return result; }}
      captchaPending={captchaPending}
      linkEntries={linkEntries}
      defaultSavePath={destination}
      onAddUrl={addUrl}
      onTaskAction={(task, action) => void taskAction(task, action)}
      onBulkAction={(action, ids) => void bulkAction(action, ids)}
      onRemoveTask={(id, deleteFiles) => {
        if (Array.isArray(id)) void removeTasks(id, deleteFiles);
        else void removeTask(id, true, deleteFiles);
      }}
      onClearCompleted={() => void clearCompleted()}
      onOpenFolder={(path) => void openPath(path).catch((error) => setMessage(error instanceof Error ? error.message : "Could not open folder"))}
      onCaptchaSolve={async (id, solution, generation) => { await solveCaptcha(id, solution, generation); await refresh(); }}
      onCaptchaSkip={async (id, generation) => { await skipCaptcha(id, "single", generation); await refresh(); }}
      onCaptureImport={importBrowserCapture}
      onMediaImport={importMediaCapture}
      onMediaPlan={planMedia}
      onSettingChange={(tab, key, value) => void updateSetting(tab, key, value)}
      onLinkAdd={addLinks}
      onLinkUpdate={updateLink}
      onLinkBulkUpdate={bulkUpdateLinksHandler}
      onLinkDelete={deleteLink}
      onLinkBulkDelete={bulkDeleteLinksHandler}
      onDismissCaptureBatch={async (batchId) => {
        try { await dismissCaptureBatches([batchId]); await refresh(); }
        catch (error) { setMessage(error instanceof Error ? error.message : "Could not dismiss the capture"); }
      }}
      onLinkInspect={inspectCapturedLink}
      onLinkRetry={retryLinks}
      onLinkEnqueue={enqueueLinks}
      onLinkClear={clearLinks}
      onPickDirectory={pickDirectory}
      onSaveSecret={saveSecret}
      onPrioritizeTask={(id) => void prioritizeTask(id)}
      onHistoryRetry={(id) => {
        // A download removed from the list lives on in History: retrying adds it again.
        if (tasks.some((t) => t.id === id)) { void taskAction({ id }, "restart"); return; }
        const record = historyRecords.find((h) => h.id === id);
        if (record) void addUrl(record.source_url, record.destination);
      }}
      onHistoryClear={() => void clearHistory().then(() => { setHistoryRecords([]); setMessage("History cleared"); },
        (error: unknown) => setMessage(error instanceof Error ? error.message : "Could not clear history"))}
      onOpenSetupWizard={() => setOnboardingOpen(true)}
    />
    {pythonSetupRequired && (
      <PythonSetupModal
        onDismiss={() => setPythonSetupRequired(false)}
        onSuccess={() => {
          setPythonSetupRequired(false);
          void refresh();
        }}
      />
    )}
    {onboardingOpen && (
      <WelcomeWizard
        onDismiss={() => {
          setOnboardingOpen(false);
          try {
            localStorage.setItem("onboarding_dismissed", "true");
            void updateUiSettings({ general: { onboardingCompleted: true } });
          } catch {}
        }}
        onSettingChange={(tab, key, value) => void updateSetting(tab, key, value)}
        currentTheme={String(readSetting(uiSettings, "appearance", "themeName", "midnight"))}
        acrylic={Boolean(readSetting(uiSettings, "appearance", "acrylic", true))}
      />
    )}
    {archiveChoices[0] && (
      <ArchiveChoicePrompt
        choice={archiveChoices[0]}
        onExtract={() => extractArchiveChoice(archiveChoices[0])}
        onKeep={() => dismissArchiveChoice(archiveChoices[0])}
      />
    )}
  </div>;
}
