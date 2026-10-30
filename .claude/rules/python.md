---
paths:
  - "src/**/*.py"
---

# Python rules

- **Layering (ADR-0003):** `domain` imports only the standard library and Pydantic. `application` depends on `domain` and on `typing.Protocol` ports. Concrete classes are wired only in the composition root.
- **Typing (ADR-0002):** `mypy --strict` clean. No `Any` in public signatures. `# type: ignore[code]` only with a reason comment.
- **PEP 649 (ADR-0002):** never import a type used in a FastAPI or Pydantic runtime annotation only under `TYPE_CHECKING`.
- **Validation:** Pydantic v2 models at every boundary — HTTP, LLM output, MCP, settings.
- **Configuration:** only through the `Settings` class; never read `os.environ` directly.
- **User scoping (ADR-0004, ADR-0005):** every repository method that touches user data takes `user_id`, and `user_id` comes from the authenticated session. An ID from a URL or form (such as a `conversation_id`) is always looked up together with that `user_id`, and someone else's is a 404, never a 403 (ADR-0021). Every request resolves an `active` user; admin rights are derived from `ADMIN_EMAILS` per request, never read from the database.
- **Sports (ADR-0006, ADR-0032):** no branching on sport names outside `domain/sports/<sport>/`. A sport's tables are reached only through its `ActivityStore`, resolved by the registry.
- **Time (ADR-0014):** no `datetime.now()`, `date.today()` or `utcnow()` outside the `Clock` adapter. Stored timestamps are timezone-aware UTC; the athlete's local day is derived from `User.timezone` at the edge.
- **External sources (ADR-0016, ADR-0017):** a public data source is reached only through its port, from an adapter. Only a route, crag or place name leaves the app.
- **Device location (ADR-0027):** a position arrives only in a POST body, lives as a `GeoPoint` for one request, and is never stored, logged, traced, put in a prompt or decision state, or sent out. No geolocation code path runs unless `users.location_consent_at` is set.
- **Units and scales, Europe first (PRD 0008 G15):** normalizers and conversions cover metric units and the European grade scales completely, with tests. Imperial units and V/YDS are best effort: a value code can't place keeps its original string with no converted value, never a guess.
- **Gym grades (ADR-0027):** a `gym`-scale grade gets `french` and `ordinal` only from the athlete's mapping for that venue; a bare number is never read as Font, French or V.
- **Onboarding (ADR-0028):** an onboarding request never awaits a model call, except the summary stream. A background read is an `asyncio` task held in the app's task set, and writes its result only if the answer's `revision` still matches. Only the summary confirm writes what a read produced (the profile, `goals`), and it deletes the athlete's `onboarding_answers` in the same transaction. The sport pick, the athlete's own choice, sets `user_sports` and the browser's timezone at once; confirm writes them again.
- **Telemetry (ADR-0018):** the tracer provider and OTLP exporter are built only in the composition root, from `Settings`, off by default. No telemetry vendor SDK in application code, and nothing that can raise into a route. Tracing, metrics and the logging config are wired there too (ADR-0030).
- **Logs and labels (ADR-0030):** log through `logging.getLogger(__name__)`; the JSON formatter adds `trace_id`, `span_id`, `request_id` and the opaque `user_id`. Never log an email, a name, an athlete's message, a prompt or reply, a key, a position or a client IP. Spans and metric labels carry no user identity; metric labels are bounded sets (route template, template ID, model, outcome), never raw paths or IDs.
- **Request and model-call limits (ADR-0029):** the composition root wraps both model gateways in one in-process budget. A refused call is not sent and writes no `llm_calls` row. Sign-in routes are limited by `CF-Connecting-IP`, other requests by the session cookie. Do not add Redis for this, and do not read the wall clock to count the window.
- **SOLID:** small classes with one job; extend through new implementations or plugins, not growing conditionals.
- **Library APIs:** check context7 before using one.
- **New dependencies:** before adding one to `pyproject.toml`, check it's actively maintained and has no known CVEs — an AI suggestion is not itself a vetting step. After `uv add`, audit the locked set: `uv export --format requirements-txt --no-hashes --all-groups --no-emit-project -o <scratch>/req.txt && uvx pip-audit -r <scratch>/req.txt --disable-pip --no-deps`.
