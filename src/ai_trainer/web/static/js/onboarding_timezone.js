// Prefills the onboarding timezone step with the browser's IANA zone (ticket #77). The server
// renders UTC as the default, so with no browser zone, or one the list does not know, nothing
// changes. The choice is only made while the athlete has not touched the select. Listeners sit
// on `document`, so they survive htmx swapping the step.
(function () {
  function browserZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      return "";
    }
  }

  function prefill(root) {
    var select = root.querySelector("[data-timezone-select]");
    if (!select || select.dataset.touched || select.getAttribute("aria-invalid")) {
      return;
    }
    var zone = browserZone();
    var option = Array.prototype.find.call(select.options, function (candidate) {
      return candidate.value === zone;
    });
    if (!option) {
      return;
    }
    select.value = zone;
    option.textContent = zone + " (" + root.dataset.detectedSuffix + ")";
  }

  function run(scope) {
    scope.querySelectorAll("[data-timezone]").forEach(prefill);
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-timezone-select]")) {
      event.target.dataset.touched = "1";
    }
  });
  document.addEventListener("htmx:after:swap", function () {
    run(document);
  });
  run(document);
})();
