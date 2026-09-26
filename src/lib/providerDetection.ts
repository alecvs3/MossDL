import type { ProviderMetadata } from "../api";
import type { Provider } from "../figma/types";

export const providerLabels: Record<string, string> = {
  youtube: "YouTube",
  github: "GitHub",
  gdrive: "Google Drive",
  googledrive: "Google Drive",
  dropbox: "Dropbox",
  steam: "Steam",
  transferit: "Transfer.it",
  mega: "Mega",
  mediafire: "MediaFire",
  pixeldrain: "Pixeldrain",
  "1fichier": "1fichier",
  krakenfiles: "Krakenfiles",
  cyberdrop: "CyberDrop",
  vikingfile: "Vikingfile",
  datanodes: "Datanodes",
  fuckingfast: "FuckingFast",
  filekeeper: "Filekeeper",
  direct: "Direct",
};

export function detectProvider(
  url: string,
  providerId?: string | null,
  providersCatalog?: ProviderMetadata[]
): Provider {
  if (providerId && providerId !== "generic") return providerId.toLowerCase() as Provider;
  if (!url) return "direct";

  if (providersCatalog && providersCatalog.length > 0) {
    try {
      const parsedHost = new URL(url.startsWith("http") ? url : `https://${url}`).hostname.toLowerCase();
      const matched = providersCatalog.find((p) =>
        p.hosts?.some((h) => {
          const hClean = h.toLowerCase().replace(/^\*\./, "");
          return parsedHost === hClean || parsedHost.endsWith(`.${hClean}`);
        })
      );
      if (matched) return matched.id as Provider;
    } catch {}
  }

  const u = url.toLowerCase();
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube" as Provider;
  if (u.includes("github.com") || u.includes("raw.githubusercontent.com")) return "github" as Provider;
  if (u.includes("drive.google.com") || u.includes("docs.google.com")) return "gdrive";
  if (u.includes("dropbox.com")) return "dropbox" as Provider;
  if (u.includes("steamstatic.com") || u.includes("steampowered.com") || u.includes("steamcommunity.com")) return "steam" as Provider;
  if (u.includes("transfer.it") || u.includes("transferit")) return "transferit";
  if (u.includes("mega.nz") || u.includes("mega.co.nz")) return "mega";
  if (u.includes("mediafire.com")) return "mediafire";
  if (u.includes("pixeldrain.com")) return "pixeldrain";
  if (u.includes("1fichier.com")) return "1fichier";
  if (u.includes("krakenfiles.com")) return "krakenfiles";
  if (u.includes("cyberdrop.me") || u.includes("cyberdrop.to")) return "cyberdrop";
  if (u.includes("vikingfile.com") || u.includes("vik1ngfile.site")) return "vikingfile";
  if (u.includes("datanodes.to")) return "datanodes";
  if (u.includes("fuckingfast.co")) return "fuckingfast";
  if (u.includes("filekeeper.to") || u.includes("filekeeper.com")) return "filekeeper";
  return "direct";
}

export function providerLabel(provider: string, providersCatalog?: ProviderMetadata[]): string {
  if (providersCatalog) {
    const meta = providersCatalog.find((p) => p.id === provider);
    if (meta?.display_name) return meta.display_name;
  }
  return (
    providerLabels[provider] ||
    provider
      .replace(/[._-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase()) ||
    "Direct"
  );
}
