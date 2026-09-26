use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::Mutex;
use std::time::SystemTime;
use tauri::{menu::{Menu, MenuItem}, tray::TrayIconBuilder, Manager, WebviewUrl, WebviewWindowBuilder};
use url::Url;

mod updates;

#[cfg(windows)]
use std::sync::OnceLock;

#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::CreateMutexW;
#[cfg(windows)]
use windows_sys::Win32::UI::WindowsAndMessaging::{FindWindowW, SetForegroundWindow, ShowWindow, SW_RESTORE};

#[cfg(windows)]
static INSTANCE_MUTEX: OnceLock<usize> = OnceLock::new();

#[cfg(windows)]
fn ensure_single_instance() -> bool {
    let name: Vec<u16> = "Local\\TransferManager.MainWindow\0".encode_utf16().collect();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
    if handle.is_null() {
        return true;
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe { CloseHandle(handle) };
        let title: Vec<u16> = "MossDL\0".encode_utf16().collect();
        let window = unsafe { FindWindowW(std::ptr::null(), title.as_ptr()) };
        if !window.is_null() {
            unsafe {
                ShowWindow(window, SW_RESTORE);
                SetForegroundWindow(window);
            }
        }
        return false;
    }
    let _ = INSTANCE_MUTEX.set(handle as usize);
    true
}

#[cfg(not(windows))]
fn ensure_single_instance() -> bool { true }

struct EngineProcess {
    _child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    request_id: u64,
    started_at: SystemTime,
}

impl Drop for EngineProcess {
    fn drop(&mut self) {
        if self._child.try_wait().ok().flatten().is_none() {
            let _ = self._child.kill();
            let _ = self._child.wait();
        }
    }
}

struct EngineState(Mutex<Option<EngineProcess>>);

/// Resolve a helper binary that ships as a bundle resource but is also compiled
/// into `src-tauri/target` during development.
///
/// `tauri dev` copies `bundle.resources` into the target directory once, and that
/// copy then goes stale: a debug run would keep launching a weeks-old binary long
/// after `cargo build` produced a fixed one, with no indication anything was
/// wrong. Debug builds therefore prefer the most recently compiled artifact and
/// fall back to the packaged copy; release builds keep the packaged copy first.
fn resolve_helper_binary(
    resource_dir: Option<&PathBuf>,
    workspace: &Path,
    dir_name: &str,
    file_name: &str,
) -> Option<PathBuf> {
    let packaged = || {
        resource_dir
            .map(|dir| dir.join("resources").join(dir_name).join(file_name))
            .filter(|path| path.is_file())
            .or_else(|| {
                resource_dir
                    .map(|dir| dir.join(file_name))
                    .filter(|path| path.is_file())
            })
    };
    let compiled = || {
        let target = workspace.join("src-tauri").join("target");
        [
            target.join("release").join(file_name),
            target.join("debug").join(file_name),
        ]
        .into_iter()
        .filter(|path| path.is_file())
        .max_by_key(|path| path.metadata().and_then(|meta| meta.modified()).ok())
    };
    if cfg!(debug_assertions) {
        compiled().or_else(packaged)
    } else {
        packaged().or_else(compiled)
    }
}

