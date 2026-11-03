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

  // The licence select (#456): Detect from the file shows no licence fields; a licence shows
  // its URL, copyright statement and attribution; Other also asks what permission is held.
  // The hint, its warning tint for SA and ND, and the button's label follow the pick.
  function showLicence(form) {
    var select = form.querySelector("[data-licence-input]");
    if (!select) {
      return;
    }
    var option = select.options[select.selectedIndex];
    var picked = select.value !== "detect";
    form.querySelectorAll("[data-licence-field]").forEach(function (field) {
      field.hidden = !picked;
    });
    form.querySelector("[data-licence-note]").hidden = select.value !== "other";
    form.querySelector("[data-licence-select]").classList.toggle("lg:col-span-2", !picked);
    var hint = form.querySelector("[data-licence-hint]");
    var warn = option.dataset.warn === "true";
    hint.textContent = option.dataset.hint;
    hint.classList.toggle("text-warning", warn);
    hint.classList.toggle("lbl-2", !warn);
    var submit = form.querySelector("[data-licence-submit]");
    submit.textContent = picked ? submit.dataset.ingest : submit.dataset.detect;
  }

  // The browser's own checks; the server's are the ones that count. A field the licence pick
  // hides isn't asked for.
  function check(form) {
    clearErrors(form);
    var ok = checkFile(form);
    form.querySelectorAll("[data-required]").forEach(function (field) {
      if (field.closest("[hidden]")) {
        return;
      }
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
    var target = event.target;
    if (target instanceof HTMLSelectElement && target.matches("[data-licence-input]")) {
      showLicence(target.form);
      return;
    }
    var input = target;
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
    } else {
      // The chooser was dismissed and the browser emptied the input.
      name.textContent = name.dataset.empty;
      hint.textContent = hint.dataset.empty;
    }
    clearErrors(form);
  });

  // The dashed file box is a drop target too. While the sheet is open, a file dropped anywhere
  // else is ignored rather than opened by the browser, which would lose the form.
  function sheetOpen() {
    var root = region();
    var sheet = root && sheetOf(root);
    return Boolean(sheet && !sheet.hidden);
  }

  function dropLabel(event) {
    return event.target instanceof Element ? event.target.closest("[data-file-label]") : null;
  }

  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      if (!sheetOpen()) {
        return;
      }
      event.preventDefault();
      var label = dropLabel(event);
      event.dataTransfer.dropEffect = label ? "copy" : "none";
      if (label) {
        label.classList.add("border-primary");
      }
    });
  });

  document.addEventListener("dragleave", function (event) {
    var label = dropLabel(event);
    if (label && !label.contains(event.relatedTarget)) {
      label.classList.remove("border-primary");
    }
  });

  document.addEventListener("drop", function (event) {
    if (!sheetOpen()) {
      return;
    }
    event.preventDefault();
    var label = dropLabel(event);
    var files = event.dataTransfer && event.dataTransfer.files;
    if (!label || !files || files.length === 0) {
      return;
    }
    label.classList.remove("border-primary");
    var input = label.querySelector("[data-file-input]");
    var one = new DataTransfer();
    one.items.add(files[0]);
    input.files = one.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
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
  // Cancel's abort lands here too, and it has already dropped the row on purpose.
  document.addEventListener("htmx:error", function (event) {
    var form = event.target;
    var error = event.detail && event.detail.error;
    if (error && error.name === "AbortError") {
      return;
    }
    if (form instanceof HTMLFormElement && form.matches("[data-knowledge-form]")) {
      hideUploading(region());
      openSheet();
    }
  });
})();
