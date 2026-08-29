//! Own HTTP/2 client stack — byte-exact Chrome wire behavior.
//!
//! Everything Akamai fingerprints is implemented here, not delegated to an
//! off-the-shelf h2 library (off-the-shelf leaks): exact SETTINGS set/order,
//! the preface WINDOW_UPDATE (15,663,105 → 15 MiB connection window),
//! HEADERS-with-priority framing, and Chrome's pseudo-header order
//! (`:method, :authority, :scheme, :path` → Akamai's `m,a,s,p`).
//!
//! Ground truth: tests/fixtures/antibot/chrome_fingerprint_reference.json
//! (raw.http2.sent_frames, captured from real Chromium).

pub mod flow;
pub mod frames;
pub mod hpack;
mod tables;

pub use flow::CHROME_H2_SETTINGS;
pub use frames::{frame_types, Frame};

use std::io::{Read, Write};

use flow::{CHROME_CONN_WINDOW_INCREMENT, CHROME_STREAM_INITIAL_WINDOW};
use frames::flags;

/// HTTP/2 connection preface magic (RFC 7540 §3.5).
pub const PREFACE_MAGIC: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";

/// The exact bytes Chrome sends right after the TLS handshake completes:
/// preface magic + SETTINGS + WINDOW_UPDATE(stream 0, 15 MiB window).
pub fn chrome_preface_bytes() -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(PREFACE_MAGIC);
    out.extend_from_slice(
        &Frame {
            ftype: frame_types::SETTINGS,
            flags: 0,
            stream_id: 0,
            payload: frames::settings_payload(CHROME_H2_SETTINGS),
        }
        .encode(),
    );
    out.extend_from_slice(
        &Frame {
            ftype: frame_types::WINDOW_UPDATE,
            flags: 0,
            stream_id: 0,
            payload: frames::window_update_payload(CHROME_CONN_WINDOW_INCREMENT).to_vec(),
        }
        .encode(),
    );
    out
}

/// Chrome's request pseudo-header order — NOTE: `:authority` before `:scheme`,
/// which is what produces Akamai's `m,a,s,p` segment.
pub const PSEUDO_ORDER: [&str; 4] = [":method", ":authority", ":scheme", ":path"];

/// Chrome's regular-header order and values for a navigational GET, exactly
/// as captured from real Chromium (sent_frames HEADERS entry). `:authority`,
/// `:path` and `user-agent` are substituted per request.
pub const CHROME_REQUEST_HEADERS: &[(&str, &str)] = &[
    (":method", "GET"),
    (":authority", "{authority}"),
    (":scheme", "https"),
    (":path", "{path}"),
    (
        "sec-ch-ua",
        "\"Chromium\";v=\"151\", \"Not=A?Brand\";v=\"99\"",
    ),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", "\"Windows\""),
    ("upgrade-insecure-requests", "1"),
    ("user-agent", "{user_agent}"),
    ("accept-language", "en-US"),
    (
        "accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    ),
    ("sec-fetch-site", "none"),
    ("sec-fetch-mode", "navigate"),
    ("sec-fetch-user", "?1"),
    ("sec-fetch-dest", "document"),
    ("accept-encoding", "gzip, deflate, br, zstd"),
    ("priority", "u=0, i"),
];

/// Default UA matching the pinned Chrome posture (fingerprints.py CHROME_TARGET).
pub const CHROME_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36";

/// Build the ordered request header list for a navigational GET.
/// `extra` headers are appended after Chrome's own (e.g. cookies) — Chrome
/// appends cookies at the end of the header block.
pub fn build_request_headers(
    authority: &str,
    path: &str,
    user_agent: &str,
    extra: &[(String, String)],
) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = CHROME_REQUEST_HEADERS
        .iter()
        .map(|&(n, v)| {
            let v = v
                .replace("{authority}", authority)
                .replace("{path}", path)
                .replace("{user_agent}", user_agent);
            (n.to_string(), v)
        })
        .collect();
    for (n, v) in extra {
        out.push((n.clone(), v.clone()));
    }
    out
}

/// An HTTP/2 response.
#[derive(Debug)]
pub struct H2Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// A single-request HTTP/2 client over an established TLS stream.
/// One stream per connection (request/abort), which matches a fresh Chrome
/// connection for the first request; pooling/resumption is Task 3.
pub struct H2Client<T: Read + Write> {
    stream: T,
    buf: Vec<u8>, // read buffer
    enc: hpack::Encoder,
    dec: hpack::Decoder,
    conn_window: flow::ReceiveWindow,
    stream_window: flow::ReceiveWindow,
}

const MAX_BODY: usize = 32 * 1024 * 1024;

impl<T: Read + Write> H2Client<T> {
    pub fn new(stream: T) -> Self {
        H2Client {
            stream,
            buf: Vec::new(),
            enc: hpack::Encoder::new(65_536),
            dec: hpack::Decoder::new(65_536),
            conn_window: flow::ReceiveWindow::new(flow::CHROME_CONNECTION_WINDOW),
            stream_window: flow::ReceiveWindow::new(CHROME_STREAM_INITIAL_WINDOW),
        }
    }

