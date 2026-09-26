//! End to end over loopback: a second WireGuard peer plays the VPN server
//! (see loopback.rs). The tunnel is reached the way the app reaches it,
//! through its SOCKS port.

use super::loopback::{vpn_server, BULK, SERVER_SECRET};
use super::{TunnelConfig, Tunnels};
use base64::Engine as _;
use boringtun::x25519::{PublicKey, StaticSecret};
use tokio::net::UdpSocket;

const CLIENT_SECRET: [u8; 32] = [9; 32];

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

async fn start_tunnel() -> (Tunnels, String) {
    let server = UdpSocket::bind("127.0.0.1:0").await.expect("server socket");
    let endpoint = server.local_addr().expect("addr");
    tokio::spawn(vpn_server(server, PublicKey::from(&StaticSecret::from(CLIENT_SECRET))));
    let config: TunnelConfig = serde_json::from_value(serde_json::json!({
        "private_key": b64(&CLIENT_SECRET),
        "addresses": ["10.0.0.2/32"],
        "dns": ["10.0.0.1"],
        "peer": {
            "public_key": b64(PublicKey::from(&StaticSecret::from(SERVER_SECRET)).as_bytes()),
            "endpoint": endpoint.to_string(),
        },
    })).expect("config");
    let tunnels = Tunnels::default();
    let started = tunnels.start("test", config).await.expect("tunnel starts");
    let socks = started["socks"].as_str().expect("socks address").to_string();
    (tunnels, socks)
}

fn client(socks: &str) -> reqwest::Client {
    reqwest::Client::builder()
        .proxy(reqwest::Proxy::all(format!("socks5h://{socks}")).expect("proxy"))
        .build()
        .expect("client")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn fetches_by_name_through_the_tunnel() {
    let (tunnels, socks) = start_tunnel().await;
    let body = client(&socks).get("http://test.internal/small").send().await.expect("request").text().await.expect("body");
    assert_eq!(body, "hello");
    let missing = client(&socks).get("http://nowhere.internal/").send().await;
    assert!(missing.is_err(), "a name the tunnel's resolver does not know must fail, not leak to the local one");
    assert!(tunnels.stop("test").await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_stopped_tunnel_refuses_instead_of_going_direct() {
    let (tunnels, socks) = start_tunnel().await;
    tunnels.stop("test").await;
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert!(client(&socks).get("http://test.internal/small").send().await.is_err());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_server_that_never_answers_is_reported() {
    let silent = UdpSocket::bind("127.0.0.1:0").await.expect("socket");
    let config: TunnelConfig = serde_json::from_value(serde_json::json!({
        "private_key": b64(&CLIENT_SECRET), "addresses": ["10.0.0.2/32"],
        "peer": {"public_key": b64(&[3u8; 32]), "endpoint": silent.local_addr().unwrap().to_string()},
    })).expect("config");
    let error = Tunnels::default().start("dead", config).await.expect_err("no handshake");
    assert!(error.contains("no WireGuard handshake"), "{error}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn bulk_throughput() {
    let (tunnels, socks) = start_tunnel().await;
    let started = std::time::Instant::now();
    let bytes = client(&socks).get("http://test.internal/bulk").send().await.expect("request").bytes().await.expect("body");
    let seconds = started.elapsed().as_secs_f64();
    assert_eq!(bytes.len(), BULK);
    assert!(bytes.iter().all(|b| *b == 0x5A));
    eprintln!("tunnel loopback: {:.1} MB/s", BULK as f64 / seconds / 1e6);
    tunnels.stop("test").await;
}
