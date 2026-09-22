# ADR-0012: compile Tailwind CSS v4 + daisyUI 5 with the standalone CLI, no Node
# anywhere. This stage is discarded — curl, the CLI binary and the daisyUI plugin
# bundle never reach the final image, only the compiled stylesheet does.
FROM python:3.14-slim-trixie AS css-builder
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts/build_css.sh scripts/build_css.sh
COPY src/ai_trainer/web/static/css/input.css src/ai_trainer/web/static/css/input.css
COPY src/ai_trainer/web/templates src/ai_trainer/web/templates
RUN ./scripts/build_css.sh

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
COPY --from=css-builder /app/src/ai_trainer/web/static/css/app.css \
    src/ai_trainer/web/static/css/app.css
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
