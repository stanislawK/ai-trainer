"""Self-service account deletion (G7, ADR-0005, ADR-0019, ticket #17), explicitly named as
e2e-worthy: it is the app's one irreversible, destructive action, and the typed-confirmation
gate that guards it is client-side JS with no other test coverage. The cascade and tenancy
guarantees themselves are proven at the integration layer
(`tests/integration/test_delete_account_route.py`); this spec only proves the real browser
interaction: cancelling deletes nothing, and confirming actually signs the athlete out."""

from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL


def test_cancelling_the_modal_deletes_nothing(signed_in_page: Page) -> None:
    signed_in_page.goto(f"{BASE_URL}/settings")
    signed_in_page.get_by_role("button", name="Delete account…").click()
    dialog = signed_in_page.locator("#delete-account-dialog")
    expect(dialog).to_be_visible()
    confirm_button = dialog.get_by_role("button", name="Delete account")
    expect(confirm_button).to_be_disabled()

    dialog.get_by_label("Type DELETE to confirm").fill("delete")
    expect(confirm_button).to_be_disabled()

    dialog.get_by_role("button", name="Cancel").click()

    expect(dialog).to_be_hidden()
    signed_in_page.reload()
    expect(signed_in_page.get_by_role("heading", name="Settings")).to_be_visible()


def test_typing_delete_and_confirming_deletes_the_account_and_signs_out(
    signed_in_page: Page,
) -> None:
    signed_in_page.goto(f"{BASE_URL}/settings")
    signed_in_page.get_by_role("button", name="Delete account…").click()
    dialog = signed_in_page.locator("#delete-account-dialog")
    confirm_button = dialog.get_by_role("button", name="Delete account")

    dialog.get_by_label("Type DELETE to confirm").fill("DELETE")
    expect(confirm_button).to_be_enabled()
    confirm_button.click()

    expect(signed_in_page).to_have_url(f"{BASE_URL}/")
    signed_in_page.goto(f"{BASE_URL}/settings")
    expect(signed_in_page.get_by_role("heading", name="Please sign in")).to_be_visible()
