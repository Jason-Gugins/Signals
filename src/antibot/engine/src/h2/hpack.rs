//! HPACK (RFC 7541) — full implementation: static table (61 entries), the
//! complete 257-symbol Huffman code, and a size-bounded dynamic table with
//! Chrome's indexing policy.
//!
//! Clean-room implementation from the RFC text; the code tables in
//! `tables.rs` were machine-extracted from the RFC plain text (Appendix A/B)
//! and cross-validated against the Appendix C examples.

use super::tables::{HUFFMAN_CODES, STATIC_TABLE};

pub const STATIC_TABLE_LEN: usize = 61;

// ---------------------------------------------------------------------------
// Huffman code (RFC 7541 §5.2, Appendix B)
// ---------------------------------------------------------------------------

/// Append the Huffman code for `byte` (padding with EOS bits to the octet
/// boundary) to a bit-buffer. Returns the finished octets.
struct BitWriter {
    out: Vec<u8>,
    acc: u64,
    nbits: u32,
}

impl BitWriter {
    fn new() -> Self {
        BitWriter {
            out: Vec::new(),
            acc: 0,
            nbits: 0,
        }
    }
    fn push(&mut self, value: u32, len: u8) {
        self.acc = (self.acc << len) | value as u64;
        self.nbits += len as u32;
        while self.nbits >= 8 {
            self.nbits -= 8;
            self.out.push((self.acc >> self.nbits) as u8);
        }
        self.acc &= (1 << self.nbits) - 1;
    }
    /// EOS-pad to the octet boundary and flush.
    fn finish(mut self) -> Vec<u8> {
        if self.nbits > 0 {
            let pad = 8 - self.nbits;
            // Most significant bits of EOS (all ones) are the padding.
            self.acc = (self.acc << pad) | ((1u64 << pad) - 1);
            self.out.push(self.acc as u8);
        }
        self.out
    }
}

/// Huffman-encode `data` per RFC 7541 §5.2 (EOS padding).
pub fn huffman_encode(data: &[u8]) -> Vec<u8> {
    let mut w = BitWriter::new();
    for &b in data {
        let (code, len) = HUFFMAN_CODES[b as usize];
        w.push(code, len);
    }
    w.finish()
}

/// Bit-reader over Huffman-encoded string data.
struct BitReader<'a> {
    data: &'a [u8],
    bitpos: usize,
}

impl<'a> BitReader<'a> {
    fn peek(&self, n: u32) -> u32 {
        // Peek up to 30 bits, zero-padded past the end.
        let mut v: u32 = 0;
        for i in 0..n {
            let pos = self.bitpos + i as usize;
            let bit = if pos / 8 < self.data.len() {
                (self.data[pos / 8] >> (7 - pos % 8)) & 1
            } else {
                0
            };
            v = (v << 1) | bit as u32;
        }
        v
    }
    fn consume(&mut self, n: u32) {
        self.bitpos += n as usize;
    }
    fn eos_padding_ok(&self) -> bool {
        // Remaining bits (0..7) must all be ones (EOS MSBs) and < 8 bits.
        let rem_bits = self.data.len() * 8 - self.bitpos;
        if rem_bits >= 8 {
            return false;
        }
        for i in 0..rem_bits {
            let pos = self.bitpos + i;
            if (self.data[pos / 8] >> (7 - pos % 8)) & 1 == 0 {
                return false;
            }
        }
        true
    }
}