fn start_engine(app: &tauri::AppHandle) -> Result<EngineProcess, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;
    let resource_dir = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.to_path_buf()));
    let manifest_workspace = Path::new(env!("CARGO_MANIFEST_DIR")).parent().map(|p| p.to_path_buf());
    let workspace = manifest_workspace
        .filter(|p| p.is_dir())
        .or_else(|| resource_dir.clone().filter(|p| p.is_dir()))
        .or_else(|| exe_dir.clone().filter(|p| p.is_dir()))
        .unwrap_or_else(|| data_dir.clone());
    let plugin_dir = resource_dir.as_ref().and_then(|dir| {
        let direct = dir.join("plugins");
        if direct.exists() { Some(direct) } else {
            let nested = dir.join("resources").join("plugins");
            nested.exists().then_some(nested)
        }
    });
    let sidecar_name = if cfg!(target_os = "windows") {
        "transfer-engine.exe"
    } else {
        "transfer-engine"
    };
    // Debug builds always run the Python engine from source. A frozen
    // `transfer-engine` sidecar left in the resource directory would otherwise
    // silently shadow every local engine change, and the only symptom would be
    // fixes that appear not to work.
    let sidecar = if cfg!(debug_assertions) {
        None
    } else {
        resource_dir.as_ref().map(|dir| dir.join(sidecar_name))
    };
    let core_name = if cfg!(target_os = "windows") {
        "transfer-core.exe"
    } else {
        "transfer-core"
    };
    let core_path = resolve_helper_binary(resource_dir.as_ref(), &workspace, "transfer-core", core_name);
    let archive_name = if cfg!(target_os = "windows") {
        "archive-worker.exe"
    } else {
        "archive-worker"
    };
    let archive_path =
        resolve_helper_binary(resource_dir.as_ref(), &workspace, "archive-worker", archive_name);
    let mut command = match sidecar.filter(|path| path.exists()) {
        Some(path) => {
            let mut command = Command::new(path);
            command.arg(data_dir.to_string_lossy().as_ref());
            if let Some(path) = core_path.as_ref() {
                command.env("TRANSFER_CORE_PATH", path);
            }
            if let Some(path) = archive_path.as_ref() {
                command.env("ARCHIVE_WORKER_PATH", path);
            }
            if let Some(path) = plugin_dir.as_ref() {
                command.env("TRANSFER_PLUGIN_DIRS", path);
            }
            command
        }
        None => {
            let python = if cfg!(target_os = "windows") {
                "python"
            } else {
                "python3"
            };
            let mut command = Command::new(python);
            command.args(["-m", "engine.service", data_dir.to_string_lossy().as_ref()]);
            command.current_dir(&workspace);
            command.env("TRANSFER_DEV_RELOAD", "1");
            // Ensure python can import engine even if working directory is outside workspace
            let mut python_paths = Vec::new();
            python_paths.push(workspace.to_string_lossy().to_string());
            if let Some(r) = resource_dir.as_ref() {
                python_paths.push(r.to_string_lossy().to_string());
                let engine_sub = r.join("engine");
                if engine_sub.is_dir() {
                    python_paths.push(r.to_string_lossy().to_string());
                }
            }
            if let Some(e) = exe_dir.as_ref() {
                python_paths.push(e.to_string_lossy().to_string());
            }
            if let Ok(existing) = std::env::var("PYTHONPATH") {
                python_paths.push(existing);
            }
            let sep = if cfg!(target_os = "windows") { ";" } else { ":" };
            command.env("PYTHONPATH", python_paths.join(sep));
            if let Some(path) = core_path.as_ref() {
                command.env("TRANSFER_CORE_PATH", path);
            }
            if let Some(path) = archive_path.as_ref() {
                command.env("ARCHIVE_WORKER_PATH", path);
            }
            if let Some(path) = plugin_dir.as_ref() {
                command.env("TRANSFER_PLUGIN_DIRS", path);
            }
            command
        }
    };
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("could not start Python engine: {e}"))?;
    let stdin = child.stdin.take().ok_or("engine stdin unavailable")?;
    let stdout = child.stdout.take().ok_or("engine stdout unavailable")?;
    if let Some(stderr) = child.stderr.take() {
        std::thread::Builder::new()
            .name("engine-stderr-reader".into())
            .spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines().flatten() {
                    eprintln!("[engine:stderr] {}", line);
                }
            })
            .ok();
    }
    Ok(EngineProcess {
        _child: child,
        stdin,
        stdout: BufReader::new(stdout),
        request_id: 0,
        started_at: SystemTime::now(),
    })
}

