from playwright.sync_api import Page, Response

from tests.e2e.conftest import BASE_URL

_TRACKED_PATHS = ("/static/css/app.css", "/static/vendor/htmx-4.0.0.min.js")


def test_app_css_and_htmx_load_with_200(signed_in_page: Page) -> None:
    statuses: dict[str, int] = {}

    def _record(response: Response) -> None:
        if response.url.endswith(_TRACKED_PATHS):
            statuses[response.url] = response.status

    signed_in_page.on("response", _record)
    signed_in_page.goto(f"{BASE_URL}/")
    signed_in_page.wait_for_load_state("networkidle")

    assert len(statuses) == len(_TRACKED_PATHS), (
        f"expected both {_TRACKED_PATHS} to be requested, got {statuses}"
    )
    assert all(status == 200 for status in statuses.values()), statuses
