// Proposals on the admin's knowledge page (ADR-0036, the Admin Staging mock, ticket #461). Each
// proposal has a checkbox in the phone card and one in the desktop row: ticking one ticks its
// twin, and any selection raises the bulk bar with its count. Reject asks in a dialog first;
// Approve posts at once. Both send the checked IDs (`hx-include`), the server counts a repeated
// one once, and its reply re-renders the whole `#knowledge` region, which clears the selection
// and may carry a toast, dismissed here.
(function () {
  function region() {
    return document.getElementById("knowledge");
  }

  // One count per source, whichever of its twins is checked.
  function selected(root) {
    var ids = {};
    root.querySelectorAll("[data-pick]:checked").forEach(function (box) {
      ids[box.value] = true;
    });
    return Object.keys(ids).length;
  }

  function showBar(root) {
    var bar = root.querySelector("[data-knowledge-bulk]");
    if (!bar) {
      return;
    }
    var count = selected(root);
    bar.hidden = count === 0;
    var label = bar.querySelector("[data-knowledge-selected]");
    label.textContent = label.dataset.template.replace("{n}", String(count));
  }

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.matches("[data-pick]")) {
      return;
    }
    var root = region();
    root.querySelectorAll("[data-pick]").forEach(function (twin) {
      if (twin !== box && twin.value === box.value) {
        twin.checked = box.checked;
      }
    });
    showBar(root);
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element) || !target.closest("#knowledge")) {
      return;
    }
    if (target.closest("[data-knowledge-reject]")) {
      var dialog = document.getElementById("knowledge-reject");
      var title = dialog && dialog.querySelector("[data-knowledge-reject-title]");
      var count = selected(region());
      if (title) {
        title.textContent = count === 1 ? title.dataset.one : title.dataset.many.replace("{n}", String(count));
      }
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    } else if (target.closest("[data-knowledge-dismiss]")) {
      var toast = target.closest("[data-knowledge-toast]");
      if (toast) {
        toast.remove();
      }
    }
  });

  // A page restored from the back-forward cache keeps ticked boxes; show the bar for them.
  window.addEventListener("pageshow", function () {
    var root = region();
    if (root) {
      showBar(root);
    }
  });
})();
