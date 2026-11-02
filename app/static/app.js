// Clickable table rows.
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, input, select")) location.href = row.dataset.href;
});

// "+ New" dropdown.
const toggle = document.querySelector("[data-toggle-menu]");
if (toggle) {
  const menu = toggle.nextElementSibling;
  toggle.addEventListener("click", () => (menu.hidden = !menu.hidden));
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".new-menu")) menu.hidden = true;
  });
}

// Filter bars submit on change.
for (const form of document.querySelectorAll("form[data-autosubmit]")) {
  form.addEventListener("change", () => form.submit());
}

// Live priority preview from impact x urgency (mirrors config.priority_for).
const priorityDisplay = document.getElementById("priority-display");
const [impact, urgency] = ["impact", "urgency"].map((n) => document.querySelector(`select[name=${n}]`));
if (priorityDisplay && impact && urgency) {
  const labels = { 1: "1 - Critical", 2: "2 - High", 3: "3 - Moderate", 4: "4 - Low", 5: "5 - Planning" };
  const update = () => (priorityDisplay.value = labels[+impact.value + +urgency.value - 1] || "");
  impact.addEventListener("change", update);
  urgency.addEventListener("change", update);
  update();
}
