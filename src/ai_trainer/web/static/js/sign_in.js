// Shows the Google button's "Opening Google…" state while the `/auth/login` redirect is
// pending (ADR-0019, ticket #59). The button stays a plain link, so sign-in still works
// without JS; this only swaps its contents and ignores repeat clicks. Delegated on
// `document`, like `reload.js`, so it still works after an htmx partial swap.
(function () {
  var BUTTON = "[data-google-sign-in]";

  function setBusy(button, busy) {
    button.setAttribute("aria-busy", String(busy));
    button.setAttribute("aria-disabled", String(busy));
    button.classList.toggle("pointer-events-none", busy);
    button.querySelector("[data-idle]").hidden = busy;
    button.querySelector("[data-busy]").hidden = !busy;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(BUTTON);
    if (!button) return;
    // A modifier-click opens a new tab; this page isn't leaving, so it isn't busy.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
      return;
    }
    if (button.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }
    setBusy(button, true);
  });

  // Coming back from Google with the Back button restores this page from the bfcache with
  // the spinner still showing; reset it so the button works again.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll(BUTTON).forEach(function (button) {
      setBusy(button, false);
    });
  });
})();
