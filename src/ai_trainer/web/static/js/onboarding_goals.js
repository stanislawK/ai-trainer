// Wires the onboarding goals step's cards (tickets #76 and #99). A card's "Add goal" appends
// a copy of that card's row `<template>`, which already carries the card's sport; a row's
// remove button takes it out of the form, so it is never posted and never stored. Listeners sit on `document`, so they survive htmx swapping the step.
(function () {
  document.addEventListener("click", function (event) {
    var remove = event.target.closest("[data-goal-remove]");
    if (remove) {
      var row = remove.closest("[data-goal-row]");
      var list = row && row.parentElement;
      if (row) {
        row.remove();
      }
      var next = list && list.querySelector("[data-goal-row] input[name='goal_text']");
      if (next) {
        next.focus();
      }
      return;
    }

    var add = event.target.closest("[data-goal-add]");
    if (!add) {
      return;
    }
    var card = add.closest("[data-goal-card]");
    var template = card && card.querySelector("template[data-goal-template]");
    var goals = card && card.querySelector("[data-goal-list]");
    if (!template || !goals) {
      return;
    }
    goals.appendChild(template.content.cloneNode(true));
    var added = goals.lastElementChild;
    var input = added && added.querySelector("input[name='goal_text']");
    if (input) {
      input.focus();
    }
  });
})();
