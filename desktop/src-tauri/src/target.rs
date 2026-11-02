//! Box addresses and deep links. Pure functions, unit-tested; no I/O here.

use std::net::IpAddr;

use serde::Serialize;
use url::{Host, Url};

/// What went wrong, as a stable `code` the setup page translates, plus a `detail` line for ops.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Problem {
    pub code: &'static str,
    pub detail: String,
}

impl Problem {
    pub fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self { code, detail: detail.into() }
    }
}

/// `192.168.1.50:5180`, `https://192.168.1.50:5180/j/x?role=b` or `raum.local` -> the box origin
/// (`https://192.168.1.50:5180/`). HTTPS only (the microphone needs a secure context), except
/// plain HTTP on this computer itself, which browsers also treat as secure (`scripts/dev.sh`).
pub fn normalize(input: &str) -> Result<Url, Problem> {
    let s = input.trim();
    if s.is_empty() {
        return Err(Problem::new("address", "empty address"));
    }
    let with_scheme = if s.contains("://") { s.to_string() } else { format!("https://{s}") };
    let url = Url::parse(&with_scheme).map_err(|e| Problem::new("address", e.to_string()))?;
    if url.host_str().is_none_or(str::is_empty) {
        return Err(Problem::new("address", "no host in the address"));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(Problem::new("address", "user names and passwords do not belong in the address"));
    }
    match url.scheme() {
        "https" => {}
        "http" if is_loopback(&url) => {}
        "http" => {
            return Err(Problem::new(
                "insecure",
                "plain http: the microphone needs https (or this computer itself)",
            ))
        }
        other => return Err(Problem::new("address", format!("unsupported scheme {other}:"))),
    }
    Url::parse(&url.origin().ascii_serialization()).map_err(|e| Problem::new("address", e.to_string()))
}

fn is_loopback(url: &Url) -> bool {
    match url.host() {
        Some(Host::Domain(d)) => d.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(ip)) => IpAddr::V4(ip).is_loopback(),
        Some(Host::Ipv6(ip)) => IpAddr::V6(ip).is_loopback(),
        None => false,
    }
}

/// Same scheme, host and port.
pub fn same_origin(a: &Url, b: &Url) -> bool {
    a.origin() == b.origin()
}

/// Pages the shell itself serves: the setup page in `setup/` (tauri://localhost on macOS and
/// Linux, http(s)://tauri.localhost on Windows).
pub fn is_local_page(url: &Url) -> bool {
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    }
}

/// The local setup page. `change=1` stops it from connecting to the saved box on its own.
pub fn local_setup_url(change: bool) -> Url {
    let base = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    let mut url = Url::parse(base).expect("static url");
    if change {
        url.set_query(Some("change=1"));
    }
    url
}

/// `raum://` links -> the path to open on the configured box. Only known routes and parameters
/// pass; a link can never change which box the app talks to.
///
/// - `raum://j/<room>?role=a|b&lang=xx` and `raum://join?room=<room>&role=..&lang=..` -> `/j/<room>?..`
/// - `raum://l/<room>` (listen-only), `raum://t/<room>?theme=dark|light` (table screen)
/// - `raum://open` or `raum://` -> `/`
pub fn deep_link_path(link: &Url) -> Option<String> {
    if link.scheme() != "raum" {
        return None;
    }
    let host = link.host_str().unwrap_or("");
    let segments: Vec<&str> = link
        .path_segments()
        .map(|s| s.filter(|x| !x.is_empty()).collect())
        .unwrap_or_default();
    let query: Vec<(String, String)> = link.query_pairs().into_owned().collect();
    let param = |key: &str| query.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str());

    let (route, room) = match (host, segments.as_slice()) {
        ("j" | "l" | "t", [room]) => (host, (*room).to_string()),
        ("join", []) => ("j", param("room")?.to_string()),
        ("open" | "", []) => return Some("/".into()),
        _ => return None,
    };
    if !valid_room(&room) {
        return None;
    }

    let mut qs = url::form_urlencoded::Serializer::new(String::new());
    match route {
        "j" => {
            if let Some(role) = param("role").filter(|r| matches!(*r, "a" | "b")) {
                qs.append_pair("role", role);
            }
            if let Some(lang) = param("lang").filter(|l| valid_lang(l)) {
                qs.append_pair("lang", lang);
            }
        }
        "t" => {
            if let Some(theme) = param("theme").filter(|t| matches!(*t, "dark" | "light")) {
                qs.append_pair("theme", theme);
            }
        }
        _ => {}
    }
    let qs = qs.finish();
    let mut path = format!("/{route}/{room}");
    if !qs.is_empty() {
        path.push('?');
        path.push_str(&qs);
    }
    Some(path)
}

/// Room ids as the box makes them (`sz-1234`); nothing that could leave the route.
fn valid_room(room: &str) -> bool {
    (1..=64).contains(&room.len()) && room.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Language codes as raum-core accepts them (`^[a-z]{2,3}$`).
fn valid_lang(lang: &str) -> bool {
    (2..=3).contains(&lang.len()) && lang.bytes().all(|b| b.is_ascii_lowercase())
}

/// The health answer of raum-core (`GET /api/health`), reduced to what the setup page shows.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Health {
    pub profile: Option<String>,
    pub version: Option<String>,
}

