// Enter sends an onboarding answer and Shift+Enter starts a new line, as in the chat composer
// (F20, ticket #254). Listens on `document`, so it survives htmx swapping the card.
(function () {
  document.addEventListener("keydown", function (event) {
    var field = event.target;
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) {
      return;
    }
    if (!field.matches || !field.matches("[data-onboarding-composer] textarea")) {
      return;
    }
    event.preventDefault();
    field.form.requestSubmit();
  });
})();
