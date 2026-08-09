/**
 * showBadgeToast(badges) - pops up a small "Badge unlocked" card for each
 * newly-earned badge in the array (usually 0 or 1, occasionally more if
 * one action crosses several thresholds at once, e.g. finishing the last
 * lesson in a course also crosses an XP milestone).
 *
 * Shared by every page that can award a badge (js/lesson.js,
 * js/dashboard-widgets.js) instead of each one building its own popup.
 */
window.showBadgeToast = function (badges) {
  if (!badges || !badges.length) return;

  let stack = document.getElementById("tv-badge-toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "tv-badge-toast-stack";
    stack.className = "tv-badge-toast-stack";
    document.body.appendChild(stack);
  }

  badges.forEach((badge, i) => {
    setTimeout(() => {
      const toast = document.createElement("div");
      toast.className = "tv-badge-toast";
      toast.innerHTML =
        '<span class="tv-badge-toast__icon">' + badge.icon + "</span>" +
        '<div class="tv-badge-toast__text">' +
          '<strong>Badge unlocked: ' + badge.title + "</strong>" +
          '<span>' + badge.description + "</span>" +
        "</div>";
      stack.appendChild(toast);
      requestAnimationFrame(() => toast.classList.add("is-visible"));
      setTimeout(() => {
        toast.classList.remove("is-visible");
        setTimeout(() => toast.remove(), 300);
      }, 4200);
    }, i * 350);
  });
};
