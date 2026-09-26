//! A SOCKS5 server on 127.0.0.1 in front of the tunnel's stack.
//!
//! CONNECT only, no authentication: it listens on loopback for this app's own
//! engine, transfers and browser. Hostnames are resolved inside the tunnel.

use super::stack::{Cmd, Link, Target};
use smoltcp::wire::IpAddress;
use std::net::{Ipv4Addr, Ipv6Addr};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot, Notify};

const REPLY_OK: u8 = 0x00;
const REPLY_FAILURE: u8 = 0x01;
const REPLY_HOST_UNREACHABLE: u8 = 0x04;
const REPLY_REFUSED: u8 = 0x05;
const REPLY_COMMAND_UNSUPPORTED: u8 = 0x07;
const REPLY_ADDRESS_UNSUPPORTED: u8 = 0x08;

pub async fn serve(listener: TcpListener, cmds: mpsc::Sender<Cmd>, wake: Arc<Notify>) {
    loop {
        match listener.accept().await {
            Ok((client, _)) => {
                let (cmds, wake) = (cmds.clone(), wake.clone());
                tokio::spawn(async move {
                    if let Err(error) = handle(client, cmds, wake).await {
                        eprintln!("[tunnel] socks client: {error}");
                    }
                });
            }
            Err(error) => {
                eprintln!("[tunnel] socks accept: {error}");
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
        }
    }
}

async fn reply(client: &mut TcpStream, code: u8) -> std::io::Result<()> {
    client.write_all(&[0x05, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0]).await
}

async fn read_target(client: &mut TcpStream) -> std::io::Result<Result<Target, u8>> {
    let mut head = [0u8; 4];
    client.read_exact(&mut head).await?;
    if head[1] != 0x01 {
        return Ok(Err(REPLY_COMMAND_UNSUPPORTED));
    }
    let target = match head[3] {
        0x01 => {
            let mut raw = [0u8; 6];
            client.read_exact(&mut raw).await?;
            let ip = Ipv4Addr::new(raw[0], raw[1], raw[2], raw[3]);
            Target::Ip(IpAddress::from(ip), u16::from_be_bytes([raw[4], raw[5]]))
        }
        0x04 => {
            let mut raw = [0u8; 18];
            client.read_exact(&mut raw).await?;
            let octets: [u8; 16] = raw[..16].try_into().expect("16 bytes");
            Target::Ip(IpAddress::from(Ipv6Addr::from(octets)), u16::from_be_bytes([raw[16], raw[17]]))
        }
        0x03 => {
            let len = client.read_u8().await? as usize;
            let mut raw = vec![0u8; len + 2];
            client.read_exact(&mut raw).await?;
            let port = u16::from_be_bytes([raw[len], raw[len + 1]]);
            raw.truncate(len);
            match String::from_utf8(raw) {
                Ok(name) => Target::Name(name, port),
                Err(_) => return Ok(Err(REPLY_ADDRESS_UNSUPPORTED)),
            }
        }
        _ => return Ok(Err(REPLY_ADDRESS_UNSUPPORTED)),
    };
    Ok(Ok(target))
}

async fn handle(mut client: TcpStream, cmds: mpsc::Sender<Cmd>, wake: Arc<Notify>) -> Result<(), String> {
    client.set_nodelay(true).map_err(|e| e.to_string())?;
    // Greeting: version, then the methods offered. Only "no auth" is accepted.
    let mut greeting = [0u8; 2];
    client.read_exact(&mut greeting).await.map_err(|e| e.to_string())?;
    if greeting[0] != 0x05 {
        return Err(format!("not SOCKS5 (version byte {})", greeting[0]));
    }
    let mut methods = vec![0u8; greeting[1] as usize];
    client.read_exact(&mut methods).await.map_err(|e| e.to_string())?;
    if !methods.contains(&0x00) {
        client.write_all(&[0x05, 0xFF]).await.map_err(|e| e.to_string())?;
        return Err("client offered no unauthenticated method".into());
    }
    client.write_all(&[0x05, 0x00]).await.map_err(|e| e.to_string())?;

    let target = match read_target(&mut client).await.map_err(|e| e.to_string())? {
        Ok(target) => target,
        Err(code) => {
            let _ = reply(&mut client, code).await;
            return Err(format!("unsupported request (reply {code})"));
        }
    };
    let (tx, rx) = oneshot::channel();
    cmds.send(Cmd::Connect { target, reply: tx }).await.map_err(|_| "tunnel stopped".to_string())?;
    let link = match rx.await.map_err(|_| "tunnel stopped".to_string())? {
        Ok(link) => link,
        Err(error) => {
            let code = if error.contains("resolve") { REPLY_HOST_UNREACHABLE }
                else if error.contains("refused") { REPLY_REFUSED }
                else { REPLY_FAILURE };
            let _ = reply(&mut client, code).await;
            return Err(error);
        }
    };
    reply(&mut client, REPLY_OK).await.map_err(|e| e.to_string())?;
    pump(client, link, cmds, wake).await;
    Ok(())
}

async fn pump(client: TcpStream, link: Link, cmds: mpsc::Sender<Cmd>, wake: Arc<Notify>) {
    let Link { id, mut from_remote, credit } = link;
    let (mut reader, mut writer) = client.into_split();
    let upstream = {
        let cmds = cmds.clone();
        async move {
            let mut buf = vec![0u8; 64 * 1024];
            loop {
                let n = match reader.read(&mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => n,
                };
                // Wait for room in the tunnel before taking more from the client.
                match credit.acquire_many(n as u32).await {
                    Ok(permit) => permit.forget(),
                    Err(_) => return, // connection gone inside the tunnel
                }
                if cmds.send(Cmd::Data { id, bytes: buf[..n].to_vec() }).await.is_err() {
                    return;
                }
            }
            let _ = cmds.send(Cmd::Close { id }).await;
        }
    };
    let downstream = async move {
        while let Some(chunk) = from_remote.recv().await {
            if writer.write_all(&chunk).await.is_err() {
                break;
            }
            // Room freed in the channel: let the stack read more from the socket.
            wake.notify_one();
        }
        let _ = writer.shutdown().await;
    };
    tokio::join!(upstream, downstream);
}
