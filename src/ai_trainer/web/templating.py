"""Jinja2 environment wiring (ADR-0012). Undefined variables fail loudly, never render empty."""

from pathlib import Path
from typing import Any

import jinja2
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from ai_trainer.web.icons import render_icon

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _csrf_context_processor(request: Request) -> dict[str, Any]:
    """Injects `csrf_token` into every `TemplateResponse` (ADR-0005 invariant 3, ticket #15),
    so no route has to remember to thread it through by hand. `CsrfMiddleware` sets
    `request.state.csrf_token`; a request that reaches a template without it (a test app that
    doesn't wire that middleware) gets `None`, same as no session."""
    return {"csrf_token": getattr(request.state, "csrf_token", None)}


def build_templates() -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
        undefined=jinja2.StrictUndefined,
    )
    env.globals["render_icon"] = render_icon
    return Jinja2Templates(env=env, context_processors=[_csrf_context_processor])
