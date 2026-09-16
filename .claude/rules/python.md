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
- **SOLID:** small classes with one job; extend through new implementations or plugins, not growing conditionals.
- **Library APIs:** check context7 before using one.
