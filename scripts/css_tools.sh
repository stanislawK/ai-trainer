# Shared by build_css.sh and watch_css.sh: downloads the pinned Tailwind CLI binary and the
# daisyUI plugin bundles (ADR-0012, ADR-0019). Sourced only, needs $TAILWIND_VERSION and
# $DAISYUI_VERSION (css_versions.sh) already set.

fetch_css_tools() {
  local tools_dir="$1" css_dir="$2"

  local os arch tw_platform
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$os-$arch" in
    linux-x86_64) tw_platform=linux-x64 ;;
    linux-aarch64) tw_platform=linux-arm64 ;;
    darwin-arm64) tw_platform=macos-arm64 ;;
    darwin-x86_64) tw_platform=macos-x64 ;;
    *)
      echo "css_tools.sh: unsupported platform $os-$arch" >&2
      exit 1
      ;;
  esac

  # --retry: GitHub Releases occasionally answers a fresh runner's first request with a
  # transient 4xx/5xx (found at #37); a few quick retries clear it without masking a real,
  # persistent failure (bad version pin, DNS).
  curl -sLfo "$tools_dir/tailwindcss" --retry 3 --retry-delay 2 --retry-connrefused \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${tw_platform}"
  chmod +x "$tools_dir/tailwindcss"

  curl -sLfo "$css_dir/daisyui.js" --retry 3 --retry-delay 2 --retry-connrefused \
    "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui.js"
  curl -sLfo "$css_dir/daisyui-theme.js" --retry 3 --retry-delay 2 --retry-connrefused \
    "https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui-theme.js"
}
