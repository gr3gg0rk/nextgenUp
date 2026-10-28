// NextGenUp desktop shell: spawns the bundled Python server as a sidecar
// and shows the web UI in a native window.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use parking_lot::Mutex;
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

const SERVER_PORT: u16 = 18790;

struct ServerChild(Mutex<Option<Child>>);

fn server_binary_name() -> &'static str {
    if cfg!(windows) {
        "nextgenup-server.exe"
    } else {
        "nextgenup-server"
    }
}

// PyInstaller one-file binaries run as a bootloader that spawns the real
// server as its own child, so killing only the direct child leaves an
// orphan. Kill the whole tree: the process group on Unix, taskkill /T on
// Windows.
fn kill_server_tree(child: &mut Child) {
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGTERM);
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .output();
    }
    let _ = child.kill();
    let _ = child.wait();
}

// Session media must not outlive the app: uploads, results and extracted
// video frames are transient, and tasks.db only maps task ids to those
// files (its rows would point at deleted outputs anyway; the server
// tolerates a missing db and recreates it on next start). models/ is kept:
// optional AI models are expensive one-time downloads. Best effort — a
// locked file (e.g. antivirus) must not block exit.
fn wipe_session_files(data_dir: &std::path::Path) {
    for name in ["uploads", "outputs", "frames"] {
        let dir = data_dir.join(name);
        match std::fs::remove_dir_all(&dir) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => eprintln!("failed to clear {}: {e}", dir.display()),
        }
        std::fs::create_dir_all(&dir).ok();
    }
    for db in ["tasks.db", "tasks.db-journal"] {
        let path = data_dir.join(db);
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => eprintln!("failed to remove {}: {e}", path.display()),
        }
    }
}

// Check GitHub releases for a newer signed build; ask, install, restart.
fn spawn_update_check(handle: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let updater = match handle.updater() {
            Ok(u) => u,
            Err(_) => return,
        };
        let update = match updater.check().await {
            Ok(Some(u)) => u,
            _ => return,
        };

        let msg = format!(
            "NextGenUp {} is available (you have {}).\n\nDownload and install now?",
            update.version, update.current_version
        );
        let install = handle
            .dialog()
            .message(msg)
            .title("Update available")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Install".to_string(),
                "Later".to_string(),
            ))
            .blocking_show();

        if install {
            // Stop the sidecar BEFORE installing: Windows cannot overwrite a
            // running exe, and on Windows download_and_install exits the
            // process as soon as the installer starts, so cleanup placed
            // after it would never run.
            if let Some(state) = handle.try_state::<ServerChild>() {
                if let Some(mut child) = state.0.lock().take() {
                    kill_server_tree(&mut child);
                }
            }
            if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
                handle.restart();
            }
        }
    });
}

fn main() {
    // Linux only (compiled out on macOS and Windows): WebKitGTK's DMA-BUF
    // renderer aborts on some Fedora/NVIDIA setups with "Could not create
    // surfaceless EGL display: EGL_BAD_ALLOC" (issue #8). Must be set before
    // the webview starts; a value the user set themselves is respected.
    #[cfg(target_os = "linux")]
    {
        if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        }
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let exe_dir = std::env::current_exe()?
                .parent()
                .expect("executable has a parent dir")
                .to_path_buf();
            let server = exe_dir.join(server_binary_name());

            let data_dir = app
                .path()
                .app_data_dir()
                .expect("app data dir available");
            std::fs::create_dir_all(&data_dir).ok();
            std::fs::create_dir_all(data_dir.join("models")).ok();

            if server.exists() {
                let mut cmd = Command::new(&server);
                cmd.env("PORT", SERVER_PORT.to_string())
                    .env("NEXTGENUP_DATA", &data_dir)
                    // The desktop backend must never listen on the network;
                    // LAN serving is a web-mode (run-from-source) feature.
                    .env("NEXTGENUP_HOST", "127.0.0.1")
                    .current_dir(&data_dir);
                #[cfg(unix)]
                {
                    use std::os::unix::process::CommandExt;
                    cmd.process_group(0);
                }
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
                    cmd.creation_flags(CREATE_NO_WINDOW);
                }
                match cmd.spawn() {
                    Ok(child) => {
                        app.manage(ServerChild(Mutex::new(Some(child))));
                    }
                    Err(e) => eprintln!("failed to start server sidecar: {e}"),
                }
            } else {
                // Dev fallback: assume `python app.py` is running separately.
                eprintln!("server sidecar not found at {server:?}; expecting a dev server");
            }

            spawn_update_check(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building NextGenUp")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // Only the packaged desktop session wipes: state exists only
                // when we spawned the sidecar. In dev mode NEXTGENUP_DATA is
                // the repo cwd, which must not be wiped on exit.
                let desktop_session = app.try_state::<ServerChild>().is_some();
                if let Some(state) = app.try_state::<ServerChild>() {
                    if let Some(mut child) = state.0.lock().take() {
                        kill_server_tree(&mut child);
                    }
                }
                if desktop_session {
                    if let Ok(data_dir) = app.path().app_data_dir() {
                        wipe_session_files(&data_dir);
                    }
                }
            }
        });
}
