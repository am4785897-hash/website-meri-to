/**
 * TVDashboardWidgets - Daily Challenge + Project Gallery wiring for the
 * dashboard. Same module/CSRF pattern as js/playground.js: a single
 * window.TVDashboardWidgets.init() call wires up whatever of these
 * elements exist on the current page, and does nothing for the ones
 * that don't (so this file is safe to include on any dashboard-family
 * page, not just dashboard.html).
 *
 *   - #tv-challenge-timer   : countdown to the next UTC-midnight reset
 *   - #tv-challenge-action  : Start Challenge -> Mark Complete -> done
 *   - #tv-project-grid .tv-like-btn : per-project ❤ toggle
 */
window.TVDashboardWidgets = (function () {
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  // -------------------------------------------------------------------
  // Daily Challenge countdown - purely client-side ticking down to the
  // reset_at_iso timestamp the server rendered in. When it hits zero we
  // just reload so the new day's challenge (and a fresh not_started
  // status) comes from the server, rather than trying to fake the
  // rotation in JS.
  // -------------------------------------------------------------------
  function initCountdown() {
    const el = document.getElementById("tv-challenge-timer");
    if (!el) return;

    const resetAt = new Date(el.dataset.reset).getTime();
    if (Number.isNaN(resetAt)) return;

    function tick() {
      const remaining = resetAt - Date.now();
      if (remaining <= 0) {
        el.textContent = "00:00:00";
        window.location.reload();
        return;
      }
      const totalSeconds = Math.floor(remaining / 1000);
      const h = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
      const m = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
      const s = String(totalSeconds % 60).padStart(2, "0");
      el.textContent = `${h}:${m}:${s}`;
    }

    tick();
    setInterval(tick, 1000);
  }

  // -------------------------------------------------------------------
  // Daily Challenge action button - Start Challenge -> Mark Complete ->
  // disabled "Completed Today". Also bumps the Total XP / Level stat
  // cards in place once XP is actually awarded, no reload needed.
  // -------------------------------------------------------------------
  function initChallengeButton() {
    const btn = document.getElementById("tv-challenge-action");
    if (!btn) return;

    btn.addEventListener("click", async function () {
      const status = btn.dataset.status;
      const endpoint = status === "started"
        ? "/api/daily-challenge/complete"
        : "/api/daily-challenge/start";

      btn.disabled = true;
      try {
        const resp = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        });
        const data = await resp.json();
        if (data.error) {
          btn.disabled = false;
          return;
        }

        btn.dataset.status = data.status;

        if (data.status === "started") {
          btn.textContent = "Mark Complete";
          btn.disabled = false;
        } else if (data.status === "complete") {
          btn.textContent = "✓ Completed Today";
          btn.disabled = true;

          if (data.xp_awarded) {
            const xpEl = document.getElementById("tv-stat-total-xp");
            const levelEl = document.getElementById("tv-stat-level");
            if (xpEl && typeof data.total_xp === "number") {
              xpEl.textContent = data.total_xp.toLocaleString();
            }
            if (levelEl && typeof data.level === "number") {
              levelEl.textContent = data.level;
            }
          }
          if (window.showBadgeToast) window.showBadgeToast(data.new_badges);
        }
      } catch (err) {
        btn.disabled = false;
      }
    });
  }

  // -------------------------------------------------------------------
  // Project Gallery like buttons - optimistic-free (waits for the
  // server's real count back) so the number shown always matches the DB.
  // -------------------------------------------------------------------
  function initProjectLikes() {
    const grid = document.getElementById("tv-project-grid");
    if (!grid) return;

    grid.addEventListener("click", async function (e) {
      const btn = e.target.closest(".tv-like-btn");
      if (!btn) return;
      e.preventDefault();

      const slug = btn.dataset.slug;
      btn.disabled = true;
      try {
        const resp = await fetch(`/api/project-like/${encodeURIComponent(slug)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        });
        const data = await resp.json();
        if (data.error) return;

        btn.classList.toggle("is-liked", data.liked);
        const countEl = btn.querySelector(".tv-like-btn__count");
        if (countEl) countEl.textContent = data.likes_count;
      } catch (err) {
        // Silently leave the button as it was - the click simply didn't take.
      } finally {
        btn.disabled = false;
      }
    });
  }

  function init() {
    initCountdown();
    initChallengeButton();
    initProjectLikes();
  }

  return { init };
})();
