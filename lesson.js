/**
 * Course lesson page interactivity (works for any course - Python,
 * Web Penetration Testing, Machine Learning Basics, Flutter App Dev).
 * Tabs, the one-question quick-check quiz, and the Mark Complete action.
 * Depends on window.TVPlayground (js/playground.js) for the embedded
 * Code Playground - loaded separately, see python-lesson.html.
 */
(function () {
  const lessonRoot = document.querySelector("[data-lesson-slug]");
  if (!lessonRoot) return;

  const lessonSlug = lessonRoot.dataset.lessonSlug;
  const courseSlug = lessonRoot.dataset.courseSlug; // which course this lesson belongs to - any course, not just Python
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.content : "";

  // --- Tabs ---
  const tabs = document.querySelectorAll(".tv-tab");
  const panels = document.querySelectorAll(".tv-tab-panel");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("is-active"));
      panels.forEach((p) => p.classList.remove("is-active"));
      tab.classList.add("is-active");
      const panel = document.getElementById("tv-panel-" + tab.dataset.tab);
      if (panel) panel.classList.add("is-active");
    });
  });

  // --- Quiz ---
  const quizOptions = document.querySelectorAll(".tv-quiz-option");
  const quizFeedback = document.getElementById("tv-quiz-feedback");

  quizOptions.forEach((btn) => {
    btn.addEventListener("click", async () => {
      const chosenIndex = Number(btn.dataset.index);
      quizOptions.forEach((b) => (b.disabled = true));

      try {
        const resp = await fetch("/api/lesson-quiz-check", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({ course_slug: courseSlug, lesson_slug: lessonSlug, chosen_index: chosenIndex }),
        });
        const data = await resp.json();

        btn.classList.add(data.correct ? "is-correct" : "is-wrong");
        if (quizFeedback) {
          let text = data.correct ? "Correct! " : "Not quite. ";
          text += data.explanation || "";
          if (data.xp_awarded) text += ` (+${data.xp_awarded} XP)`;
          quizFeedback.textContent = text;
          quizFeedback.classList.add("is-visible", data.correct ? "is-correct" : "is-wrong");
        }
        if (window.showBadgeToast) window.showBadgeToast(data.new_badges);
      } catch (err) {
        if (quizFeedback) {
          quizFeedback.textContent = "Couldn't reach the server - try again.";
          quizFeedback.classList.add("is-visible", "is-wrong");
        }
        quizOptions.forEach((b) => (b.disabled = false));
      }
    });
  });

  // --- Bookmark toggle ---
  const bookmarkBtn = document.getElementById("tv-lesson-bookmark");
  if (bookmarkBtn) {
    const label = document.getElementById("tv-lesson-bookmark-label");
    bookmarkBtn.addEventListener("click", async () => {
      bookmarkBtn.disabled = true;
      try {
        const resp = await fetch("/api/bookmark/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({
            target_type: bookmarkBtn.dataset.targetType,
            target_id: Number(bookmarkBtn.dataset.targetId),
          }),
        });
        const data = await resp.json();
        bookmarkBtn.classList.toggle("is-bookmarked", data.bookmarked);
        if (label) label.textContent = data.bookmarked ? "Bookmarked" : "Bookmark";
      } finally {
        bookmarkBtn.disabled = false;
      }
    });
  }

  // --- Mark Complete ---
  const completeBtn = document.getElementById("tv-mark-complete");
  if (completeBtn) {
    completeBtn.addEventListener("click", async () => {
      completeBtn.disabled = true;
      completeBtn.textContent = "Saving...";
      try {
        const resp = await fetch("/api/lesson-complete", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({ course_slug: courseSlug, lesson_slug: lessonSlug }),
        });
        const data = await resp.json();
        completeBtn.textContent = `Completed (+${data.xp_awarded} XP)`;
        if (window.showBadgeToast) window.showBadgeToast(data.new_badges);
        // Reloads so the sidebar lesson list, progress bar, and the
        // now-unlocked next lesson all reflect the fresh server state.
        setTimeout(() => window.location.reload(), 900);
      } catch (err) {
        completeBtn.disabled = false;
        completeBtn.textContent = "Couldn't save - try again";
      }
    });
  }
})();
