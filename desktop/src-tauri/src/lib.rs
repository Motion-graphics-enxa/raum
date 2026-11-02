//! RAUM desktop shell (plan F-08): the box's own web app in a native window. No second UI.
//!
//! The only local page is `setup/index.html` (the box address). Everything else is the box,
//! loaded live from `https://<box>[:port]`, exactly as a browser would show it.
//!
//! Guard rails:
//! - Navigation: a window shows the local setup page (main window only) or the verified box
//!   origin. Every other URL is cancelled (on_navigation).
//! - Permissions: microphone (and autoplay) are granted to pages of the box origin only; every
//!   other request is denied (on_permission_request). The OS still asks once per app (macOS TCC).
//! - IPC: two commands, callable from the local setup page only (capabilities/setup.json).
//!   Box pages get no IPC.
//! - TLS: the operating system's trust store for both the health check and the webview. Nothing
//!   is switched off; an untrusted box certificate is shown as such (desktop/README.md).

mod health;
mod store;
mod target;

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU32, Ordering},
        Arc, Mutex,
    },
};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    webview::{NewWindowResponse, PageLoadEvent, PermissionKind, PermissionResponse},
    AppHandle, Manager, Runtime, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_deep_link::DeepLinkExt;

use crate::target::Problem;

const MAIN: &str = "main";
const SHELL_SCHEME: &str = "raum-shell";

/// WKWebView ignores `window.print()`; hand it to the native print panel instead (macOS only).
/// The page asks by navigating to raum-shell://print, which on_navigation cancels and answers.
#[cfg(target_os = "macos")]
const PRINT_SHIM: &str = "(function(){if(location.protocol!=='https:'&&location.protocol!=='http:')return;\
window.print=function(){location.href='raum-shell://print';};})();";

#[derive(Default)]
struct Shell {
    /// The verified box origin: the only remote origin any window may show.
    box_origin: Mutex<Option<Url>>,
    /// What the setup field shows: the saved box, or the last address tried.
    address: Mutex<Option<String>>,
    /// A box path waiting for a connection (from a raum:// link).
    pending: Mutex<Option<String>>,
    /// The page each window shows (by label), for the permission check.
    pages: Mutex<HashMap<String, Url>>,
    /// Extra box windows (target=_blank, e.g. the table screen).
    extra: AtomicU32,
}

impl Shell {
    fn is_box(&self, url: &Url) -> bool {
        self.box_origin.lock().unwrap().as_ref().is_some_and(|b| target::same_origin(b, url))
    }

    fn shows_box(&self, label: &str) -> bool {
        self.pages.lock().unwrap().get(label).is_some_and(|u| self.is_box(u))
    }

    fn may_navigate<R: Runtime>(&self, app: &AppHandle<R>, label: &str, url: &Url) -> bool {
        if target::is_local_page(url) {
            return label == MAIN;
        }
        match url.scheme() {
            // empty frames the page may create for itself
            "about" => matches!(url.path(), "blank" | "srcdoc"),
            SHELL_SCHEME => {
                if url.host_str() == Some("print") && self.shows_box(label) {
                    let (app, label) = (app.clone(), label.to_string());
                    // after this navigation callback has returned
                    std::thread::spawn(move || {
                        if let Some(w) = app.get_webview_window(&label) {
                            let _ = w.print();
                        }
                    });
                }
                false
            }
            _ => self.is_box(url),
        }
    }

    fn permission(&self, label: &str, kind: PermissionKind) -> PermissionResponse {
        match kind {
            PermissionKind::Microphone | PermissionKind::Autoplay if self.shows_box(label) => PermissionResponse::Allow,
            _ => PermissionResponse::Deny,
        }
    }
}

type ShellState = Arc<Shell>;

/// One window, with the guard rails above. `main` starts on the setup page; extra windows show box
/// pages the app opened with target=_blank (table screen, a second participant).
fn build_window<R: Runtime>(app: &AppHandle<R>, label: &str, url: WebviewUrl) -> tauri::Result<WebviewWindow<R>> {
    let shell: ShellState = app.state::<ShellState>().inner().clone();
    let (nav, load, perm, popup) = (shell.clone(), shell.clone(), shell.clone(), shell);
    let (nav_app, popup_app) = (app.clone(), app.clone());
    let nav_label = label.to_string();

    let builder = WebviewWindowBuilder::new(app, label, url)
        .title("RAUM")
        .inner_size(1180.0, 800.0)
        .min_inner_size(380.0, 560.0)
        .on_navigation(move |url| nav.may_navigate(&nav_app, &nav_label, url))
        .on_page_load(move |w, p| {
            if p.event() == PageLoadEvent::Started {
                load.pages.lock().unwrap().insert(w.label().to_string(), p.url().clone());
            }
        })
        .on_permission_request(move |w, kind| perm.permission(w.label(), kind))
        .on_new_window(move |url, _features| {
            if popup.is_box(&url) {
                let n = popup.extra.fetch_add(1, Ordering::Relaxed) + 1;
                let app = popup_app.clone();
                std::thread::spawn(move || {
                    let _ = build_window(&app, &format!("box-{n}"), WebviewUrl::External(url));
                });
            }
            // Never a popup tied to the opener; other sites open nowhere.
            NewWindowResponse::Deny
        });
    #[cfg(target_os = "macos")]
    let builder = builder.initialization_script(PRINT_SHIM);

    let window = builder.build()?;
    #[cfg(target_os = "linux")]
    linux_media(&window);
    Ok(window)
}

