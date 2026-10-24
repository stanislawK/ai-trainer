// Posts the browser's IANA zone with the onboarding sport pick (ADR-0028, ticket #254). The
// server falls back to UTC when it is empty or unknown, and the summary shows it to correct.
(function () {
  function browserZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      return "";
    }
  }

  function fill(scope) {
    scope.querySelectorAll("[data-onboarding-zone] input[name=timezone]").forEach(function (input) {
      input.value = browserZone();
    });
  }

  document.addEventListener("htmx:after:swap", function () {
    fill(document);
  });
  fill(document);
})();
