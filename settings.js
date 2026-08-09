(function () {
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.content : "";

  function showMessage(el, text, isError) {
    el.textContent = text;
    el.classList.remove("is-success", "is-error");
    el.classList.add("is-visible", isError ? "is-error" : "is-success");
  }

  const profileForm = document.getElementById("tv-profile-form");
  if (profileForm) {
    profileForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = document.getElementById("tv-profile-message");
      const username = document.getElementById("tv-settings-username").value.trim();
      try {
        const resp = await fetch("/api/settings/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({ username }),
        });
        const data = await resp.json();
        if (data.error) {
          showMessage(msg, data.error, true);
        } else {
          showMessage(msg, "Saved.", false);
        }
      } catch (err) {
        showMessage(msg, "Couldn't reach the server - try again.", true);
      }
    });
  }

  const passwordForm = document.getElementById("tv-password-form");
  if (passwordForm) {
    passwordForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = document.getElementById("tv-password-message");
      const currentPassword = document.getElementById("tv-current-password").value;
      const newPassword = document.getElementById("tv-new-password").value;
      try {
        const resp = await fetch("/api/settings/password", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
        });
        const data = await resp.json();
        if (data.error) {
          showMessage(msg, data.error, true);
        } else {
          showMessage(msg, "Password updated.", false);
          passwordForm.reset();
        }
      } catch (err) {
        showMessage(msg, "Couldn't reach the server - try again.", true);
      }
    });
  }
})();
