document.addEventListener("DOMContentLoaded", () => {
  const dropzones = document.querySelectorAll("[data-dropzone]");
  const copyButtons = document.querySelectorAll("[data-copy-url]");

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
      const csvFile = droppedFiles.find((file) => {
        const name = file.name.toLowerCase();
        return name.endsWith(".csv") || file.type.includes("csv") || file.type === "";
      });

      if (!csvFile) {
        fileName.textContent = "Only CSV files are supported";
        dropzone.classList.add("is-error");
        return;
      }

      if (typeof DataTransfer !== "undefined") {
        const transfer = new DataTransfer();
        transfer.items.add(csvFile);
        input.files = transfer.files;
      } else {
        input.files = event.dataTransfer.files;
      }
      updateFileName();
    });

    updateFileName();
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
        window.prompt("Copy this link:", fullUrl);
      }
    });
  }
});
