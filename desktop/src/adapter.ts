import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface DesktopFile {
  path: string;
  name: string;
  size: number;
  sha256: string;
  modifiedAtMs?: number;
}

export interface DiscoveredFile {
  path: string;
  name: string;
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export const isDesktop = (): boolean =>
  typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);

/** Selects source files and grants this process read access to those exact paths. */
export async function chooseSourceFiles(): Promise<DesktopFile[]> {
  if (!isDesktop()) return [];
  return invoke<DesktopFile[]>("choose_source_files");
}

/** Reads at most 8 MiB. The path must have been selected or discovered under an approved folder. */
export async function readFileChunk(
  path: string,
  offset: number,
  length: number,
): Promise<Uint8Array> {
  if (!isDesktop()) throw new Error("Desktop file access is unavailable in the browser");
  const encoded = await invoke<string>("read_file_chunk", { path, offset, length });
  const binary = atob(encoded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

/** Starts an OS watcher after the user explicitly approves a directory. */
export async function chooseAndWatchDirectory(
  onFile: (file: DiscoveredFile) => void,
): Promise<{ directory: string; stop: () => Promise<void> } | undefined> {
  if (!isDesktop()) return undefined;
  const directory = await invoke<string | null>("choose_watch_directory");
  if (!directory) return undefined;
  const unlisten: UnlistenFn = await listen<DiscoveredFile>("desktop://file-discovered", (event) => {
    onFile(event.payload);
  });
  await invoke("start_directory_watch", { path: directory });
  return {
    directory,
    stop: async () => {
      unlisten();
      await invoke("stop_directory_watch");
    },
  };
}

export async function openPowerBiFile(path: string): Promise<void> {
  if (!isDesktop()) throw new Error("Power BI Desktop can only be opened from the Windows client");
  await invoke("open_power_bi_file", { path });
}

/**
 * PBIX to PBIP is intentionally not automated. On Windows this opens the PBIX
 * in Power BI Desktop so an operator can use Microsoft's supported Save As flow.
 */
export async function startPbixToPbipAssistance(path: string): Promise<string> {
  if (!isDesktop()) throw new Error("This assisted flow requires the Windows client");
  return invoke<string>("start_pbix_to_pbip_assistance", { path });
}
