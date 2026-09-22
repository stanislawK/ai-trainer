# ADR-0002: pin the uv binary, sync deps without the project, copy source, sync again.
FROM python:3.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"

USER nonroot

EXPOSE 8000

# The probe's own timeout must exceed the DB ping's connect_timeout inside
# /health (2s default), or a slow-to-fail DB can time out the probe itself
# before the route gets to report "unavailable" gracefully.
HEALTHCHECK --interval=5s --timeout=6s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"

CMD ["uvicorn", "ai_trainer.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