fn newest_dev_source_mtime(root: &Path) -> Option<SystemTime> {
    let mut newest: Option<SystemTime> = None;
    let mut pending = vec![root.to_path_buf()];
    while let Some(path) = pending.pop() {
        let entries = match std::fs::read_dir(path) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let entry_path = entry.path();
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(_) => continue,
            };
            if file_type.is_dir() {
                if entry_path.file_name().and_then(|name| name.to_str()) != Some("__pycache__") {
                    pending.push(entry_path);
                }
                continue;
            }
            let is_backend_source = entry_path.extension().and_then(|ext| ext.to_str())
                .map(|ext| matches!(ext, "py" | "jsonc"))
                .unwrap_or(false);
            if !is_backend_source { continue; }
            if let Ok(modified) = entry.metadata().and_then(|metadata| metadata.modified()) {
                newest = Some(newest.map_or(modified, |current| current.max(modified)));
            }
        }
    }
    newest
}

fn dev_engine_reload_required(engine: &EngineProcess) -> bool {
    if !cfg!(debug_assertions) && std::env::var("TRANSFER_DEV_RELOAD").ok().as_deref() != Some("1") {
        return false;
    }
    // Throttle disk scans to at most once every 2.0s to avoid hammering NTFS on every 500ms IPC snapshot
    static LAST_CHECK: Mutex<Option<std::time::Instant>> = Mutex::new(None);
    if let Ok(mut guard) = LAST_CHECK.lock() {
        if let Some(last) = *guard {
            if last.elapsed() < std::time::Duration::from_millis(2000) {
                return false;
            }
        }
        *guard = Some(std::time::Instant::now());
    }
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or_else(|| Path::new("."));
    [workspace.join("engine"), workspace.join("plugins")]
        .iter()
        .filter_map(|path| newest_dev_source_mtime(path))
        .any(|modified| modified > engine.started_at)
}

