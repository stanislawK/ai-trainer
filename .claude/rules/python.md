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
- **User scoping (ADR-0004, ADR-0005):** every repository method that touches user data takes `user_id`, and `user_id` comes from the authenticated session. Every request resolves an `active` user; admin rights are derived from `ADMIN_EMAILS` per request, never read from the database.
- **Sports (ADR-0006):** no branching on sport names outside `domain/sports/<sport>/`.
- **Time (ADR-0014):** no `datetime.now()`, `date.today()` or `utcnow()` outside the `Clock` adapter. Stored timestamps are timezone-aware UTC; the athlete's local day is derived from `User.timezone` at the edge.
- **External sources (ADR-0016, ADR-0017):** a public data source is reached only through its port, from an adapter. Only a route, crag or place name leaves the app.
- **Telemetry (ADR-0018):** the tracer provider and OTLP exporter are built only in the composition root, from `Settings`, off by default. No telemetry vendor SDK in application code, and nothing that can raise into a route.
- **SOLID:** small classes with one job; extend through new implementations or plugins, not growing conditionals.
- **Library APIs:** check context7 before using one.
- **New dependencies:** before adding one to `pyproject.toml`, check it's actively maintained and has no known CVEs — an AI suggestion is not itself a vetting step. After `uv add`, audit the locked set: `uv export --format requirements-txt --no-hashes --all-groups --no-emit-project -o <scratch>/req.txt && uvx pip-audit -r <scratch>/req.txt --disable-pip --no-deps`.
