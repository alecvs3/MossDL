import React from "react";
import { ExplorePage, type ExplorePageProps } from "../pages/explore/ExplorePage";
import CollectionPlanReview from "../../components/CollectionPlanReview";
import { DownloadsPage } from "../pages/DownloadsPage";
import { HistoryPage } from "../pages/HistoryPage";
import { SettingsPage } from "../pages/SettingsPage";
import { CaptchasPage } from "../pages/CaptchasPage";
import { ConnectionsPage } from "../pages/routes/ConnectionsPage";
import type { FigmaAppProps, DownloadWithHistory, SettingsTab } from "../types";

export interface FigmaRouterProps extends FigmaAppProps {
  downloadViews: DownloadWithHistory[];
  selectedDownloadIds: string[];
  setSelectedDownloadIds: React.Dispatch<React.SetStateAction<string[]>>;
  sheetId: string | null;
  setSheetId: React.Dispatch<React.SetStateAction<string | null>>;
  focusedDownloadId: string | null;
  setFocusedDownloadId: React.Dispatch<React.SetStateAction<string | null>>;
  openAddWindow: () => void;
  handleQueueAssets: (assets: { name: string; url: string; size: number; type: string }[]) => void | Promise<void>;
  selectedHistoryIds: string[];
  setSelectedHistoryIds: React.Dispatch<React.SetStateAction<string[]>>;
  changeSetting: (tab: SettingsTab, key: string, value: string | boolean) => void;
  openSettings: () => void;
  /** A page URL handed to Explore from elsewhere (e.g. the Add URL window). */
  exploreUrl?: string | null;
}

export function FigmaRouter(props: FigmaRouterProps) {
  const {
    page,
    onPageChange,
    settings,
    defaultSavePath,
    downloadViews,
    selectedDownloadIds,
    setSelectedDownloadIds,
    sheetId,
    setSheetId,
    focusedDownloadId,
    setFocusedDownloadId,
    onTaskAction,
    onBulkAction,
    onRemoveTask,
    onAddUrl,
    openSettings,
    onPrioritizeTask,
    onPickDirectory,
    onOpenFolder,
    openAddWindow,
    linkEntries,
    onLinkAdd,
    captureBatches,
    onCaptureImport,
    onMediaImport,
    onMediaPlan,
    captchaPending,
    onCaptchaSolve,
    onCaptchaSkip,
    routes,
    collectionPlans,
    activeCollection,
    onCollectionOpen,
    onCollectionSelect,
    onCollectionEnqueue,
    onCollectionCancel,
    onCollectionContinue,
    history,
    selectedHistoryIds,
    setSelectedHistoryIds,
    onHistoryRetry,
    onHistoryClear,
    changeSetting,
    onSaveSecret,
  } = props;

  switch (page) {
    case "downloads":
      return (
        <DownloadsPage
          rowSize={settings.rowSize}
          showSpeeds={settings.showSpeeds}
          defaultSavePath={defaultSavePath}
          downloads={downloadViews}
          selectedIds={selectedDownloadIds}
          onSelectionChange={setSelectedDownloadIds}
          sheetId={sheetId}
          setSheetId={setSheetId}
          focusedId={focusedDownloadId}
          onFocusChange={setFocusedDownloadId}
          onTaskAction={onTaskAction as any}
          onBulkAction={onBulkAction as any}
          onRemove={onRemoveTask}
          onAddUrl={onAddUrl as any}
          onOpenSettings={openSettings}
          onPrioritize={onPrioritizeTask}
          onPickDirectory={onPickDirectory}
          onOpenFolder={onOpenFolder}
          onNavigate={onPageChange}
          onOpenAdd={openAddWindow}
          captchaPending={captchaPending}
          captchaShowAutoBanner={settings.captchaShowAutoBanner ?? true}
          captchaAutoSolve={settings.captchaAutoSolve ?? false}
          onCaptchaAutoSolveChange={(enabled) => changeSetting("captcha", "captchaAutoSolve", enabled)}
          onCaptchaSolve={onCaptchaSolve}
        />
      );

    case "explore":
      return (
        <ExplorePage
          downloads={downloadViews}
          captureBatches={captureBatches ?? []}
          linkEntries={linkEntries ?? []}
          defaultSavePath={defaultSavePath}
          initialUrl={props.exploreUrl}
          onAddUrl={onAddUrl as ExplorePageProps["onAddUrl"]}
          onCaptureImport={onCaptureImport}
          onDismissCaptureBatch={props.onDismissCaptureBatch}
          onLinkAdd={onLinkAdd}
          onLinkBulkDelete={props.onLinkBulkDelete}
          onLinkEnqueue={props.onLinkEnqueue}
          onPickDirectory={onPickDirectory}
          onShowDownloads={() => onPageChange("downloads")}
          onMediaPlan={onMediaPlan}
          onMediaImport={onMediaImport}
        />
      );

    case "captchas":
      return (
        <CaptchasPage
          captchaPending={captchaPending as any}
          onCaptchaSolve={onCaptchaSolve as any}
          onCaptchaSkip={onCaptchaSkip as any}
          onAddUrl={onAddUrl as any}
          autoSolve={settings.captchaAutoSolve ?? false}
          onAutoSolveChange={(enabled) => changeSetting("captcha", "captchaAutoSolve", enabled)}
        />
      );

    case "routes":
      return (
        <ConnectionsPage
          routes={routes ?? []}
          settings={{
            activeLocation: settings.activeLocation,
            autoSwitchOnQuota: settings.autoSwitchOnQuota,
            switchIncludesProxies: settings.switchIncludesProxies,
            allowDirectFallback: settings.allowDirectFallback,
          }}
          onSettingChange={(key, value) => changeSetting("routes", key, value)}
        />
      );

    case "collections":
      return (
        <CollectionPlanReview
          plans={collectionPlans as any}
          activePlan={activeCollection as any}
          defaultDestination={defaultSavePath}
          onOpen={
            (onCollectionOpen as any) ??
            (async () => {
              throw new Error("Collection review is unavailable");
            })
          }
          onSelect={
            (onCollectionSelect as any) ??
            (async () => {
              throw new Error("Collection selection is unavailable");
            })
          }
          onEnqueue={
            (onCollectionEnqueue as any) ??
            (async () => {
              throw new Error("Collection enqueue is unavailable");
            })
          }
          onCancel={
            (onCollectionCancel as any) ??
            (async () => {
              throw new Error("Collection cancellation is unavailable");
            })
          }
          onContinue={
            (onCollectionContinue as any) ??
            (async () => {
              throw new Error("Collection continuation is unavailable");
            })
          }
        />
      );

    case "history":
      return (
        <HistoryPage
          items={history}
          selectedIds={selectedHistoryIds}
          onSelectionChange={setSelectedHistoryIds}
          onRemove={onRemoveTask}
          onRetry={(item) => onHistoryRetry?.(item.id)}
          onBulkAction={onBulkAction}
          onClear={onHistoryClear}
        />
      );

    case "settings":
      return (
        <SettingsPage
          settings={settings}
          routes={routes as any}
          captchaPending={captchaPending as any}
          onSettingChange={changeSetting as any}
          onPickDirectory={onPickDirectory}
          onSaveSecret={onSaveSecret as any}
          onCaptchaSolve={onCaptchaSolve as any}
          onCaptchaSkip={onCaptchaSkip as any}
        />
      );

    default:
      return null;
  }
}