#[tauri::command]
fn engine_request(
    state: tauri::State<'_, EngineState>,
    app: tauri::AppHandle,
    method: String,
    params: Value,
) -> Result<Value, String> {
    let call_start = std::time::Instant::now();
    let mut params = params;
    if method == "clipboard_set_autostart" || method == "app_set_autostart" {
        if let Some(object) = params.as_object_mut() {
            if !object.contains_key("command") {
                if let Ok(executable) = std::env::current_exe() {
                    object.insert(
                        "command".into(),
                        json!(format!("\"{}\"", executable.display())),
                    );
                }
            }
        }
    }
    let mut guard = state
        .0
        .lock()
        .map_err(|e| {
            let msg = format!("engine lock poisoned: {e}");
            eprintln!("============================================================");
            eprintln!(" [FATAL] [Engine::{}] {}", method, msg);
            eprintln!("============================================================");
            msg
        })?;
    if guard.is_none() {
        eprintln!("[Engine] No engine process — spawning for method '{}'", method);
        *guard = Some(start_engine(&app)?);
    }
    if guard.as_ref().is_some_and(dev_engine_reload_required) {
        eprintln!("[Engine] Dev source change detected — reloading engine for method '{}'", method);
        *guard = Some(start_engine(&app)?);
    }
    let engine = guard.as_mut().ok_or_else(|| {
        let msg = "engine unavailable after spawn attempt".to_string();
        eprintln!("============================================================");
        eprintln!(" [FATAL] [Engine::{}] {}", method, msg);
        eprintln!("============================================================");
        msg
    })?;
    let engine_pid = engine._child.id();
    let engine_uptime_secs = engine.started_at.elapsed().map(|d| d.as_secs()).unwrap_or(0);
    engine.request_id += 1;
    let req_id = engine.request_id;
    let request =
        json!({ "jsonrpc": "2.0", "id": req_id, "method": method, "params": params });

    // --- Write request to engine stdin ---
    if let Err(e) = writeln!(engine.stdin, "{}", request).and_then(|_| engine.stdin.flush()) {
        let elapsed = call_start.elapsed();
        let exit_status = engine._child.try_wait().ok().flatten();
        eprintln!("============================================================");
        eprintln!(" [ENGINE PIPE FAILURE] method: '{}' (request #{})", method, req_id);
        eprintln!(" Error:          {}", e);
        eprintln!(" Error Kind:     {:?}", e.kind());
        eprintln!(" Engine PID:     {}", engine_pid);
        eprintln!(" Engine Uptime:  {}s", engine_uptime_secs);
        eprintln!(" Elapsed:        {:.1}ms", elapsed.as_secs_f64() * 1000.0);
        if let Some(status) = exit_status {
            eprintln!(" Process Exited: {}", status);
        } else {
            eprintln!(" Process Status: still running (pipe broken despite live process)");
        }
        eprintln!(" Params:         {}", serde_json::to_string(&params).unwrap_or_default());
        eprintln!("============================================================");
        *guard = None;
        return Err(format!(
            "engine communication failed: {e} (pid={engine_pid}, uptime={engine_uptime_secs}s, method={method}); will respawn on next request"
        ));
    }

    // --- Read response from engine stdout ---
    let mut line = String::new();
    if let Err(e) = engine.stdout.read_line(&mut line) {
        let elapsed = call_start.elapsed();
        let exit_status = engine._child.try_wait().ok().flatten();
        eprintln!("============================================================");
        eprintln!(" [ENGINE READ FAILURE] method: '{}' (request #{})", method, req_id);
        eprintln!(" Error:          {}", e);
        eprintln!(" Error Kind:     {:?}", e.kind());
        eprintln!(" Engine PID:     {}", engine_pid);
        eprintln!(" Engine Uptime:  {}s", engine_uptime_secs);
        eprintln!(" Elapsed:        {:.1}ms", elapsed.as_secs_f64() * 1000.0);
        if let Some(status) = exit_status {
            eprintln!(" Process Exited: {}", status);
        } else {
            eprintln!(" Process Status: still running");
        }
        eprintln!(" Partial Read:   {:?}", if line.is_empty() { "(empty)" } else { &line });
        eprintln!("============================================================");
        *guard = None;
        return Err(format!(
            "failed reading engine response: {e} (pid={engine_pid}, method={method}); will respawn on next request"
        ));
    }
    if line.is_empty() {
        let elapsed = call_start.elapsed();
        let exit_status = engine._child.try_wait().ok().flatten();
        eprintln!("============================================================");
        eprintln!(" [ENGINE UNEXPECTED EXIT] method: '{}' (request #{})", method, req_id);
        eprintln!(" Engine PID:     {}", engine_pid);
        eprintln!(" Engine Uptime:  {}s", engine_uptime_secs);
        eprintln!(" Elapsed:        {:.1}ms", elapsed.as_secs_f64() * 1000.0);
        if let Some(status) = exit_status {
            eprintln!(" Exit Status:    {}", status);
        } else {
            eprintln!(" Exit Status:    unknown (process vanished)");
        }
        eprintln!(" Params:         {}", serde_json::to_string(&params).unwrap_or_default());
        eprintln!("============================================================");
        *guard = None;
        return Err(format!(
            "engine exited unexpectedly (pid={engine_pid}, uptime={engine_uptime_secs}s, method={method}); will respawn on next request"
        ));
    }

    // --- Parse JSON response ---
    let response: Value = match serde_json::from_str(&line) {
        Ok(v) => v,
        Err(e) => {
            let elapsed = call_start.elapsed();
            let preview = if line.len() > 500 { &line[..500] } else { &line };
            eprintln!("============================================================");
            eprintln!(" [ENGINE JSON PARSE FAILURE] method: '{}' (request #{})", method, req_id);
            eprintln!(" Parse Error:    {}", e);
            eprintln!(" Engine PID:     {}", engine_pid);
            eprintln!(" Elapsed:        {:.1}ms", elapsed.as_secs_f64() * 1000.0);
            eprintln!(" Raw Response:   {:?}", preview);
            eprintln!(" Response Len:   {} bytes", line.len());
            eprintln!("============================================================");
            return Err(format!("engine returned invalid JSON for {method}: {e}"));
        }
    };

    // --- Handle RPC error response ---
    if let Some(error) = response.get("error") {
        let elapsed = call_start.elapsed();
        let err_msg = error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("engine error");
        let err_code = error.get("code").and_then(Value::as_i64);
        let err_type = error.get("type").and_then(Value::as_str);
        let err_data = error.get("data");
        let traceback = error.get("traceback").and_then(Value::as_str);

        eprintln!("============================================================");
        eprintln!(" [ENGINE RPC ERROR] method: '{}' (request #{})", method, req_id);
        eprintln!(" Error:          {}", err_msg);
        if let Some(code) = err_code {
            eprintln!(" Code:           {}", code);
        }
        if let Some(t) = err_type {
            eprintln!(" Type:           {}", t);
        }
        if let Some(data) = err_data {
            if let Ok(pretty) = serde_json::to_string_pretty(data) {
                eprintln!(" Data:           {}", pretty);
            }
        }
        eprintln!(" Engine PID:     {}", engine_pid);
        eprintln!(" Elapsed:        {:.1}ms", elapsed.as_secs_f64() * 1000.0);
        eprintln!(" Params:         {}", serde_json::to_string(&params).unwrap_or_default());
        if let Some(tb) = traceback {
            eprintln!("--- Traceback ---");
            eprintln!("{}", tb);
            eprintln!("--- End Traceback ---");
        }
        eprintln!("============================================================");
        return Err(err_msg.into());
    }
    Ok(response.get("result").cloned().unwrap_or(Value::Null))
}

