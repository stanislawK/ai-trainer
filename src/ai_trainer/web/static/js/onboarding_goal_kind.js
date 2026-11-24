// The summary's goal kind chips (PRD 0013 B45, ticket #628): a radio group per goal, where a
// second tap on the picked chip clears it. A radio is checked before its click event fires, so
// each input remembers whether it was the picked one.
(function () {
  document.addEventListener("click", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.hasAttribute("data-goal-kind")) {
      return;
    }
    var group = document.querySelectorAll('input[data-goal-kind][name="' + CSS.escape(input.name) + '"]');
    if (input.dataset.wasChecked === "1") {
      input.checked = false;
      input.dataset.wasChecked = "";
    } else {
      group.forEach(function (each) {
        each.dataset.wasChecked = "";
      });
      input.dataset.wasChecked = "1";
    }
  });
})();
