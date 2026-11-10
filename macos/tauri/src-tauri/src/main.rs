#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
use std::env;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{Manager, RunEvent};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const READY_PREFIX: &str = "DICT_SERVER_READY ";
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct Backend(Mutex<Option<Child>>);

fn show_startup_error(app: tauri::AppHandle, message: String) {
    let app_for_window = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(window) = app_for_window.get_webview_window("main") {
            let text = format!("Backend failed to start: {message}");
            if let Ok(json) = serde_json::to_string(&text) {
                let _ = window.eval(&format!(
                    "document.querySelector('p').textContent = {json};"
                ));
            }
            let _ = window.set_title("LanguageTrainer — startup error");
        }
    });
}

fn portable_root() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("DICT_PORTABLE_ROOT") {
        return Ok(PathBuf::from(path));
    }
    env::current_exe()
        .map_err(|error| format!("cannot locate desktop executable: {error}"))?
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "desktop executable has no parent directory".to_string())
}

fn backend_name() -> &'static str {
    if cfg!(windows) {
        "dict-backend.exe"
    } else {
        "dict-backend"
    }
}

fn start_backend(app: tauri::AppHandle) -> Result<Child, String> {
    let root = portable_root()?;
    // The backend ships as a PyInstaller onedir folder (backend/) so it starts
    // without unpacking a onefile bundle on every launch.
    let backend = root.join("backend").join(backend_name());
    let resources = root.join("resources");
    let data = root.join("data");

    if !backend.is_file() {
        return Err(format!(
            "backend executable not found: {}",
            backend.display()
        ));
    }
    if !resources.is_dir() {
        return Err(format!(
            "resources directory not found: {}",
            resources.display()
        ));
    }
    std::fs::create_dir_all(&data)
        .map_err(|error| format!("cannot create {}: {error}", data.display()))?;

    let mut command = Command::new(&backend);
    command
        .args(["server", "--host", "127.0.0.1", "--port", "0", "--init"])
        .current_dir(&root)
        .env("DICT_ROOT_DIR", &resources)
        .env("DICT_DATA_DIR", &data)
        .env("DICT_ARGOS_ROOT", data.join("argos-translate"))
        .env("NLTK_DATA", resources.join("nltk_data"))
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot start {}: {error}", backend.display()))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "cannot capture backend output".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "cannot capture backend errors".to_string())?;

    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            eprintln!("backend: {line}");
        }
    });

    std::thread::spawn(move || {
        let mut ready = false;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(json) = line.strip_prefix(READY_PREFIX) {
                match serde_json::from_str::<Value>(json)
                    .ok()
                    .and_then(|value| value["url"].as_str().map(str::to_owned))
                {
                    Some(url) => {
                        ready = true;
                        let app_for_window = app.clone();
                        let _ = app.run_on_main_thread(move || {
                            if let Some(window) = app_for_window.get_webview_window("main") {
                                if let Ok(parsed) = url.parse() {
                                    let _ = window.navigate(parsed);
                                    let _ = window.set_title("LanguageTrainer");
                                }
                            }
                        });
                    }
                    None => show_startup_error(
                        app.clone(),
                        format!("invalid readiness message: {json}"),
                    ),
                }
            }
        }
        if !ready {
            show_startup_error(app, "the backend exited before becoming ready".to_string());
        }
    });

    Ok(child)
}

fn stop_backend(app: &tauri::AppHandle) {
    let state = app.state::<Backend>();
    if let Ok(mut child) = state.0.lock() {
        if let Some(mut child) = child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    };
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            match start_backend(app.handle().clone()) {
                Ok(child) => {
                    let state = app.state::<Backend>();
                    let mut backend = state
                        .0
                        .lock()
                        .map_err(|_| std::io::Error::other("backend state poisoned"))?;
                    *backend = Some(child);
                }
                Err(error) => show_startup_error(app.handle().clone(), error),
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the desktop application");

    app.run(|app, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_backend(app);
        }
    });
}
