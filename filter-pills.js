/**
 * TVFilterPills - generic client-side filter-pill wiring, shared by any
 * page with a ".tv-filter-pill" bar filtering cards in a grid by a
 * `data-categories` attribute (space-separated). Originally lived as an
 * inline script on dashboard.html (Recommended for You); pulled out here
 * so the Projects page can reuse the exact same behavior instead of a
 * copy-pasted duplicate.
 *
 *   TVFilterPills.init(filterBarId, gridId, cardSelector)
 *
 * A pill with data-filter="all" (or any filter value not present in any
 * card's data-categories) matches every card - handy for an "All" /
 * "All Projects" pill without special-casing it in the markup.
 */
window.TVFilterPills = (function () {
  function init(filterBarId, gridId, cardSelector) {
    const filterBar = document.getElementById(filterBarId);
    const grid = document.getElementById(gridId);
    if (!filterBar || !grid) return;

    const cards = Array.from(grid.querySelectorAll(cardSelector));

    filterBar.addEventListener("click", function (e) {
      const btn = e.target.closest(".tv-filter-pill");
      if (!btn) return;

      filterBar.querySelectorAll(".tv-filter-pill").forEach((p) => p.classList.remove("is-active"));
      btn.classList.add("is-active");

      const filter = btn.dataset.filter;
      cards.forEach((card) => {
        const categories = card.dataset.categories.split(" ");
        const matches = filter === "all" || categories.includes(filter);
        card.style.display = matches ? "" : "none";
      });
    });
  }

  return { init };
})();