#[tauri::command]
fn dev_log(_level: String, _tag: String, _message: String, _details: Option<Value>) {
    #[cfg(debug_assertions)]
    {
        eprintln!("[{_level}] [{_tag}] {_message}");
        if let Some(d) = _details {
            if let Ok(pretty) = serde_json::to_string_pretty(&d) {
                eprintln!("  Details: {pretty}");
            }
        }
    }
}

#[tauri::command]
fn engine_restart(
    state: tauri::State<'_, EngineState>,
    app: tauri::AppHandle,
) -> Result<bool, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "engine lock poisoned".to_string())?;
    *guard = Some(start_engine(&app)?);
    Ok(true)
}

fn browser_script_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("workspace path unavailable")?;
    let development = workspace.join("scripts").join("build_browser_extension.py");
    if development.is_file() {
        return Ok(development);
    }
    app.path()
        .resource_dir()
        .map(|dir| dir.join("scripts").join("build_browser_extension.py"))
        .map_err(|error| error.to_string())
        .and_then(|path| if path.is_file() { Ok(path) } else { Err("browser build helper is not packaged".into()) })
}

fn browser_host_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let name = if cfg!(target_os = "windows") { "transfer-engine.exe" } else { "transfer-engine" };
    let resource = app.path().resource_dir().map_err(|error| error.to_string())?.join(name);
    if resource.is_file() {
        return Ok(resource);
    }
    Err("the packaged transfer engine is required before browser capture installation".into())
}

