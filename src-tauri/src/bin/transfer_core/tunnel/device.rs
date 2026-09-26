//! The tunnel's network interface as smoltcp sees it: two packet queues.
//!
//! Inbound packets are what WireGuard decrypted; outbound ones are waiting to
//! be encrypted and sent. Nothing touches the host's own network stack.

use smoltcp::phy::{self, Device, DeviceCapabilities, Medium};
use smoltcp::time::Instant;
use std::collections::VecDeque;

pub struct QueueDevice {
    pub inbound: VecDeque<Vec<u8>>,
    pub outbound: VecDeque<Vec<u8>>,
    mtu: usize,
}

impl QueueDevice {
    pub fn new(mtu: usize) -> Self {
        Self { inbound: VecDeque::new(), outbound: VecDeque::new(), mtu }
    }
}

pub struct Rx(Vec<u8>);
pub struct Tx<'a>(&'a mut VecDeque<Vec<u8>>);

impl phy::RxToken for Rx {
    fn consume<R, F: FnOnce(&[u8]) -> R>(self, f: F) -> R {
        f(&self.0)
    }
}

impl phy::TxToken for Tx<'_> {
    fn consume<R, F: FnOnce(&mut [u8]) -> R>(self, len: usize, f: F) -> R {
        let mut packet = vec![0; len];
        let result = f(&mut packet);
        self.0.push_back(packet);
        result
    }
}

impl Device for QueueDevice {
    type RxToken<'a> = Rx;
    type TxToken<'a> = Tx<'a>;

    fn receive(&mut self, _: Instant) -> Option<(Rx, Tx<'_>)> {
        let packet = self.inbound.pop_front()?;
        Some((Rx(packet), Tx(&mut self.outbound)))
    }

    fn transmit(&mut self, _: Instant) -> Option<Tx<'_>> {
        Some(Tx(&mut self.outbound))
    }

    fn capabilities(&self) -> DeviceCapabilities {
        let mut caps = DeviceCapabilities::default();
        caps.medium = Medium::Ip;
        caps.max_transmission_unit = self.mtu;
        caps
    }
}
