//! Happy Eyeballs (RFC 8300 / Chrome behavior): race IPv6 vs IPv4 connection
//! attempts with a ~250ms stagger so slow-or-broken IPv6 paths never stall
//! the fetch. Chrome resolves both families, starts connecting to the
//! preferred family (v6) immediately, and if it hasn't connected within the
//! stagger window also starts the other family; first connect to complete
//! wins, the loser is dropped.
//!
//! std-only (net, thread, mpsc) — no new crates.

use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::time::Duration;

/// Chrome's Happy Eyeballs stagger (Resolution Delay / interface attempt gap).
pub const STAGGER_MS: u64 = 250;

/// One DNS resolution, partitioned per address family.
pub struct FamilySplit {
    pub v4: Vec<SocketAddr>,
    pub v6: Vec<SocketAddr>,
}

impl FamilySplit {
    /// Resolve `(host, port)` and partition the results into IPv6 and IPv4
    /// lists (preserving each list's resolver order — Chrome tries the
    /// first address of the preferred family first).
    pub fn resolve(host: &str, port: u16) -> Result<FamilySplit, String> {
        let addrs: Vec<SocketAddr> = (host, port)
            .to_socket_addrs()
            .map_err(|e| format!("resolve {host}: {e}"))?
            .collect();
        let mut v4 = Vec::new();
        let mut v6 = Vec::new();
        for a in addrs {
            if a.is_ipv6() {
                v6.push(a);
            } else {
                v4.push(a);
            }
        }
        Ok(FamilySplit { v4, v6 })
    }
}

/// Attempt a TCP connect with a bounded timeout (thread-based, std-only).
fn connect_timeout(addr: SocketAddr, timeout: Duration) -> Result<TcpStream, String> {
    // std's TcpStream::connect_timeout requires a single SocketAddr — exactly
    // what we have per-family.
    TcpStream::connect_timeout(&addr, timeout).map_err(|e| format!("tcp connect {addr}: {e}"))
}

/// Happy Eyeballs connect: race the two address families with `stagger_ms`
/// between attempts. Returns the first connected stream; the losing attempt
/// (if any) is dropped when its thread finishes.
///
/// Single-family hosts connect directly (the stagger is moot).
pub fn connect_happy_eyeballs(host: &str, port: u16, stagger_ms: u64) -> Result<TcpStream, String> {
    let split = FamilySplit::resolve(host, port)?;

    // Only one family present (or neither): behave exactly like a plain dial.
    match (split.v6.is_empty(), split.v4.is_empty()) {
        (true, true) => return Err(format!("no addresses resolved for {host}")),
        (true, false) => return connect_timeout(split.v4[0], Duration::from_secs(15)),
        (false, true) => return connect_timeout(split.v6[0], Duration::from_secs(15)),
        (false, false) => {}
    }

    // Both families: preferred = v6 (Chrome's Happy Eyeballs preference).
    // Channel capacity 2: whichever completes first is received; a second
    // late result is consumed/dropped at the end so no thread leaks errors.
    let (tx, rx) = mpsc::channel::<Result<TcpStream, String>>();

    // Preferred family dials immediately.
    {
        let tx = tx.clone();
        let addr = split.v6[0];
        std::thread::spawn(move || {
            let _ = tx.send(connect_timeout(addr, Duration::from_secs(15)));
        });
    }

    // Other family sleeps the stagger, then dials.
    {
        let tx = tx.clone();
        let addr = split.v4[0];
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(stagger_ms));
            let _ = tx.send(connect_timeout(addr, Duration::from_secs(15)));
        });
    }
    drop(tx);

    // Block until either connect completes (first to finish wins).
    let first = rx
        .recv()
        .map_err(|_| format!("happy eyeballs: no connect attempt finished for {host}"))?;
    // Drain a possible late second result so its thread's stream/error is
    // dropped promptly (non-blocking).
    let _ = rx.try_recv();

    first
}

/// JSON report of a Happy Eyeballs dial: family partition counts, the
/// stagger, and which family's connection won (testability helper).
pub fn connect_eyeballs_timing(host: &str, port: u16, stagger_ms: u64) -> Result<String, String> {
    let split = FamilySplit::resolve(host, port)?;
    let stream = connect_happy_eyeballs(host, port, stagger_ms)?;
    let winner_family = if stream.local_addr().map(|a| a.is_ipv6()).unwrap_or(false) {
        "v6"
    } else {
        "v4"
    };
    Ok(serde_json::json!({
        "v4_addrs": split.v4.len(),
        "v6_addrs": split.v6.len(),
        "stagger_ms": stagger_ms,
        "winner_family": winner_family,
    })
    .to_string())
}

/// JSON report of the per-family DNS partition (no connection — unit tests).
pub fn split_families_json(host: &str, port: u16) -> Result<String, String> {
    let split = FamilySplit::resolve(host, port)?;
    Ok(serde_json::json!({
        "v4_addrs": split.v4.len(),
        "v6_addrs": split.v6.len(),
    })
    .to_string())
}