fn browser_lifecycle(app: &tauri::AppHandle, action: &str, registration_root: Option<String>) -> Result<Value, String> {
    let script = browser_script_path(app)?;
    let output_dir = app.path().app_data_dir().map_err(|error| error.to_string())?.join("browser-extension");
    let python = if cfg!(target_os = "windows") { "python" } else { "python3" };
    let mut command = Command::new(python);
    command.arg(script).arg(action).arg("--output").arg(output_dir);
    if let Some(root) = registration_root {
        command.arg("--registration-root").arg(root);
    }
    if action == "install" {
        command.arg("--host-path").arg(browser_host_path(app)?);
    }
    let output = command.output().map_err(|error| format!("could not run browser lifecycle helper: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    serde_json::from_slice(&output.stdout).map_err(|error| format!("invalid browser lifecycle response: {error}"))
}

#[tauri::command]
fn browser_install(app: tauri::AppHandle, registration_root: Option<String>) -> Result<Value, String> {
    browser_lifecycle(&app, "install", registration_root)
}

#[tauri::command]
fn browser_status(app: tauri::AppHandle, registration_root: Option<String>) -> Result<Value, String> {
    browser_lifecycle(&app, "status", registration_root)
}

#[tauri::command]
fn browser_uninstall(app: tauri::AppHandle, registration_root: Option<String>) -> Result<Value, String> {
    browser_lifecycle(&app, "uninstall", registration_root)
}

/// Applies a Windows backdrop material, falling back through the materials the
/// running Windows build supports. Returns the material that actually applied,
/// or "none" when the window stays opaque.
fn apply_window_material(window: &tauri::WebviewWindow, material: &str) -> Result<String, String> {
    #[cfg(target_os = "windows")]
    {
        use tauri::utils::WindowEffect;
        use tauri::window::EffectsBuilder;
        if material == "none" {
            window
                .set_effects(EffectsBuilder::new().build())
                .map_err(|error| error.to_string())?;
            return Ok("none".into());
        }
        // Preferred material first, then the ones older Windows builds support.
        let order: [&str; 3] = match material {
            "acrylic" => ["acrylic", "mica", "blur"],
            _ => ["mica", "acrylic", "blur"],
        };
        let mut last_error = String::from("no supported window material");
        for name in order {
            let effect = match name {
                "acrylic" => WindowEffect::Acrylic,
                "blur" => WindowEffect::Blur,
                _ => WindowEffect::Mica,
            };
            match window.set_effects(EffectsBuilder::new().effect(effect).build()) {
                Ok(()) => return Ok(name.into()),
                Err(error) => last_error = error.to_string(),
            }
        }
        let _ = window.set_effects(EffectsBuilder::new().build());
        Err(last_error)
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = (window, material);
        Ok("none".into())
    }
}

#[tauri::command]
fn set_window_material(window: tauri::WebviewWindow, material: String) -> Result<String, String> {
    apply_window_material(&window, &material)
}

#[tauri::command]
fn window_action(window: tauri::WebviewWindow, action: String) -> Result<(), String> {
    match action.as_str() {
        "minimize" => window.minimize(),
        "toggle_maximize" => {
            if window.is_maximized().map_err(|error| error.to_string())? {
                window.unmaximize()
            } else {
                window.maximize()
            }
        }
        "close" => window.close(),
        "destroy" => window.destroy(),
        "start_dragging" => window.start_dragging(),
        _ => return Err(format!("unknown window action: {action}")),
    }
    .map_err(|error: tauri::Error| error.to_string())
}

#[tauri::command]
fn pick_directory() -> Result<Option<String>, String> {
    Ok(rfd::FileDialog::new()
        .set_title("Choose download directory")
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned()))
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    if path.starts_with("http://") || path.starts_with("https://") {
        // URLs come from remote pages, so they must never pass through a shell:
        // `cmd /C start` treats `&`, `|` and `^` in a URL as command syntax.
        #[cfg(target_os = "windows")]
        let mut command = {
            let mut command = Command::new("rundll32.exe");
            command.args(["url.dll,FileProtocolHandler", &path]);
            command
        };
        #[cfg(target_os = "macos")]
        let mut command = {
            let mut command = Command::new("open");
            command.arg(&path);
            command
        };
        #[cfg(all(unix, not(target_os = "macos")))]
        let mut command = {
            let mut command = Command::new("xdg-open");
            command.arg(&path);
            command
        };
        return command
            .spawn()
            .map(|_| ())
            .map_err(|error| {
                eprintln!("[DesktopShell] Failed to open URL '{}': {}", path, error);
                error.to_string()
            });
    }

    let path = std::path::PathBuf::from(path);
    if !path.exists() {
        return Err("path does not exist".into());
    }
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("explorer");
        command.arg(path);
        command
    };
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("open");
        command.arg(path);
        command
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut command = Command::new("xdg-open");
        command.arg(path);
        command
    };
    command.spawn().map(|_| ()).map_err(|error| error.to_string())
}

