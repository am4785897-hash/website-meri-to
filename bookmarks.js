(function () {
  const list = document.getElementById("tv-bookmarks-list");
  const emptyMsg = document.getElementById("tv-bookmarks-empty");
  if (!list) return;

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.content : "";

  list.querySelectorAll(".tv-bookmark-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".tv-bookmark-row");
      await fetch("/api/bookmark/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({
          target_type: row.dataset.targetType,
          target_id: Number(row.dataset.targetId),
        }),
      });
      row.remove();
      if (emptyMsg) emptyMsg.style.display = list.children.length ? "none" : "block";
    });
  });
})();
