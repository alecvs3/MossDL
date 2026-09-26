//! Long-lived session plumbing: one process serving many transfers.
//!
//! The core used to be spawned once per file. A fifty-file package meant fifty
//! process launches and fifty cold connection pools, and nothing could be
//! shared or governed across them. A session keeps the process alive so
//! transfers reuse connections to the same host, and so pausing can ask a
//! transfer to stop rather than killing it mid-write.

use reqwest::Client;
use std::collections::HashMap;
use std::io::{self, Write};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

pub const PAUSING: u8 = 1;
pub const CANCELING: u8 = 2;

/// Error messages the engine matches on to tell intent from failure.
pub const PAUSED_MESSAGE: &str = "transfer paused by request";
pub const CANCELED_MESSAGE: &str = "transfer canceled by request";

/// A transfer's stop switch, checked wherever a worker is about to do more work.
#[derive(Debug, Default)]
pub struct TransferControl {
    state: AtomicU8,
}

impl TransferControl {
    pub fn pause(&self) {
        self.state.store(PAUSING, Ordering::Relaxed);
    }

    pub fn cancel(&self) {
        self.state.store(CANCELING, Ordering::Relaxed);
    }

    /// `Err` once the engine has asked this transfer to stop.
    ///
    /// Workers propagate it like any other failure, which unwinds the retry
    /// loops and lets the writer finish what it already accepted -- the part
    /// file stays consistent, so a paused transfer resumes instead of
    /// restarting.
    pub fn check(&self) -> Result<(), String> {
        match self.state.load(Ordering::Relaxed) {
            PAUSING => Err(PAUSED_MESSAGE.to_string()),
            CANCELING => Err(CANCELED_MESSAGE.to_string()),
            _ => Ok(()),
        }
    }
}

/// Serializes writes so concurrent transfers cannot interleave a line.
fn stdout_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

pub fn emit(payload: &serde_json::Value) {
    let _guard = stdout_lock().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let mut out = io::stdout().lock();
    let _ = writeln!(out, "{payload}");
    let _ = out.flush();
}

/// Clients, one per distinct route.
///
/// reqwest fixes a proxy per client, so transfers sharing a route share a
/// connection pool and transfers on different routes stay properly separated.
#[derive(Default)]
pub struct ClientPool {
    clients: Mutex<HashMap<Option<String>, Client>>,
}

impl ClientPool {
    pub fn get(&self, proxy: Option<&str>) -> Result<Client, String> {
        let key = proxy.map(str::to_string);
        if let Some(client) = self
            .clients
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(&key)
        {
            return Ok(client.clone());
        }
        let client = build_client(proxy)?;
        self.clients
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(key, client.clone());
        Ok(client)
    }
}

pub fn build_client(proxy: Option<&str>) -> Result<Client, String> {
    let mut builder = Client::builder()
        .user_agent("transfer-manager-rust-core/0.1")
        .tcp_nodelay(true)
        .pool_max_idle_per_host(64)
        .tcp_keepalive(Duration::from_secs(30))
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(60));
    if let Some(endpoint) = proxy.map(str::trim).filter(|value| !value.is_empty()) {
        // no_proxy is deliberately not honoured: the route profile is an
        // explicit user choice, so leaking around it would defeat the point.
        let proxy = reqwest::Proxy::all(endpoint)
            .map_err(|error| format!("route proxy {endpoint} is unusable: {error}"))?;
        builder = builder.proxy(proxy);
    }
    builder.build().map_err(|error| error.to_string())
}

/// The transfers currently in flight, so pause and cancel can find them.
#[derive(Default)]
pub struct Registry {
    entries: Mutex<HashMap<String, Arc<TransferControl>>>,
}

impl Registry {
    pub fn register(&self, transfer_id: &str) -> Arc<TransferControl> {
        let control = Arc::new(TransferControl::default());
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(transfer_id.to_string(), control.clone());
        control
    }

    pub fn forget(&self, transfer_id: &str) {
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(transfer_id);
    }

    /// Ask a transfer to stop. False when it already finished.
    pub fn stop(&self, transfer_id: &str, cancel: bool) -> bool {
        match self
            .entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(transfer_id)
        {
            Some(control) => {
                if cancel {
                    control.cancel();
                } else {
                    control.pause();
                }
                true
            }
            None => false,
        }
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_fresh_control_is_running() {
        assert!(TransferControl::default().check().is_ok());
    }

    #[test]
    fn pause_and_cancel_are_distinguishable() {
        let control = TransferControl::default();
        control.pause();
        assert_eq!(control.check().unwrap_err(), PAUSED_MESSAGE);

        let control = TransferControl::default();
        control.cancel();
        assert_eq!(control.check().unwrap_err(), CANCELED_MESSAGE);
    }

    #[test]
    fn registry_tracks_and_forgets_transfers() {
        let registry = Registry::default();
        let control = registry.register("task-1");
        assert_eq!(registry.len(), 1);
        assert!(registry.stop("task-1", false));
        assert_eq!(control.check().unwrap_err(), PAUSED_MESSAGE);

        registry.forget("task-1");
        assert_eq!(registry.len(), 0);
        assert!(!registry.stop("task-1", false), "a finished transfer cannot be stopped");
    }

    #[test]
    fn clients_are_reused_per_route() {
        let pool = ClientPool::default();
        pool.get(None).expect("direct client");
        pool.get(None).expect("direct client again");
        pool.get(Some("socks5h://127.0.0.1:1080")).expect("proxied client");
        assert_eq!(
            pool.clients.lock().unwrap().len(),
            2,
            "one client per distinct route, shared across transfers"
        );
    }

    #[test]
    fn an_unusable_proxy_is_an_error_not_a_direct_client() {
        assert!(build_client(Some("http://")).is_err());
    }
}
