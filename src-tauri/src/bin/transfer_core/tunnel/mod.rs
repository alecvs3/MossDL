//! WireGuard tunnels run inside the transfer core, one per route.
//!
//! Each tunnel is a userspace WireGuard peer with its own TCP/IP stack,
//! reachable as a SOCKS5 port on 127.0.0.1. Nothing is installed on the
//! system: no driver, no adapter, no admin rights, and no traffic leaves on
//! the real interface except the encrypted UDP to the VPN server. When the
//! tunnel is down its port refuses connections, so nothing can fall back to
//! the direct path.

mod config;
mod device;
mod socks;
mod stack;
mod wg;

#[cfg(test)]
mod loopback;
#[cfg(test)]
mod tests;

pub use config::TunnelConfig;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot, Mutex, Notify};
use tokio::task::JoinHandle;

/// How long a new tunnel may take to complete its first handshake.
const HANDSHAKE_WAIT: Duration = Duration::from_secs(10);

struct Running {
    socks: SocketAddr,
    endpoint: SocketAddr,
    cmds: mpsc::Sender<stack::Cmd>,
    tasks: [JoinHandle<()>; 2],
}

impl Running {
    async fn stats(&self) -> Option<wg::PeerStats> {
        let (reply, rx) = oneshot::channel();
        self.cmds.send(stack::Cmd::Stats { reply }).await.ok()?;
        rx.await.ok()
    }

    fn describe(&self, stats: Option<wg::PeerStats>) -> serde_json::Value {
        serde_json::json!({
            "socks": self.socks.to_string(),
            "endpoint": self.endpoint.to_string(),
            "handshake_age": stats.as_ref().and_then(|s| s.handshake_age),
            "tx_bytes": stats.as_ref().map(|s| s.tx_bytes),
            "rx_bytes": stats.as_ref().map(|s| s.rx_bytes),
        })
    }

    fn stop(self) {
        for task in self.tasks {
            task.abort();
        }
    }
}

#[derive(Default)]
pub struct Tunnels {
    running: Mutex<HashMap<String, Running>>,
    next_index: std::sync::atomic::AtomicU32,
}

impl Tunnels {
    /// Start (or return the already running) tunnel for `id`.
    pub async fn start(&self, id: &str, config: TunnelConfig) -> Result<serde_json::Value, String> {
        let mut running = self.running.lock().await;
        if let Some(existing) = running.get(id) {
            let stats = existing.stats().await;
            if stats.is_some() {
                return Ok(existing.describe(stats));
            }
            // Its task died; replace it.
            if let Some(dead) = running.remove(id) {
                dead.stop();
            }
        }
        let resolved = config.resolve()?;
        let index = self.next_index.fetch_add(1, std::sync::atomic::Ordering::Relaxed) << 8;
        let peer = wg::Peer::connect(
            resolved.private_key, resolved.peer_public_key, resolved.preshared_key,
            resolved.keepalive, resolved.endpoint, index,
        ).await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.map_err(|e| format!("socks bind: {e}"))?;
        let socks_addr = listener.local_addr().map_err(|e| e.to_string())?;
        let (cmds, cmd_rx) = mpsc::channel(1024);
        let wake = Arc::new(Notify::new());
        let stack = stack::Stack::new(&resolved);
        let stack_task = tokio::spawn(stack::run(stack, peer, cmd_rx, wake.clone()));
        let socks_task = tokio::spawn(socks::serve(listener, cmds.clone(), wake));
        let tunnel = Running { socks: socks_addr, endpoint: resolved.endpoint, cmds, tasks: [stack_task, socks_task] };

        // Only report a tunnel that has actually reached its server.
        let deadline = tokio::time::Instant::now() + HANDSHAKE_WAIT;
        loop {
            let stats = tunnel.stats().await;
            if stats.as_ref().and_then(|s| s.handshake_age).is_some() {
                let described = tunnel.describe(stats);
                running.insert(id.to_string(), tunnel);
                return Ok(described);
            }
            if tokio::time::Instant::now() >= deadline {
                tunnel.stop();
                return Err(format!(
                    "no WireGuard handshake with {} within {}s: the key may be revoked, or UDP to the server is blocked",
                    resolved.endpoint, HANDSHAKE_WAIT.as_secs()
                ));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    pub async fn stop(&self, id: &str) -> bool {
        match self.running.lock().await.remove(id) {
            Some(tunnel) => {
                tunnel.stop();
                true
            }
            None => false,
        }
    }

    pub async fn status(&self) -> serde_json::Value {
        let running = self.running.lock().await;
        let mut out = serde_json::Map::new();
        for (id, tunnel) in running.iter() {
            let stats = tunnel.stats().await;
            out.insert(id.clone(), tunnel.describe(stats));
        }
        serde_json::Value::Object(out)
    }
}
