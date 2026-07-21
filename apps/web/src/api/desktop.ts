export type DesktopFile = { path: string; name: string; size: number; sha256: string; modifiedAtMs?: number }

type TauriInternals = { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> }
declare global { interface Window { __TAURI_INTERNALS__?: TauriInternals } }

export const isDesktop = () => Boolean(window.__TAURI_INTERNALS__?.invoke)
export async function chooseDesktopFiles(): Promise<DesktopFile[]> { return window.__TAURI_INTERNALS__?.invoke<DesktopFile[]>('choose_source_files') ?? [] }
export async function readDesktopChunk(path: string, offset: number, length: number): Promise<Uint8Array> {
  const encoded = await window.__TAURI_INTERNALS__?.invoke<string>('read_file_chunk', { path, offset, length })
  if (!encoded) throw new Error('桌面文件读取失败')
  const binary=atob(encoded), bytes=new Uint8Array(binary.length); for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i); return bytes
}
