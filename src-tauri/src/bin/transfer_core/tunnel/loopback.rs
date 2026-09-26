//! A WireGuard peer that plays the VPN server, for tests: its own TCP/IP
//! stack, a DNS responder (test.internal is 10.0.0.1, nothing else exists)
//! and an HTTP server with /small ("hello") and /bulk (32 MiB).
//!
//! Used by the tunnel's own tests and, through the wg-test-peer binary, by
//! the engine's integration tests. It is never part of the app.

use super::device::QueueDevice;
use boringtun::noise::{Tunn, TunnResult};
use boringtun::x25519::{PublicKey, StaticSecret};
use smoltcp::iface::{Config, Interface, SocketHandle, SocketSet};
use smoltcp::socket::{tcp, udp};
use smoltcp::time::Instant;
use smoltcp::wire::{HardwareAddress, IpAddress, IpCidr};
use std::net::SocketAddr;
use tokio::net::UdpSocket;

pub const SERVER_SECRET: [u8; 32] = [7; 32];
pub const BULK: usize = 32 * 1024 * 1024;

/// A DNS answer: test.internal is 10.0.0.1, everything else does not exist.
fn dns_answer(query: &[u8]) -> Option<Vec<u8>> {
    let mut pos = 12;
    let mut labels = Vec::new();
    while *query.get(pos)? != 0 {
        let len = query[pos] as usize;
        labels.push(String::from_utf8_lossy(query.get(pos + 1..pos + 1 + len)?).to_string());
        pos += len + 1;
    }
    let question_end = pos + 5;
    let known = labels.join(".") == "test.internal";
    let mut reply = query.get(..question_end)?.to_vec();
    reply[2] = 0x81;
    reply[3] = if known { 0x80 } else { 0x83 };
    reply[6..8].copy_from_slice(&[0, known as u8]);
    if known {
        reply.extend_from_slice(&[0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 10, 0, 0, 1]);
    }
    Some(reply)
}

/// Response head (with any small body) and how many bulk bytes follow.
/// HEAD gets headers only; /bulk honours a Range, as a file host does.
fn respond(request: &[u8]) -> (Vec<u8>, usize) {
    let text = String::from_utf8_lossy(request);
    let head = text.starts_with("HEAD ");
    let path = text.split_whitespace().nth(1).unwrap_or("/");
    if path != "/bulk" {
        let mut out = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n".to_vec();
        if !head {
            out.extend_from_slice(b"hello");
        }
        return (out, 0);
    }
    let range = text.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        if !name.eq_ignore_ascii_case("range") {
            return None;
        }
        let (start, end) = value.trim().strip_prefix("bytes=")?.split_once('-')?;
        let start: usize = start.parse().ok()?;
        let end: usize = if end.is_empty() { BULK - 1 } else { end.parse().ok()? };
        Some((start, end.min(BULK - 1)))
    });
    let (status, len, extra) = match range {
        Some((start, end)) => ("206 Partial Content", end + 1 - start, format!("Content-Range: bytes {start}-{end}/{BULK}\r\n")),
        None => ("200 OK", BULK, String::new()),
    };
    let header = format!("HTTP/1.1 {status}\r\nContent-Length: {len}\r\nAccept-Ranges: bytes\r\n{extra}Connection: close\r\n\r\n");
    (header.into_bytes(), if head { 0 } else { len })
}

struct HttpConn {
    handle: SocketHandle,
    request: Vec<u8>,
    header: Vec<u8>,
    remaining: usize,
}