/// WebKitGTK ships with getUserMedia and WebRTC switched off; LiveKit needs both.
#[cfg(target_os = "linux")]
fn linux_media<R: Runtime>(window: &WebviewWindow<R>) {
    let _ = window.with_webview(|pw| {
        use webkit2gtk::{SettingsExt, WebViewExt};
        if let Some(settings) = pw.inner().settings() {
            settings.set_enable_media_stream(true);
            settings.set_enable_webrtc(true);
            settings.set_media_playback_requires_user_gesture(false);
        }
    });
}

fn main_window<R: Runtime>(app: &AppHandle<R>) -> Option<WebviewWindow<R>> {
    app.get_webview_window(MAIN)
}

fn focus<R: Runtime>(w: &WebviewWindow<R>) {
    let _ = w.unminimize();
    let _ = w.show();
    let _ = w.set_focus();
}

/// Back to the box-address page; `change` keeps it from reconnecting on its own.
fn show_setup<R: Runtime>(app: &AppHandle<R>, change: bool) {
    if let Some(w) = main_window(app) {
        let _ = w.navigate(target::local_setup_url(change));
        focus(&w);
    }
}

/// raum:// links: open the room on the box, or keep the path until a box is connected.
fn open_links<R: Runtime>(app: &AppHandle<R>, urls: Vec<Url>) {
    let Some(path) = urls.iter().find_map(target::deep_link_path) else {
        return;
    };
    let shell = app.state::<ShellState>();
    let dest = shell.box_origin.lock().unwrap().as_ref().and_then(|b| b.join(&path).ok());
    match (dest, main_window(app)) {
        (Some(dest), Some(w)) => {
            let _ = w.navigate(dest);
            focus(&w);
        }
        (None, w) => {
            *shell.pending.lock().unwrap() = Some(path);
            if let Some(w) = w {
                focus(&w);
            }
        }
        (Some(_), None) => {}
    }
}

#[derive(Serialize)]
struct SetupState {
    /// Prefill for the address field (saved box or last tried).
    address: Option<String>,
    /// A room waiting for the box, e.g. "/j/sz-1234?role=b".
    pending: Option<String>,
}

#[tauri::command]
fn setup_state(shell: State<'_, ShellState>) -> SetupState {
    SetupState {
        address: shell.address.lock().unwrap().clone(),
        pending: shell.pending.lock().unwrap().clone(),
    }
}

#[derive(Serialize)]
struct Connected {
    origin: String,
    profile: Option<String>,
    version: Option<String>,
}

/// Check `<address>/api/health`, remember the box, open it (or the waiting room) in the main window.
#[tauri::command]
async fn connect(app: AppHandle, shell: State<'_, ShellState>, address: String) -> Result<Connected, Problem> {
    let origin = target::normalize(&address)?;
    *shell.address.lock().unwrap() = Some(origin.origin().ascii_serialization());

    let probe = origin.clone();
    let health = tauri::async_runtime::spawn_blocking(move || health::check(&probe))
        .await
        .map_err(|e| Problem::new("internal", e.to_string()))??;

    store::save(&app, &origin).map_err(|e| Problem::new("internal", format!("could not save the address: {e}")))?;
    *shell.box_origin.lock().unwrap() = Some(origin.clone());

    let path = shell.pending.lock().unwrap().take().unwrap_or_else(|| "/".into());
    let dest = origin.join(&path).map_err(|e| Problem::new("address", e.to_string()))?;
    let w = main_window(&app).ok_or_else(|| Problem::new("internal", "no main window"))?;
    w.navigate(dest).map_err(|e| Problem::new("internal", e.to_string()))?;

    Ok(Connected { origin: origin.origin().ascii_serialization(), profile: health.profile, version: health.version })
}

fn menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::default(app)?;
    let connection = Submenu::with_items(
        app,
        "Box",
        true,
        &[
            &MenuItem::with_id(app, "box-address", "Box address…", true, Some("CmdOrCtrl+Shift+B"))?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?,
        ],
    )?;
    menu.append(&connection)?;
    Ok(menu)
}

fn on_menu<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "box-address" => show_setup(app, true),
        "reload" => {
            let windows = app.webview_windows();
            let w = windows.values().find(|w| w.is_focused().unwrap_or(false)).or_else(|| windows.get(MAIN));
            if let Some(w) = w {
                let _ = w.reload();
            }
        }
        _ => {}
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // First, so a second launch (Windows/Linux deep link) hands its URL to this instance.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = main_window(app) {
                focus(&w);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .manage(ShellState::default())
        .invoke_handler(tauri::generate_handler![setup_state, connect])
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .setup(|app| {
            let handle = app.handle().clone();
            {
                let shell = app.state::<ShellState>();
                *shell.address.lock().unwrap() = store::load(&handle).map(|u| u.origin().ascii_serialization());
            }
            app.set_menu(menu(&handle)?)?;
            build_window(&handle, MAIN, WebviewUrl::App("index.html".into()))?;

            #[cfg(any(windows, target_os = "linux"))]
            let _ = app.deep_link().register_all(); // AppImage / portable runs; installers register too
            if let Ok(Some(urls)) = app.deep_link().get_current() {
                open_links(&handle, urls);
            }
            let links = handle.clone();
            app.deep_link().on_open_url(move |event| open_links(&links, event.urls()));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the RAUM desktop shell");
}
