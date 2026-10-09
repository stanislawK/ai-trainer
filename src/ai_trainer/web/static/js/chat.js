// The chat composer (ticket #78): Send stays disabled while the message is blank, Enter sends
// and Shift+Enter breaks the line, and the latest message is kept in view. Delegated on the
// document so it survives htmx swapping the composer.
(function () {
  function sendButtonFor(input) {
    var form = input.closest("[data-chat-form]");
    return form && form.querySelector("[data-chat-send]");
  }

  function sync(input) {
    var button = sendButtonFor(input);
    if (button) {
      button.disabled = input.value.trim() === "";
    }
  }

  function scrollToLatest() {
    window.scrollTo({ top: document.documentElement.scrollHeight });
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches("[data-chat-input]")) {
      sync(event.target);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.target.matches("[data-chat-input]") && event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (event.target.value.trim() !== "") {
        event.target.form.requestSubmit();
      }
    }
  });

  document.addEventListener("htmx:after:swap", function () {
    var input = document.querySelector("[data-chat-input]");
    if (input) {
      sync(input);
      input.focus();
    }
    scrollToLatest();
  });

  var input = document.querySelector("[data-chat-input]");
  if (input) {
    sync(input);
    scrollToLatest();
  }
})();
