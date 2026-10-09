// Read docs from the JSON script (safe under CSP)
const DOCS = JSON.parse(document.getElementById("docs_json").textContent);

// State
let selectedDtype = document.getElementById("dtypeSelect").value || "";
let selectedIntent = "";
let selectedSubIntent = "";

const intentTabsEl = document.getElementById("intentTabs");
const subIntentTabsEl = document.getElementById("subIntentTabs");
const payloadTA = document.getElementById("payloadTA");
const selectionPath = document.getElementById("selectionPath");

// Forms hidden inputs
const updD = document.getElementById("update_dtype");
const updI = document.getElementById("update_intent");
const updS = document.getElementById("update_sub_intent");
const updP = document.getElementById("update_payload");

const delD = document.getElementById("delete_dtype");
const delI = document.getElementById("delete_intent");
const delS = document.getElementById("delete_sub_intent");

function unique(list){ return Array.from(new Set(list)); }

const drafts = new Map();
let loadedPath = null;
let savedPayload = '';
let submittingEntry = false;
function rememberDraft() {
  if (loadedPath === null) return;
  if (payloadTA.value === savedPayload) drafts.delete(loadedPath);
  else drafts.set(loadedPath, payloadTA.value);
}
function updateDraftStatus() {
  submittingEntry = false;
  rememberDraft();
  document.getElementById('draftStatus').textContent = drafts.has(loadedPath)
    ? 'Unsaved changes. Switching entries keeps your edits until you leave this page.' : '';
}
payloadTA.addEventListener('input', updateDraftStatus);
window.addEventListener('beforeunload', event => {
  if (submittingEntry) return;
  rememberDraft();
  if (!drafts.size) return;
  event.preventDefault();
  event.returnValue = '';
});

const intentController = UIComponents.tabs(intentTabsEl, { onSelect: button => {
  if (selectedIntent === button.dataset.value) return;
  selectedIntent = button.dataset.value;
  selectedSubIntent = '';
  refreshUI();
}});
const topicController = UIComponents.tabs(subIntentTabsEl, { onSelect: button => {
  if (selectedSubIntent === button.dataset.value) return;
  selectedSubIntent = button.dataset.value;
  refreshUI();
}});

function refreshUI(){
  rememberDraft();
  const focusedGroup = document.activeElement?.closest('[role="tablist"]')?.id;
  const filtered = selectedDtype ? DOCS.filter(d => d.dtype === selectedDtype) : [];

  // Build intents
  const intents = unique(filtered.map(d => d.intent)).sort();
  if (!intents.includes(selectedIntent)) selectedIntent = intents[0] || "";
  intentTabsEl.innerHTML = "";
  intents.forEach(intent => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tab" + (intent === selectedIntent ? " active" : "");
    b.textContent = intent;
    b.dataset.value = intent;
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-controls', 'knowledge-topics-panel');
    intentTabsEl.appendChild(b);
  });


  // Build sub-intents
  const subFiltered = filtered.filter(d => d.intent === selectedIntent);
  const subIntents = unique(subFiltered.map(d => d.sub_intent)).sort();
  if (!subIntents.includes(selectedSubIntent)) selectedSubIntent = subIntents[0] || "";
  subIntentTabsEl.innerHTML = "";
  subIntents.forEach(si => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tab" + (si === selectedSubIntent ? " active" : "");
    b.textContent = si;
    b.dataset.value = si;
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-controls', 'knowledge-payload-panel');
    subIntentTabsEl.appendChild(b);
  });


  // Show payload
  let current = null;
  if (selectedDtype && selectedIntent && selectedSubIntent) {
    current = DOCS.find(d => d.dtype === selectedDtype && d.intent === selectedIntent && d.sub_intent === selectedSubIntent) || null;
  }
  loadedPath = current ? JSON.stringify([selectedDtype, selectedIntent, selectedSubIntent]) : null;
  savedPayload = current ? JSON.stringify(current.payload, null, 2) : '';
  payloadTA.value = drafts.get(loadedPath) ?? savedPayload;
  updateDraftStatus();
  const intentButton = Array.from(intentTabsEl.children).find(button => button.dataset.value === selectedIntent);
  const topicButton = Array.from(subIntentTabsEl.children).find(button => button.dataset.value === selectedSubIntent);
  intentController.select(intentButton, false);
  topicController.select(topicButton, false);
  if (focusedGroup === intentTabsEl.id) intentButton?.focus();
  if (focusedGroup === subIntentTabsEl.id) topicButton?.focus();

  // Path + hidden inputs
  const path = selectedDtype ? `${selectedDtype} › ${selectedIntent || "—"} › ${selectedSubIntent || "—"}` : "—";
  selectionPath.textContent = path;

  updD.value = delD.value = selectedDtype || "";
  updI.value = delI.value = selectedIntent || "";
  updS.value = delS.value = selectedSubIntent || "";
}

