// Wires the Settings page's theme toggle and keeps <html data-theme>, localStorage["theme"]
// and <meta name="theme-color"> in sync (ADR-0019, ticket #41). Loaded on every page: the
// inline pre-paint script in base.html may already have applied a stored theme before this
// runs, and a toggle's buttons only exist on pages that render one (Settings) — this file
// works fine when it finds none.
(function () {
  var STORAGE_KEY = "theme";
  var THEMES = ["trainer-dark", "trainer-light"];

  function currentTheme() {
    var theme = document.documentElement.getAttribute("data-theme");
    return THEMES.indexOf(theme) === -1 ? "trainer-dark" : theme;
  }

  function syncMetaThemeColor() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      return;
    }
    var color = getComputedStyle(document.documentElement)
      .getPropertyValue("--color-base-100")
      .trim();
    if (color) {
      meta.setAttribute("content", color);
    }
  }

  function syncToggleButtons(theme) {
    document.querySelectorAll("[data-theme-option]").forEach(function (button) {
      var active = button.getAttribute("data-theme-option") === theme;
      button.setAttribute("aria-pressed", String(active));
      button.classList.toggle("tab-active", active);
      button.classList.toggle("shadow-sm", active);
      button.classList.toggle("text-base-content/70", !active);
    });
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      // Storage unavailable (blocked, private mode): the theme still applies to this page.
    }
    syncMetaThemeColor();
    syncToggleButtons(theme);
  }

  // Delegated on `document`, not bound per-button: this file is loaded once with `defer` and
  // never re-runs, so a direct binding would go stale if a toggle's buttons were ever swapped
  // in later (an htmx partial swap, ticket #41 skeptic finding) instead of arriving with the
  // initial page load.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-option]");
    if (button) {
      applyTheme(button.getAttribute("data-theme-option"));
    }
  });

  syncMetaThemeColor();
  syncToggleButtons(currentTheme());
})();
