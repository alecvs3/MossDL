//! Dynamic range scheduling for one segmented transfer.
//!
//! Each worker owns a slot whose `end` a thief may shorten and whose `current`
//! the worker advances as bytes land. When a worker finishes early it steals the
//! back half of the largest live slot. When a worker gives up (the host refused
//! the extra connection: 429, 503 and the like), its slot is abandoned and the
//! next worker to finish adopts the whole unfinished range -- so a host that
//! allows fewer connections than requested slows the transfer down instead of
//! failing it.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

struct Slot {
    /// Last byte this slot covers; shortened by steals and adoption.
    end: Arc<AtomicU64>,
    /// Next byte the owning worker will write.
    current: Arc<AtomicU64>,
    /// The owning worker stopped before reaching `end`.
    abandoned: AtomicBool,
    /// Its bytes now belong to another slot (adopted). An explicit flag, since
    /// `end = current - 1` cannot say "empty" for a range starting at byte 0.
    closed: AtomicBool,
}

impl Slot {
    fn remaining(&self) -> u64 {
        if self.closed.load(Ordering::Acquire) {
            return 0;
        }
        let cur = self.current.load(Ordering::Acquire);
        let end = self.end.load(Ordering::Acquire);
        if end >= cur { end - cur + 1 } else { 0 }
    }
}

pub struct Trackers {
    pub index: usize,
    pub end: Arc<AtomicU64>,
    pub current: Arc<AtomicU64>,
}

pub struct RangeScheduler {
    slots: Mutex<Vec<Arc<Slot>>>,
    min_steal_bytes: u64,
}

impl RangeScheduler {
    const BLOCK_ALIGN: u64 = 64 * 1024;

    pub fn new(ranges: &[(u64, u64)], min_steal_bytes: u64) -> Self {
        let scheduler = Self { slots: Mutex::new(Vec::new()), min_steal_bytes };
        for &(start, end) in ranges {
            scheduler.add_slot(start, end);
        }
        scheduler
    }

    fn slots(&self) -> Vec<Arc<Slot>> {
        self.slots.lock().map(|slots| slots.clone()).unwrap_or_default()
    }

    /// Register a range (an initial segment or a stolen/adopted one) and hand
    /// back the shared trackers its worker must use.
    pub fn add_slot(&self, start: u64, end: u64) -> Trackers {
        let slot = Arc::new(Slot {
            end: Arc::new(AtomicU64::new(end)),
            current: Arc::new(AtomicU64::new(start)),
            abandoned: AtomicBool::new(false),
            closed: AtomicBool::new(false),
        });
        let trackers = Trackers { index: 0, end: slot.end.clone(), current: slot.current.clone() };
        let mut slots = self.slots.lock().expect("range scheduler lock");
        slots.push(slot);
        Trackers { index: slots.len() - 1, ..trackers }
    }

    /// The shared trackers of an existing slot (the initial segments).
    pub fn trackers(&self, index: usize) -> Option<Trackers> {
        self.slots().get(index).map(|slot| Trackers { index, end: slot.end.clone(), current: slot.current.clone() })
    }

    /// The worker on `index` stopped for good; its unfinished bytes need a new owner.
    pub fn abandon(&self, index: usize) {
        if let Some(slot) = self.slots().get(index) {
            slot.abandoned.store(true, Ordering::Release);
        }
    }

    /// Take over the whole unfinished range of an abandoned slot, if any.
    pub fn adopt(&self) -> Option<(u64, u64)> {
        for slot in self.slots() {
            if !slot.abandoned.load(Ordering::Acquire) || slot.remaining() == 0 {
                continue;
            }
            // Winning this exchange makes us the only adopter of these bytes.
            if slot.abandoned.compare_exchange(true, false, Ordering::AcqRel, Ordering::Relaxed).is_ok() {
                slot.closed.store(true, Ordering::Release);
                let cur = slot.current.load(Ordering::Acquire);
                let end = slot.end.load(Ordering::Acquire);
                return Some((cur, end));
            }
        }
        None
    }

    /// Split the largest live range in two and return the back half.
    pub fn try_steal(&self) -> Option<(u64, u64)> {
        let victim = self
            .slots()
            .into_iter()
            .filter(|slot| !slot.abandoned.load(Ordering::Acquire))
            .max_by_key(|slot| slot.remaining())?;
        let cur = victim.current.load(Ordering::Relaxed);
        let old_end = victim.end.load(Ordering::Acquire);
        if old_end < cur || old_end - cur + 1 < 2 * self.min_steal_bytes {
            return None;
        }
        let split = (cur + (old_end - cur + 1) / 2) & !(Self::BLOCK_ALIGN - 1);
        if split <= cur || split > old_end {
            return None;
        }
        victim
            .end
            .compare_exchange(old_end, split - 1, Ordering::AcqRel, Ordering::Relaxed)
            .ok()
            .map(|_| (split, old_end))
    }

    /// Bytes nobody is going to download: what abandoned slots still hold.
    pub fn orphaned_bytes(&self) -> u64 {
        self.slots()
            .iter()
            .filter(|slot| slot.abandoned.load(Ordering::Acquire))
            .map(|slot| slot.remaining())
            .sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MB: u64 = 1024 * 1024;

    #[test]
    fn a_finished_worker_steals_the_back_half_of_the_largest_range() {
        let scheduler = RangeScheduler::new(&[(0, 64 * MB - 1), (64 * MB, 80 * MB - 1)], 8 * MB);
        let (start, end) = scheduler.try_steal().expect("a steal");
        assert_eq!((start, end), (32 * MB, 64 * MB - 1));
    }

    #[test]
    fn an_abandoned_range_is_adopted_whole_and_only_once() {
        let scheduler = RangeScheduler::new(&[(0, 99), (100, 199)], MB);
        scheduler.slots()[1].current.store(150, Ordering::Release); // it got halfway
        scheduler.abandon(1);
        assert_eq!(scheduler.orphaned_bytes(), 50);
        assert_eq!(scheduler.adopt(), Some((150, 199)));
        assert_eq!(scheduler.adopt(), None, "the same bytes must not be handed out twice");
        assert_eq!(scheduler.orphaned_bytes(), 0);
    }

    #[test]
    fn abandoned_ranges_are_not_split_by_thieves() {
        let scheduler = RangeScheduler::new(&[(0, 64 * MB - 1)], 8 * MB);
        scheduler.abandon(0);
        assert_eq!(scheduler.try_steal(), None);
        assert_eq!(scheduler.adopt(), Some((0, 64 * MB - 1)));
    }

    #[test]
    fn a_range_starting_at_byte_zero_is_adopted_only_once() {
        // Emptying by "end = current - 1" left byte 0 adoptable forever: a
        // strict host then saw 232,000 one-byte requests.
        let scheduler = RangeScheduler::new(&[(0, 99), (100, 199)], MB);
        scheduler.abandon(0);
        assert_eq!(scheduler.adopt(), Some((0, 99)));
        assert_eq!(scheduler.adopt(), None);
        assert_eq!(scheduler.orphaned_bytes(), 0);
    }

    #[test]
    fn stolen_ranges_get_their_own_slot() {
        let scheduler = RangeScheduler::new(&[(0, 99)], MB);
        let trackers = scheduler.add_slot(100, 199);
        assert_eq!(trackers.index, 1);
        trackers.current.store(120, Ordering::Release);
        scheduler.abandon(1);
        assert_eq!(scheduler.adopt(), Some((120, 199)));
    }
}
