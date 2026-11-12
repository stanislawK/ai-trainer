// Exercise media under Admin (#554): Dismiss closes the toast an approval leaves.
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element) || !target.closest("[data-media-dismiss]")) {
      return;
    }
    var toast = target.closest("[data-media-toast]");
    if (toast) {
      toast.remove();
    }
  });
})();
