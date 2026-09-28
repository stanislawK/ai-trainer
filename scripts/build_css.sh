#!/usr/bin/env bash
# Compiles Tailwind CSS v4 + daisyUI 5 with the standalone CLI — no Node, ever (ADR-0012).
# Used by the Dockerfile's css-builder stage and by `make css` for local dev.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/css_versions.sh"
source "$SCRIPT_DIR/css_tools.sh"

WEB_DIR="src/ai_trainer/web"
CSS_DIR="$WEB_DIR/static/css"
TOOLS_DIR="$(mktemp -d)"

# daisyui.js and daisyui-theme.js are build-time Tailwind plugins, not runtime assets — they
# must sit next to input.css for the `@plugin "./daisyui.js"` and `@plugin "./daisyui-theme.js"`
# directives (theme.css, ADR-0019) to find them, so they land in the tracked (though
# gitignored) source tree rather than $TOOLS_DIR.
# The cleanup trap is registered *before* the download — not after — so a failed
# or partial fetch (network drop, bad version pin) can never leave it behind for
# a later `docker build`'s `COPY . /app` to sweep into the final image.
trap 'rm -rf "$TOOLS_DIR" "$CSS_DIR/daisyui.js" "$CSS_DIR/daisyui-theme.js"' EXIT
fetch_css_tools "$TOOLS_DIR" "$CSS_DIR"

"$TOOLS_DIR/tailwindcss" -i "$CSS_DIR/input.css" -o "$CSS_DIR/app.css" --minify