fn serve_http(sockets: &mut SocketSet<'static>, conns: &mut [HttpConn]) {
    for conn in conns.iter_mut() {
        let socket = sockets.get_mut::<tcp::Socket>(conn.handle);
        if !socket.is_open() {
            socket.listen(80).expect("listen");
            conn.request.clear();
            continue;
        }
        while socket.can_recv() && conn.header.is_empty() && conn.remaining == 0 {
            let mut buf = [0u8; 2048];
            let n = socket.recv_slice(&mut buf).unwrap_or(0);
            if n == 0 {
                break;
            }
            conn.request.extend_from_slice(&buf[..n]);
            if conn.request.windows(4).any(|w| w == b"\r\n\r\n") {
                let (header, body) = respond(&conn.request);
                conn.header = header;
                conn.remaining = body;
            }
        }
        while socket.can_send() && !conn.header.is_empty() {
            let n = socket.send_slice(&conn.header).unwrap_or(0);
            conn.header.drain(..n);
            if n == 0 {
                break;
            }
        }
        while socket.can_send() && conn.header.is_empty() && conn.remaining > 0 {
            let chunk = vec![0x5A; conn.remaining.min(64 * 1024)];
            let n = socket.send_slice(&chunk).unwrap_or(0);
            conn.remaining -= n;
            if n == 0 {
                break;
            }
        }
        if !conn.request.is_empty() && conn.header.is_empty() && conn.remaining == 0 && socket.may_send() {
            socket.close();
        }
    }
}

/// The "VPN server": answers the handshake and serves 10.0.0.1.
pub async fn vpn_server(udp_socket: UdpSocket, client_public: PublicKey) {
    let mut tunn = Tunn::new(StaticSecret::from(SERVER_SECRET), client_public, None, None, 1, None);
    let mut device = QueueDevice::new(1420);
    let mut iface = Interface::new(Config::new(HardwareAddress::Ip), &mut device, Instant::now());
    iface.update_ip_addrs(|a| a.push(IpCidr::new(IpAddress::v4(10, 0, 0, 1), 24)).expect("address"));
    let mut sockets = SocketSet::new(vec![]);
    let dns = sockets.add(udp::Socket::new(
        udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 8], vec![0; 8192]),
        udp::PacketBuffer::new(vec![udp::PacketMetadata::EMPTY; 8], vec![0; 8192]),
    ));
    sockets.get_mut::<udp::Socket>(dns).bind(53).expect("bind dns");
    let mut http: Vec<HttpConn> = (0..8)
        .map(|_| HttpConn {
            handle: sockets.add(tcp::Socket::new(tcp::SocketBuffer::new(vec![0; 1 << 20]), tcp::SocketBuffer::new(vec![0; 4 << 20]))),
            request: Vec::new(), header: Vec::new(), remaining: 0,
        })
        .collect();
    let (mut datagram, mut out) = (vec![0u8; 70_000], vec![0u8; 70_000]);
    let mut client: Option<SocketAddr> = None;
    loop {
        let wait = iface.poll_delay(Instant::now(), &sockets).map(|d| d.total_micros()).unwrap_or(5_000).min(5_000);
        if let Ok(Ok((n, from))) = tokio::time::timeout(std::time::Duration::from_micros(wait), udp_socket.recv_from(&mut datagram)).await {
            client = Some(from);
            let mut input = &datagram[..n];
            loop {
                match tunn.decapsulate(Some(from.ip()), input, &mut out) {
                    TunnResult::WriteToNetwork(reply) => {
                        let _ = udp_socket.send_to(reply, from).await;
                        input = &[];
                    }
                    TunnResult::WriteToTunnelV4(p, _) | TunnResult::WriteToTunnelV6(p, _) => {
                        device.inbound.push_back(p.to_vec());
                        break;
                    }
                    _ => break,
                }
            }
        }
        let now = Instant::now();
        iface.poll(now, &mut device, &mut sockets);
        let socket = sockets.get_mut::<udp::Socket>(dns);
        while let Ok((query, meta)) = socket.recv() {
            if let Some(answer) = dns_answer(query) {
                let _ = socket.send_slice(&answer, meta.endpoint);
            }
        }
        serve_http(&mut sockets, &mut http);
        iface.poll(now, &mut device, &mut sockets);
        if let TunnResult::WriteToNetwork(p) = tunn.update_timers(&mut out) {
            if let Some(to) = client {
                let _ = udp_socket.send_to(p, to).await;
            }
        }
        for packet in device.outbound.drain(..) {
            if let (TunnResult::WriteToNetwork(p), Some(to)) = (tunn.encapsulate(&packet, &mut out), client) {
                let _ = udp_socket.send_to(p, to).await;
            }
        }
    }
}
