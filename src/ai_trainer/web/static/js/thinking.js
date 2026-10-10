// The chat's thinking indicator (ticket #83, F14): after about 8 s without a first token it
// reveals an elapsed-time hint. The label and the hint's words live in the template; this only
// counts seconds. Scans on load and after every htmx swap, so it also covers a reply placeholder
// that arrives with a sent message or a page reload.
(function () {
  var HINT_AFTER_MS = 8000;
  var timer = null;

  function indicators() {
    return document.querySelectorAll("[data-thinking]");
  }

  function tick() {
    var now = Date.now();
    var live = indicators();
    live.forEach(function (indicator) {
      if (!indicator.dataset.thinkingSince) {
        indicator.dataset.thinkingSince = String(now);
      }
      var elapsed = now - Number(indicator.dataset.thinkingSince);
      var hint = indicator.querySelector("[data-thinking-hint]");
      if (!hint || elapsed < HINT_AFTER_MS) {
        return;
      }
      hint.hidden = false;
      var seconds = hint.querySelector("[data-thinking-elapsed]");
      if (seconds) {
        seconds.textContent = String(Math.floor(elapsed / 1000));
      }
    });
    if (live.length === 0) {
      clearInterval(timer);
      timer = null;
    }
  }

  function scan() {
    if (indicators().length > 0 && timer === null) {
      tick();
      timer = setInterval(tick, 1000);
    }
  }

  document.addEventListener("htmx:after:swap", scan);
  scan();
})();
