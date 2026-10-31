// The gym draft's edit form (ticket #398): which of a set's fields show for the exercise's
// measure, and adding and removing a set's row. Delegated from the document, so a form htmx swaps
// in works without a re-bind. Every field of every row is posted whether shown or not, so the
// server keeps what the athlete didn't touch; a field the new measure hides is cleared here.
(() => {
  const SET_INDEX = /\.sets\.(?:\d+|__set__)\./;

  const rowsOf = (group) => [...group.querySelectorAll("[data-set-row]")].filter(
    (row) => !row.closest("template"),
  );

  // Shows the fields the measure uses. Only a change of measure clears the ones it hides: adding
  // a set must not blank what the other rows hold.
  const showFields = (group, { clear }) => {
    const measure = group.querySelector("select[data-measure]").value;
    const shown = JSON.parse(group.dataset.shows)[measure]?.shows ?? [];
    for (const row of rowsOf(group)) {
      for (const field of row.querySelectorAll("[data-field]")) {
        const visible = shown.includes(field.dataset.field);
        field.classList.toggle("hidden", !visible);
        if (!visible && clear) field.querySelector("input").value = "";
      }
    }
  };

  const renumber = (group) => {
    const rows = rowsOf(group);
    rows.forEach((row, index) => {
      for (const input of row.querySelectorAll("[name]")) {
        input.name = input.name.replace(SET_INDEX, `.sets.${index}.`);
      }
      row.querySelector("[data-set-number]").textContent = String(index + 1);
      const remove = row.querySelector("[data-remove-set]");
      remove.disabled = rows.length === 1;
      remove.setAttribute("aria-label", `Remove set ${index + 1}`);
    });
  };

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const group = target?.closest("[data-exercise]");
    if (!group) return;
    if (target.closest("[data-add-set]")) {
      const template = group.querySelector("template[data-set-template]");
      const row = template.content.firstElementChild.cloneNode(true);
      template.before(row);
      renumber(group);
      showFields(group, { clear: false });
      row.querySelector("[data-field]:not(.hidden) input")?.focus();
    } else if (target.closest("[data-remove-set]")) {
      if (rowsOf(group).length > 1) {
        target.closest("[data-set-row]").remove();
        renumber(group);
      }
    }
  });

  document.addEventListener("change", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target?.matches("select[data-measure]")) return;
    showFields(target.closest("[data-exercise]"), { clear: true });
  });
})();
