import type { SettingsTab } from "../api";

export type SettingType = "boolean" | "number" | "string";
export type SettingDefinition = {
  tab: SettingsTab;
  key: string;
  type: SettingType;
  min?: number;
  max?: number;
  capability?: string;
};

const definitions: SettingDefinition[] = [
  { tab: "general", key: "autoStart", type: "boolean" },
  { tab: "general", key: "systemTray", type: "boolean" },
  { tab: "general", key: "startWithWindows", type: "boolean", capability: "autostart" },
  { tab: "general", key: "clearOnExit", type: "boolean" },
  { tab: "general", key: "confirmClear", type: "boolean" },
  { tab: "general", key: "hotkeys", type: "string" },
  { tab: "general", key: "savePath", type: "string" },
  { tab: "general", key: "clipboardWatcher", type: "boolean", capability: "clipboard" },
  { tab: "general", key: "maxConcurrent", type: "number", min: 1, max: 64 },
  { tab: "general", key: "retryFailed", type: "boolean" },
  { tab: "general", key: "retryCount", type: "number", min: 0, max: 20 },
  { tab: "general", key: "onboardingCompleted", type: "boolean" },
  { tab: "network", key: "speedLimit", type: "boolean", capability: "bandwidth" },
  { tab: "network", key: "speedLimitVal", type: "number", min: 0, max: 102400, capability: "bandwidth" },
  { tab: "network", key: "proxy", type: "boolean" },
  { tab: "network", key: "proxyAddr", type: "string" },
  { tab: "network", key: "connectionsPerFile", type: "number", min: 1, max: 32 },
  { tab: "network", key: "timeoutSeconds", type: "number", min: 1, max: 3600 },
  { tab: "network", key: "adblock", type: "boolean" },
  { tab: "network", key: "adblockFullLists", type: "boolean" },
  { tab: "appearance", key: "acrylic", type: "boolean" },
  { tab: "appearance", key: "rowSize", type: "string" },
  { tab: "appearance", key: "showSpeeds", type: "boolean" },
  { tab: "appearance", key: "colorAccent", type: "string" },
  { tab: "appearance", key: "colorMode", type: "string" },
  { tab: "appearance", key: "language", type: "string" },
  { tab: "appearance", key: "themeName", type: "string" },
  { tab: "appearance", key: "uiScale", type: "string" },
  { tab: "appearance", key: "autoFullscreenScale", type: "boolean" },
  { tab: "appearance", key: "fontFamily", type: "string" },
  { tab: "notifications", key: "notifComplete", type: "boolean", capability: "notifications" },
  { tab: "notifications", key: "notifFailed", type: "boolean", capability: "notifications" },
  { tab: "notifications", key: "notifPause", type: "boolean", capability: "notifications" },
  { tab: "notifications", key: "notifSound", type: "boolean", capability: "notifications" },
  { tab: "notifications", key: "soundPreset", type: "string", capability: "notifications" },
  { tab: "captcha", key: "captchaAcknowledged", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaMaster", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaAudio", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaHcaptcha", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaRecaptcha", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaPositional", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captcha2captcha", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaFlare", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaFlareEndpoint", type: "string", capability: "captcha" },
  { tab: "captcha", key: "captchaAutoOpenManual", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaShowAutoBanner", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaAutoSolve", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaAutoSkip", type: "boolean", capability: "captcha" },
  { tab: "captcha", key: "captchaAutoSkipSecs", type: "number", min: 0, max: 3600, capability: "captcha" },
  { tab: "captcha", key: "captchaSound", type: "boolean", capability: "captcha" },
  { tab: "routes", key: "routeType", type: "string", capability: "routes" },
  { tab: "routes", key: "activeLocation", type: "string", capability: "routes" },
  { tab: "routes", key: "autoSwitchOnQuota", type: "boolean", capability: "routes" },
  { tab: "routes", key: "switchIncludesProxies", type: "boolean", capability: "routes" },
  { tab: "routes", key: "allowDirectFallback", type: "boolean", capability: "routes" },
];

export function settingDefinition(tab: SettingsTab, key: string): SettingDefinition | undefined {
  return definitions.find((definition) => definition.tab === tab && definition.key === key);
}

export function validateSetting(definition: SettingDefinition, value: unknown): { ok: boolean; reason?: string } {
  if (definition.type === "boolean") return typeof value === "boolean" ? { ok: true } : { ok: false, reason: "Expected true or false" };
  if (definition.type === "string") return typeof value === "string" ? { ok: true } : { ok: false, reason: "Expected text" };
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return { ok: false, reason: "Expected a number" };
  if (definition.min !== undefined && numeric < definition.min) return { ok: false, reason: `Must be at least ${definition.min}` };
  if (definition.max !== undefined && numeric > definition.max) return { ok: false, reason: `Must be at most ${definition.max}` };
  return { ok: true };
}