#[tauri::command]
fn check_python_environment(app: tauri::AppHandle) -> Result<Value, String> {
    let resource_dir = app.path().resource_dir().ok();
    let sidecar_name = if cfg!(target_os = "windows") {
        "transfer-engine.exe"
    } else {
        "transfer-engine"
    };
    let sidecar_exists = resource_dir
        .as_ref()
        .map(|dir| dir.join(sidecar_name).exists())
        .unwrap_or(false);

    let python_cmd = if cfg!(target_os = "windows") { "python" } else { "python3" };
    let output = Command::new(python_cmd)
        .arg("--version")
        .output();

    match output {
        Ok(out) if out.status.success() => {
            let version_str = String::from_utf8_lossy(&out.stdout)
                .trim()
                .to_string();
            let final_ver = if version_str.is_empty() {
                String::from_utf8_lossy(&out.stderr).trim().to_string()
            } else {
                version_str
            };
            Ok(json!({
                "installed": true,
                "version": final_ver,
                "sidecar_available": sidecar_exists,
                "can_run": true
            }))
        }
        _ => {
            Ok(json!({
                "installed": false,
                "version": null,
                "sidecar_available": sidecar_exists,
                "can_run": sidecar_exists
            }))
        }
    }
}

#[tauri::command]
fn install_python_winget() -> Result<String, String> {
    #[cfg(target_os = "windows")]
    {
        let mut cmd = Command::new("powershell");
        cmd.args(["-NoProfile", "-Command", "Start-Process winget -ArgumentList 'install --id Python.Python.3.12 --exact --source winget --accept-source-agreements --accept-package-agreements' -Verb RunAs"]);
        cmd.spawn().map_err(|e| format!("could not launch winget installer: {e}"))?;
        Ok("Installer launched with administrator privileges".into())
    }
    #[cfg(not(target_os = "windows"))]
    {
        Err("Automatic installer is only supported on Windows. Please install Python using your system package manager.".into())
    }
}

fn get_or_create_dev_logs_window(app: &tauri::AppHandle) -> Result<tauri::WebviewWindow, String> {
    if let Some(window) = app.get_webview_window("dev-logs") {
        return Ok(window);
    }
    let initial_url = if let Ok(raw_url) = std::env::var("TRANSFER_DEV_URL") {
        if let Ok(mut parsed) = Url::parse(&raw_url) {
            parsed.set_query(Some("mode=dev-logs"));
            WebviewUrl::External(parsed)
        } else {
            WebviewUrl::App("index.html?mode=dev-logs".into())
        }
    } else {
        #[cfg(debug_assertions)]
        {
            if let Some(dev_url) = &app.config().build.dev_url {
                let mut u = dev_url.clone();
                u.set_query(Some("mode=dev-logs"));
                WebviewUrl::External(u)
            } else if let Ok(mut u) = Url::parse("http://localhost:1420") {
                u.set_query(Some("mode=dev-logs"));
                WebviewUrl::External(u)
            } else {
                WebviewUrl::App("index.html?mode=dev-logs".into())
            }
        }
        #[cfg(not(debug_assertions))]
        {
            WebviewUrl::App("index.html?mode=dev-logs".into())
        }
    };

    let window = WebviewWindowBuilder::new(app, "dev-logs", initial_url)
        .title("MossDL — Dev Logs")
        .inner_size(960.0, 600.0)
        .min_inner_size(640.0, 400.0)
        .resizable(true)
        .visible(false)
        .build()
        .map_err(|e| format!("could not create dev-logs window: {e}"))?;

    let win_clone = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = win_clone.hide();
        }
    });

    Ok(window)
}

