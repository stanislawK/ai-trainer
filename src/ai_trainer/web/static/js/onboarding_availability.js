// Wires the onboarding availability step (ticket #105). Each day chip owns a hidden
// `minutes_N` field; one slider edits the field of the selected chip. Clicking a chip selects
// it and rebinds the slider to that day, so no other day's value moves. The strings come from
// `data-fmt-*` attributes on the form, which live in the template. Listeners sit on `document`,
// so they survive htmx swapping the step.
(function () {
  function form(el) {
    return el.closest("[data-availability]");
  }

  function selected(root) {
    return root.querySelector("[data-day-chip][aria-pressed='true']");
  }

  function input(root, chip) {
    return root.querySelector("[data-day-input='" + chip.dataset.dayChip + "']");
  }

  function long(root, m) {
    var f = root.dataset;
    if (m === 0) {
      return f.fmtRest;
    }
    var hours = Math.floor(m / 60) ? Math.floor(m / 60) + " " + f.fmtHour : "";
    var mins = m % 60 ? (m % 60) + " " + f.fmtMinute : "";
    return [hours, mins].filter(Boolean).join(" ");
  }

  function short(root, m) {
    var f = root.dataset;
    if (m === 0) {
      return f.fmtRest;
    }
    if (m < 60) {
      return m + f.fmtShortMinute;
    }
    return Math.floor(m / 60) + f.fmtShortHour + (m % 60 || "");
  }

  function summary(root) {
    var days = 0;
    var total = 0;
    root.querySelectorAll("[data-day-input]").forEach(function (field) {
      var m = Number(field.value);
      if (m > 0) {
        days += 1;
        total += m;
      }
    });
    if (days === 0) {
      return root.dataset.fmtNone;
    }
    var template = days === 1 ? root.dataset.fmtOne : root.dataset.fmtMany;
    return template.replace("{days}", days).replace("{time}", long(root, total));
  }

  function paintChip(root, chip, minutes, isSelected) {
    chip.setAttribute("aria-pressed", isSelected ? "true" : "false");
    chip.setAttribute("aria-label", chip.dataset.dayName + ", " + long(root, minutes));
    chip.querySelector("[data-day-short]").textContent = short(root, minutes);
    chip.classList.toggle("bg-primary/20", minutes > 0);
    chip.classList.toggle("bg-base-content/10", minutes === 0);
    chip.classList.toggle("text-base-content/70", minutes === 0);
    chip.classList.toggle("ring-2", isSelected);
    chip.classList.toggle("ring-primary", isSelected);
  }

  function paintSlider(root) {
    var chip = selected(root);
    var slider = root.querySelector("[data-day-slider]");
    var minutes = Number(input(root, chip).value);
    slider.value = minutes;
    root.querySelectorAll("[data-slider-name]").forEach(function (el) {
      el.textContent = chip.dataset.dayName;
    });
    root.querySelector("[data-slider-value]").textContent = long(root, minutes);
    root.querySelectorAll("[data-summary]").forEach(function (el) {
      el.textContent = summary(root);
    });
  }

  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-day-chip]");
    var root = chip && form(chip);
    if (!root) {
      return;
    }
    var previous = selected(root);
    if (previous && previous !== chip) {
      paintChip(root, previous, Number(input(root, previous).value), false);
    }
    paintChip(root, chip, Number(input(root, chip).value), true);
    paintSlider(root);
  });

  document.addEventListener("input", function (event) {
    var slider = event.target.closest("[data-day-slider]");
    var root = slider && form(slider);
    if (!root) {
      return;
    }
    var chip = selected(root);
    var minutes = Number(slider.value);
    input(root, chip).value = minutes;
    paintChip(root, chip, minutes, true);
    paintSlider(root);
  });
})();
