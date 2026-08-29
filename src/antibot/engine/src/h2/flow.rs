//! Flow control (RFC 7540 §5.2) with Chrome's exact window management.
//!
//! Chrome (observed from live captures, ground truth in
//! tests/fixtures/antibot/chrome_fingerprint_reference.json):
//!   - connection-level initial window 65,535 (RFC default) replenished once
//!     at preface time with WINDOW_UPDATE(0, 15,663,105) → total 15 MiB
//!   - per-stream INITIAL_WINDOW_SIZE = 6,291,456 via SETTINGS

/// Chrome's target connection receive window: 15 MiB.
pub const CHROME_CONNECTION_WINDOW: u32 = 15 * 1024 * 1024;
/// RFC default initial windows (connection + per-stream before SETTINGS).
pub const RFC_DEFAULT_WINDOW: u32 = 65_535;
/// The exact WINDOW_UPDATE increment Chrome sends on stream 0 at preface.
pub const CHROME_CONN_WINDOW_INCREMENT: u32 = CHROME_CONNECTION_WINDOW - RFC_DEFAULT_WINDOW;
/// Chrome's stream-level INITIAL_WINDOW_SIZE setting.
pub const CHROME_STREAM_INITIAL_WINDOW: u32 = 6_291_456;

/// Chrome's SETTINGS frame, exact key set and order (Akamai-fingerprinted).
pub const CHROME_H2_SETTINGS: &[(u16, u32)] = &[
    (0x1, 65_536),    // HEADER_TABLE_SIZE
    (0x2, 0),         // ENABLE_PUSH
    (0x4, 6_291_456), // INITIAL_WINDOW_SIZE
    (0x6, 262_144),   // MAX_HEADER_LIST_SIZE
];

/// Tracks a receive window and emits WINDOW_UPDATE decisions the way Chrome
/// does: replenish in bulk once half the advertised window has been consumed.
#[derive(Debug)]
pub struct ReceiveWindow {
    capacity: u32,
    remaining: i64,
}

impl ReceiveWindow {
    pub fn new(capacity: u32) -> Self {
        ReceiveWindow {
            capacity,
            remaining: capacity as i64,
        }
    }

    /// A DATA frame of `len` bytes arrived on this window.
    pub fn on_data(&mut self, len: usize) {
        self.remaining -= len as i64;
    }

    /// If we should replenish now, return the increment (and consume the
    /// decision): Chrome sends one WINDOW_UPDATE for the consumed amount once
    /// the window has dropped below half its capacity.
    pub fn replenish(&mut self) -> Option<u32> {
        if self.remaining < self.capacity as i64 / 2 {
            let inc = self.capacity as i64 - self.remaining;
            let inc = inc.min(0x7fff_ffff) as u32;
            self.remaining = self.capacity as i64;
            Some(inc)
        } else {
            None
        }
    }

    pub fn remaining(&self) -> i64 {
        self.remaining
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chrome_window_increment() {
        // Ground truth: peet.ws observed WINDOW_UPDATE(0, 15663105).
        assert_eq!(CHROME_CONN_WINDOW_INCREMENT, 15_663_105);
        assert_eq!(
            RFC_DEFAULT_WINDOW + CHROME_CONN_WINDOW_INCREMENT,
            CHROME_CONNECTION_WINDOW
        );
    }

    #[test]
    fn chrome_settings_exact() {
        assert_eq!(
            CHROME_H2_SETTINGS,
            &[(0x1, 65_536), (0x2, 0), (0x4, 6_291_456), (0x6, 262_144)]
        );
    }

    #[test]
    fn replenish_at_half() {
        let mut w = ReceiveWindow::new(1000);
        assert_eq!(w.replenish(), None);
        w.on_data(499);
        assert_eq!(w.replenish(), None);
        w.on_data(1);
        assert_eq!(w.replenish(), Some(500));
        assert_eq!(w.remaining(), 1000);
    }
}