// Dropdown change
document.getElementById("dtypeSelect").addEventListener("change", (e) => {
  selectedDtype = e.target.value || "";
  selectedIntent = "";
  selectedSubIntent = "";
  refreshUI();
});

// Wire buttons under textarea
document.getElementById("btnFormat").addEventListener("click", () => {
  JSONEditor.check(payloadTA, { format: true });
  updateDraftStatus();
});
document.getElementById("btnValidate").addEventListener("click", () => JSONEditor.check(payloadTA));

// Update form submit flow (replaces onsubmit="return ...")
document.getElementById("updateForm").addEventListener("submit", (e) => {
  if (!selectedDtype || !selectedIntent || !selectedSubIntent) {
    e.preventDefault();
    notify("Select a Knowledge Entry", "warning");
    return;
  }
  if (!JSONEditor.check(payloadTA, { notifySuccess: false })) {
    e.preventDefault();
    return;
  }
  if (!confirm("Save changes to this entry?")) {
    e.preventDefault();
    return;
  }
  rememberDraft();
  if (Array.from(drafts.keys()).some(key => key !== loadedPath) &&
      !confirm('Other entries have unsaved edits. Save this entry and discard those other edits?')) {
    e.preventDefault();
    return;
  }
  updP.value = payloadTA.value;
  submittingEntry = true;
});

// Delete form submit flow
document.getElementById("deleteForm").addEventListener("submit", (e) => {
  if (!selectedDtype || !selectedIntent || !selectedSubIntent) {
    e.preventDefault();
    notify("Select a Knowledge Entry", "warning");
    return;
  }
  if (!confirm("Delete this entry? This cannot be undone.")) { e.preventDefault(); return; }
  rememberDraft();
  if (Array.from(drafts.keys()).some(key => key !== loadedPath) &&
      !confirm('Other entries have unsaved edits. Delete this entry and discard those other edits?')) {
    e.preventDefault();
    return;
  }
  submittingEntry = true;
});

// Modal open/close + validation
const addModal = document.getElementById("addModal");
const openAddModalBtn = document.getElementById("openAddModal");
const addForm = document.getElementById("addForm");
const addPayloadTA = document.getElementById("add_payload");
const addDialog = UIComponents.dialog(addModal);

function openAddModal() {
  document.getElementById("add_dtype").value =
    selectedDtype || (document.getElementById("add_dtype").options[0]?.value || "");
  addDialog.open(openAddModalBtn);
  document.getElementById("add_dtype").focus();
}
function closeAddModal() {
  addDialog.close();
}

openAddModalBtn.addEventListener("click", openAddModal);
document.getElementById("btnAddCancel").addEventListener("click", closeAddModal);

document.getElementById("btnAddFormat").addEventListener("click", () => JSONEditor.check(addPayloadTA, { format: true }));
document.getElementById("btnAddValidate").addEventListener("click", () => JSONEditor.check(addPayloadTA));

addForm.addEventListener("submit", (e) => {
  if (!JSONEditor.check(addPayloadTA, { notifySuccess: false })) {
    e.preventDefault();
  }
});

// Initial paint
refreshUI();
