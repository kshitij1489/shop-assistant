function confirmAndValidate(){
  const ta = document.getElementById("json_blob");
  if (!JSONEditor.check(ta, { notifySuccess: false })) return false;
  return confirm("Save all entries? This will upsert rows for the selected dtype.");
}

document.getElementById('uploadForm').addEventListener('submit', event => {
  if (!confirmAndValidate()) event.preventDefault();
});
