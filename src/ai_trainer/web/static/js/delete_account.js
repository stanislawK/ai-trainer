// Wires the Settings page's danger zone (G7, ADR-0019, ticket #17). Opening or cancelling
// the delete-account modal deletes nothing: the confirm button stays disabled until the
// athlete types the exact confirmation phrase, and only clicking it (via htmx) ever posts.
(function () {
  var CONFIRMATION_PHRASE = "DELETE";

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-delete-account-trigger]")) {
      return;
    }
    var modal = document.getElementById("delete-account-dialog");
    if (!modal || typeof modal.showModal !== "function") {
      return;
    }
    var input = modal.querySelector("[data-delete-account-input]");
    var confirmButton = modal.querySelector("[data-delete-account-confirm]");
    if (input) {
      input.value = "";
    }
    if (confirmButton) {
      confirmButton.disabled = true;
    }
    modal.showModal();
  });

  document.addEventListener("input", function (event) {
    var input = event.target.closest("[data-delete-account-input]");
    if (!input) {
      return;
    }
    var modal = input.closest("dialog");
    var confirmButton = modal && modal.querySelector("[data-delete-account-confirm]");
    if (confirmButton) {
      confirmButton.disabled = input.value !== CONFIRMATION_PHRASE;
    }
  });
})();
