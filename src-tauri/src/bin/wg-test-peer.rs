//! A WireGuard "VPN server" on loopback for the engine's integration tests.
//!
//! Prints {"conf": "<a client .conf for this peer>"} and serves until stdin
//! closes. Never bundled with the app.

use base64::Engine as _;
use boringtun::x25519::{PublicKey, StaticSecret};
use std::io::Read;

#[path = "transfer_core/tunnel/device.rs"]
mod device;
#[path = "transfer_core/tunnel/loopback.rs"]
mod loopback;

#[tokio::main]
async fn main() {
    const CLIENT_SECRET: [u8; 32] = [9; 32];
    let b64 = |bytes: &[u8]| base64::engine::general_purpose::STANDARD.encode(bytes);
    let client_public = PublicKey::from(&StaticSecret::from(CLIENT_SECRET));
    let socket = tokio::net::UdpSocket::bind("127.0.0.1:0").await.expect("bind");
    let endpoint = socket.local_addr().expect("address");
    let public = PublicKey::from(&StaticSecret::from(loopback::SERVER_SECRET));
    let conf = format!(
        "[Interface]
PrivateKey = {}
Address = 10.0.0.2/32
DNS = 10.0.0.1

[Peer]
PublicKey = {}
AllowedIPs = 0.0.0.0/0
Endpoint = {endpoint}
",
        b64(&CLIENT_SECRET),
        b64(public.as_bytes()),
    );
    println!("{}", serde_json::json!({ "conf": conf }));
    tokio::spawn(loopback::vpn_server(socket, client_public));
    // Serve until the test closes our stdin.
    tokio::task::spawn_blocking(|| {
        let _ = std::io::stdin().read(&mut [0u8; 1]);
    })
    .await
    .expect("stdin watcher");
}
