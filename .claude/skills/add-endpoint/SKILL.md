---
name: add-endpoint
description: Checklist for adding or changing an HTTP route (HTMX page or partial, SSE stream, or JSON API) in the FastAPI app.
---

# Add an endpoint (ADR-0003, ADR-0005, ADR-0012)

1. The route lives in `src/ai_trainer/web/` and calls exactly one application use case; no business logic in the route.
2. Authentication is required by default, and the session dependency yields only an `active` user (ADR-0005). Take `user_id` from it, never from the request. An admin-only route derives admin rights from `ADMIN_EMAILS`, and its tests include a signed-in non-admin being refused.
3. Validate input and output with Pydantic models.
4. Non-GET requests are CSRF-checked.
5. HTML: a full page for normal requests, a partial for `HX-Request`. Streaming uses `fastapi.sse.EventSourceResponse`.
6. Tests (via `/run-tdd`):
   - a unit test for the use case with fake ports;
   - integration tests for the route: happy path, unauthenticated → refused, other user's data → not found, CSRF missing → refused, one validation error.
7. Regenerate the OpenAPI snapshot; never hand-edit it.
