(() => {
  const grid = document.querySelector("[data-game-grid]");
  const search = document.querySelector("[data-search]");
  const saleOnly = document.querySelector("[data-sale-only]");
  const sortButtons = document.querySelectorAll("[data-sort]");
  const countEl = document.querySelector("[data-visible-count]");
  if (!grid) return;

  const cards = Array.from(grid.querySelectorAll(".card"));
  let currentSort = "newest";

  const number = (el, key) => Number(el.dataset[key] || 0);

  const sorters = {
    newest: (a, b) => String(b.dataset.added || "").localeCompare(String(a.dataset.added || "")),
    rising: (a, b) =>
      number(b, "ccuDelta") - number(a, "ccuDelta") ||
      number(b, "ccu") - number(a, "ccu"),
    rating: (a, b) => number(b, "positive") - number(a, "positive"),
    discount: (a, b) => number(b, "discount") - number(a, "discount"),
  };

  function apply() {
    const q = (search?.value || "").trim().toLowerCase();
    const onlySale = Boolean(saleOnly?.checked);
    const sorter = sorters[currentSort] || sorters.rising;

    const visible = cards
      .map((card) => {
        const hay = `${card.dataset.name || ""} ${card.dataset.developer || ""}`.toLowerCase();
        const matchQuery = !q || hay.includes(q);
        const matchSale = !onlySale || number(card, "discount") > 0;
        const show = matchQuery && matchSale;
        card.hidden = !show;
        return show ? card : null;
      })
      .filter(Boolean)
      .sort(sorter);

    visible.forEach((card) => grid.appendChild(card));
    if (countEl) countEl.textContent = String(visible.length);
  }

  sortButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      currentSort = btn.dataset.sort || "newest";
      sortButtons.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      apply();
    });
  });

  search?.addEventListener("input", apply);
  saleOnly?.addEventListener("change", apply);
  apply();
})();
