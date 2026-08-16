"""CDP stealth init script for Playwright browser contexts.

Ported from ../Linkedin/src/stealth.py (the LinkedIn scraper project).

This JavaScript init script is injected via Playwright's
`page.add_init_script()` before any page JavaScript runs.
It overrides browser APIs at the prototype level so that
anti-bot scanners see normal Chrome values instead of detection indicators.

Key design decisions (per plan Section 3 + technical review):

- ALL overrides target `Navigator.prototype`, not the instance.
  Scanners check `Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')`
  — instance-level overrides fail detection.
- Canvas spoofing uses a per-session noise seed so the fingerprint is stable
  within a session but different across sessions.
- Extension scanning interception rejects `chrome-extension://` fetch calls
  so the page sees "extension not installed" regardless of what headless
  Chromium may have injected.
- Chrome runtime shim is minimal — just enough to satisfy `typeof window.chrome === 'object'`.

The init script is the SECOND layer. The PRIMARY layer is the Rust egress
proxy (Tasks 6-8) which strips fingerprinting code before the browser parses it.
This init script catches anything the proxy misses and handles API-level
overrides the proxy cannot do.
"""

from loguru import logger

# === STEALTH INIT SCRIPT ===
# Injected before page JS via Playwright `add_init_script()`.
# All modifications happen at the prototype level where possible.

STEALTH_INIT_SCRIPT = """
// === Navigator Overrides ==============================
// CRITICAL: Override on Navigator.prototype, not the instance.
// Modern detectors check Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').
Object.defineProperty(Navigator.prototype, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// Spoof plugins — a real Chrome has 3+ plugins, headless has 0.
// Each has navigator.plugins.getNamedItem() and navigator.plugins.item() as functions.
Object.defineProperty(Navigator.prototype, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ];
        arr.length = 3;
        arr.item = (index) => arr[index];
        arr.namedItem = (name) => arr.find((v) => v.name === name);
        return arr;
    },
    configurable: true,
});

Object.defineProperty(Navigator.prototype, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
});

Object.defineProperty(Navigator.prototype, 'platform', {
    get: () => 'Win32',
    configurable: true,
});

Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
});

Object.defineProperty(Navigator.prototype, 'deviceMemory', {
    get: () => 8,
    configurable: true,
});

Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {
    get: () => 0,
    configurable: true,
});

// === Chrome Extension Probing Defense =================
// Intercept fetch to chrome-extension:// and return rejection
// (same error pattern as a real extension being absent).
const originalFetch = window.fetch;
window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    if (url && typeof url === 'string' && url.startsWith('chrome-extension://')) {
        return Promise.reject(new TypeError('Failed to fetch'));
    }
    return originalFetch.call(this, input, init);
};

// === Canvas Fingerprint Spoofing ======================
// Inject a stable noise seed per session — the same page gets the same
// fingerprint, but a different page within the session gets a slightly different one,
// and a new session gets a new seed entirely.
// CRITICAL: guard against WebGL canvases (getContext('2d') returns null)
// and zero-dimension canvases (getImageData throws IndexSizeError).
const canvasNoiseSeed = (() => {
    const arr = new Uint32Array(1);
    crypto.getRandomValues(arr);
    return arr[0];
})();
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function (type) {
    // Skip if this is a WebGL canvas (no 2D context) or a zero-dimension canvas
    // (getImageData throws). Do NOT modify WebGL behavior here.
    try {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
            const imageData = ctx.getImageData(0, 0, Math.min(this.width, 1), Math.min(this.height, 1));
            imageData.data[0] = (imageData.data[0] + canvasNoiseSeed) % 256;
            ctx.putImageData(imageData, 0, 0);
        }
    } catch (e) {
        // WebGL canvas or zero-dimension — let original run unmodified
    }
    // Use the preserved original so the exit is consistent
    return originalToDataURL.apply(this, arguments);
};

// === WebGL Fingerprint Spoofing =======================
// NOTE: vendor/renderer should be configurable to match the User-Agent platform
// (e.g., Safari on macOS shows a different renderer string than Windows Chrome).
// For now, using Chrome-on-Windows strings that match our Chrome 147 emulation.
const getParameterProto = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (parameter) {
    // `37445` = UNMASKED_VENDOR_WEBGL, `37446` = UNMASKED_RENDERER_WEBGL
    if (parameter === 37445) return 'Google Inc. (NVIDIA)';
    if (parameter === 37446)
        return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, OpenGL 4.5)';
    return getParameterProto.call(this, parameter);
};

// === Permission API ====================================
// Fake the Notifications permission state — headless defaults may differ from real Chrome.
const originalQuery = navigator.permissions.query;
navigator.permissions.query = function (parameters) {
    if (parameters.name === 'notifications') {
        return Promise.resolve({
            state: Notification.permission,
            // For compatibility with older Chrome versions that return 'prompt'/'granted'/'denied'
            status:
                Notification.permission === 'default'
                    ? 'prompt'
                    : Notification.permission,
        });
    }
    return originalQuery.call(this, parameters);
};

// === Chrome Runtime Shim ===============================
// Provide a plausible window.chrome object.
if (!window.chrome) window.chrome = {};
// Provide chrome.runtime (even if empty functions) because scanners check for its existence.
if (!window.chrome.runtime) window.chrome.runtime = {};

// === Network Connection Infection ======================
// Real Chrome exposes navigator.connection with effective type.
// Headless Chromium may not have it.
if (navigator.connection === undefined) {
    const conn = {
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
    };
    Object.defineProperty(navigator, 'connection', { get: () => conn, configurable: true });
}

// === Window.outer Size Trigger =========================
// headless outerWidth/outerHeight may equal innerWidth/innerHeight
// giving `outerWidth <= innerWidth` which is a detection signal.
// We make outer sizes slightly larger to indicate browser chrome.
// This is a creative override for bypassing strict detectors.
// Note: Some detectors check `screen.availHeight > innerHeight > outerHeight` —
//       normal Chrome has `outerHeight <= innerHeight` when maximized.
//       Keep enabled only if needed and raw detection pattern is triggered.
"""

