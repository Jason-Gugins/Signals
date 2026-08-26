import inspect
from src.core.patchright_browser import PatchrightBrowserFetcher


def test_patchright_browser_fetcher_class_exists():
    assert PatchrightBrowserFetcher is not None

def test_patchright_browser_fetcher_has_fetch_method():
    assert hasattr(PatchrightBrowserFetcher, "fetch")

def test_patchright_browser_fetcher_has_start_and_close():
    assert hasattr(PatchrightBrowserFetcher, "start")
    assert hasattr(PatchrightBrowserFetcher, "close")

def test_patchright_browser_fetcher_fetch_signature_matches_browser():
    """fetch() should accept the same kwargs as BrowserFetcher.fetch."""
    sig = inspect.signature(PatchrightBrowserFetcher.fetch)
    expected_params = {"self", "url", "source", "domain", "wait_ms", "capture_html", "scroll"}
    actual_params = set(sig.parameters.keys())
    assert expected_params.issubset(actual_params)

def test_patchright_browser_fetcher_has_warmup_params():
    """fetch() should accept warmup_url and warmup_ms for behavioral warm-up."""
    sig = inspect.signature(PatchrightBrowserFetcher.fetch)
    assert "warmup_url" in sig.parameters
    assert "warmup_ms" in sig.parameters
