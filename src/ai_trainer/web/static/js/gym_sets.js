// The gym draft's edit form (ticket #398): which of a set's fields show for the exercise's
// measure, and adding and removing a set's row. Delegated from the document, so a form htmx swaps
// in works without a re-bind. Every field of every row is posted whether shown or not, so the
// server keeps what the athlete didn't touch; a field the new measure hides is cleared here.
// The pace note (#626) shows only while the measure is distance and time. A circuit's Rounds
// field (#632) keeps each of its exercises at one row per round: raising it copies each
// exercise's last row, as a new set, and lowering it drops the last rows (ADR-0032 #632
// amendment). A circuit's exercises have no add or remove control of their own.
(() => {
  const SET_INDEX = /\.sets\.(?:\d+|__set__)\./;
  // An exercise holds at most this many sets (`MAX_SETS` in the gym payload): each is a row.
  const MAX_SETS = 100;

  const rowsOf = (group) => [...group.querySelectorAll("[data-set-row]")].filter(
    (row) => !row.closest("template"),
  );

  // Shows the fields the measure uses. Only a change of measure clears the ones it hides: adding
  // a set must not blank what the other rows hold.
  const showFields = (group, { clear }) => {
    const measure = group.querySelector("select[data-measure]").value;
    const shown = JSON.parse(group.dataset.shows)[measure]?.shows ?? [];
    group.querySelector("[data-pace-note]")?.classList.toggle("hidden", measure !== "distance_time");
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
      if (!remove) return;
      remove.disabled = rows.length === 1;
      remove.setAttribute("aria-label", `Remove set ${index + 1}`);
    });
    const add = group.querySelector("[data-add-set]");
    if (add) add.disabled = rows.length >= MAX_SETS;
  };

  // One row per round for each exercise in the circuit; a value that isn't 1 to MAX_SETS
  // whole rounds leaves the rows as they are, and the server says what is wrong with it.
  const followRounds = (input) => {
    const rounds = Number(input.value);
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > MAX_SETS) return;
    for (const group of input.closest("[data-circuit]").querySelectorAll("[data-exercise]")) {
      let rows = rowsOf(group);
      while (rows.length > rounds) rows.pop().remove();
      while (rows.length < rounds) {
        const last = rows[rows.length - 1];
        const copy = last.cloneNode(true);
        const source = copy.querySelector('input[name$=".src"]');
        if (source) source.value = "";
        last.after(copy);
        rows = rowsOf(group);
      }
      renumber(group);
    }
  };

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const group = target?.closest("[data-exercise]");
    if (!group) return;
    if (target.closest("[data-add-set]")) {
      if (rowsOf(group).length >= MAX_SETS) return;
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
    // Rounds follow on `change`, once the value settles: on `input`, typing "10" would pass
    // through "1" and drop every round past the first (#632 code review).
    if (target?.matches("input[data-rounds]")) {
      followRounds(target);
      return;
    }
    if (!target?.matches("select[data-measure]")) return;
    showFields(target.closest("[data-exercise]"), { clear: true });
  });
})();