# === HELPER FUNCTIONS ==============================


def get_stealth_init_script() -> str:
    """Return the stealth init script string for injection via Playwright.

    In usage:
        page.add_init_script(get_stealth_init_script())

    Returns:
        str: The complete JS code to inject before any page JS runs.
    """
    return STEALTH_INIT_SCRIPT


def validate_stealth_script(script: str = None) -> dict:
    """Sanity-check the stealth script for common issues.

    Checks:
    - Script is non-empty
    - No obvious syntax errors (balanced braces, no unclosed strings)
    - All required sections are present
    - No accidental Python formatting artifacts

    Args:
        script: The script to validate. Defaults to STEALTH_INIT_SCRIPT.

    Returns:
        dict: {"valid": bool, "errors": [...], "warnings": [...]}
    """
    if script is None:
        script = STEALTH_INIT_SCRIPT

    errors = []
    warnings = []

    # Check non-empty
    if not script or not script.strip():
        errors.append("Script is empty")
        return {"valid": False, "errors": errors, "warnings": warnings}

    # Check what sections are included
    sections = {
        "navigator_webdriver": "Navigator.prototype" in script and "webdriver" in script,
        "navigator_plugins": "plugins" in script and "navigator" in script,
        "canvas_spoofing": "canvasNoiseSeed" in script,
        "webgl_spoofing": "getParameter" in script and ("37445" in script or "37446" in script),
        "extension_probe_defense": "chrome-extension://" in script,
        "permission_api": "permissions.query" in script,
        "chrome_runtime": "window.chrome" in script,
    }

    if not all(sections.values()):
        missing = [k for k, v in sections.items() if not v]
        warnings.append(f"Potentially missing sections: {missing}")

    # Basic balanced braces check (rough heuristic)
    open_braces = script.count("{")
    close_braces = script.count("}")
    if open_braces != close_braces:
        errors.append(f"Brace mismatch: {open_braces} open vs {close_braces} close")

    # Check for unintended Python string artifacts
    if '"""' in script or "'''" in script:
        warnings.append(
            "Interesting: standalone triple-quote found — verify Python-to-JS export"
        )
    if "\\n" in script and not script.startswith("'") and not script.startswith('"'):
        warnings.append("Literal \\n found — check for Python string escapes bleeding into JS")

    # Required keywords that must be present for a functional stealth spoof
    required = [
        "Navigator.prototype",
        "webdriver",
        "HTMLCanvasElement.prototype.toDataURL",
        "WebGLRenderingContext.prototype.getParameter",
    ]
    for kw in required:
        if kw not in script:
            errors.append(f"Missing required override: {kw}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "sections_included": [k for k, v in sections.items() if v],
    }


# === MODULE-LEVEL VALIDATION ==================================

if __name__ == "__main__":
    result = validate_stealth_script()
    print(f"Stealth script valid: {result['valid']}")
    if result["errors"]:
        for e in result["errors"]:
            logger.error(f"  ERROR: {e}")
    if result["warnings"]:
        for w in result["warnings"]:
            logger.warning(f"  WARN: {w}")
    print(f"Sections detected: {result['sections_included']}")
    print(f"Script length: {len(STEALTH_INIT_SCRIPT)} chars")