    fn write_all(&mut self, data: &[u8]) -> Result<(), String> {
        self.stream
            .write_all(data)
            .map_err(|e| format!("h2 write: {e}"))
    }

    /// Send the client preface (magic + Chrome SETTINGS + WINDOW_UPDATE).
    pub fn send_preface(&mut self) -> Result<(), String> {
        self.write_all(&chrome_preface_bytes())
    }

    /// Send a GET request on stream 1 and read the response to END_STREAM.
    pub fn get(&mut self, headers: &[(String, String)]) -> Result<H2Response, String> {
        let block = self.enc.encode(headers);
        // Chrome sends HEADERS with the PRIORITY flag: exclusive dep on 0,
        // weight byte 255 (displayed 256).
        let mut payload = frames::priority_payload(true, 0, 255).to_vec();
        payload.extend_from_slice(&block);
        let headers_frame = Frame {
            ftype: frame_types::HEADERS,
            flags: flags::END_STREAM | flags::END_HEADERS | flags::PRIORITY,
            stream_id: 1,
            payload,
        };
        self.write_all(&headers_frame.encode())?;
        self.read_response(1)
    }

    /// Read frames until the given stream is complete; service the
    /// connection (SETTINGS ACK, PING ACK, flow control) as Chrome would.
    fn read_response(&mut self, stream_id: u32) -> Result<H2Response, String> {
        let mut status: Option<u16> = None;
        let mut resp_headers: Vec<(String, String)> = Vec::new();
        let mut body: Vec<u8> = Vec::new();
        let mut header_block: Vec<u8> = Vec::new();
        let mut headers_done = false;

        loop {
            let frame = self.next_frame()?;
            match frame.ftype {
                frame_types::SETTINGS => {
                    if !frame.is_ack() {
                        let ack = Frame {
                            ftype: frame_types::SETTINGS,
                            flags: flags::ACK,
                            stream_id: 0,
                            payload: vec![],
                        };
                        self.write_all(&ack.encode())?;
                    }
                }
                frame_types::PING => {
                    if !frame.is_ack() {
                        let ack = Frame {
                            ftype: frame_types::PING,
                            flags: flags::ACK,
                            stream_id: 0,
                            payload: frame.payload.clone(),
                        };
                        self.write_all(&ack.encode())?;
                    }
                }
                frame_types::WINDOW_UPDATE => { /* peer adjusting send windows */ }
                frame_types::HEADERS => {
                    if frame.stream_id == stream_id {
                        let mut block = frame.payload.clone();
                        if frame.flags & flags::PRIORITY != 0 && block.len() >= 5 {
                            block.drain(..5);
                        }
                        // PADDED handling (Chrome doesn't pad, but be correct)
                        if frame.flags & flags::PADDED != 0 && !block.is_empty() {
                            let pad = block[0] as usize;
                            block.drain(..1);
                            if block.len() >= pad {
                                block.truncate(block.len() - pad);
                            }
                        }
                        header_block.extend_from_slice(&block);
                        if frame.flags & flags::END_HEADERS != 0 {
                            let hs = self.dec.decode(&header_block)?;
                            header_block.clear();
                            headers_done = true;
                            for (n, v) in hs {
                                if n == ":status" {
                                    status = v.parse().ok();
                                } else {
                                    resp_headers.push((n, v));
                                }
                            }
                        }
                    }
                }
                frame_types::CONTINUATION => {
                    if frame.stream_id == stream_id && !headers_done {
                        header_block.extend_from_slice(&frame.payload);
                        if frame.flags & flags::END_HEADERS != 0 {
                            let hs = self.dec.decode(&header_block)?;
                            header_block.clear();
                            headers_done = true;
                            for (n, v) in hs {
                                if n == ":status" {
                                    status = v.parse().ok();
                                } else {
                                    resp_headers.push((n, v));
                                }
                            }
                        }
                    }
                }
                frame_types::DATA => {
                    if frame.stream_id == stream_id {
                        let mut payload = frame.payload.clone();
                        if frame.flags & flags::PADDED != 0 && !payload.is_empty() {
                            let pad = payload[0] as usize;
                            payload.drain(..1);
                            if payload.len() >= pad {
                                payload.truncate(payload.len() - pad);
                            }
                        }
                        self.conn_window.on_data(payload.len());
                        self.stream_window.on_data(payload.len());
                        body.extend_from_slice(&payload);
                        if body.len() > MAX_BODY {
                            return Err("h2: response too large".into());
                        }
                        // Replenish like Chrome: one bulk WINDOW_UPDATE each
                        // for stream 0 and stream N once half is consumed.
                        if let Some(inc) = self.conn_window.replenish() {
                            self.write_all(
                                &Frame {
                                    ftype: frame_types::WINDOW_UPDATE,
                                    flags: 0,
                                    stream_id: 0,
                                    payload: frames::window_update_payload(inc).to_vec(),
                                }
                                .encode(),
                            )?;
                        }
                        if let Some(inc) = self.stream_window.replenish() {
                            self.write_all(
                                &Frame {
                                    ftype: frame_types::WINDOW_UPDATE,
                                    flags: 0,
                                    stream_id,
                                    payload: frames::window_update_payload(inc).to_vec(),
                                }
                                .encode(),
                            )?;
                        }
                    }
                }
                frame_types::GOAWAY => {
                    return Err(format!(
                        "h2: server GOAWAY ({} debug)",
                        String::from_utf8_lossy(&frame.payload)
                    ));
                }
                frame_types::RST_STREAM => {
                    if frame.stream_id == stream_id {
                        return Err("h2: stream reset by server".into());
                    }
                }
                _ => {}
            }
            if headers_done
                && frame.ftype == frame_types::DATA
                && frame.flags & flags::END_STREAM != 0
            {
                return Ok(H2Response {
                    status: status.ok_or("h2: no :status in response")?,
                    headers: resp_headers,
                    body,
                });
            }
            // Guard: END_STREAM HEADERS (no body) — handled above only for
            // DATA; also finish if headers carried END_STREAM.
            if headers_done
                && frame.ftype == frame_types::HEADERS
                && frame.flags & flags::END_STREAM != 0
            {
                return Ok(H2Response {
                    status: status.ok_or("h2: no :status in response")?,
                    headers: resp_headers,
                    body,
                });
            }
        }
    }

