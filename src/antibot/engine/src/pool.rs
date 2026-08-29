//! Task 3 temporal stealth, part 2: origin-keyed HTTP/2 connection pool.
//!
//! A fresh TCP+TLS+h2 connection per request is a bot signal — Chrome keeps a
//! keep-alive socket per origin and multiplexes successive requests onto it as
//! new h2 streams. We keep at most one live h2 connection per origin (Chrome's
//! single-socket posture for a simple browsing session) and hand it back for
//! reuse; the caller redials when the pooled connection has died (idle close,
//! GOAWAY) and returns the fresh one to the pool.

use std::collections::HashMap;
use std::net::TcpStream;
use std::time::{Duration, Instant};

use boring::ssl::SslStream;

use crate::h2::H2Client;

/// Chrome-like keep-alive idle window before the pooled connection is
/// considered stale (~90s of server-side keep-alive headroom).
pub const CHROME_IDLE_TIMEOUT: Duration = Duration::from_secs(90);

pub struct PooledConnection {
    pub client: H2Client<SslStream<TcpStream>>,
    last_used: Instant,
}

impl PooledConnection {
    fn new(client: H2Client<SslStream<TcpStream>>) -> Self {
        PooledConnection {
            client,
            last_used: Instant::now(),
        }
    }
}

/// Origin -> pooled h2 connection (keep-alive reuse).
pub struct ConnectionPool {
    conns: HashMap<String, PooledConnection>,
    idle_timeout: Duration,
}

impl ConnectionPool {
    pub fn new() -> Self {
        ConnectionPool {
            conns: HashMap::new(),
            idle_timeout: CHROME_IDLE_TIMEOUT,
        }
    }

    /// Take the pooled connection for an origin out of the pool, if it exists
    /// and is within the idle window. Ownership transfers to the caller so a
    /// failed reuse attempt cannot poison the pool; the caller re-inserts the
    /// (fresh or revived) connection afterwards.
    pub fn take(&mut self, origin: &str) -> Option<H2Client<SslStream<TcpStream>>> {
        let idle_ok = |c: &PooledConnection| c.last_used.elapsed() < self.idle_timeout;
        match self.conns.remove(origin) {
            Some(c) if idle_ok(&c) => Some(c.client),
            _ => None,
        }
    }

    /// Put a (freshly dialed or successfully reused) connection back.
    pub fn insert(&mut self, origin: &str, client: H2Client<SslStream<TcpStream>>) {
        self.conns
            .insert(origin.to_string(), PooledConnection::new(client));
    }

    /// Drop connections idle beyond the keep-alive window (call opportunistically).
    pub fn evict_idle(&mut self) {
        let timeout = self.idle_timeout;
        self.conns.retain(|_, c| c.last_used.elapsed() < timeout);
    }

    pub fn len(&self) -> usize {
        self.conns.len()
    }

    pub fn is_empty(&self) -> bool {
        self.conns.is_empty()
    }

    pub fn contains(&self, origin: &str) -> bool {
        self.conns.contains_key(origin)
    }

    /// Close everything (mirrors closing the browser profile).
    pub fn clear(&mut self) {
        self.conns.clear();
    }
}

impl Default for ConnectionPool {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_pool_take_is_none() {
        let mut p = ConnectionPool::new();
        assert!(p.take("example.com").is_none());
    }

    #[test]
    fn evict_idle_drops_stale_entries() {
        let mut p = ConnectionPool::new();
        p.idle_timeout = Duration::from_secs(0);
        p.evict_idle();
        assert!(p.is_empty());
    }
}
