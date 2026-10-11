"""Fixtures shared by the pytest-playwright specs (ADR-0013): a signed-in page reached
through `scripts/dev_session.py`'s printed cookie, at the mobile viewport G11 requires by
default. These specs need the running compose stack — `make up` (or `make up-build`), then
`make e2e` — and are excluded from a bare `uv run pytest` (`testpaths` in `pyproject.toml`).
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import BrowserContext, Page

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEV_SESSION_SCRIPT = REPO_ROOT / "scripts" / "dev_session.py"
BASE_URL = "http://localhost:8000"


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Every spec runs at the 390x844 mobile viewport by default (PRD-0003 G11)."""
    return {**browser_context_args, "viewport": {"width": 390, "height": 844}}


def _seed_dev_session(*args: str) -> SetCookieParam:
    """Runs `scripts/dev_session.py` against the compose stack's Postgres and returns the
    cookie payload it prints, ready for `BrowserContext.add_cookies`."""
    result = subprocess.run(
        [sys.executable, str(DEV_SESSION_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    cookie: SetCookieParam = json.loads(result.stdout)
    return cookie


@pytest.fixture
def dev_session_cookie() -> SetCookieParam:
    return _seed_dev_session()


@pytest.fixture
def not_onboarded_cookie() -> SetCookieParam:
    """The fresh dev user, reset on every run to no sports, goals or `onboarded_at`, so a
    spec always starts at the sports step."""
    return _seed_dev_session("--not-onboarded")


@pytest.fixture
def signed_in_page(context: BrowserContext, dev_session_cookie: SetCookieParam) -> Iterator[Page]:
    """A page whose context already carries a valid `session_id` cookie, reached only
    through `dev_session.py` (ADR-0013) — never a real Google sign-in."""
    context.add_cookies([dev_session_cookie])
    page = context.new_page()
    yield page
    page.close()


@pytest.fixture
def not_onboarded_page(
    context: BrowserContext, not_onboarded_cookie: SetCookieParam
) -> Iterator[Page]:
    """A page signed in as a brand-new account that still has to go through onboarding."""
    context.add_cookies([not_onboarded_cookie])
    page = context.new_page()
    yield page
    page.close()
