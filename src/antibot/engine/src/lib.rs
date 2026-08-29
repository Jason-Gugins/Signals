use pyo3::prelude::*;

mod h2;
mod tls;

#[pyfunction]
fn engine_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

#[pyfunction]
fn tls_library_version() -> String {
    // boring-sys bindgen exposes BoringSSL's version text constant
    unsafe { std::ffi::CStr::from_ptr(boring_sys::OPENSSL_VERSION_TEXT.as_ptr() as *const _) }
        .to_string_lossy()
        .to_string()
}

/// Effective Chrome TLS posture of the engine as a JSON string
/// (grease, extension permutation, ECH-GREASE, ALPS, brotli cert-compression,
/// SCT, OCSP, ALPN list, ...). See tls.rs + BUILD_NOTES.md for API-gap notes.
#[pyfunction]
fn tls_config_json() -> String {
    tls::tls_config_json()
}

/// Core h2 GET over the Chrome TLS connection; returns the (decompressed)
/// response body bytes. `extra_headers` may carry e.g. cookies.
pub(crate) fn h2_fetch_bytes(
    url: &str,
    method: &str,
    extra_headers: &[(String, String)],
) -> Result<Vec<u8>, String> {
    if method != "GET" {
        return Err(format!("engine h2 fetch supports GET only (got {method})"));
    }
    let u = tls::parse_url(url)?;
    let tls = tls::connect_chrome_tls(&u.host)?;
    let mut client = h2::H2Client::new(tls);
    client.send_preface()?;
    let headers = h2::build_request_headers(&u.host, &u.path, h2::CHROME_USER_AGENT, extra_headers);
    let resp = client.get(&headers)?;
    decode_content_encoding(&resp)
}

/// Decompress the response body per content-encoding (Chrome advertises
/// gzip, deflate, br, zstd; the engine decodes gzip/deflate/br).
fn decode_content_encoding(resp: &h2::H2Response) -> Result<Vec<u8>, String> {
    let enc = resp
        .headers
        .iter()
        .find(|(n, _)| n == "content-encoding")
        .map(|(_, v)| v.to_ascii_lowercase());
    match enc.as_deref() {
        None | Some("") | Some("identity") => Ok(resp.body.clone()),
        Some("br") => {
            let mut out = Vec::new();
            brotli::BrotliDecompress(&mut std::io::Cursor::new(&resp.body), &mut out)
                .map_err(|e| format!("brotli decode: {e}"))?;
            Ok(out)
        }
        Some("gzip") | Some("deflate") => {
            let mut out = Vec::new();
            let mut d = flate2::read::MultiGzDecoder::new(std::io::Cursor::new(&resp.body));
            std::io::Read::read_to_end(&mut d, &mut out)
                .map_err(|e| format!("gzip decode: {e}"))?;
            Ok(out)
        }
        Some(other) => Err(format!("unsupported content-encoding: {other}")),
    }
}

/// Perform a real h2 GET of `url` with the Chrome-configured engine and
/// return JSON: {"status": int, "headers": [[name, value], ...], "body": str}.
/// The body is returned lossily decoded as UTF-8 (JSON/text endpoints).
#[pyfunction]
#[pyo3(signature = (url, headers=None))]
fn fetch_h2(url: &str, headers: Option<Vec<(String, String)>>) -> PyResult<String> {
    let extra = headers.unwrap_or_default();
    let (status, resp_headers, body) =
        fetch_h2_parts(url, &extra).map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e))?;
    let value = serde_json::json!({
        "status": status,
        "headers": resp_headers
            .iter()
            .map(|(n, v)| serde_json::json!([n, v]))
            .collect::<Vec<_>>(),
        "body": String::from_utf8_lossy(&body),
        "body_b64": base64_encode(&body),
        "body_len": body.len(),
    });
    Ok(value.to_string())
}

fn fetch_h2_parts(
    url: &str,
    extra: &[(String, String)],
) -> Result<(u16, Vec<(String, String)>, Vec<u8>), String> {
    let u = tls::parse_url(url)?;
    let tls = tls::connect_chrome_tls(&u.host)?;
    let mut client = h2::H2Client::new(tls);
    client.send_preface()?;
    let headers = h2::build_request_headers(&u.host, &u.path, h2::CHROME_USER_AGENT, extra);
    let resp = client.get(&headers)?;
    let body = decode_content_encoding(&resp)?;
    Ok((resp.status, resp.headers, body))
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

#[pyfunction]
fn probe_fingerprint(url: &str) -> PyResult<String> {
    match tls::probe_fingerprint(url) {
        Ok(v) => Ok(v.to_string()),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

// ---------------------------------------------------------------------------
// PyO3 test helpers — expose the h2 internals for Python-side unit tests
// ---------------------------------------------------------------------------

/// HPACK-encode an ordered header list (Chrome indexing policy, Huffman).
#[pyfunction]
fn hpack_encode(headers: Vec<(String, String)>) -> PyResult<Vec<u8>> {
    let mut enc = h2::hpack::Encoder::new(65_536);
    Ok(enc.encode(&headers))
}

/// HPACK-decode a header block (fresh 65,536-byte dynamic table).
#[pyfunction]
fn hpack_decode(block: Vec<u8>) -> PyResult<Vec<(String, String)>> {
    let mut dec = h2::hpack::Decoder::new(65_536);
    dec.decode(&block)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e))
}

/// Huffman-encode raw bytes (EOS padding) — test vector helper.
#[pyfunction]
fn huffman_encode(data: Vec<u8>) -> PyResult<Vec<u8>> {
    Ok(h2::hpack::huffman_encode(&data))
}

/// Huffman-decode data; raises ValueError on invalid input.
#[pyfunction]
fn huffman_decode(data: Vec<u8>) -> PyResult<Vec<u8>> {
    h2::hpack::huffman_decode(&data).map_err(|e| pyo3::exceptions::PyValueError::new_err(e))
}

/// SETTINGS frame (header + payload) for the given (id, value) pairs.
#[pyfunction]
fn build_settings_frame(settings: Vec<(u16, u32)>) -> PyResult<Vec<u8>> {
    Ok(h2::Frame {
        ftype: h2::frame_types::SETTINGS,
        flags: 0,
        stream_id: 0,
        payload: h2::frames::settings_payload(&settings),
    }
    .encode())
}

/// Chrome's exact connection preface bytes: magic + SETTINGS + WINDOW_UPDATE.
#[pyfunction]
fn chrome_h2_preface() -> PyResult<Vec<u8>> {
    Ok(h2::chrome_preface_bytes())
}

#[pymodule]
fn signals_antibot(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(engine_version, m)?)?;
    m.add_function(wrap_pyfunction!(tls_library_version, m)?)?;
    m.add_function(wrap_pyfunction!(tls_config_json, m)?)?;
    m.add_function(wrap_pyfunction!(probe_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(fetch_h2, m)?)?;
    m.add_function(wrap_pyfunction!(hpack_encode, m)?)?;
    m.add_function(wrap_pyfunction!(hpack_decode, m)?)?;
    m.add_function(wrap_pyfunction!(huffman_encode, m)?)?;
    m.add_function(wrap_pyfunction!(huffman_decode, m)?)?;
    m.add_function(wrap_pyfunction!(build_settings_frame, m)?)?;
    m.add_function(wrap_pyfunction!(chrome_h2_preface, m)?)?;
    Ok(())
}
