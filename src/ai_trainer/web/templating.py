"""Jinja2 environment wiring (ADR-0012). Undefined variables fail loudly, never render empty."""

from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from ai_trainer.web.icons import render_icon
from ai_trainer.web.nav import visible_sections

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _csrf_context_processor(request: Request) -> dict[str, Any]:
    """Injects `csrf_token` into every `TemplateResponse` (ADR-0005 invariant 3, ticket #15),
    so no route has to remember to thread it through by hand. `CsrfMiddleware` sets
    `request.state.csrf_token`; a request that reaches a template without it (a test app that
    doesn't wire that middleware) gets `None`, same as no session."""
    return {"csrf_token": getattr(request.state, "csrf_token", None)}


def _nav_context_processor(request: Request) -> dict[str, Any]:
    """Injects the signed-in shell's nav state into every `TemplateResponse` (ADR-0019,
    ticket #40), the same way `_csrf_context_processor` injects `csrf_token`. A request whose
    gate middleware never ran (a test app that wires only its own router) still renders, with
    no sections beyond the always-visible ones and no user."""
    is_admin = bool(getattr(request.state, "is_admin", False))
    return {
        "nav_sections": visible_sections(is_admin=is_admin),
        "is_admin": is_admin,
        "current_user": getattr(request.state, "user", None),
        "current_path": request.url.path,
    }


def _format_date(value: datetime) -> str:
    """Renders `2 Sep 2025`-style dates (the Admin users mock, ticket #16); admin timestamps
    aren't athlete-facing, so unlike ADR-0014 they need no timezone conversion."""
    return f"{value.day} {value.strftime('%b %Y')}"


def build_templates() -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
        undefined=jinja2.StrictUndefined,
    )
    env.globals["render_icon"] = render_icon
    env.filters["dateformat"] = _format_date
    return Jinja2Templates(
        env=env, context_processors=[_csrf_context_processor, _nav_context_processor]
    )
