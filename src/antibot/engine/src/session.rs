//! Task 3 temporal stealth, part 1: per-origin TLS session-ticket store.
//!
//! Chrome resumes TLS sessions per origin (abbreviated handshakes on the
//! second visit to a site). Scrapers never do — that's the tell. We store the
//! `SSL_SESSION` BoringSSL hands us after each full handshake, keyed by origin
//! host, and feed it back via `SSL_set_session` on the next dial to the same
//! origin.
//!
//! boring 4.22 has everything needed on safe-ish paths: `SslRef::set_session`
//! (unsafe, like the ALPS pattern), `SslRef::session_reused()`,
//! `SslStream::ssl().session()`, and `SslSession` is `Clone` (refcounted
//! BoringSSL object) + `Send`/`Sync` via boring's foreign-type impls. No raw
//! FFI required for this piece.

use std::collections::HashMap;

use boring::ssl::{SslSession, SslSessionRef};

/// Per-origin store of TLS session tickets. One entry per origin — Chrome
/// keeps exactly one current session per origin (the newest ticket wins).
pub struct SessionStore {
    sessions: HashMap<String, SslSession>,
}

impl SessionStore {
    pub fn new() -> Self {
        SessionStore {
            sessions: HashMap::new(),
        }
    }

    /// The stored session for an origin, if any.
    pub fn get(&self, origin: &str) -> Option<&SslSessionRef> {
        self.sessions.get(origin).map(|s| s.as_ref())
    }

    /// Remember the session handed out by a handshake. The newest ticket for
    /// an origin replaces any older one (Chrome semantics).
    pub fn store(&mut self, origin: &str, session: &SslSessionRef) {
        self.sessions.insert(origin.to_string(), session.to_owned());
    }

    pub fn len(&self) -> usize {
        self.sessions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }

    /// Drop everything (mirrors closing the browser profile).
    pub fn clear(&mut self) {
        self.sessions.clear();
    }
}

impl Default for SessionStore {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn store_starts_empty() {
        let s = SessionStore::new();
        assert!(s.is_empty());
        assert!(s.get("example.com").is_none());
    }

    #[test]
    fn clear_drops_all() {
        let mut s = SessionStore::new();
        s.clear();
        assert!(s.is_empty());
    }
}
