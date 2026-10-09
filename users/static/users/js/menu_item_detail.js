// Both editors consume JSON encoded by Django, preserving booleans and quotes.
document.querySelectorAll('textarea[data-json-source]').forEach(ta => {
  const source = document.getElementById(ta.dataset.jsonSource);
  if (!source) return;
  try {
    const value = JSON.parse(source.textContent);
    ta.value = JSON.stringify(value ?? JSON.parse(ta.dataset.jsonEmpty || '{}'), null, 2);
  } catch (e) {
    // Preserve malformed content for correction without interrupting other editors.
    ta.value = source.textContent;
  }
});

function formatAllJSON(form){
  for (const field of form.querySelectorAll("textarea.json:not([name='preparation'])")) {
    if (!JSONEditor.check(field, { format: true, notifySuccess: false })) return;
  }
  notify('JSON Formatted');
}

document.querySelectorAll('[data-format-all-json]').forEach(button => {
  button.addEventListener('click', () => formatAllJSON(button.form));
});
