#!/usr/bin/env bash
# Compiles Tailwind CSS v4 + daisyUI 5 with the standalone CLI — no Node, ever (ADR-0012).
# Used by the Dockerfile's css-builder stage and by `make css` for local dev.
set -euo pipefail

TAILWIND_VERSION="${TAILWIND_VERSION:-4.3.3}"
DAISYUI_VERSION="${DAISYUI_VERSION:-5.7.43}"
WEB_DIR="src/ai_trainer/web"
CSS_DIR="$WEB_DIR/static/css"
TOOLS_DIR="$(mktemp -d)"
trap 'rm -rf "$TOOLS_DIR"' EXIT

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$os-$arch" in
  linux-x86_64) tw_platform=linux-x64 ;;
  linux-aarch64) tw_platform=linux-arm64 ;;
  darwin-arm64) tw_platform=macos-arm64 ;;
  darwin-x86_64) tw_platform=macos-x64 ;;
  *)
    echo "build_css.sh: unsupported platform $os-$arch" >&2
    exit 1
    ;;
esac

# --retry: GitHub Releases occasionally answers a fresh runner's first request with a
# transient 4xx/5xx (found at #37, once CI started actually building this image); a few
# quick retries clear it without masking a real, persistent failure (bad version pin, DNS).
curl -sLfo "$TOOLS_DIR/tailwindcss" --retry 3 --retry-delay 2 --retry-connrefused \
  "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${tw_platform}"
chmod +x "$TOOLS_DIR/tailwindcss"

# daisyui.js and daisyui-theme.js are build-time Tailwind plugins, not runtime assets — they
# must sit next to input.css for the `@plugin "./daisyui.js"` and `@plugin "./daisyui-theme.js"`
# directives (theme.css, ADR-0019) to find them, so they land in the tracked (though
# gitignored) source tree rather than $TOOLS_DIR.
# The cleanup trap is registered *before* the download — not after — so a failed
# or partial fetch (network drop, bad version pin) can never leave it behind for
# a later `docker build`'s `COPY . /app` to sweep into the final image.
trap 'rm -rf "$TOOLS_DIR" "$CSS_DIR/daisyui.js" "$CSS_DIR/daisyui-theme.js"' EXIT
curl -sLfo "$CSS_DIR/daisyui.js" --retry 3 --retry-delay 2 --retry-connrefused \
  "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui.js"
curl -sLfo "$CSS_DIR/daisyui-theme.js" --retry 3 --retry-delay 2 --retry-connrefused \
  "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui-theme.js"

"$TOOLS_DIR/tailwindcss" -i "$CSS_DIR/input.css" -o "$CSS_DIR/app.css" --minify
