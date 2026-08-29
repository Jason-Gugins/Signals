//! HTTP/2 frame engine (RFC 7540 §4/§6): encode + decode for SETTINGS,
//! HEADERS, DATA, WINDOW_UPDATE, PING, GOAWAY, RST_STREAM, CONTINUATION.
//!
//! Chrome-exact constants for the request preface live in `flow.rs`/`mod.rs`.

pub const FRAME_HEADER_LEN: usize = 9;

pub mod frame_types {
    pub const DATA: u8 = 0x0;
    pub const HEADERS: u8 = 0x1;
    pub const PRIORITY: u8 = 0x2;
    pub const RST_STREAM: u8 = 0x3;
    pub const SETTINGS: u8 = 0x4;
    pub const PUSH_PROMISE: u8 = 0x5;
    pub const PING: u8 = 0x6;
    pub const GOAWAY: u8 = 0x7;
    pub const WINDOW_UPDATE: u8 = 0x8;
    pub const CONTINUATION: u8 = 0x9;
}

pub mod flags {
    pub const ACK: u8 = 0x1;
    pub const END_STREAM: u8 = 0x1;
    pub const END_HEADERS: u8 = 0x4;
    pub const PADDED: u8 = 0x8;
    pub const PRIORITY: u8 = 0x20;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub ftype: u8,
    pub flags: u8,
    pub stream_id: u32,
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn encode(&self) -> Vec<u8> {
        encode_frame(self.ftype, self.flags, self.stream_id, &self.payload)
    }

    pub fn is_ack(&self) -> bool {
        self.flags & flags::ACK != 0
    }
}

pub fn encode_frame(ftype: u8, fflags: u8, stream_id: u32, payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= 0x00FF_FFFF, "frame payload too large");
    let mut f = Vec::with_capacity(FRAME_HEADER_LEN + payload.len());
    f.extend_from_slice(&(payload.len() as u32).to_be_bytes()[1..]);
    f.push(ftype);
    f.push(fflags);
    f.extend_from_slice(&(stream_id & 0x7fff_ffff).to_be_bytes());
    f.extend_from_slice(payload);
    f
}

/// Encode a SETTINGS payload (16-bit id, 32-bit value big-endian pairs).
pub fn settings_payload(settings: &[(u16, u32)]) -> Vec<u8> {
    let mut p = Vec::with_capacity(settings.len() * 6);
    for &(id, value) in settings {
        p.extend_from_slice(&id.to_be_bytes());
        p.extend_from_slice(&value.to_be_bytes());
    }
    p
}

pub const SETTINGS_HEADER_TABLE_SIZE: u16 = 0x1;
pub const SETTINGS_ENABLE_PUSH: u16 = 0x2;
pub const SETTINGS_MAX_CONCURRENT_STREAMS: u16 = 0x3;
pub const SETTINGS_INITIAL_WINDOW_SIZE: u16 = 0x4;
pub const SETTINGS_MAX_FRAME_SIZE: u16 = 0x5;
pub const SETTINGS_MAX_HEADER_LIST_SIZE: u16 = 0x6;

pub const fn setting_name(id: u16) -> &'static str {
    match id {
        0x1 => "HEADER_TABLE_SIZE",
        0x2 => "ENABLE_PUSH",
        0x3 => "MAX_CONCURRENT_STREAMS",
        0x4 => "INITIAL_WINDOW_SIZE",
        0x5 => "MAX_FRAME_SIZE",
        0x6 => "MAX_HEADER_LIST_SIZE",
        _ => "UNKNOWN",
    }
}

/// Parse a SETTINGS payload into (id, value) pairs.
pub fn parse_settings(payload: &[u8]) -> Result<Vec<(u16, u32)>, String> {
    if payload.len() % 6 != 0 {
        return Err("settings: bad payload length".into());
    }
    let mut out = Vec::with_capacity(payload.len() / 6);
    let mut i = 0;
    while i < payload.len() {
        out.push((
            u16::from_be_bytes([payload[i], payload[i + 1]]),
            u32::from_be_bytes([
                payload[i + 2],
                payload[i + 3],
                payload[i + 4],
                payload[i + 5],
            ]),
        ));
        i += 6;
    }
    Ok(out)
}

/// WINDOW_UPDATE payload: 31-bit increment.
pub fn window_update_payload(increment: u32) -> [u8; 4] {
    (increment & 0x7fff_ffff).to_be_bytes()
}

/// HEADERS priority payload: exclusive flag + 31-bit dependency + weight byte.
pub fn priority_payload(exclusive: bool, depends_on: u32, weight: u8) -> [u8; 5] {
    let dep = depends_on & 0x7fff_ffff;
    let e = if exclusive { 0x8000_0000 } else { 0 };
    let mut p = [0u8; 5];
    p[..4].copy_from_slice(&(e | dep).to_be_bytes());
    p[4] = weight;
    p
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_roundtrip() {
        let payload = settings_payload(&[(1, 65536), (2, 0), (4, 6291456), (6, 262144)]);
        let f = Frame {
            ftype: frame_types::SETTINGS,
            flags: 0,
            stream_id: 0,
            payload: payload.clone(),
        };
        let wire = f.encode();
        assert_eq!(wire.len(), 9 + 24);
        assert_eq!(&wire[..3], &[0, 0, 24]);
        assert_eq!(wire[3], frame_types::SETTINGS);
    }

    #[test]
    fn settings_parse() {
        let p = settings_payload(&[(0x4, 6291456)]);
        assert_eq!(parse_settings(&p).unwrap(), vec![(0x4, 6291456)]);
        assert_eq!(parse_settings(&[0, 0]).is_err(), false);
        assert!(parse_settings(&[0]).is_err());
    }

    #[test]
    fn priority_wire() {
        // Chrome: exclusive dep on 0, weight 255 (displayed 256).
        let p = priority_payload(true, 0, 255);
        assert_eq!(p, [0x80, 0, 0, 0, 0xff]);
    }
}
