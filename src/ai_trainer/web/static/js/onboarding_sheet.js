// Cycles the onboarding summary sheet's status lines (ticket #257, F21): every 1.8 s the shown
// line slides up and out, and the next one slides in from below, as in the mock. The words live
// in the template; this only moves `data-phase` (out, pre, in) and the `hidden` class, and the slide is
// Tailwind classes keyed on the phase, which reduced motion turns off. Stops once the summary
// card has replaced the sheet.
(function () {
  if (window.onboardingSheet) {
    return;
  }
  window.onboardingSheet = true;

  var EVERY_MS = 1800;
  var OUT_MS = 180;
  var shown = 0;
  var timer = null;

  function lines() {
    return document.querySelectorAll("[data-sheet-line]");
  }

  function step() {
    var all = lines();
    if (all.length === 0) {
      clearInterval(timer);
      timer = null;
      return;
    }
    var current = all[shown % all.length];
    current.dataset.phase = "out";
    setTimeout(function () {
      current.classList.add("hidden");
      delete current.dataset.phase;
      shown = (shown + 1) % all.length;
      var next = all[shown];
      next.dataset.phase = "pre";
      next.classList.remove("hidden");
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          next.dataset.phase = "in";
        });
      });
    }, OUT_MS);
  }

  function scan() {
    if (lines().length > 0 && timer === null) {
      shown = 0;
      timer = setInterval(step, EVERY_MS);
    }
  }

  document.addEventListener("htmx:after:swap", scan);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }
})();