    /// Pull the next complete frame from the stream (handles read boundaries).
    fn next_frame(&mut self) -> Result<Frame, String> {
        loop {
            if self.buf.len() >= frames::FRAME_HEADER_LEN {
                let len = ((self.buf[0] as usize) << 16)
                    | ((self.buf[1] as usize) << 8)
                    | self.buf[2] as usize;
                let end = frames::FRAME_HEADER_LEN + len;
                if self.buf.len() >= end {
                    let ftype = self.buf[3];
                    let fflags = self.buf[4];
                    let sid = u32::from_be_bytes([
                        self.buf[5] & 0x7f,
                        self.buf[6],
                        self.buf[7],
                        self.buf[8],
                    ]);
                    let payload = self.buf[frames::FRAME_HEADER_LEN..end].to_vec();
                    self.buf.drain(..end);
                    return Ok(Frame {
                        ftype,
                        flags: fflags,
                        stream_id: sid,
                        payload,
                    });
                }
            }
            let mut chunk = [0u8; 16384];
            let n = self
                .stream
                .read(&mut chunk)
                .map_err(|e| format!("h2 read: {e}"))?;
            if n == 0 {
                return Err("h2: connection closed before END_STREAM".into());
            }
            self.buf.extend_from_slice(&chunk[..n]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preface_wire_matches_chrome_capture() {
        let pre = chrome_preface_bytes();
        // magic
        assert_eq!(&pre[..24], PREFACE_MAGIC);
        // SETTINGS frame: len 24, type 4, stream 0, payload = 4 settings
        assert_eq!(&pre[24..33], &[0, 0, 24, 4, 0, 0, 0, 0, 0]);
        assert_eq!(
            &pre[33..57],
            &[
                0, 1, 0, 1, 0, 0, // HEADER_TABLE_SIZE 65536
                0, 2, 0, 0, 0, 0, // ENABLE_PUSH 0
                0, 4, 0, 0x60, 0, 0, // INITIAL_WINDOW_SIZE 6291456
                0, 6, 0, 4, 0, 0, // MAX_HEADER_LIST_SIZE 262144
            ]
        );
        // WINDOW_UPDATE stream 0 increment 15663105
        assert_eq!(&pre[57..66], &[0, 0, 4, 8, 0, 0, 0, 0, 0]);
        assert_eq!(&pre[66..70], &15_663_105u32.to_be_bytes());
        assert_eq!(pre.len(), 70);
    }

    #[test]
    fn pseudo_header_order_is_chrome() {
        assert_eq!(PSEUDO_ORDER, [":method", ":authority", ":scheme", ":path"]);
        let hs = build_request_headers("example.com", "/", CHROME_USER_AGENT, &[]);
        let names: Vec<&str> = hs.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(&names[..4], &[":method", ":authority", ":scheme", ":path"]);
        assert_eq!(names[4], "sec-ch-ua");
        assert_eq!(names.last().unwrap(), &"priority");
    }

    #[test]
    fn header_substitution() {
        let hs = build_request_headers("tls.peet.ws", "/api/all", CHROME_USER_AGENT, &[]);
        assert_eq!(hs[1].1, "tls.peet.ws");
        assert_eq!(hs[3].1, "/api/all");
        assert!(hs[8].1.contains("Chrome/151"));
    }
}