/// Accept only raum-core's own answer (`{"ok": true, "service": "raum-core", ...}`).
pub fn parse_health(body: &str) -> Result<Health, Problem> {
    let v: serde_json::Value = serde_json::from_str(body)
        .map_err(|_| Problem::new("not_raum", "the answer to /api/health is not JSON"))?;
    if v.get("service").and_then(|s| s.as_str()) != Some("raum-core") {
        return Err(Problem::new("not_raum", "/api/health does not say service: raum-core"));
    }
    let text = |k: &str| v.get(k).and_then(|s| s.as_str()).map(str::to_string);
    Ok(Health { profile: text("profile"), version: text("version") })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    #[test]
    fn normalize_adds_https_and_keeps_only_the_origin() {
        assert_eq!(normalize("192.168.1.50:5180").unwrap().as_str(), "https://192.168.1.50:5180/");
        assert_eq!(normalize(" https://192.168.1.50:5180/j/sz-1?role=b#x ").unwrap().as_str(), "https://192.168.1.50:5180/");
        assert_eq!(normalize("https://box.example.ts.net:443").unwrap().as_str(), "https://box.example.ts.net/");
        assert_eq!(normalize("HTTPS://Raum.Local").unwrap().as_str(), "https://raum.local/");
        assert_eq!(normalize("[fd00::5]:5180").unwrap().as_str(), "https://[fd00::5]:5180/");
    }

    #[test]
    fn normalize_allows_http_only_on_this_computer() {
        assert_eq!(normalize("http://localhost:5180").unwrap().as_str(), "http://localhost:5180/");
        assert_eq!(normalize("http://127.0.0.1:5180").unwrap().as_str(), "http://127.0.0.1:5180/");
        assert_eq!(normalize("http://[::1]:5180").unwrap().as_str(), "http://[::1]:5180/");
        assert_eq!(normalize("http://192.168.1.50:5180").unwrap_err().code, "insecure");
        assert_eq!(normalize("http://localhost.evil.example").unwrap_err().code, "insecure");
    }

    #[test]
    fn normalize_rejects_junk() {
        for bad in ["", "   ", "ftp://box", "https://", "https://user:pw@box", "file:///etc/passwd", "javascript:alert(1)"] {
            assert!(normalize(bad).is_err(), "{bad:?} should be rejected");
        }
    }

    #[test]
    fn same_origin_compares_scheme_host_port() {
        let b = normalize("192.168.1.50:5180").unwrap();
        assert!(same_origin(&b, &url("https://192.168.1.50:5180/r/sz-1")));
        assert!(!same_origin(&b, &url("https://192.168.1.50/r/sz-1")));
        assert!(!same_origin(&b, &url("http://192.168.1.50:5180/")));
        assert!(!same_origin(&b, &url("https://192.168.1.51:5180/")));
        let d = normalize("box.example").unwrap();
        assert!(same_origin(&d, &url("https://box.example:443/x")));
    }

    #[test]
    fn local_pages() {
        assert!(is_local_page(&url("tauri://localhost/index.html")));
        assert!(is_local_page(&url("http://tauri.localhost/index.html?change=1")));
        assert!(!is_local_page(&url("https://localhost:5180/")));
        assert!(!is_local_page(&url("https://tauri.localhost.evil.example/")));
        assert!(is_local_page(&local_setup_url(true)));
        assert_eq!(local_setup_url(true).query(), Some("change=1"));
    }

    #[test]
    fn deep_links_map_to_box_paths() {
        let p = |s: &str| deep_link_path(&url(s));
        assert_eq!(p("raum://j/sz-1234?role=b&lang=ar").as_deref(), Some("/j/sz-1234?role=b&lang=ar"));
        assert_eq!(p("raum://j/sz-1234/").as_deref(), Some("/j/sz-1234"));
        assert_eq!(p("raum://join?room=kz-77&role=a").as_deref(), Some("/j/kz-77?role=a"));
        assert_eq!(p("raum://l/sz-1234").as_deref(), Some("/l/sz-1234"));
        assert_eq!(p("raum://t/sz-1234?theme=dark").as_deref(), Some("/t/sz-1234?theme=dark"));
        assert_eq!(p("raum://open").as_deref(), Some("/"));
        assert_eq!(p("raum://").as_deref(), Some("/"));
    }

    #[test]
    fn deep_links_drop_unknown_parameters_and_routes() {
        let p = |s: &str| deep_link_path(&url(s));
        assert_eq!(p("raum://j/sz-1?role=admin&lang=EN&box=https://evil").as_deref(), Some("/j/sz-1"));
        assert_eq!(p("raum://j/sz-1?lang=de-DE").as_deref(), Some("/j/sz-1"));
        assert_eq!(p("raum://admin"), None);
        assert_eq!(p("raum://r/sz-1"), None);
        assert_eq!(p("raum://j/..%2Fadmin"), None);
        assert_eq!(p("raum://j/a/b"), None);
        assert_eq!(p("raum://join"), None);
        assert_eq!(p("https://192.168.1.50/j/sz-1"), None);
        let long = format!("raum://j/{}", "x".repeat(65));
        assert_eq!(p(&long), None);
    }

    #[test]
    fn health_must_come_from_raum_core() {
        let ok = parse_health(r#"{"ok":true,"service":"raum-core","profile":"spark","version":"0.4.0","rooms":0}"#).unwrap();
        assert_eq!(ok.profile.as_deref(), Some("spark"));
        assert_eq!(ok.version.as_deref(), Some("0.4.0"));
        assert_eq!(parse_health(r#"{"ok":true,"service":"something-else"}"#).unwrap_err().code, "not_raum");
        assert_eq!(parse_health("<html>router login</html>").unwrap_err().code, "not_raum");
    }
}
