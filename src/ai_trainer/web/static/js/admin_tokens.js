// The admin's tokens page (ADR-0036, ticket #455). It opens the revoke dialogs, opens the dialog
// that shows a new token once, copies the token, and removes that dialog, with the token in it,
// when it closes. The server never sends the token again.
(function () {
  function showCreated() {
    var dialog = document.querySelector("#tokens [data-token-created]");
    if (dialog && !dialog.open && typeof dialog.showModal === "function") {
      dialog.showModal();
      var value = dialog.querySelector("[data-token-value]");
      if (value) {
        value.select();
      }
    }
  }

  function copied(button) {
    var idle = button.querySelector("[data-token-copy-idle]");
    var done = button.querySelector("[data-token-copy-done]");
    if (idle && done) {
      idle.hidden = true;
      done.hidden = false;
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element) || !target.closest("#tokens")) {
      return;
    }
    var copy = target.closest("[data-token-copy]");
    if (copy) {
      var value = copy.closest("dialog").querySelector("[data-token-value]");
      if (value && navigator.clipboard) {
        navigator.clipboard.writeText(value.value).then(function () {
          copied(copy);
        });
      } else if (value) {
        value.select();
      }
      return;
    }
    var opener = target.closest("[data-token-revoke]");
    var dialog = opener && document.getElementById(opener.dataset.tokenRevoke);
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });

  // Closing the created dialog (Done or Escape) drops it, so the token leaves the page.
  document.addEventListener(
    "close",
    function (event) {
      var dialog = event.target;
      if (dialog instanceof HTMLDialogElement && dialog.matches("[data-token-created]")) {
        dialog.remove();
      }
    },
    true
  );

  document.addEventListener("htmx:after:swap", showCreated);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showCreated);
  } else {
    showCreated();
  }
})();
