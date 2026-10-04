(function () {
  const picker = document.querySelector("[data-tenant-picker]");
  if (picker) {
    picker.addEventListener("change", function () {
      const base = picker.dataset.base;
      const query = picker.value ? "?edit=" + encodeURIComponent(picker.value) : "";
      window.location.assign(base + query + "#edit-tenant");
    });
  }

  const form = document.querySelector("[data-tenant-edit-form]");
  const savedNode = document.getElementById("tenant-edit-saved");
  if (!form || !savedNode) return;

  const saveButton = form.querySelector("[data-save-edits]");
  const dialog = document.querySelector("[data-tenant-edit-dialog]");
  const saved = JSON.parse(savedNode.textContent);
  const fields = Array.from(form.querySelectorAll("input, textarea")).filter(function (field) {
    return field.type !== "hidden";
  });

  function storedValue(field) {
    return Object.prototype.hasOwnProperty.call(saved, field.name) ? saved[field.name] : "";
  }

  function syncSaveButton() {
    const dirty = fields.some(function (field) {
      return field.value !== storedValue(field);
    });
    saveButton.disabled = !dirty;
  }

  fields.forEach(function (field) {
    field.addEventListener("input", syncSaveButton);
  });
  syncSaveButton();

  let approved = false;
  form.addEventListener("submit", function (event) {
    if (approved) return;
    event.preventDefault();
    if (!saveButton.disabled) dialog.showModal();
  });
  dialog.querySelector("[data-cancel-edits]").addEventListener("click", function () {
    dialog.close();
  });
  dialog.querySelector("[data-confirm-edits]").addEventListener("click", function () {
    approved = true;
    dialog.close();
    form.requestSubmit();
  });
})();
