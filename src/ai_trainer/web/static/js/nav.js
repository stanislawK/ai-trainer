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

// The phone's conversation drawer (F17, ticket #213): a daisyUI drawer, whose hidden checkbox
// the list button and the sheet's close button flip. The overlay closes it on its own; this
// adds Escape, keeps `aria-expanded` honest, moves focus into the sheet and back, and closes
// it once a conversation or New conversation is picked, since htmx swaps the chat behind it.
(function () {
  var opener = document.querySelector("[data-drawer-open]");
  var toggle = opener && document.getElementById(opener.getAttribute("data-drawer-open"));
  if (!toggle) {
    return;
  }
  var sheet = document.getElementById(opener.getAttribute("aria-controls"));

  function setOpen(open) {
    if (toggle.checked !== open) {
      toggle.checked = open;
    }
    opener.setAttribute("aria-expanded", String(open));
    if (open) {
      var close = sheet && sheet.querySelector("[data-drawer-close]");
      if (close) {
        close.focus();
      }
    } else if (sheet && sheet.contains(document.activeElement)) {
      opener.focus();
    }
  }

  opener.addEventListener("click", function () {
    setOpen(true);
  });
  toggle.addEventListener("change", function () {
    setOpen(toggle.checked);
  });
  document.addEventListener("click", function (event) {
    if (!toggle.checked) {
      return;
    }
    if (event.target.closest("[data-drawer-close], #" + sheet.id + " [data-conversation-link]")) {
      setOpen(false);
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && toggle.checked) {
      setOpen(false);
    }
  });
})();
