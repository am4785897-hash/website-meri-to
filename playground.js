/**
 * TVPlayground - shared Code Playground wiring.
 *
 * Used by the dashboard AND every course lesson page - one shared module
 * so a fix or upgrade here applies everywhere at once instead of hunting
 * down duplicated copies.
 *
 * Usage (see dashboard.html / python-lesson.html for real examples):
 *
 *   TVPlayground.init({
 *     codeId: "tv-playground-code",
 *     gutterId: "tv-playground-gutter",
 *     stdinId: "tv-playground-stdin",
 *     outputId: "tv-playground-output",
 *     langId: "tv-playground-lang",
 *     runId: "tv-playground-run",
 *     resetId: "tv-playground-reset",
 *   });
 *
 * All ids are optional except codeId/outputId/runId - pass only the ids
 * that exist on your page (e.g. a lesson page might not have a language
 * <select>, since only Python runs today).
 *
 * v2 additions (all editor-experience only - /api/run-code itself is
 * unchanged): real Tab-key indenting, Ctrl/Cmd+Enter to run, auto-continued
 * indent on Enter, a Copy and a Download-as-.py button (auto-injected next
 * to Run/Reset, no template changes needed), an execution timer, and
 * stdout/stderr rendered as separate, distinctly colored blocks instead of
 * one flat text dump.
 */
