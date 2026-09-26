//! The tunnel's TCP/IP stack: one task owns the smoltcp interface and the
//! WireGuard peer, and serves connections for the SOCKS front end.

use super::config::Resolved;
use super::device::QueueDevice;
use super::wg::{Peer, PeerStats};
use smoltcp::iface::{Config, Interface, SocketHandle, SocketSet};
use smoltcp::socket::dns::{self, GetQueryResultError, QueryHandle};
use smoltcp::socket::tcp::{self, CongestionControl, State};
use smoltcp::time::{Duration, Instant};
use smoltcp::wire::{DnsQueryType, HardwareAddress, IpAddress};
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use tokio::sync::{mpsc, oneshot, Notify, Semaphore};

/// Receive window per connection: 1 MiB carries ~16 MB/s at 60 ms RTT, and a
/// segmented download opens many connections, so memory stays bounded.
const RX_BUFFER: usize = 1024 * 1024;
const TX_BUFFER: usize = 256 * 1024;
/// Bytes a client may have queued toward the tunnel before its reader waits.
pub const CLIENT_CREDIT: usize = 1024 * 1024;
const DNS_TTL: std::time::Duration = std::time::Duration::from_secs(300);
const DNS_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

pub enum Target {
    Ip(IpAddress, u16),
    Name(String, u16),
}

/// A connected stream: bytes from the remote side, and credit for sending.
pub struct Link {
    pub id: u64,
    pub from_remote: mpsc::Receiver<Vec<u8>>,
    pub credit: Arc<Semaphore>,
}

pub enum Cmd {
    Connect { target: Target, reply: oneshot::Sender<Result<Link, String>> },
    Data { id: u64, bytes: Vec<u8> },
    Close { id: u64 },
    Stats { reply: oneshot::Sender<PeerStats> },
}

struct Conn {
    handle: SocketHandle,
    to_client: Option<mpsc::Sender<Vec<u8>>>,
    credit: Arc<Semaphore>,
    pending: VecDeque<u8>,
    client_done: bool,
    closing: bool,
    /// Held until the handshake completes, then handed to the SOCKS side.
    connecting: Option<(oneshot::Sender<Result<Link, String>>, mpsc::Receiver<Vec<u8>>)>,
}

struct Resolving {
    name: String,
    port: u16,
    query: QueryHandle,
    reply: oneshot::Sender<Result<Link, String>>,
    started: std::time::Instant,
}

pub struct Stack {
    iface: Interface,
    device: QueueDevice,
    sockets: SocketSet<'static>,
    dns: SocketHandle,
    dns_cache: HashMap<String, (IpAddress, std::time::Instant)>,
    resolving: Vec<Resolving>,
    conns: HashMap<u64, Conn>,
    next_id: u64,
    next_port: u16,
}

fn seed() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());
    hasher.finish()
}

impl Stack {
    pub fn new(resolved: &Resolved) -> Self {
        let mut device = QueueDevice::new(resolved.mtu);
        let mut config = Config::new(HardwareAddress::Ip);
        config.random_seed = seed();
        let mut iface = Interface::new(config, &mut device, Instant::now());
        iface.update_ip_addrs(|addrs| {
            for cidr in &resolved.addresses {
                if addrs.push(*cidr).is_err() {
                    eprintln!("[tunnel] ignored extra tunnel address {cidr}");
                }
            }
        });
        // Everything goes to the peer; on an IP-only link the gateway is nominal.
        for cidr in &resolved.addresses {
            let routed = match cidr.address() {
                IpAddress::Ipv4(v4) => iface.routes_mut().add_default_ipv4_route(v4).map(|_| ()),
                IpAddress::Ipv6(v6) => iface.routes_mut().add_default_ipv6_route(v6).map(|_| ()),
            };
            if routed.is_err() {
                eprintln!("[tunnel] no room for a default route via {cidr}");
            }
        }
        let mut sockets = SocketSet::new(vec![]);
        let queries: Vec<Option<dns::DnsQuery>> = (0..16).map(|_| None).collect();
        let dns = sockets.add(dns::Socket::new(&[resolved.dns], queries));
        Self {
            iface, device, sockets, dns,
            dns_cache: HashMap::new(), resolving: Vec::new(), conns: HashMap::new(),
            next_id: 1, next_port: 49_152 + (seed() % 8_000) as u16,
        }
    }

    pub fn deliver(&mut self, packet: &[u8]) {
        self.device.inbound.push_back(packet.to_vec());
    }

    pub fn take_outbound(&mut self) -> Vec<Vec<u8>> {
        self.device.outbound.drain(..).collect()
    }

