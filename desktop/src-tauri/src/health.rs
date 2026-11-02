//! `GET <box>/api/health`, verified with the operating system's trust store (the one the webview
//! uses too), so "this computer trusts the box's certificate" is checked before the app loads.
//! TLS verification is never switched off: an untrusted box certificate is an error the setup
//! page explains (install the box's root certificate, see desktop/README.md).

use std::time::Duration;

use ureq::tls::{RootCerts, TlsConfig};
use url::Url;

use crate::target::{parse_health, Health, Problem};

const TIMEOUT: Duration = Duration::from_secs(6);
const BODY_LIMIT: u64 = 64 * 1024;

pub fn check(origin: &Url) -> Result<Health, Problem> {
    let url = origin.join("/api/health").map_err(|e| Problem::new("address", e.to_string()))?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(TIMEOUT))
        .max_redirects(0)
        .http_status_as_error(false)
        .tls_config(TlsConfig::builder().root_certs(RootCerts::PlatformVerifier).build())
        .build()
        .into();

    let mut resp = agent
        .get(url.as_str())
        .header("Accept", "application/json")
        .call()
        .map_err(|e| classify(&e, origin))?;
    let status = resp.status().as_u16();
    if status != 200 {
        return Err(Problem::new("not_raum", format!("{url} answered HTTP {status}")));
    }
    let body = resp
        .body_mut()
        .with_config()
        .limit(BODY_LIMIT)
        .read_to_string()
        .map_err(|e| Problem::new("not_raum", e.to_string()))?;
    parse_health(&body)
}

/// Sort transport errors into what the person at the laptop can act on.
fn classify(err: &ureq::Error, origin: &Url) -> Problem {
    let text = error_chain(err);
    let lower = text.to_lowercase();
    let host = origin.host_str().unwrap_or_default();
    match err {
        ureq::Error::Timeout(_) => Problem::new("unreachable", format!("{host}: no answer within {}s", TIMEOUT.as_secs())),
        ureq::Error::HostNotFound => Problem::new("unreachable", format!("{host}: name not found")),
        // https:// to a port that speaks plain http (e.g. scripts/dev.sh without --https)
        _ if lower.contains("corrupt message") || lower.contains("invalidcontenttype") => {
            Problem::new("not_raum", format!("{text} (this port does not speak https)"))
        }
        ureq::Error::Rustls(_) | ureq::Error::Tls(_) => Problem::new("tls", text),
        _ if lower.contains("certificate") || lower.contains("unknownissuer") || lower.contains("handshake") => {
            Problem::new("tls", text)
        }
        _ => Problem::new("unreachable", text),
    }
}

fn error_chain(err: &dyn std::error::Error) -> String {
    let mut out = err.to_string();
    let mut cur = err.source();
    while let Some(e) = cur {
        let s = e.to_string();
        if !out.contains(&s) {
            out.push_str(": ");
            out.push_str(&s);
        }
        cur = e.source();
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Live check against a running box or `scripts/dev.sh`:
    /// `RAUM_TEST_BOX=https://192.168.1.50:5180 cargo test -- --ignored live_box`
    #[test]
    #[ignore]
    fn live_box() {
        let addr = std::env::var("RAUM_TEST_BOX").expect("set RAUM_TEST_BOX");
        let origin = crate::target::normalize(&addr).unwrap();
        match check(&origin) {
            Ok(h) => println!("ok: {h:?}"),
            Err(p) => panic!("{}: {}", p.code, p.detail),
        }
    }

    #[test]
    fn nothing_listening_is_unreachable() {
        // Port 9 (discard) on loopback: refused immediately on any CI runner.
        let origin = crate::target::normalize("http://127.0.0.1:9").unwrap();
        assert_eq!(check(&origin).unwrap_err().code, "unreachable");
    }
}
