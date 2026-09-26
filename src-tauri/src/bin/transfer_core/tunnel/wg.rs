//! One WireGuard peer: encrypts the stack's packets onto UDP and decrypts
//! what comes back.

use boringtun::noise::{errors::WireGuardError, Tunn, TunnResult};
use boringtun::x25519::{PublicKey, StaticSecret};
use std::net::SocketAddr;
use std::time::Duration;
use tokio::net::UdpSocket;

/// Room for the largest packet plus WireGuard's framing.
const BUFFER: usize = 65_536 + 256;

pub struct Peer {
    tunn: Tunn,
    socket: UdpSocket,
    endpoint: SocketAddr,
    out: Vec<u8>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PeerStats {
    /// Seconds since the last completed handshake; None until the first one.
    pub handshake_age: Option<f64>,
    pub tx_bytes: usize,
    pub rx_bytes: usize,
}

impl Peer {
    pub async fn connect(
        private_key: [u8; 32],
        peer_public_key: [u8; 32],
        preshared_key: Option<[u8; 32]>,
        keepalive: Option<u16>,
        endpoint: SocketAddr,
        index: u32,
    ) -> Result<Self, String> {
        let bind: SocketAddr = if endpoint.is_ipv4() { "0.0.0.0:0" } else { "[::]:0" }
            .parse()
            .expect("literal socket address");
        let socket = UdpSocket::bind(bind).await.map_err(|e| format!("udp bind: {e}"))?;
        let tunn = Tunn::new(
            StaticSecret::from(private_key),
            PublicKey::from(peer_public_key),
            preshared_key,
            keepalive,
            index,
            None,
        );
        Ok(Self { tunn, socket, endpoint, out: vec![0; BUFFER] })
    }

    async fn send(socket: &UdpSocket, endpoint: SocketAddr, datagram: &[u8]) {
        if let Err(error) = socket.send_to(datagram, endpoint).await {
            eprintln!("[tunnel] udp send to {endpoint} failed: {error}");
        }
    }

    /// Start the handshake now rather than on the first packet.
    pub async fn initiate(&mut self) {
        if let TunnResult::WriteToNetwork(datagram) = self.tunn.format_handshake_initiation(&mut self.out, false) {
            Self::send(&self.socket, self.endpoint, datagram).await;
        }
    }

    /// Encrypt one IP packet from the stack and send it.
    pub async fn send_packet(&mut self, packet: &[u8]) {
        match self.tunn.encapsulate(packet, &mut self.out) {
            TunnResult::WriteToNetwork(datagram) => Self::send(&self.socket, self.endpoint, datagram).await,
            TunnResult::Err(error) => eprintln!("[tunnel] encapsulate failed: {error:?}"),
            // Done: queued until the handshake completes.
            _ => {}
        }
    }

    /// Wait for the next datagram from the peer.
    pub async fn recv(&self, buf: &mut [u8]) -> Option<usize> {
        match self.socket.recv_from(buf).await {
            Ok((n, from)) if from == self.endpoint => Some(n),
            Ok((_, from)) => {
                eprintln!("[tunnel] ignored a datagram from {from}, not the peer");
                None
            }
            Err(error) => {
                // Windows reports an ICMP port-unreachable from an earlier send
                // as a receive error; the socket stays usable.
                eprintln!("[tunnel] udp receive: {error}");
                None
            }
        }
    }

    /// A datagram that is already waiting, without blocking.
    pub fn try_recv(&self, buf: &mut [u8]) -> Option<usize> {
        match self.socket.try_recv_from(buf) {
            Ok((n, from)) if from == self.endpoint => Some(n),
            _ => None,
        }
    }

    /// Decrypt a datagram; decrypted IP packets go to `deliver`.
    pub async fn receive(&mut self, datagram: &[u8], deliver: &mut (dyn FnMut(&[u8]) + Send)) {
        let mut input = datagram;
        loop {
            match self.tunn.decapsulate(None, input, &mut self.out) {
                TunnResult::WriteToNetwork(reply) => {
                    // A handshake answer or packets queued behind it; keep
                    // draining until boringtun has nothing more to send.
                    Self::send(&self.socket, self.endpoint, reply).await;
                    input = &[];
                }
                TunnResult::WriteToTunnelV4(packet, _) | TunnResult::WriteToTunnelV6(packet, _) => {
                    deliver(packet);
                    return;
                }
                TunnResult::Done => return,
                TunnResult::Err(error) => {
                    eprintln!("[tunnel] decapsulate failed: {error:?}");
                    return;
                }
            }
        }
    }

    /// Handshake renewal and keepalives; call every ~250 ms.
    pub async fn tick(&mut self) {
        match self.tunn.update_timers(&mut self.out) {
            TunnResult::WriteToNetwork(datagram) => Self::send(&self.socket, self.endpoint, datagram).await,
            TunnResult::Err(WireGuardError::ConnectionExpired) => {
                // boringtun gave up on the session; start over rather than
                // waiting for the next outgoing packet to notice.
                self.initiate().await;
            }
            TunnResult::Err(error) => eprintln!("[tunnel] timers: {error:?}"),
            _ => {}
        }
    }

    pub fn stats(&self) -> PeerStats {
        let (age, tx_bytes, rx_bytes, _, _) = self.tunn.stats();
        PeerStats { handshake_age: age.map(|d: Duration| d.as_secs_f64()), tx_bytes, rx_bytes }
    }
}
