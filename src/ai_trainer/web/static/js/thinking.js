// The chat's thinking indicator (ticket #83, F14): after about 8 s without a first token it
// reveals an elapsed-time hint. Until the server reports a phase, its word cycles every 2.5 s
// through the template's `data-thinking-words`, with a soft vertical bump that reduced motion
// leaves out (ticket #266, Loading v4); the phase swap removes the word, which ends the cycling.
// The words live in the template; this only counts seconds and picks the next one. Scans on load
// and after every htmx swap, so it also covers a reply placeholder that arrives with a sent
// message or a page reload.
(function () {
  var HINT_AFTER_MS = 8000;
  var WORD_EVERY_MS = 2500;
  var BUMP_OUT_MS = 180;
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var timer = null;
  var cycler = null;

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

  function show(word, text) {
    word.textContent = text;
    word.dataset.bump = "pre";
    // Two frames so the "pre" position paints before the word slides in from it.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        delete word.dataset.bump;
      });
    });
  }

  function cycle() {
    var live = document.querySelectorAll("[data-thinking-words] [data-thinking-word]");
    live.forEach(function (word) {
      var words = JSON.parse(word.parentElement.dataset.thinkingWords);
      var next = words[(words.indexOf(word.textContent) + 1) % words.length];
      if (reducedMotion.matches) {
        word.textContent = next;
        return;
      }
      word.dataset.bump = "out";
      setTimeout(function () {
        show(word, next);
      }, BUMP_OUT_MS);
    });
    if (live.length === 0) {
      clearInterval(cycler);
      cycler = null;
    }
  }

  function scan() {
    if (indicators().length > 0 && timer === null) {
      tick();
      timer = setInterval(tick, 1000);
    }
    if (document.querySelector("[data-thinking-word]") && cycler === null) {
      cycler = setInterval(cycle, WORD_EVERY_MS);
    }
  }

  document.addEventListener("htmx:after:swap", scan);
  scan();
})();