window.TVPlayground = (function () {
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function init(ids) {
    const codeEl = document.getElementById(ids.codeId);
    const gutterEl = ids.gutterId ? document.getElementById(ids.gutterId) : null;
    const stdinEl = ids.stdinId ? document.getElementById(ids.stdinId) : null;
    const outputEl = document.getElementById(ids.outputId);
    const langEl = ids.langId ? document.getElementById(ids.langId) : null;
    const runBtn = document.getElementById(ids.runId);
    const resetBtn = ids.resetId ? document.getElementById(ids.resetId) : null;
    if (!codeEl || !outputEl || !runBtn) return null;

    const DEFAULT_CODE = codeEl.value;
    const DEFAULT_STDIN = stdinEl ? stdinEl.value : "";
    const DEFAULT_OUTPUT_HTML = outputEl.innerHTML;
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = csrfMeta ? csrfMeta.content : "";
    const INDENT = "    ";

    // ---- Line-number gutter ----
    function renderGutter() {
      if (!gutterEl) return;
      const lineCount = codeEl.value.split("\n").length;
      let lines = "";
      for (let i = 1; i <= lineCount; i++) lines += i + "\n";
      gutterEl.textContent = lines;
    }

    function syncGutterScroll() {
      if (gutterEl) gutterEl.scrollTop = codeEl.scrollTop;
    }

    codeEl.addEventListener("input", renderGutter);
    codeEl.addEventListener("scroll", syncGutterScroll);
    renderGutter();

    // ---- Real editor behavior: Tab to indent, Enter to auto-continue
    // indentation, Ctrl/Cmd+Enter to run - the things a plain <textarea>
    // doesn't give you for free. ----
    codeEl.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        run();
      } else if (e.key === "Tab") {
        e.preventDefault();
        const start = codeEl.selectionStart;
        const end = codeEl.selectionEnd;
        if (start !== end && codeEl.value.slice(start, end).includes("\n")) {
          // Multi-line selection: indent/outdent every selected line.
          const lineStart = codeEl.value.lastIndexOf("\n", start - 1) + 1;
          const block = codeEl.value.slice(lineStart, end);
          const indented = block
            .split("\n")
            .map((line) => (e.shiftKey ? line.replace(new RegExp("^" + INDENT), "") : INDENT + line))
            .join("\n");
          codeEl.value = codeEl.value.slice(0, lineStart) + indented + codeEl.value.slice(end);
          codeEl.selectionStart = lineStart;
          codeEl.selectionEnd = lineStart + indented.length;
        } else {
          codeEl.value = codeEl.value.slice(0, start) + INDENT + codeEl.value.slice(end);
          codeEl.selectionStart = codeEl.selectionEnd = start + INDENT.length;
        }
        renderGutter();
      } else if (e.key === "Enter") {
        const start = codeEl.selectionStart;
        const lineStart = codeEl.value.lastIndexOf("\n", start - 1) + 1;
        const currentLine = codeEl.value.slice(lineStart, start);
        const leadingWhitespace = (currentLine.match(/^\s*/) || [""])[0];
        const extra = /:\s*$/.test(currentLine) ? INDENT : ""; // auto-indent after `if x:` etc.
        e.preventDefault();
        const insert = "\n" + leadingWhitespace + extra;
        codeEl.value = codeEl.value.slice(0, start) + insert + codeEl.value.slice(codeEl.selectionEnd);
        codeEl.selectionStart = codeEl.selectionEnd = start + insert.length;
        renderGutter();
      }
    });

    // ---- Auto-injected toolbar: Copy + Download, next to Run/Reset ----
    const toolbar = runBtn.parentElement;
    if (toolbar) {
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "tv-btn tv-btn--ghost tv-btn--sm";
      copyBtn.textContent = "Copy";
      copyBtn.addEventListener("click", async function () {
        try {
          await navigator.clipboard.writeText(codeEl.value);
          const original = copyBtn.textContent;
          copyBtn.textContent = "Copied!";
          setTimeout(() => (copyBtn.textContent = original), 1200);
        } catch (err) {
          codeEl.select();
          document.execCommand("copy");
        }
      });

      const downloadBtn = document.createElement("button");
      downloadBtn.type = "button";
      downloadBtn.className = "tv-btn tv-btn--ghost tv-btn--sm";
      downloadBtn.textContent = "Download";
      downloadBtn.addEventListener("click", function () {
        const blob = new Blob([codeEl.value], { type: "text/x-python" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "playground.py";
        a.click();
        URL.revokeObjectURL(url);
      });

      toolbar.appendChild(copyBtn);
      toolbar.appendChild(downloadBtn);
    }

    // ---- Run ----
    function renderOutput(result) {
      const ms = result.ms;
      const timeLabel = typeof ms === "number" ? `<span class="tv-output__timing">${ms} ms</span>` : "";
      if (result.error) {
        outputEl.innerHTML = `${timeLabel}<span class="tv-output__stderr">${escapeHtml(result.error)}</span>`;
        return;
      }
      const blocks = [];
      if (result.stdout) blocks.push(`<span class="tv-output__stdout">${escapeHtml(result.stdout)}</span>`);
      if (result.stderr) blocks.push(`<span class="tv-output__stderr">${escapeHtml(result.stderr)}</span>`);
      outputEl.innerHTML = timeLabel + (blocks.join("\n") || '<span class="tv-output__muted">(no output)</span>');
    }

    async function run() {
      outputEl.innerHTML = '<span class="tv-output__muted">Running...</span>';
      runBtn.disabled = true;
      const startedAt = performance.now();
      try {
        const resp = await fetch("/api/run-code", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({
            code: codeEl.value,
            stdin: stdinEl ? stdinEl.value : "",
            language: langEl ? langEl.value : "Python",
          }),
        });
        const data = await resp.json();
        const ms = Math.round(performance.now() - startedAt);
        renderOutput(Object.assign({}, data, { ms }));
      } catch (err) {
        renderOutput({ error: "Couldn't reach the server. Please try again." });
      } finally {
        runBtn.disabled = false;
      }
    }

    runBtn.addEventListener("click", run);

    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        codeEl.value = DEFAULT_CODE;
        if (stdinEl) stdinEl.value = DEFAULT_STDIN;
        outputEl.innerHTML = DEFAULT_OUTPUT_HTML;
        renderGutter();
      });
    }

    return { run, renderGutter };
  }

  return { init };
})();
