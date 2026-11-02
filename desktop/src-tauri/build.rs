fn main() {
    // App commands get explicit allow-/deny- permissions, so nothing is callable unless a
    // capability grants it (capabilities/setup.json: the local setup page only).
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["setup_state", "connect"])),
    )
    .expect("failed to run tauri-build");
}
