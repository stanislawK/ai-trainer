// The exercise card's More (F23, ADR-0031, ticket #581; the Exercise Card mock, #580 option 2c).
// Whether a candidate's how-to fits its two-line clamp can only be measured at the row's real
// width, so this sets `data-more` on a `[data-candidate]` whose text overflows, which shows its
// chevron toggle and reserves room for it, and re-measures after every htmx swap and on resize.
// A tap on the toggle flips `data-open`, which lifts the clamp and turns the chevron, along with
// `aria-expanded` and the label. The toggle has no htmx attribute and sits outside the row button,
// so it never answers the question (ADR-0015). Styles are Tailwind variants in the template.
(function () {
  if (window.exerciseCardMore) {
    return;
  }
  window.exerciseCardMore = true;

  function measure(candidate) {
    var text = candidate.querySelector("[data-howto]");
    if (!text || candidate.hasAttribute("data-open")) {
      return;
    }
    // Measured at full width: a text that fits there needs no toggle.
    candidate.removeAttribute("data-more");
    if (text.scrollHeight > text.clientHeight + 1) {
      candidate.setAttribute("data-more", "");
    }
  }

  function scan() {
    document.querySelectorAll("[data-candidate]").forEach(measure);
  }

  function toggle(button) {
    var candidate = button.closest("[data-candidate]");
    var open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.setAttribute("aria-label", open ? button.dataset.labelLess : button.dataset.labelMore);
    candidate.toggleAttribute("data-open", open);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-howto-toggle]");
    if (button) {
      toggle(button);
    }
  });

  document.addEventListener("htmx:after:swap", scan);
  window.addEventListener("resize", scan);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }
})();