    pub fn delay(&mut self) -> std::time::Duration {
        let wait = self.iface.poll_delay(Instant::now(), &self.sockets).unwrap_or(Duration::from_millis(100));
        std::time::Duration::from_micros(wait.total_micros()).min(std::time::Duration::from_millis(100))
    }

    pub fn command(&mut self, cmd: Cmd) {
        match cmd {
            Cmd::Connect { target: Target::Ip(ip, port), reply } => self.open(ip, port, reply),
            Cmd::Connect { target: Target::Name(name, port), reply } => self.resolve(name, port, reply),
            Cmd::Data { id, bytes } => {
                if let Some(conn) = self.conns.get_mut(&id) {
                    conn.pending.extend(bytes);
                }
            }
            Cmd::Close { id } => {
                if let Some(conn) = self.conns.get_mut(&id) {
                    conn.client_done = true;
                }
            }
            // The task answers these before they get here: it owns the peer.
            Cmd::Stats { .. } => {}
        }
    }

    fn resolve(&mut self, name: String, port: u16, reply: oneshot::Sender<Result<Link, String>>) {
        if let Ok(ip) = name.parse::<std::net::IpAddr>() {
            return self.open(IpAddress::from(ip), port, reply);
        }
        let key = name.to_ascii_lowercase();
        if let Some((ip, at)) = self.dns_cache.get(&key) {
            if at.elapsed() < DNS_TTL {
                let ip = *ip;
                return self.open(ip, port, reply);
            }
        }
        let socket = self.sockets.get_mut::<dns::Socket>(self.dns);
        match socket.start_query(self.iface.context(), &key, DnsQueryType::A) {
            Ok(query) => self.resolving.push(Resolving { name: key, port, query, reply, started: std::time::Instant::now() }),
            Err(error) => {
                let _ = reply.send(Err(format!("cannot resolve {name} through the tunnel: {error:?}")));
            }
        }
    }

    fn open(&mut self, ip: IpAddress, port: u16, reply: oneshot::Sender<Result<Link, String>>) {
        let mut socket = tcp::Socket::new(
            tcp::SocketBuffer::new(vec![0; RX_BUFFER]),
            tcp::SocketBuffer::new(vec![0; TX_BUFFER]),
        );
        socket.set_congestion_control(CongestionControl::Cubic);
        socket.set_nagle_enabled(false);
        socket.set_timeout(Some(Duration::from_secs(30)));
        socket.set_keep_alive(Some(Duration::from_secs(25)));
        self.next_port = if self.next_port >= 65_000 { 49_152 } else { self.next_port + 1 };
        if let Err(error) = socket.connect(self.iface.context(), (ip, port), self.next_port) {
            let _ = reply.send(Err(format!("connect to {ip}:{port}: {error:?}")));
            return;
        }
        let handle = self.sockets.add(socket);
        let (to_client, from_remote) = mpsc::channel(64);
        let id = self.next_id;
        self.next_id += 1;
        self.conns.insert(id, Conn {
            handle, to_client: Some(to_client), credit: Arc::new(Semaphore::new(CLIENT_CREDIT)),
            pending: VecDeque::new(), client_done: false, closing: false,
            connecting: Some((reply, from_remote)),
        });
    }

    /// Advance the stack: packets in, socket work, packets out.
    pub fn poll(&mut self) {
        let now = Instant::now();
        self.iface.poll(now, &mut self.device, &mut self.sockets);
        self.finish_lookups();
        self.service();
        // Data written above only becomes packets on the next poll.
        self.iface.poll(now, &mut self.device, &mut self.sockets);
    }

    fn finish_lookups(&mut self) {
        let socket = self.sockets.get_mut::<dns::Socket>(self.dns);
        let mut pending = Vec::new();
        let mut settled = Vec::new();
        for lookup in self.resolving.drain(..) {
            match socket.get_query_result(lookup.query) {
                Err(GetQueryResultError::Pending) if lookup.started.elapsed() < DNS_TIMEOUT => pending.push(lookup),
                Err(GetQueryResultError::Pending) => {
                    socket.cancel_query(lookup.query);
                    settled.push((lookup, None));
                }
                Ok(addrs) => {
                    let ip = addrs.first().copied();
                    settled.push((lookup, ip));
                }
                Err(_) => settled.push((lookup, None)),
            }
        }
        self.resolving = pending;
        for (lookup, ip) in settled {
            match ip {
                Some(ip) => {
                    self.dns_cache.insert(lookup.name, (ip, std::time::Instant::now()));
                    self.open(ip, lookup.port, lookup.reply);
                }
                None => {
                    let _ = lookup.reply.send(Err(format!("{} did not resolve through the tunnel", lookup.name)));
                }
            }
        }
    }

