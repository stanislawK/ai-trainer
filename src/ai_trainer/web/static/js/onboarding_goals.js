// Wires the onboarding goals step's rows (ticket #76). "Add goal" appends a copy of the
// row `<template>`; a row's remove button takes it out of the form, so it is never posted
// and never stored. Listeners sit on `document`, so they survive htmx swapping the step.
(function () {
  document.addEventListener("click", function (event) {
    var remove = event.target.closest("[data-goal-remove]");
    if (remove) {
      var row = remove.closest("[data-goal-row]");
      var list = row && row.parentElement;
      if (row) {
        row.remove();
      }
      var next = list && list.querySelector("[data-goal-row] input");
      if (next) {
        next.focus();
      }
      return;
    }

    var add = event.target.closest("[data-goal-add]");
    if (!add) {
      return;
    }
    var form = add.closest("form");
    var template = form && form.querySelector("template[data-goal-template]");
    var goals = form && form.querySelector("[data-goal-list]");
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
