use pyo3::prelude::*;

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

/// Perform a real TLS fetch of `url` with the Chrome-configured context and
/// return (as JSON) whatever fingerprint data the endpoint echoes
/// (for tls.peet.ws/api/all: ja4, ja3_hash, akamai_h2, tls_extensions, ...).
#[pyfunction]
fn probe_fingerprint(url: &str) -> PyResult<String> {
    match tls::probe_fingerprint(url) {
        Ok(v) => Ok(v.to_string()),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(e)),
    }
}

#[pymodule]
fn signals_antibot(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(engine_version, m)?)?;
    m.add_function(wrap_pyfunction!(tls_library_version, m)?)?;
    m.add_function(wrap_pyfunction!(tls_config_json, m)?)?;
    m.add_function(wrap_pyfunction!(probe_fingerprint, m)?)?;
    Ok(())
}
