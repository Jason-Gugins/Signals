use pyo3::prelude::*;

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

#[pymodule]
fn signals_antibot(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(engine_version, m)?)?;
    m.add_function(wrap_pyfunction!(tls_library_version, m)?)?;
    Ok(())
}
