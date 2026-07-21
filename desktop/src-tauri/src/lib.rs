use std::{
    collections::HashSet,
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::Mutex,
    time::UNIX_EPOCH,
};

use base64::{engine::general_purpose::STANDARD, Engine};
use notify::{EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{Emitter, State};

const MAX_CHUNK_BYTES: u64 = 8 * 1024 * 1024;
const ALLOWED_EXTENSIONS: &[&str] = &["csv", "xlsx", "xls", "zip", "pbix"];

#[derive(Default)]
struct AccessState {
    selected_files: Mutex<HashSet<PathBuf>>,
    selected_roots: Mutex<HashSet<PathBuf>>,
    watcher: Mutex<Option<RecommendedWatcher>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopFile {
    path: String,
    name: String,
    size: u64,
    sha256: String,
    modified_at_ms: Option<u128>,
}

#[derive(Clone, Serialize)]
struct DiscoveredFile {
    path: String,
    name: String,
}

fn canonical_file(path: &Path) -> Result<PathBuf, String> {
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("Unable to access file: {error}"))?;
    if !canonical.is_file() {
        return Err("The selected path is not a file".into());
    }
    Ok(canonical)
}

fn is_allowed_extension(path: &Path) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .map(|value| ALLOWED_EXTENSIONS.contains(&value.to_ascii_lowercase().as_str()))
        .unwrap_or(false)
}

fn is_authorized(path: &Path, access: &AccessState) -> bool {
    if access
        .selected_files
        .lock()
        .expect("selected files lock")
        .contains(path)
    {
        return true;
    }
    access
        .selected_roots
        .lock()
        .expect("selected roots lock")
        .iter()
        .any(|root| path.starts_with(root))
}