    fn service(&mut self) {
        let mut finished = Vec::new();
        for (&id, conn) in self.conns.iter_mut() {
            let socket = self.sockets.get_mut::<tcp::Socket>(conn.handle);
            if let Some((reply, from_remote)) = conn.connecting.take() {
                match socket.state() {
                    State::Established => {
                        let link = Link { id, from_remote, credit: conn.credit.clone() };
                        if reply.send(Ok(link)).is_err() {
                            socket.abort();
                        }
                    }
                    State::Closed | State::TimeWait | State::Closing => {
                        let _ = reply.send(Err("connection refused or timed out inside the tunnel".into()));
                        finished.push(id);
                        continue;
                    }
                    _ => {
                        conn.connecting = Some((reply, from_remote));
                        continue;
                    }
                }
            }
            // Client to remote.
            while !conn.pending.is_empty() && socket.can_send() {
                let (chunk, _) = conn.pending.as_slices();
                match socket.send_slice(chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        conn.pending.drain(..n);
                        conn.credit.add_permits(n);
                    }
                }
            }
            if conn.client_done && conn.pending.is_empty() && !conn.closing {
                socket.close();
                conn.closing = true;
            }
            // Remote to client, only as fast as the client drains it: a full
            // channel leaves data in the socket and the window closes.
            while socket.can_recv() {
                let Some(to_client) = conn.to_client.as_ref() else { break };
                match to_client.try_reserve() {
                    Ok(permit) => {
                        let mut chunk = vec![0; socket.recv_queue().min(64 * 1024)];
                        match socket.recv_slice(&mut chunk) {
                            Ok(n) => {
                                chunk.truncate(n);
                                permit.send(chunk);
                            }
                            Err(_) => break,
                        }
                    }
                    Err(mpsc::error::TrySendError::Full(())) => break,
                    Err(mpsc::error::TrySendError::Closed(())) => {
                        socket.abort();
                        break;
                    }
                }
            }
            if !socket.may_recv() && socket.recv_queue() == 0 {
                // Remote finished sending: closing the channel ends the client's write half.
                conn.to_client = None;
            }
            if matches!(socket.state(), State::Closed | State::TimeWait) {
                finished.push(id);
            }
        }
        for id in finished {
            if let Some(conn) = self.conns.remove(&id) {
                conn.credit.close();
                self.sockets.remove(conn.handle);
            }
        }
    }

    pub fn shutdown(&mut self) {
        for conn in self.conns.values() {
            conn.credit.close();
            self.sockets.get_mut::<tcp::Socket>(conn.handle).abort();
        }
        let now = Instant::now();
        self.iface.poll(now, &mut self.device, &mut self.sockets);
    }
}

/// The task: runs until every command sender is gone.
pub async fn run(mut stack: Stack, mut peer: Peer, mut cmds: mpsc::Receiver<Cmd>, wake: Arc<Notify>) {
    let mut datagram = vec![0u8; 65_536 + 256];
    let mut timers = tokio::time::interval(std::time::Duration::from_millis(250));
    peer.initiate().await;
    loop {
        let delay = stack.delay();
        tokio::select! {
            received = peer.recv(&mut datagram) => {
                if let Some(n) = received {
                    peer.receive(&datagram[..n], &mut |p| stack.deliver(p)).await;
                }
                // Drain a burst before polling: one poll per datagram would
                // cap throughput at the scheduler's pace.
                for _ in 0..256 {
                    let Some(n) = peer.try_recv(&mut datagram) else { break };
                    peer.receive(&datagram[..n], &mut |p| stack.deliver(p)).await;
                }
            }
            cmd = cmds.recv() => match cmd {
                None => break,
                Some(Cmd::Stats { reply }) => { let _ = reply.send(peer.stats()); }
                Some(cmd) => {
                    stack.command(cmd);
                    while let Ok(more) = cmds.try_recv() {
                        match more {
                            Cmd::Stats { reply } => { let _ = reply.send(peer.stats()); }
                            other => stack.command(other),
                        }
                    }
                }
            },
            _ = timers.tick() => peer.tick().await,
            _ = wake.notified() => {}
            _ = tokio::time::sleep(delay) => {}
        }
        stack.poll();
        for packet in stack.take_outbound() {
            peer.send_packet(&packet).await;
        }
    }
    stack.shutdown();
    for packet in stack.take_outbound() {
        peer.send_packet(&packet).await;
    }
}
