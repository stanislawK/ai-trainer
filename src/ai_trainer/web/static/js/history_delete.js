// Opens the History session's delete dialog (B4, the History v4 mock's "delete A", ticket
// #518). Opening or cancelling it deletes nothing: only its Delete session button sends the
// DELETE, through htmx. One listener on the document, so it still works after htmx swaps the
// session in.
(function () {
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    var opener = target.closest("[data-history-delete]");
    if (!opener) {
      return;
    }
    var dialog = document.getElementById(opener.getAttribute("data-history-delete"));
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });
})();
