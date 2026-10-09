/* All JSON editor actions report their result through the shared notification. */
(() => {
  function check(field, { format = false, notifySuccess = true } = {}) {
    // Preparation accepts plain text, including an empty value.
    if (field.name === 'preparation') {
      field.removeAttribute('aria-invalid');
      return true;
    }
    try {
      const value = JSON.parse(field.value.trim() || field.dataset.jsonEmpty || '{}');
      if (format) field.value = JSON.stringify(value, null, 2);
      field.removeAttribute('aria-invalid');
      if (notifySuccess) window.notify(format ? 'JSON Formatted' : 'JSON Valid');
      return true;
    } catch (_error) {
      field.setAttribute('aria-invalid', 'true');
      field.focus();
      window.notify('Invalid JSON', 'error');
      return false;
    }
  }

  window.JSONEditor = { check };

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-json-action]');
    if (!button) return;
    const field = document.getElementById(button.dataset.jsonTarget);
    if (field) check(field, { format: button.dataset.jsonAction === 'format' });
  });

  document.addEventListener('submit', event => {
    const selector = event.target.dataset.jsonValidate;
    if (!selector) return;
    for (const field of event.target.querySelectorAll(selector)) {
      if (!check(field, { notifySuccess: false })) {
        event.preventDefault();
        return;
      }
    }
  });
})();
