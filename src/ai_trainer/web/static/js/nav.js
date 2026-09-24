// Syncs `aria-expanded` on the chat page's sections trigger with the native Popover API's
// open state (ADR-0019, ticket #40). Opening, closing, light-dismiss and Escape-to-close all
// come from the browser for free via `popover`/`popovertarget`; this only keeps the button's
// accessible state honest.
(function () {
  document.querySelectorAll("[data-sections-trigger]").forEach(function (trigger) {
    var targetId = trigger.getAttribute("popovertarget");
    var popover = targetId && document.getElementById(targetId);
    if (!popover) {
      return;
    }
    popover.addEventListener("toggle", function (event) {
      trigger.setAttribute("aria-expanded", String(event.newState === "open"));
    });
  });
})();
