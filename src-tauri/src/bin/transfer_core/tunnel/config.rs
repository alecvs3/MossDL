//! A tunnel's settings, as the engine sends them (already parsed from a .conf).

use base64::Engine as _;
use serde::Deserialize;
use smoltcp::wire::{IpAddress, IpCidr};
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};

#[derive(Debug, Deserialize)]
pub struct TunnelConfig {
    pub private_key: String,
    /// The tunnel's own addresses, "10.64.12.34/32" or bare.
    pub addresses: Vec<String>,
    /// Resolvers reached *through* the tunnel; hostnames never touch the local one.
    #[serde(default)]
    pub dns: Vec<String>,
    #[serde(default)]
    pub mtu: Option<usize>,
    pub peer: PeerConfig,
}

#[derive(Debug, Deserialize)]
pub struct PeerConfig {
    pub public_key: String,
    #[serde(default)]
    pub preshared_key: Option<String>,
    /// "host:port"; the one lookup that must happen outside the tunnel.
    pub endpoint: String,
    #[serde(default)]
    pub persistent_keepalive: Option<u16>,
}

/// Everything a tunnel needs, validated and decoded.
pub struct Resolved {
    pub private_key: [u8; 32],
    pub peer_public_key: [u8; 32],
    pub preshared_key: Option<[u8; 32]>,
    pub endpoint: SocketAddr,
    pub keepalive: Option<u16>,
    pub addresses: Vec<IpCidr>,
    pub dns: IpAddress,
    pub mtu: usize,
}

pub fn key(value: &str, what: &str) -> Result<[u8; 32], String> {
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value.trim())
        .map_err(|_| format!("{what} is not valid base64"))?;
    bytes
        .try_into()
        .map_err(|_| format!("{what} must be 32 bytes"))
}

fn cidr(value: &str) -> Result<IpCidr, String> {
    let value = value.trim();
    let (addr, prefix) = match value.split_once('/') {
        Some((addr, prefix)) => (addr, Some(prefix)),
        None => (value, None),
    };
    let ip: IpAddr = addr.parse().map_err(|_| format!("address {value:?} is not an IP"))?;
    let default_prefix = if ip.is_ipv4() { 32 } else { 128 };
    let prefix = match prefix {
        Some(p) => p.parse::<u8>().map_err(|_| format!("address {value:?} has a bad prefix"))?,
        None => default_prefix,
    };
    Ok(IpCidr::new(IpAddress::from(ip), prefix))
}

impl TunnelConfig {
    pub fn resolve(&self) -> Result<Resolved, String> {
        let addresses = self
            .addresses
            .iter()
            .flat_map(|a| a.split(','))
            .filter(|a| !a.trim().is_empty())
            .map(cidr)
            .collect::<Result<Vec<_>, _>>()?;
        if !addresses.iter().any(|a| matches!(a.address(), IpAddress::Ipv4(_))) {
            return Err("the tunnel needs an IPv4 address".into());
        }
        let dns = self
            .dns
            .iter()
            .flat_map(|d| d.split(','))
            .filter_map(|d| d.trim().parse::<IpAddr>().ok())
            .find(|d| d.is_ipv4())
            .map(IpAddress::from)
            // No resolver in the file: a public one, still asked through the tunnel.
            .unwrap_or(IpAddress::v4(1, 1, 1, 1));
        let endpoint = self
            .peer
            .endpoint
            .to_socket_addrs()
            .map_err(|e| format!("endpoint {:?}: {e}", self.peer.endpoint))?
            .next()
            .ok_or_else(|| format!("endpoint {:?} did not resolve", self.peer.endpoint))?;
        Ok(Resolved {
            private_key: key(&self.private_key, "PrivateKey")?,
            peer_public_key: key(&self.peer.public_key, "peer PublicKey")?,
            preshared_key: self
                .peer
                .preshared_key
                .as_deref()
                .filter(|k| !k.trim().is_empty())
                .map(|k| key(k, "PresharedKey"))
                .transpose()?,
            endpoint,
            keepalive: self.peer.persistent_keepalive.or(Some(25)),
            addresses,
            dns,
            // WireGuard's own default. Leaves room for its 80 bytes of overhead
            // inside a 1500-byte path.
            mtu: self.mtu.unwrap_or(1420).clamp(1280, 1500),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=";

    fn config(addresses: &[&str]) -> TunnelConfig {
        TunnelConfig {
            private_key: KEY.into(),
            addresses: addresses.iter().map(|a| a.to_string()).collect(),
            dns: vec!["10.64.0.1, fd00::1".into()],
            mtu: None,
            peer: PeerConfig {
                public_key: KEY.into(),
                preshared_key: Some(String::new()),
                endpoint: "127.0.0.1:51820".into(),
                persistent_keepalive: None,
            },
        }
    }

    #[test]
    fn resolves_a_provider_style_config() {
        let resolved = config(&["10.64.1.2/32,fc00:bbbb::2/128"]).resolve().unwrap();
        assert_eq!(resolved.addresses.len(), 2);
        assert_eq!(resolved.dns, IpAddress::v4(10, 64, 0, 1));
        assert_eq!(resolved.mtu, 1420);
        assert!(resolved.preshared_key.is_none(), "an empty PresharedKey means none");
    }

    #[test]
    fn rejects_bad_keys_and_ipv6_only_tunnels() {
        let mut bad = config(&["10.0.0.2"]);
        bad.private_key = "short".into();
        assert!(bad.resolve().is_err());
        assert!(config(&["fc00::2/128"]).resolve().is_err());
    }
}
