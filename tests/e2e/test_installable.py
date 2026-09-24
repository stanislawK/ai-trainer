from playwright.sync_api import Page

from tests.e2e.conftest import BASE_URL


def test_chrome_reports_the_signed_in_page_as_installable(signed_in_page: Page) -> None:
    """AC2 (ticket #42): the manifest, its icons and `start_url` satisfy Chrome's own
    installability check with no service worker (ADR-0019's explicit "no service worker"
    decision), read straight from CDP rather than inferred from any of our own assumptions."""
    signed_in_page.goto(f"{BASE_URL}/")
    signed_in_page.wait_for_load_state("networkidle")
    cdp = signed_in_page.context.new_cdp_session(signed_in_page)
    cdp.send("Page.enable")

    result = cdp.send("Page.getInstallabilityErrors")

    assert result["installabilityErrors"] == [], result["installabilityErrors"]