fn describe_file(path: &Path) -> Result<DesktopFile, String> {
    let metadata = path
        .metadata()
        .map_err(|error| format!("Unable to inspect file: {error}"))?;
    let mut input = File::open(path).map_err(|error| format!("Unable to open file: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = input
            .read(&mut buffer)
            .map_err(|error| format!("Unable to hash file: {error}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    let modified_at_ms = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_millis());
    Ok(DesktopFile {
        path: path.to_string_lossy().into_owned(),
        name: path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
        size: metadata.len(),
        sha256: format!("{:x}", hasher.finalize()),
        modified_at_ms,
    })
}

#[tauri::command]
async fn choose_source_files(access: State<'_, AccessState>) -> Result<Vec<DesktopFile>, String> {
    let files = rfd::AsyncFileDialog::new()
        .add_filter("Business data", ALLOWED_EXTENSIONS)
        .pick_files()
        .await
        .unwrap_or_default();
    let mut result = Vec::with_capacity(files.len());
    for selected in files {
        let path = canonical_file(selected.path())?;
        if !is_allowed_extension(&path) {
            return Err("Unsupported file type".into());
        }
        access
            .selected_files
            .lock()
            .map_err(|_| "File access lock failed")?
            .insert(path.clone());
        result.push(describe_file(&path)?);
    }
    Ok(result)
}

#[tauri::command]
async fn choose_watch_directory(access: State<'_, AccessState>) -> Result<Option<String>, String> {
    let Some(directory) = rfd::AsyncFileDialog::new().pick_folder().await else {
        return Ok(None);
    };
    let canonical = directory
        .path()
        .canonicalize()
        .map_err(|error| format!("Unable to access directory: {error}"))?;
    access
        .selected_roots
        .lock()
        .map_err(|_| "Directory access lock failed")?
        .insert(canonical.clone());
    Ok(Some(canonical.to_string_lossy().into_owned()))
}

#[tauri::command]
fn start_directory_watch(
    app: tauri::AppHandle,
    access: State<'_, AccessState>,
    path: String,
) -> Result<(), String> {
    let canonical = PathBuf::from(path)
        .canonicalize()
        .map_err(|error| format!("Unable to access directory: {error}"))?;
    if !access
        .selected_roots
        .lock()
        .map_err(|_| "Directory access lock failed")?
        .contains(&canonical)
    {
        return Err("Select this directory before monitoring it".into());
    }
    let mut watcher = notify::recommended_watcher(move |result: notify::Result<notify::Event>| {
        let Ok(event) = result else { return };
        if !matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_)) {
            return;
        }
        for path in event
            .paths
            .into_iter()
            .filter(|path| path.is_file() && is_allowed_extension(path))
        {
            let payload = DiscoveredFile {
                path: path.to_string_lossy().into_owned(),
                name: path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned(),
            };
            let _ = app.emit("desktop://file-discovered", payload);
        }
    })
    .map_err(|error| format!("Unable to create directory monitor: {error}"))?;
    watcher
        .watch(&canonical, RecursiveMode::Recursive)
        .map_err(|error| format!("Unable to monitor directory: {error}"))?;
    *access
        .watcher
        .lock()
        .map_err(|_| "Directory monitor lock failed")? = Some(watcher);
    Ok(())
}

#[tauri::command]
fn stop_directory_watch(access: State<'_, AccessState>) -> Result<(), String> {
    access
        .watcher
        .lock()
        .map_err(|_| "Directory monitor lock failed")?
        .take();
    Ok(())
}

#[tauri::command]
fn read_file_chunk(
    access: State<'_, AccessState>,
    path: String,
    offset: u64,
    length: u64,
) -> Result<String, String> {
    if length == 0 || length > MAX_CHUNK_BYTES {
        return Err("Chunk length must be between 1 byte and 8 MiB".into());
    }
    let canonical = canonical_file(Path::new(&path))?;
    if !is_authorized(&canonical, &access) {
        return Err("File access was not approved by the user".into());
    }
    let size = canonical
        .metadata()
        .map_err(|error| format!("Unable to inspect file: {error}"))?
        .len();
    if offset > size {
        return Err("Chunk offset is beyond the end of the file".into());
    }
    let read_length = length.min(size - offset) as usize;
    let mut input =
        File::open(canonical).map_err(|error| format!("Unable to open file: {error}"))?;
    input
        .seek(SeekFrom::Start(offset))
        .map_err(|error| format!("Unable to seek file: {error}"))?;
    let mut buffer = vec![0_u8; read_length];
    input
        .read_exact(&mut buffer)
        .map_err(|error| format!("Unable to read file: {error}"))?;
    Ok(STANDARD.encode(buffer))
}

#[cfg(target_os = "windows")]
fn open_pbix(path: &str) -> Result<(), String> {
    let canonical = canonical_file(Path::new(path))?;
    if canonical
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| value.eq_ignore_ascii_case("pbix"))
        != Some(true)
    {
        return Err("Only PBIX files can be opened with Power BI Desktop".into());
    }
    open::that_detached(canonical)
        .map_err(|error| format!("Windows could not open Power BI Desktop: {error}"))
}

#[cfg(not(target_os = "windows"))]
fn open_pbix(_path: &str) -> Result<(), String> {
    Err("Power BI Desktop is supported only by the Windows client; macOS and Linux do not provide this capability".into())
}

#[tauri::command]
fn open_power_bi_file(path: String) -> Result<(), String> {
    open_pbix(&path)
}

#[tauri::command]
fn start_pbix_to_pbip_assistance(path: String) -> Result<String, String> {
    open_pbix(&path)?;
    Ok("Power BI Desktop has been opened. Use its supported Save as Power BI Project flow; conversion is not unattended.".into())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AccessState::default())
        .invoke_handler(tauri::generate_handler![
            choose_source_files,
            choose_watch_directory,
            start_directory_watch,
            stop_directory_watch,
            read_file_chunk,
            open_power_bi_file,
            start_pbix_to_pbip_assistance,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Commerce Analytics desktop client");
}
