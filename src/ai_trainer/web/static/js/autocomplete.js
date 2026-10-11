// Composer suggestions (ticket #85, F13). A starter chip fills the composer without sending.
// While the athlete types, #chat-suggestions shows up to six suggestions that match, with the
// match marked: ↑/↓ move, Enter or a tap picks one into the composer, Esc closes. The
// suggestions, their labels and the mark's look live in templates; this only filters and picks.
// Delegated on the document so it survives htmx swapping the composer.
(function () {
  var MAX_SHOWN = 6;
  var MIN_CHARS = 2;

  function listbox() {
    return document.getElementById("chat-suggestions");
  }

  function isInput(element) {
    return element instanceof Element && element.matches("[data-chat-input]");
  }

  function options(box) {
    return Array.prototype.slice.call(box.querySelectorAll("[role=option]"));
  }

  function shownOptions(box) {
    return options(box).filter(function (option) {
      return !option.hidden;
    });
  }

  // Sets the composer's text as if typed: the untrusted input event lets chat.js enable Send,
  // and this file ignores it, so a pick doesn't reopen the popover.
  function fill(input, text) {
    input.value = text;
    input.focus();
    input.setSelectionRange(text.length, text.length);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function mark(target, text, query, box) {
    target.textContent = "";
    var at = text.toLowerCase().indexOf(query);
    if (at < 0) {
      target.textContent = text;
      return;
    }
    var highlight = box.querySelector("[data-suggestion-mark]").content.firstElementChild.cloneNode(false);
    highlight.textContent = text.slice(at, at + query.length);
    target.append(text.slice(0, at), highlight, text.slice(at + query.length));
  }

  function activate(input, box, active) {
    options(box).forEach(function (option) {
      option.setAttribute("aria-selected", String(option === active));
    });
    input.setAttribute("aria-activedescendant", active.id);
    active.scrollIntoView({ block: "nearest" });
  }

  function close(input, box) {
    box.hidden = true;
    options(box).forEach(function (option) {
      option.setAttribute("aria-selected", "false");
    });
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function matches(option, query) {
    var text = option.dataset.suggestion.toLowerCase();
    var hint = (option.dataset.suggestionHint || "").toLowerCase();
    return text !== query && (text.indexOf(query) >= 0 || hint.indexOf(query) >= 0);
  }

  function update(input) {
    var box = listbox();
    if (!box) {
      return;
    }
    var query = input.value.trim().toLowerCase();
    var shown = [];
    options(box).forEach(function (option) {
      var show = query.length >= MIN_CHARS && shown.length < MAX_SHOWN && matches(option, query);
      option.hidden = !show;
      if (show) {
        shown.push(option);
        mark(option.querySelector("[data-suggestion-label]"), option.dataset.suggestion, query, box);
        mark(option.querySelector("[data-suggestion-detail]"), option.dataset.suggestionHint || "", query, box);
      }
    });
    box.querySelectorAll("[data-suggestion-group]").forEach(function (group) {
      group.hidden = !group.querySelector("[role=option]:not([hidden])");
    });
    if (shown.length === 0) {
      close(input, box);
      return;
    }
    box.hidden = false;
    input.setAttribute("aria-expanded", "true");
    activate(input, box, shown[0]);
  }

  function pick(input, box, option) {
    close(input, box);
    fill(input, option.dataset.suggestion);
  }

  document.addEventListener("click", function (event) {
    var chip = event.target.closest && event.target.closest("[data-chat-chip]");
    var input = document.querySelector("[data-chat-input]");
    if (chip && input) {
      fill(input, chip.dataset.chatChip);
      return;
    }
    var option = event.target.closest && event.target.closest("#chat-suggestions [role=option]");
    if (option && input) {
      pick(input, listbox(), option);
    }
  });

  // Keeps the composer focused while an option is pressed, so the focusout below doesn't close
  // the popover before the click lands.
  document.addEventListener("pointerdown", function (event) {
    if (event.target.closest && event.target.closest("#chat-suggestions")) {
      event.preventDefault();
    }
  });

  document.addEventListener("pointerover", function (event) {
    var option = event.target.closest && event.target.closest("#chat-suggestions [role=option]");
    var input = document.querySelector("[data-chat-input]");
    if (option && input) {
      activate(input, listbox(), option);
    }
  });

  document.addEventListener("input", function (event) {
    if (isInput(event.target) && event.isTrusted) {
      update(event.target);
    }
  });

  document.addEventListener("focusout", function (event) {
    var box = listbox();
    if (isInput(event.target) && box && !box.hidden) {
      close(event.target, box);
    }
  });

  // Capturing, so an Enter that picks a suggestion never reaches chat.js and sends.
  document.addEventListener(
    "keydown",
    function (event) {
      var box = listbox();
      if (!isInput(event.target) || !box || box.hidden || event.isComposing) {
        return;
      }
      var input = event.target;
      var shown = shownOptions(box);
      var current = shown.findIndex(function (option) {
        return option.getAttribute("aria-selected") === "true";
      });
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        var step = event.key === "ArrowDown" ? 1 : -1;
        activate(input, box, shown[(current + step + shown.length) % shown.length]);
      } else if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
        pick(input, box, shown[Math.max(current, 0)]);
      } else if (event.key === "Escape") {
        event.preventDefault();
        close(input, box);
      }
    },
    true
  );
})();
