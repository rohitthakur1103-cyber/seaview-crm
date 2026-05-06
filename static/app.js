document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.classList.add("js-enabled");

  const dropzones = document.querySelectorAll("[data-dropzone]");
  const copyButtons = document.querySelectorAll("[data-copy-url]");
  const qrCards = document.querySelectorAll("[data-qr-card]");
  const qrDownloadButtons = document.querySelectorAll("[data-qr-download]");

  const qrImageUrl = (targetUrl, size = 360) =>
    `https://api.qrserver.com/v1/create-qr-code/?size=${size}x${size}&format=png&data=${encodeURIComponent(targetUrl)}`;

  for (const dropzone of dropzones) {
    const input = dropzone.querySelector("[data-dropzone-input]");
    const fileName = dropzone.querySelector("[data-file-name]");
    if (!input || !fileName) {
      continue;
    }

    const updateFileName = () => {
      const selectedFile = input.files && input.files[0];
      fileName.textContent = selectedFile ? selectedFile.name : "No file chosen";
      dropzone.classList.toggle("is-filled", Boolean(selectedFile));
      dropzone.classList.remove("is-error");
    };

    const openPicker = () => input.click();

    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPicker();
      }
    });

    input.addEventListener("change", updateFileName);

    const activateDragState = (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragover");
      dropzone.classList.remove("is-error");
    };

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, activateDragState);
    });

    ["dragleave", "dragend", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
      });
    });

    dropzone.addEventListener("drop", (event) => {
      const droppedFiles = Array.from(event.dataTransfer?.files || []);
      const importFile = droppedFiles.find((file) => {
        const name = file.name.toLowerCase();
        return (
          name.endsWith(".csv") ||
          name.endsWith(".xlsx") ||
          file.type.includes("csv") ||
          file.type.includes("spreadsheet") ||
          file.type === ""
        );
      });

      if (!importFile) {
        fileName.textContent = "Only CSV or Excel files are supported";
        dropzone.classList.add("is-error");
        return;
      }

      if (typeof DataTransfer !== "undefined") {
        const transfer = new DataTransfer();
        transfer.items.add(importFile);
        input.files = transfer.files;
      } else {
        input.files = event.dataTransfer.files;
      }
      updateFileName();
    });

    updateFileName();
  }

  document.querySelectorAll("[data-import-submit-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const submitter = event.submitter || form.querySelector("button[type='submit']");
      if (submitter?.disabled) {
        return;
      }

      form.classList.add("is-submitting");
      form.setAttribute("aria-busy", "true");
      form.querySelectorAll("[data-import-loading]").forEach((note) => {
        note.hidden = false;
      });
      if (submitter) {
        const loadingLabel = submitter.getAttribute("data-loading-label");
        if (loadingLabel) {
          submitter.dataset.originalLabel = submitter.textContent || "";
          submitter.textContent = loadingLabel;
        }
      }
      form.querySelectorAll("button").forEach((button) => {
        button.disabled = true;
        button.classList.add("is-loading");
      });
    });
  });

  const importStatusShell = document.querySelector("[data-import-run-status]");
  if (importStatusShell) {
    const statusUrl = importStatusShell.getAttribute("data-status-url");
    const pollSeconds = Number(importStatusShell.getAttribute("data-poll-seconds") || "3");
    const pollMs = Math.max(pollSeconds, 1) * 1000;
    let stopped = importStatusShell.getAttribute("data-import-active") !== "true";
    let pollTimer = null;
    let connectionErrors = 0;

    const formatNumber = (value) =>
      Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });

    const setText = (selector, value) => {
      const target = importStatusShell.querySelector(selector);
      if (target) {
        target.textContent = value == null ? "" : String(value);
      }
    };

    const setHtml = (selector, value) => {
      const target = importStatusShell.querySelector(selector);
      if (target) {
        target.innerHTML = value || "";
      }
    };

    const updateImportStatus = (data) => {
      if (!data || !data.ok) {
        return;
      }

      connectionErrors = 0;
      stopped = !data.is_active;
      importStatusShell.dataset.importActive = data.is_active ? "true" : "false";
      importStatusShell.dataset.importStatus = data.status || "";
      importStatusShell.classList.toggle("is-import-complete", data.status === "completed");
      importStatusShell.classList.toggle("is-import-failed", data.status === "failed");

      const progressPanel = importStatusShell.querySelector("[data-import-progress-panel]");
      if (progressPanel) {
        progressPanel.setAttribute("aria-busy", data.is_active ? "true" : "false");
      }

      setText("[data-import-status-title]", data.status_title);
      setText("[data-import-source]", data.source_label);
      setText("[data-import-filename]", data.filename);
      setHtml("[data-import-status-pill]", data.status_pill_html);
      setText("[data-import-progress-copy]", data.progress_copy);
      setText("[data-import-safety-copy]", data.safety_copy);
      setText("[data-import-percent]", `${data.percent || 0}%`);
      setText("[data-import-rows-processed]", formatNumber(data.rows_processed));
      setText("[data-import-rows-total]", `of ${formatNumber(data.rows_total)} rows`);
      setText("[data-import-created]", formatNumber(data.customers_created));
      setText("[data-import-updated]", formatNumber(data.customers_updated));
      setText("[data-import-review]", formatNumber(data.review_needed_rows));
      setText("[data-import-skipped]", formatNumber(data.skipped_rows));
      setText("[data-import-eta]", data.eta_text);
      setText("[data-import-completion-time]", data.completion_text);
      setText("[data-import-speed]", data.speed_text);

      const progressTrack = importStatusShell.querySelector("[data-import-progress-track]");
      if (progressTrack) {
        progressTrack.setAttribute("aria-valuenow", String(data.percent || 0));
      }
      const progressBar = importStatusShell.querySelector("[data-import-progress-bar]");
      if (progressBar) {
        window.requestAnimationFrame(() => {
          progressBar.style.width = `${Math.max(0, Math.min(Number(data.percent || 0), 100))}%`;
        });
      }

      setHtml("[data-import-stage-list]", data.stage_html);
      setHtml("[data-import-error]", data.error_html);
      setHtml("[data-import-completion-panel]", data.business_summary_html);

      if (data.status === "completed") {
        setText(
          "[data-import-helper-copy]",
          "Import complete. Seaview customer intelligence, dashboard metrics, and next-action views are ready to use."
        );
        document.title = "Import complete";
      } else if (data.status === "failed") {
        setText(
          "[data-import-helper-copy]",
          "The import stopped before finishing. Review the error below, then return to imports when you are ready to retry."
        );
        document.title = "Import failed";
      } else {
        document.title = `${data.percent || 0}% import progress`;
      }
    };

    const schedulePoll = (delay = pollMs) => {
      if (stopped || !statusUrl) {
        return;
      }
      window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(pollImportStatus, delay);
    };

    async function pollImportStatus() {
      if (stopped || !statusUrl) {
        return;
      }
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`Status request failed: ${response.status}`);
        }
        const data = await response.json();
        updateImportStatus(data);
      } catch {
        connectionErrors += 1;
        setText(
          "[data-import-helper-copy]",
          connectionErrors > 1
            ? "Still checking import progress. The server may be busy saving a batch, but this page will keep trying without reloading."
            : "Checking import progress again. This page stays open while the server finishes the import."
        );
      } finally {
        schedulePoll(connectionErrors ? Math.min(pollMs * 2, 8000) : pollMs);
      }
    }

    schedulePoll(900);
  }

  for (const button of copyButtons) {
    const originalLabel = button.textContent;
    button.addEventListener("click", async () => {
      const targetPath = button.getAttribute("data-copy-url");
      if (!targetPath) {
        return;
      }

      const fullUrl = new URL(targetPath, window.location.origin).toString();
      try {
        await navigator.clipboard.writeText(fullUrl);
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = originalLabel;
        }, 1400);
      } catch {
        const card = button.closest(".public-qr-card, .qr-location-card");
        const source = card?.querySelector("[data-copy-source]");
        if (source) {
          source.focus();
          source.select();
          button.textContent = "Selected";
          window.setTimeout(() => {
            button.textContent = originalLabel;
          }, 1600);
        } else {
          window.prompt("Copy this link:", fullUrl);
        }
      }
    });
  }

  for (const card of qrCards) {
    const path = card.getAttribute("data-qr-path");
    const image = card.querySelector("[data-qr-image]");
    const target = card.querySelector("[data-qr-target]");
    if (!path || !image || !target) {
      continue;
    }

    const fullUrl = new URL(path, window.location.origin).toString();
    image.src = qrImageUrl(fullUrl);
    image.loading = "lazy";
    target.textContent = fullUrl;
  }

  for (const button of qrDownloadButtons) {
    const originalLabel = button.textContent;
    button.addEventListener("click", async () => {
      const path = button.getAttribute("data-qr-path");
      const filename = button.getAttribute("data-qr-name") || "seaview-qr.png";
      if (!path) {
        return;
      }

      const fullUrl = new URL(path, window.location.origin).toString();
      const qrUrl = qrImageUrl(fullUrl, 720);

      try {
        const response = await fetch(qrUrl);
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(objectUrl);
        button.textContent = "Downloaded";
      } catch {
        window.open(qrUrl, "_blank", "noopener,noreferrer");
        button.textContent = "Opened";
      }

      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1600);
    });
  }

  // 1. TAB SWITCHING
  const tabTriggers = document.querySelectorAll("[data-tab-trigger]");
  const tabPanels = document.querySelectorAll("[data-tab-panel]");
  const TAB_STORAGE_KEY = "seaview_marketing_tab";

  function activateTab(tabName) {
    tabTriggers.forEach((t) =>
      t.classList.toggle("tab-active", t.dataset.tabTrigger === tabName)
    );
    tabPanels.forEach((p) =>
      p.classList.toggle("tab-active", p.dataset.tabPanel === tabName)
    );
    try { sessionStorage.setItem(TAB_STORAGE_KEY, tabName); } catch {}
  }

  tabTriggers.forEach((trigger) => {
    trigger.addEventListener("click", () =>
      activateTab(trigger.dataset.tabTrigger)
    );
  });

  if (tabTriggers.length) {
    let stored = "";
    try { stored = sessionStorage.getItem(TAB_STORAGE_KEY) || ""; } catch {}
    const first = tabTriggers[0].dataset.tabTrigger;
    const requested =
      window.location.hash.replace("#", "") ||
      new URLSearchParams(window.location.search).get("tab") ||
      "";
    const requestedValid = Array.from(tabTriggers).some((t) => t.dataset.tabTrigger === requested);
    const valid = Array.from(tabTriggers).some((t) => t.dataset.tabTrigger === stored);
    activateTab(requestedValid ? requested : valid ? stored : first);
  }

  // 2. COLLAPSIBLE PANELS
  document.querySelectorAll("[data-collapsible-trigger]").forEach((trigger) => {
    const targetId = trigger.dataset.collapsibleTrigger;
    const target = document.getElementById(targetId);
    if (!target) return;
    const labelOpen = trigger.dataset.labelOpen || "Hide";
    const labelClosed = trigger.dataset.labelClosed || trigger.textContent;
    trigger.addEventListener("click", () => {
      const open = target.classList.toggle("is-open");
      trigger.textContent = open ? labelOpen : labelClosed;
    });
  });

  // 3. QR FORM INLINE SUBMISSION
  const qrForm = document.querySelector("[data-qr-form]");
  const qrConfirm = document.querySelector("[data-qr-confirm]");
  if (qrForm && qrConfirm) {
    qrForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const data = new FormData(qrForm);
      const params = new URLSearchParams(data);
      try {
        const res = await fetch(qrForm.action, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "fetch",
          },
          body: params.toString(),
        });
        const json = await res.json();
        if (json.success) {
          qrForm.style.display = "none";
          qrConfirm.classList.add("visible");
          qrConfirm.scrollIntoView({ behavior: "smooth" });
        } else {
          const errEl = qrForm.querySelector("[data-form-error]");
          if (errEl) {
            errEl.textContent = json.error || "Something went wrong.";
            errEl.style.display = "block";
          }
        }
      } catch {
        qrForm.submit();
      }
    });
  }

  // 4. OPTIONAL FIELDS TOGGLE
  document.querySelectorAll("[data-optional-toggle]").forEach((btn) => {
    const target = document.querySelector("[data-optional-fields]");
    if (!target) return;
    btn.addEventListener("click", () => {
      const open = target.classList.toggle("visible");
      btn.textContent = open ? "Less ▴" : "Tell us more (optional) ▾";
    });
  });

  // 5. FILTER BAR active state
  const filterLinks = document.querySelectorAll(".filter-link");
  const currentFilter =
    new URLSearchParams(window.location.search).get("filter") || "all";
  filterLinks.forEach((link) => {
    const linkFilter =
      new URLSearchParams(new URL(link.href, window.location.origin).search).get(
        "filter"
      ) || "all";
    link.classList.toggle("active", linkFilter === currentFilter);
  });

  // 6. MINOR FLASH TOASTS
  const MINOR_MESSAGES = [
    "Task completed",
    "Note saved",
    "Customer updated",
    "Campaign saved",
    "Captured contact saved",
    "Import canceled",
    "Thanks. Your contact",
    "marked sent",
  ];
  const msgParam = new URLSearchParams(window.location.search).get("message");
  const toast = document.getElementById("toast");
  if (
    msgParam &&
    toast &&
    MINOR_MESSAGES.some((message) => msgParam.startsWith(message))
  ) {
    const flash = document.querySelector(".flash");
    if (flash) flash.style.display = "none";
    toast.textContent = msgParam;
    toast.classList.remove("hidden");
    requestAnimationFrame(() => toast.classList.add("visible"));
    setTimeout(() => {
      toast.classList.remove("visible");
      setTimeout(() => toast.classList.add("hidden"), 220);
    }, 3000);
  }

  // 7. AI SUBMIT LOADING STATE
  document.querySelectorAll("[data-ai-submit-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const submitter = event.submitter;
      const loadingNote = form.querySelector("[data-ai-loading]");
      const buttons = form.querySelectorAll("button, a.button");

      buttons.forEach((button) => {
        if (button.tagName === "BUTTON") {
          button.disabled = true;
        }
        button.classList.add("is-disabled");
      });

      if (submitter && submitter.tagName === "BUTTON") {
        const loadingLabel =
          submitter.getAttribute("data-loading-label") || "Generating...";
        submitter.dataset.originalLabel = submitter.textContent;
        submitter.textContent = loadingLabel;
        submitter.classList.add("is-loading");
      }

      if (loadingNote) {
        loadingNote.hidden = false;
      }
    });
  });
});
