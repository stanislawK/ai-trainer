// Reloads the current page when its trigger is clicked (ADR-0019, ticket #44) — used by the
// designed 403 and 500 pages' "Reload"/"Try again" actions. A plain link can't do this: the
// page the user needs refreshed (to pick up a fresh CSRF token, or just retry) is the one
// already on screen, which may have been swapped in by htmx with no URL of its own to link to.
// Delegated on `document`, like `theme.js`, so it still works after an htmx partial swap.
(function () {
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-reload-page]")) {
      window.location.reload();
    }
  });
})();
