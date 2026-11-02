// The admin's knowledge page (B7, ADR-0033, ticket #422). It opens and closes the upload sheet
// and the delete dialogs, and runs the checks a browser can make (type, size, a blank required
// field) before anything is sent, showing the template's own messages. While the file is on its
// way it shows an Uploading row, whose Cancel aborts the request. The server checks everything
// again and answers with the whole `#knowledge` region, so its reply replaces that row.
(function () {
  var MB = 1024 * 1024;

  function sheetOf(root) {
    return root.querySelector("[data-knowledge-sheet]");
  }

  function formOf(root) {
    return root.querySelector("[data-knowledge-form]");
  }

  function region() {
    return document.getElementById("knowledge");
  }

  function openSheet() {
    var root = region();
    var sheet = root && sheetOf(root);
    if (!sheet) {
      return;
    }
    sheet.hidden = false;
    var title = sheet.querySelector('[name="title"]');
    if (title) {
      title.focus();
    }
  }

  function closeSheet() {
    var root = region();
    var sheet = root && sheetOf(root);
    if (sheet) {
      sheet.hidden = true;
    }
  }

  function size(bytes) {
    return bytes < MB ? Math.max(1, Math.round(bytes / 1024)) + " KB" : (bytes / MB).toFixed(1).replace(/\.0$/, "") + " MB";
  }

  function extensionOf(name) {
    var dot = name.lastIndexOf(".");
    return dot < 0 ? "" : name.slice(dot).toLowerCase();
  }

  function clearErrors(form) {
    form.querySelectorAll("[data-error-for]").forEach(function (message) {
      message.hidden = true;
    });
    form.querySelectorAll('[aria-invalid="true"]').forEach(function (field) {
      field.setAttribute("aria-invalid", "false");
    });
    var label = form.querySelector("[data-file-label]");
    if (label) {
      label.classList.remove("border-error");
      label.classList.add("border-base-content/20");
    }
  }

  function markFile(form, problem, bytes) {
    var message = form.querySelector('[data-error-for="file"][data-problem="' + problem + '"]');
    if (message) {
      if (problem === "too_large" && message.dataset.template) {
        message.textContent = message.dataset.template.replace("{size}", size(bytes));
      }
      message.hidden = false;
    }
    var label = form.querySelector("[data-file-label]");
    if (label) {
      label.classList.add("border-error");
      label.classList.remove("border-base-content/20");
    }
  }

  function markField(form, name, field) {
    var message = form.querySelector('[data-error-for="' + name + '"]');
    if (message) {
      message.hidden = false;
    }
    if (field) {
      field.setAttribute("aria-invalid", "true");
    }
  }

  function checkFile(form) {
    var input = form.querySelector("[data-file-input]");
    var file = input && input.files && input.files[0];
    if (!file || file.size === 0) {
      markFile(form, "missing", 0);
      return false;
    }
    if (file.size > Number(form.dataset.maxBytes)) {
      markFile(form, "too_large", file.size);
      return false;
    }
    var listed = (input.getAttribute("accept") || "").split(",");
    if (listed.indexOf(extensionOf(file.name)) < 0) {
      markFile(form, "unlisted_type", file.size);
      return false;
    }
    return true;
  }

  // The browser's own checks; the server's are the ones that count.
  function check(form) {
    clearErrors(form);
    var ok = checkFile(form);
    form.querySelectorAll("[data-required]").forEach(function (field) {
      if (!field.value.trim()) {
        markField(form, field.name, field);
        ok = false;
      }
    });
    form.querySelectorAll("[data-required-group]").forEach(function (group) {
      if (!group.querySelector("input:checked")) {
        markField(form, group.dataset.requiredGroup, null);
        ok = false;
      }
    });
    return ok;
  }

  function fill(row, form) {
    var title = form.querySelector('[name="title"]').value.trim();
    var author = form.querySelector('[name="author"]').value.trim();
    var tier = form.querySelector('[name="evidence_tier"]');
    var tierLabel = tier.options[tier.selectedIndex].textContent;
    row.querySelector("[data-uploading-title]").textContent = title;
    row.querySelector("[data-uploading-byline]").textContent = author ? author + " · " + tierLabel : tierLabel;
    return row;
  }

  function showUploading(root, form) {
    var cards = root.querySelector("[data-knowledge-cards]");
    var table = root.querySelector("[data-knowledge-table] tbody");
    var card = root.querySelector("#knowledge-uploading-card");
    var row = root.querySelector("#knowledge-uploading-row");
    root.dataset.wasEmpty = cards.hidden ? "true" : "false";
    cards.hidden = false;
    root.querySelector("[data-knowledge-table]").hidden = false;
    root.querySelector("[data-knowledge-empty]").hidden = true;
    cards.prepend(fill(card.content.firstElementChild.cloneNode(true), form));
    table.prepend(fill(row.content.firstElementChild.cloneNode(true), form));
  }

  function hideUploading(root) {
    root.querySelectorAll("[data-uploading]").forEach(function (row) {
      row.remove();
    });
    if (root.dataset.wasEmpty === "true") {
      root.querySelector("[data-knowledge-cards]").hidden = true;
      root.querySelector("[data-knowledge-table]").hidden = true;
      root.querySelector("[data-knowledge-empty]").hidden = false;
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element) || !target.closest("#knowledge")) {
      return;
    }
    if (target.closest("[data-knowledge-open]")) {
      openSheet();
    } else if (target.closest("[data-knowledge-close]")) {
      closeSheet();
    } else if (target.closest("[data-knowledge-cancel]")) {
      var root = region();
      htmx.trigger(formOf(root), "htmx:abort");
      hideUploading(root);
    } else {
      var opener = target.closest("[data-knowledge-delete]");
      var dialog = opener && document.getElementById(opener.dataset.knowledgeDelete);
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    }
  });

  document.addEventListener("keydown", function (event) {
    var root = region();
    var sheet = root && sheetOf(root);
    if (event.key === "Escape" && sheet && !sheet.hidden) {
      closeSheet();
    }
  });

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.matches("[data-file-input]")) {
      return;
    }
    var form = input.form;
    var file = input.files && input.files[0];
    var name = form.querySelector("[data-file-name]");
    var hint = form.querySelector("[data-file-hint]");
    if (file) {
      name.textContent = file.name;
      hint.textContent = hint.dataset.chosen.replace("{size}", size(file.size));
    }
    clearErrors(form);
  });

  document.addEventListener("htmx:before:request", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-knowledge-form]")) {
      return;
    }
    if (!check(form)) {
      event.preventDefault();
      return;
    }
    closeSheet();
    showUploading(region(), form);
  });

  // No reply at all (offline, a dropped connection): drop the row and show the answers again.
  document.addEventListener("htmx:error", function (event) {
    var form = event.target;
    if (form instanceof HTMLFormElement && form.matches("[data-knowledge-form]")) {
      hideUploading(region());
      openSheet();
    }
  });
})();