/// Huffman-decode `data`; errors on EOS, invalid codes or >7-bit padding.
pub fn huffman_decode(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut r = BitReader { data, bitpos: 0 };
    while r.bitpos / 8 < data.len() {
        let mut matched = false;
        // Canonical decode: try every symbol's code (257 entries, small table).
        for (sym, &(code, len)) in HUFFMAN_CODES.iter().enumerate() {
            if r.bitpos + len as usize > data.len() * 8 {
                continue; // would run past end — only valid as padding
            }
            if r.peek(len as u32) == code {
                if sym == 256 {
                    return Err("huffman: EOS symbol in payload".into());
                }
                out.push(sym as u8);
                r.consume(len as u32);
                matched = true;
                break;
            }
        }
        if !matched {
            // Either invalid code or trailing padding.
            let rem = data.len() * 8 - r.bitpos;
            if rem < 8 && r.eos_padding_ok() && rem > 0 {
                return Ok(out);
            }
            if rem == 0 {
                return Ok(out);
            }
            return Err("huffman: invalid code".into());
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Integer + string primitives (RFC 7541 §5.1/§5.2)
// ---------------------------------------------------------------------------

fn encode_integer(out: &mut Vec<u8>, value: u64, prefix_bits: u8, first_byte: u8) {
    let max = (1u64 << prefix_bits) - 1;
    if value < max {
        out.push(first_byte | value as u8);
    } else {
        out.push(first_byte | max as u8);
        let mut v = value - max;
        while v >= 128 {
            out.push((v % 128) as u8 | 0x80);
            v /= 128;
        }
        out.push(v as u8);
    }
}

struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn byte(&mut self) -> Result<u8, String> {
        let b = *self.data.get(self.pos).ok_or("hpack: truncated input")?;
        self.pos += 1;
        Ok(b)
    }
    fn integer(&mut self, prefix_bits: u8) -> Result<u64, String> {
        let first = self.byte()?;
        self.integer_from(first, prefix_bits)
    }
    fn integer_from(&mut self, first: u8, prefix_bits: u8) -> Result<u64, String> {
        let max = ((1u64 << prefix_bits) - 1) as u8 as u64;
        let mut value = (first as u64 & max);
        if value < max {
            return Ok(value);
        }
        let mut m = 0u32;
        loop {
            let b = self.byte()?;
            value += ((b & 0x7f) as u64) << m;
            if b & 0x80 == 0 {
                break;
            }
            m += 7;
            if m > 42 {
                return Err("hpack: integer overflow".into());
            }
        }
        Ok(value)
    }
    fn string(&mut self) -> Result<Vec<u8>, String> {
        let first = self.byte()?;
        let huffman = first & 0x80 != 0;
        let len = self.integer_from(first, 7)? as usize;
        if self.pos + len > self.data.len() {
            return Err("hpack: truncated string".into());
        }
        let raw = &self.data[self.pos..self.pos + len];
        self.pos += len;
        if huffman {
            huffman_decode(raw)
        } else {
            Ok(raw.to_vec())
        }
    }
}

fn encode_string(out: &mut Vec<u8>, s: &[u8]) {
    let h = huffman_encode(s);
    if h.len() < s.len() {
        encode_integer(out, h.len() as u64, 7, 0x80);
        out.extend_from_slice(&h);
    } else {
        encode_integer(out, s.len() as u64, 7, 0x00);
        out.extend_from_slice(s);
    }
}

// ---------------------------------------------------------------------------
// Dynamic table (RFC 7541 §4)
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Entry {
    name: String,
    value: String,
    size: usize, // name.len + value.len + 32
}

pub struct DynamicTable {
    entries: Vec<Entry>, // entries[0] is the NEWEST
    max_size: usize,
    size: usize,
}

impl DynamicTable {
    pub fn new(max_size: usize) -> Self {
        DynamicTable {
            entries: Vec::new(),
            max_size,
            size: 0,
        }
    }
    pub fn max_size(&self) -> usize {
        self.max_size
    }
    pub fn set_max_size(&mut self, max: usize) {
        self.max_size = max;
        self.evict_to(max);
    }
    fn evict_to(&mut self, limit: usize) {
        while self.size > limit {
            if let Some(e) = self.entries.pop() {
                self.size -= e.size;
            } else {
                break;
            }
        }
    }
    pub fn insert(&mut self, name: String, value: String) {
        let e = Entry {
            size: name.len() + value.len() + 32,
            name,
            value,
        };
        // §4.4: evict until entry fits (or table empty if entry > max).
        self.evict_to(self.max_size.saturating_sub(e.size));
        if e.size <= self.max_size {
            self.size += e.size;
            self.entries.insert(0, e);
        }
    }
    /// Index into the dynamic table: 0 = newest.
    pub fn get(&self, i: usize) -> Option<(&str, &str)> {
        self.entries
            .get(i)
            .map(|e| (e.name.as_str(), e.value.as_str()))
    }
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn size(&self) -> usize {
        self.size
    }
}

/// Lookup an index in the combined address space (static 1..=61, dynamic after).
fn lookup<'t>(index: u64, dyn_table: &'t DynamicTable) -> Result<(&'t str, &'t str), String> {
    if index == 0 {
        return Err("hpack: index 0 is invalid".into());
    }
    if index <= STATIC_TABLE_LEN as u64 {
        let e = &STATIC_TABLE[index as usize - 1];
        return Ok(*e);
    }
    let d = dyn_table
        .get(index as usize - STATIC_TABLE_LEN - 1)
        .ok_or("hpack: index out of range")?;
    Ok(d)
}

/// Find a static-table index for an exact (name, value) match.
fn static_exact(name: &str, value: &str) -> Option<u64> {
    STATIC_TABLE
        .iter()
        .position(|&(n, v)| n == name && v == value)
        .map(|p| p as u64 + 1)
}

/// Find a static-table index for a name-only match (lowest index wins).
fn static_name(name: &str) -> Option<u64> {
    STATIC_TABLE
        .iter()
        .position(|&(n, _)| n == name)
        .map(|p| p as u64 + 1)
}

// ---------------------------------------------------------------------------
// Encoder with Chrome's indexing policy
// ---------------------------------------------------------------------------

/// Chrome's dynamic-table indexing policy (observed from live Chromium h2
/// captures): frequently-repeated, stable values get literal-with-incremental-
/// indexing (`:authority`, `content-type`); values that vary per request
/// (`:path` with query strings, cookies, referers) are sent literal without
/// indexing so they never pollute the shared compression context.
const INCREMENTAL_INDEXED_NAMES: &[&str] = &[":authority", "content-type"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Indexing {
    Incremental,
    Without,
    Never,
}

pub struct Encoder {
    dyn_table: DynamicTable,
}

impl Encoder {
    pub fn new(max_table_size: usize) -> Self {
        Encoder {
            dyn_table: DynamicTable::new(max_table_size),
        }
    }
    pub fn max_table_size(&self) -> usize {
        self.dyn_table.max_size()
    }

    fn indexing_for(name: &str) -> Indexing {
        // Never index anything that looks like it carries per-request secrets
        // or unbounded values; index Chrome's known-stable set.
        if name == "cookie" || name == "authorization" {
            return Indexing::Never;
        }
        if INCREMENTAL_INDEXED_NAMES.contains(&name) {
            return Indexing::Incremental;
        }
        Indexing::Without
    }

    /// Encode a header list in order, applying the indexing policy and
    /// Huffman-coding every string (Chrome huffman-codes its literals).
    pub fn encode(&mut self, headers: &[(String, String)]) -> Vec<u8> {
        let mut out = Vec::new();
        for (name, value) in headers {
            // 1. Exact static match → indexed field.
            if let Some(idx) = static_exact(name, value) {
                encode_integer(&mut out, idx, 7, 0x80);
                continue;
            }
            // 2. Dynamic-table exact match → indexed field.
            let dyn_hit = (0..self.dyn_table.len()).find(|&i| {
                let (n, v) = self.dyn_table.get(i).unwrap();
                n == name && v == value
            });
            if let Some(i) = dyn_hit {
                let idx = STATIC_TABLE_LEN as u64 + i as u64 + 1;
                encode_integer(&mut out, idx, 7, 0x80);
                continue;
            }
            let mode = Self::indexing_for(name);
            let prefix: u8 = match mode {
                Indexing::Incremental => 0x40,
                Indexing::Without => 0x00,
                Indexing::Never => 0x10,
            };
            let pbits: u8 = match mode {
                Indexing::Incremental => 6,
                _ => 4,
            };
            match (static_name(name), mode) {
                (Some(idx), _) => {
                    // Indexed name (static), literal value.
                    encode_integer(&mut out, idx, pbits, prefix);
                }
                (None, _) => {
                    // New name.
                    out.push(prefix);
                    encode_string(&mut out, name.as_bytes());
                }
            }
            encode_string(&mut out, value.as_bytes());
            if mode == Indexing::Incremental {
                self.dyn_table.insert(name.clone(), value.clone());
            }
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Decoder
// ---------------------------------------------------------------------------

pub struct Decoder {
    dyn_table: DynamicTable,
}

impl Decoder {
    pub fn new(max_table_size: usize) -> Self {
        Decoder {
            dyn_table: DynamicTable::new(max_table_size),
        }
    }

    /// Decode a complete header block into its ordered header list.
    pub fn decode(&mut self, block: &[u8]) -> Result<Vec<(String, String)>, String> {
        let mut out = Vec::new();
        let mut c = Cursor {
            data: block,
            pos: 0,
        };
        while c.pos < c.data.len() {
            let b = c.byte()?;
            if b & 0x80 != 0 {
                // 6.1 Indexed header field.
                let idx = c.integer_from(b, 7)?;
                let (n, v) = lookup(idx, &self.dyn_table)?;
                out.push((n.to_string(), v.to_string()));
            } else if b & 0xc0 == 0x40 {
                // 6.2.1 Literal with incremental indexing.
                let idx = c.integer_from(b, 6)?;
                let name = if idx == 0 {
                    String::from_utf8_lossy(&c.string()?).into_owned()
                } else {
                    lookup(idx, &self.dyn_table)?.0.to_string()
                };
                let value = String::from_utf8_lossy(&c.string()?).into_owned();
                self.dyn_table.insert(name.clone(), value.clone());
                out.push((name, value));
            } else if b & 0xe0 == 0x00 {
                // 6.2.2 Literal without indexing.
                let idx = c.integer_from(b, 4)?;
                let name = if idx == 0 {
                    String::from_utf8_lossy(&c.string()?).into_owned()
                } else {
                    lookup(idx, &self.dyn_table)?.0.to_string()
                };
                let value = String::from_utf8_lossy(&c.string()?).into_owned();
                out.push((name, value));
            } else if b & 0xf0 == 0x10 {
                // 6.2.3 Literal never indexed.
                let idx = c.integer_from(b, 4)?;
                let name = if idx == 0 {
                    String::from_utf8_lossy(&c.string()?).into_owned()
                } else {
                    lookup(idx, &self.dyn_table)?.0.to_string()
                };
                let value = String::from_utf8_lossy(&c.string()?).into_owned();
                out.push((name, value));
            } else {
                // 6.3 Dynamic table size update.
                let new_max = c.integer_from(b, 5)? as usize;
                self.dyn_table.set_max_size(new_max);
            }
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// RFC 7541 Appendix C regression checks (compile-time-documented vectors)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn pairs(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect()
    }

    #[test]
    fn huffman_rfc_vectors() {
        assert_eq!(
            huffman_encode(b"www.example.com"),
            [0xf1, 0xe3, 0xc2, 0xe5, 0xf2, 0x3a, 0x6b, 0xa0, 0xab, 0x90, 0xf4, 0xff]
        );
        assert_eq!(
            huffman_encode(b"no-cache"),
            [0xa8, 0xeb, 0x10, 0x64, 0x9c, 0xbf]
        );
        assert_eq!(
            huffman_encode(b"custom-key"),
            [0x25, 0xa8, 0x49, 0xe9, 0x5b, 0xa9, 0x7d, 0x7f]
        );
    }

    #[test]
    fn huffman_roundtrip_all_symbols() {
        for s in 0u32..257 {
            let byte = (s % 256) as u8;
            let enc = huffman_encode(&[byte]);
            let dec = huffman_decode(&enc).unwrap();
            if s < 256 {
                assert_eq!(dec, vec![byte]);
            }
        }
        // arbitrary binary roundtrip
        let data: Vec<u8> = (0..=255u8).cycle().take(1000).collect();
        assert_eq!(huffman_decode(&huffman_encode(&data)).unwrap(), data);
    }

    #[test]
    fn static_table_shape() {
        assert_eq!(STATIC_TABLE.len(), 61);
        assert_eq!(STATIC_TABLE[0], (":authority", ""));
        assert_eq!(STATIC_TABLE[1], (":method", "GET"));
        assert_eq!(STATIC_TABLE[6], (":scheme", "https"));
    }

    #[test]
    fn rfc_c62_literal_without_indexing() {
        // :path /sample/path, custom-key: custom-value (literal, no indexing)
        let mut enc = Encoder::new(4096);
        let block = enc.encode(&pairs(&[
            (":path", "/sample/path"),
            ("custom-key", "custom-value"),
        ]));
        let mut dec = Decoder::new(4096);
        let out = dec.decode(&block).unwrap();
        assert_eq!(
            out,
            pairs(&[(":path", "/sample/path"), ("custom-key", "custom-value")])
        );
    }

    #[test]
    fn dynamic_table_eviction() {
        let mut t = DynamicTable::new(200);
        t.insert("a".into(), "x".into()); // 35
        t.insert("b".into(), "yy".into()); // 36
        assert_eq!(t.size(), 71);
        t.insert("c".into(), &"z".repeat(200)); // > max → evicts everything, itself not stored
        assert_eq!(t.size(), 0);
        assert_eq!(t.len(), 0);
    }

    #[test]
    fn rfc_c64_indexed_field() {
        let mut dec = Decoder::new(4096);
        let out = dec.decode(&[0x82, 0x86]).unwrap();
        assert_eq!(out, pairs(&[(":method", "GET"), (":scheme", "http")]));
    }
}
