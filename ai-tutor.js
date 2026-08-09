/**
 * TVAITutor - chat wiring for the AI Tutor page (ai-tutor.html).
 * Same module + CSRF pattern as js/playground.js and js/dashboard-widgets.js.
 *
 * Server contract (see webserver.py):
 *   POST /api/ai-tutor/message   JSON {message, mode}
 *                                 OR multipart/form-data {message, mode, file}
 *                              -> { reply, mode_label, attachment_url?, attachment_name?, attachment_kind? }
 *   POST /api/ai-tutor/new-chat  {} -> { conversation_id }
 */
window.TVAITutor = (function () {
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // Splits "some text ```lang\ncode\n``` more text" into a DOM fragment:
  // plain text nodes plus <pre><code> blocks, each code block followed by
  // a small Download button that saves it as a real file client-side.
  function renderBubbleContent(text) {
    const wrap = document.createElement("div");
    const codeBlockRe = /```(\w*)\n?([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;
    let blockIndex = 0;

    while ((match = codeBlockRe.exec(text)) !== null) {
      if (match.index > lastIndex) {
        const before = document.createElement("span");
        before.innerHTML = escapeHtml(text.slice(lastIndex, match.index)).replace(/\n/g, "<br>");
        wrap.appendChild(before);
      }

      const lang = (match[1] || "txt").trim();
      const code = match[2].replace(/\n$/, "");

      const codeWrap = document.createElement("div");
      codeWrap.className = "tv-tutor-code-block";

      const pre = document.createElement("pre");
      const codeEl = document.createElement("code");
      codeEl.textContent = code;
      pre.appendChild(codeEl);
      codeWrap.appendChild(pre);

      const downloadBtn = document.createElement("button");
      downloadBtn.type = "button";
      downloadBtn.className = "tv-tutor-code-download";
      downloadBtn.textContent = `⬇ Download ${lang !== "txt" ? lang : "code"}`;
      downloadBtn.addEventListener("click", function () {
        const blob = new Blob([code], { type: "text/plain" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `tutor-snippet-${blockIndex + 1}.${_extensionForLang(lang)}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      });
      codeWrap.appendChild(downloadBtn);

      wrap.appendChild(codeWrap);
      lastIndex = codeBlockRe.lastIndex;
      blockIndex += 1;
    }

    if (lastIndex < text.length) {
      const after = document.createElement("span");
      after.innerHTML = escapeHtml(text.slice(lastIndex)).replace(/\n/g, "<br>");
      wrap.appendChild(after);
    }

    return wrap;
  }

  function _extensionForLang(lang) {
    const map = {
      python: "py", py: "py", javascript: "js", js: "js", typescript: "ts",
      html: "html", css: "css", json: "json", sql: "sql", bash: "sh",
      sh: "sh", java: "java", c: "c", cpp: "cpp", yaml: "yml", xml: "xml",
    };
    return map[lang.toLowerCase()] || "txt";
  }

  function init(ids) {
    const thread = document.getElementById(ids.threadId);
    const form = document.getElementById(ids.formId);
    const input = document.getElementById(ids.inputId);
    const sendBtn = document.getElementById(ids.sendId);
    const newChatBtn = ids.newChatId ? document.getElementById(ids.newChatId) : null;
    const emptyState = ids.emptyStateId ? document.getElementById(ids.emptyStateId) : null;
    const fileInput = ids.fileInputId ? document.getElementById(ids.fileInputId) : null;
    const attachBtn = ids.attachBtnId ? document.getElementById(ids.attachBtnId) : null;
    const attachPreview = ids.attachmentPreviewId ? document.getElementById(ids.attachmentPreviewId) : null;
    const attachPreviewName = ids.attachmentPreviewNameId ? document.getElementById(ids.attachmentPreviewNameId) : null;
    const attachRemoveBtn = ids.attachmentRemoveId ? document.getElementById(ids.attachmentRemoveId) : null;
    if (!thread || !form || !input || !sendBtn) return null;

    let currentMode = "general";
    let pendingFile = null;

    function scrollToBottom() {
      thread.scrollTop = thread.scrollHeight;
    }

    function autoGrow() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 140) + "px";
    }
    input.addEventListener("input", autoGrow);

    // ---------------------------------------------------------------
    // Message rendering - one function for both history (on load) and
    // new messages (after send), so they look identical.
    // ---------------------------------------------------------------
    function appendMessage(role, text, attachment) {
      if (emptyState) emptyState.remove();

      const row = document.createElement("div");
      row.className = `tv-tutor-msg tv-tutor-msg--${role}`;

      if (role === "assistant") {
        const avatar = document.createElement("span");
        avatar.className = "tv-tutor-msg__avatar";
        avatar.textContent = "🤖";
        row.appendChild(avatar);
      }

      const bubble = document.createElement("div");
      bubble.className = "tv-tutor-msg__bubble";

      if (attachment && attachment.kind === "image" && attachment.url) {
        const img = document.createElement("img");
        img.src = attachment.url;
        img.alt = attachment.name || "Image";
        img.className = "tv-tutor-msg__image";
        bubble.appendChild(img);

        const dl = document.createElement("a");
        dl.href = attachment.url;
        dl.download = attachment.name || "image.png";
        dl.className = "tv-tutor-code-download";
        dl.textContent = "⬇ Download image";
        bubble.appendChild(dl);
      } else if (attachment && attachment.kind === "file" && attachment.url) {
        const chip = document.createElement("a");
        chip.href = attachment.url;
        chip.download = attachment.name || "file";
        chip.className = "tv-tutor-file-chip";
        chip.innerHTML = `📎 ${escapeHtml(attachment.name || "attached file")}`;
        bubble.appendChild(chip);
      }

      if (text) {
        bubble.appendChild(renderBubbleContent(text));
      }

      row.appendChild(bubble);
      thread.appendChild(row);
      scrollToBottom();
      return row;
    }

    function appendTypingIndicator() {
      const row = document.createElement("div");
      row.className = "tv-tutor-msg tv-tutor-msg--assistant";
      row.innerHTML = `
        <span class="tv-tutor-msg__avatar">🤖</span>
        <div class="tv-tutor-msg__bubble tv-tutor-msg__bubble--typing">
          <span></span><span></span><span></span>
        </div>`;
      thread.appendChild(row);
      scrollToBottom();
      return row;
    }

    // Render prior conversation history (passed from the server as JSON).
    if (ids.initialMessagesId) {
      const dataEl = document.getElementById(ids.initialMessagesId);
      if (dataEl) {
        try {
          const history = JSON.parse(dataEl.textContent || "[]");
          history.forEach((m) => {
            const attachment = m.attachment_url
              ? { url: m.attachment_url, name: m.attachment_name, kind: m.attachment_kind }
              : null;
            appendMessage(m.role, m.content, attachment);
          });
        } catch (err) {
          // Malformed history JSON - just skip rendering it rather than breaking the page.
        }
      }
    }

    // ---------------------------------------------------------------
    // File attach flow
    // ---------------------------------------------------------------
    if (attachBtn && fileInput) {
      attachBtn.addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", function () {
        const file = fileInput.files[0];
        if (!file) return;
        pendingFile = file;
        if (attachPreview && attachPreviewName) {
          attachPreviewName.textContent = `📎 ${file.name}`;
          attachPreview.hidden = false;
        }
      });
    }
    if (attachRemoveBtn) {
      attachRemoveBtn.addEventListener("click", function () {
        pendingFile = null;
        if (fileInput) fileInput.value = "";
        if (attachPreview) attachPreview.hidden = true;
      });
    }

    // ---------------------------------------------------------------
    // Sending
    // ---------------------------------------------------------------
    async function sendMessage(text) {
      const trimmed = text.trim();
      if (!trimmed && !pendingFile) return;

      const fileForThisSend = pendingFile;
      appendMessage(
        "user",
        trimmed || (fileForThisSend ? `Sent a file: ${fileForThisSend.name}` : ""),
        fileForThisSend
          ? { url: URL.createObjectURL(fileForThisSend), name: fileForThisSend.name, kind: fileForThisSend.type.startsWith("image/") ? "image" : "file" }
          : null
      );

      input.value = "";
      autoGrow();
      pendingFile = null;
      if (fileInput) fileInput.value = "";
      if (attachPreview) attachPreview.hidden = true;
      sendBtn.disabled = true;

      const typingRow = appendTypingIndicator();

      try {
        let resp;
        if (fileForThisSend) {
          const formData = new FormData();
          formData.append("message", trimmed);
          formData.append("mode", currentMode);
          formData.append("file", fileForThisSend);
          resp = await fetch("/api/ai-tutor/message", {
            method: "POST",
            headers: { "X-CSRFToken": csrfToken() },
            body: formData,
          });
        } else {
          resp = await fetch("/api/ai-tutor/message", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
            body: JSON.stringify({ message: trimmed, mode: currentMode }),
          });
        }

        const data = await resp.json();
        typingRow.remove();

        if (data.error) {
          appendMessage("assistant", `Sorry - ${data.error}`);
        } else {
          const attachment = data.attachment_url
            ? { url: data.attachment_url, name: data.attachment_name, kind: data.attachment_kind }
            : null;
          appendMessage("assistant", data.reply, attachment);
        }
      } catch (err) {
        typingRow.remove();
        appendMessage("assistant", "Couldn't reach the server. Please try again.");
      } finally {
        sendBtn.disabled = false;
        currentMode = "general"; // one-shot per quick action; typed follow-ups default back to general
        input.focus();
      }
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      sendMessage(input.value);
    });

    // Enter sends, Shift+Enter inserts a newline.
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(input.value);
      }
    });

    // Quick action / related-topic chip buttons - insert a starter prompt
    // into the input and set the active mode, but let the learner review
    // and edit before it's actually sent.
    if (ids.actionsSelector) {
      document.querySelectorAll(ids.actionsSelector).forEach((btn) => {
        btn.addEventListener("click", function () {
          currentMode = btn.dataset.mode || "general";
          const starter = (btn.dataset.starter || "").replace(/\\n/g, "\n");
          input.value = starter;
          autoGrow();
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        });
      });
    }

    if (newChatBtn) {
      newChatBtn.addEventListener("click", async function () {
        newChatBtn.disabled = true;
        try {
          await fetch("/api/ai-tutor/new-chat", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          });
          thread.innerHTML = `
            <div class="tv-tutor-msg tv-tutor-msg--assistant">
              <span class="tv-tutor-msg__avatar">🤖</span>
              <div class="tv-tutor-msg__bubble">
                New chat started. What would you like help with?
              </div>
            </div>`;
          currentMode = "general";
          pendingFile = null;
          if (fileInput) fileInput.value = "";
          if (attachPreview) attachPreview.hidden = true;
          input.value = "";
          autoGrow();
          input.focus();
        } catch (err) {
          // Leave the current thread as-is if the request fails.
        } finally {
          newChatBtn.disabled = false;
        }
      });
    }

    scrollToBottom();
    autoGrow();
  }

  return { init };
})();