#[tauri::command]
fn open_dev_logs_window(app: tauri::AppHandle) -> Result<(), String> {
    let window = get_or_create_dev_logs_window(&app)?;
    window.unminimize().map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn close_dev_logs_window(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("dev-logs") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn toggle_dev_logs_window(app: tauri::AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("dev-logs") {
        let is_vis = window.is_visible().unwrap_or(false);
        if is_vis {
            window.hide().map_err(|e| e.to_string())?;
            return Ok(false);
        } else {
            window.unminimize().map_err(|e| e.to_string())?;
            window.show().map_err(|e| e.to_string())?;
            window.set_focus().map_err(|e| e.to_string())?;
            return Ok(true);
        }
    }
    let window = get_or_create_dev_logs_window(&app)?;
    window.unminimize().map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    Ok(true)
}

#[tauri::command]
fn is_dev_logs_window_open(app: tauri::AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("dev-logs") {
        Ok(window.is_visible().unwrap_or(false))
    } else {
        Ok(false)
    }
}

#[tauri::command]
fn solve_multipart_captcha(
    state: tauri::State<'_, EngineState>,
    app: tauri::AppHandle,
    group_id: String,
    host: String,
    page_url: String,
) -> Result<Value, String> {
    engine_request(
        state,
        app,
        "solve_multipart_captcha".into(),
        json!({
            "group_id": group_id,
            "host": host,
            "page_url": page_url,
        }),
    )
}

pub fn run() {
    if !ensure_single_instance() {
        return;
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(EngineState(Mutex::new(None)))
        .setup(|app| {
            // The config normally creates this window, but direct EXE launches
            // can run with stale generated context. Build the one required
            // window explicitly so the process can never remain headless.
            let window = if let Some(window) = app.get_webview_window("main") {
                window
            } else {
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("MossDL")
                    .inner_size(1180.0, 780.0)
                    .min_inner_size(720.0, 600.0)
                    .decorations(false)
                    .transparent(true)
                    .resizable(true)
                    .center()
                    .visible(false)
                    .focused(true)
                    .build()
                    .map_err(|e| format!("could not create main window: {e}"))?
            };
            let show = MenuItem::with_id(app, "show", "Show MossDL", true, None::<&str>)
                .map_err(|e| format!("could not create tray menu: {e}"))?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)
                .map_err(|e| format!("could not create tray menu: {e}"))?;
            let menu = Menu::with_items(app, &[&show, &quit])
                .map_err(|e| format!("could not create tray menu: {e}"))?;
            TrayIconBuilder::new()
                .menu(&menu)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.destroy();
                        }
                    } else if event.id.as_ref() == "show" {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.unminimize();
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)
                .map_err(|e| format!("could not create system tray: {e}"))?;
            window.set_title("MossDL").map_err(|e| format!("could not set main window title: {e}"))?;
            // Windows draws its own material behind the transparent window; the
            // UI then tints it. Failure is never fatal: the app simply stays opaque.
            let _ = apply_window_material(&window, "mica");
            if let Ok(raw_url) = std::env::var("TRANSFER_DEV_URL") {
                let parsed = Url::parse(&raw_url)
                    .map_err(|_| "TRANSFER_DEV_URL is malformed".to_string())?;
                let host = parsed.host_str().unwrap_or_default();
                if parsed.scheme() != "http" || !matches!(host, "127.0.0.1" | "localhost") {
                    return Err("TRANSFER_DEV_URL must be a local HTTP URL".into());
                }
                window.navigate(parsed).map_err(|e| format!("could not load development frontend: {e}"))?;
            }
            // Keep the initial packaged page hidden while a development URL
            // is being installed. This prevents a visible white navigation
            // flash and also makes the first displayed document deterministic.
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            updates::update_check,
            updates::update_install,
            engine_request,
            solve_multipart_captcha,
            pick_directory,
            open_path,
            engine_restart,
            browser_install,
            browser_status,
            browser_uninstall,
            window_action,
            set_window_material,
            dev_log,
            check_python_environment,
            install_python_winget,
            open_dev_logs_window,
            close_dev_logs_window,
            toggle_dev_logs_window,
            is_dev_logs_window_open
        ])
        .run(tauri::generate_context!())
        .expect("error while running MossDL");
}
