/**
 * Notes page: create, autosave-on-edit (debounced), and delete - all via
 * fetch, so nothing here ever needs a full page reload.
 */
(function () {
  const grid = document.getElementById("tv-notes-grid");
  const emptyMsg = document.getElementById("tv-notes-empty");
  const newBtn = document.getElementById("tv-new-note");
  if (!grid) return;

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.content : "";
  const saveTimers = new WeakMap();

  function updateEmptyState() {
    if (emptyMsg) emptyMsg.style.display = grid.children.length ? "none" : "block";
  }

  function wireCard(card) {
    const titleEl = card.querySelector(".tv-note-card__title");
    const contentEl = card.querySelector(".tv-note-card__content");
    const deleteBtn = card.querySelector(".tv-note-card__delete");
    const timestampEl = card.querySelector(".tv-note-card__timestamp");

    function scheduleSave() {
      clearTimeout(saveTimers.get(card));
      saveTimers.set(card, setTimeout(saveCard, 600));
    }

    async function saveCard() {
      const noteId = card.dataset.noteId;
      const resp = await fetch(`/api/notes/${noteId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ title: titleEl.value, content: contentEl.value }),
      });
      const data = await resp.json();
      if (data.updated_at && timestampEl) timestampEl.textContent = data.updated_at;
    }

    titleEl.addEventListener("input", scheduleSave);
    contentEl.addEventListener("input", scheduleSave);

    deleteBtn.addEventListener("click", async () => {
      const noteId = card.dataset.noteId;
      await fetch(`/api/notes/${noteId}`, {
        method: "DELETE",
        headers: { "X-CSRFToken": csrfToken },
      });
      card.remove();
      updateEmptyState();
    });
  }

  grid.querySelectorAll(".tv-note-card").forEach(wireCard);

  if (newBtn) {
    newBtn.addEventListener("click", async () => {
      const resp = await fetch("/api/notes", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ title: "Untitled note", content: "" }),
      });
      const data = await resp.json();

      const card = document.createElement("div");
      card.className = "tv-note-card";
      card.dataset.noteId = data.id;
      card.innerHTML =
        '<input class="tv-note-card__title" value="' + data.title + '" placeholder="Untitled note">' +
        '<textarea class="tv-note-card__content" placeholder="Start typing..."></textarea>' +
        '<div class="tv-note-card__footer">' +
          '<span class="tv-note-card__timestamp">' + data.updated_at + "</span>" +
          '<button type="button" class="tv-note-card__delete">Delete</button>' +
        "</div>";
      grid.prepend(card);
      wireCard(card);
      updateEmptyState();
      card.querySelector(".tv-note-card__title").focus();
    });
  }
})();
