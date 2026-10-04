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

function refreshUI(){
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
    b.addEventListener("click", () => { selectedIntent = intent; selectedSubIntent = ""; refreshUI(); });
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
    b.addEventListener("click", () => { selectedSubIntent = si; refreshUI(); });
    subIntentTabsEl.appendChild(b);
  });


  // Show payload
  let current = null;
  if (selectedDtype && selectedIntent && selectedSubIntent) {
    current = DOCS.find(d => d.dtype === selectedDtype && d.intent === selectedIntent && d.sub_intent === selectedSubIntent) || null;
  }
  payloadTA.value = current ? JSON.stringify(current.payload, null, 2) : "";

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

// Utility actions
function formatJSON(){
  try {
    const obj = JSON.parse(payloadTA.value || "{}");
    payloadTA.value = JSON.stringify(obj, null, 2);
    alert("Formatted.");
  } catch(e) {
    alert("Invalid JSON: " + e.message);
  }
}
function validateJSON(){
  try { JSON.parse(payloadTA.value || "{}"); alert("Valid JSON."); }
  catch(e){ alert("Invalid JSON: " + e.message); }
}

// Wire buttons under textarea
document.getElementById("btnFormat").addEventListener("click", formatJSON);
document.getElementById("btnValidate").addEventListener("click", validateJSON);

// Update form submit flow (replaces onsubmit="return ...")
document.getElementById("updateForm").addEventListener("submit", (e) => {
  if (!selectedDtype || !selectedIntent || !selectedSubIntent) {
    e.preventDefault();
    alert("Select dtype, intent and sub_intent first.");
    return;
  }
  try {
    JSON.parse(payloadTA.value || "{}");
  } catch(e2) {
    e.preventDefault();
    alert("JSON is invalid: " + e2.message);
    return;
  }
  if (!confirm("Save changes to this entry?")) {
    e.preventDefault();
    return;
  }
  updP.value = payloadTA.value;
});

// Delete form submit flow
document.getElementById("deleteForm").addEventListener("submit", (e) => {
  if (!selectedDtype || !selectedIntent || !selectedSubIntent) {
    e.preventDefault();
    alert("Select dtype, intent and sub_intent first.");
    return;
  }
  if (!confirm("Delete this entry? This cannot be undone.")) e.preventDefault();
});

// Modal open/close + validation
const addModal = document.getElementById("addModal");
const openAddModalBtn = document.getElementById("openAddModal");
const addForm = document.getElementById("addForm");
const addPayloadTA = document.getElementById("add_payload");

function openAddModal() {
  document.getElementById("add_dtype").value =
    selectedDtype || (document.getElementById("add_dtype").options[0]?.value || "");
  addModal.style.display = "flex";
  addModal.setAttribute("aria-hidden", "false");
  document.getElementById("add_dtype").focus();
}
function closeAddModal() {
  addModal.style.display = "none";
  addModal.setAttribute("aria-hidden", "true");
  openAddModalBtn.focus();
}

addModal.addEventListener("keydown", event => {
  if (event.key === "Escape") { event.preventDefault(); closeAddModal(); }
  if (event.key !== "Tab") return;
  const controls = Array.from(addModal.querySelectorAll('button, input, select, textarea'))
    .filter(el => !el.disabled && el.type !== 'hidden');
  const first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
});
openAddModalBtn.addEventListener("click", openAddModal);
addModal.addEventListener("click", (e)=>{ if (e.target === addModal) closeAddModal(); });
document.getElementById("btnAddCancel").addEventListener("click", closeAddModal);

function formatJSONField(ta){
  try { ta.value = JSON.stringify(JSON.parse(ta.value || "{}"), null, 2); alert("Formatted."); }
  catch(e){ alert("Invalid JSON: " + e.message); }
}
function validateJSONField(ta){
  try { JSON.parse(ta.value || "{}"); alert("Valid JSON."); }
  catch(e){ alert("Invalid JSON: " + e.message); }
}

document.getElementById("btnAddFormat").addEventListener("click", () => formatJSONField(addPayloadTA));
document.getElementById("btnAddValidate").addEventListener("click", () => validateJSONField(addPayloadTA));

addForm.addEventListener("submit", (e) => {
  try { JSON.parse(addPayloadTA.value || "{}"); }
  catch(err) {
    e.preventDefault();
    alert("JSON is invalid: " + err.message);
  }
});

// Initial paint
refreshUI();
