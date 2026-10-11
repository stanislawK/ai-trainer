// The chat composer (ticket #78): Send stays disabled while the message is blank, Enter sends
// and Shift+Enter breaks the line, and the latest message is kept in view. While a reply
// streams the composer swaps Send for Stop (ticket #116), and Enter then sends nothing. Delegated on the
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

  // Stop's answer swaps the streaming row out. Cancelling the reader just before that swap
  // closes the connection, so the server sees the reader leave and cancels the call (ADR-0018).
  // Removing the row with the stream still open would abort the fetch, which htmx logs as an
  // error (#116). A Stop request that fails (no response, or an error status whose swap is cancelled) leaves the row and its stream alone, so Stop can be clicked again.
  var streams = new Map();

  document.addEventListener("htmx:sse:after:connection", function (event) {
    streams.set(event.target, event.detail.connection);
  });

  document.addEventListener("htmx:sse:close", function (event) {
    streams.delete(event.target);
  });

  document.addEventListener("htmx:before:swap", function (event) {
    var ctx = event.detail.ctx;
    var stop = ctx && ctx.sourceElement && ctx.sourceElement.closest("[data-chat-stop]");
    if (!stop) {
      return;
    }
    if (ctx.response.status >= 400) {
      event.preventDefault();
      return;
    }
    var connection = streams.get(ctx.target);
    if (connection && connection.reader) {
      connection.reader.cancel();
    }
  });

  document.addEventListener("input", function (event) {
    if (event.target.matches("[data-chat-input]")) {
      sync(event.target);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.target.matches("[data-chat-input]") && event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      // While a reply streams the composer shows Stop, not Send, so Enter sends nothing (#116).
      if (event.target.value.trim() !== "" && sendButtonFor(event.target)) {
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
