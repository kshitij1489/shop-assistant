// Basket values are catalog data; render them as text.
const basketItems = [];
function formatMinor(amount, exponent = 2) {
  const negative = amount < 0;
  const digits = String(Math.abs(amount)).padStart(exponent + 1, "0");
  return (negative ? "-" : "") + (exponent ? digits.slice(0, -exponent) + "." + digits.slice(-exponent) : digits);
}
function moneyPrefix(currency) {
  return currency === "INR" ? "₹" : (currency ? currency + " " : "");
}
function computeTotals(items) {
  return {
    currency: items[0]?.currency || "", exponent: items[0]?.exponent ?? 2,
    subtotalMinor: items.reduce((sum, item) => sum + item.lineTotalMinor, 0),
  };
}
function parseMetaToItems(meta) {
  if (Array.isArray(meta)) return meta;
  if (meta && typeof meta === "object") {
    return Array.isArray(meta.items) ? meta.items : (Array.isArray(meta.metadata) ? meta.metadata : []);
  }
  if (typeof meta === "string") {
    try { return parseMetaToItems(JSON.parse(meta)); } catch (_) {}
    const match = meta.match(/\[[\s\S]*\]|\{[\s\S]*\}/);
    if (match) {
      try {
        return parseMetaToItems(JSON.parse(match[0].replace(/'/g, '"')
          .replace(/([{,]\s*)([A-Za-z0-9_]+)\s*:/g, '$1"$2":')));
      } catch (_) {}
    }
  }
  return [];
}
function updateBasketFromMeta(meta) {
  // Missing metadata means no update; an explicit empty basket clears the table.
  if (meta == null) return;
  const mapped = parseMetaToItems(meta).map(item => ({
    name: item.name ?? item.title ?? "Item", qty: Number(item.quantity ?? item.qty ?? 1),
    size: item.size ?? item.variant ?? "", currency: item.currency, exponent: item.exponent ?? 2,
    lineTotalMinor: Number(item.line_total_minor) || 0,
  }));
  basketItems.splice(0, basketItems.length, ...mapped);
  renderBasket();
}
function renderBasket() {
  const body = document.getElementById("basketBody");
  body.replaceChildren();
  basketItems.forEach(item => {
    const row = document.createElement("tr");
    const values = [item.name, item.qty, item.size,
      moneyPrefix(item.currency) + formatMinor(item.lineTotalMinor, item.exponent), "At checkout"];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      if (index >= 3) cell.className = "va-right";
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  const totals = computeTotals(basketItems);
  document.getElementById("basketTotal").textContent =
    moneyPrefix(totals.currency) + formatMinor(totals.subtotalMinor, totals.exponent);
}
renderBasket();

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const micBtn = document.getElementById("micBtn");
const statusEl = document.getElementById("status");
const liveTextEl = document.getElementById("liveText");
const chatLog = document.getElementById("chatLog");
const autoLoop = document.getElementById("autoLoop");
const langSelect = document.getElementById("langSelect");
const CFG = window.VA_CONFIG || {};
const WEBHOOK_URL = CFG.webhookUrl || "/agent_core/voice/";
const POLL_URL = CFG.pollUrl || "/tenant/api/voice/messages/";
const TENANT_ID = CFG.tenantId || "";
const CHAT_ID = CFG.chatId || "";
function getUiLang() { return langSelect?.value || navigator.language || "en-US"; }

let previousMessageKeys = null;
let pollHandle = null;
let pollInFlight = false;
function logMsg(who, text, optimistic = false) {
  const div = document.createElement("div");
  div.className = `va-msg ${who === "You" ? "you" : "bot"}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who + ":";
  div.appendChild(label);
  div.appendChild(document.createTextNode(" " + (text || "")));
  div.dataset.text = text || "";
  if (optimistic) div.dataset.optimistic = "true";
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}
function messageKey(message) {
  return message.id || JSON.stringify([message.dir, message.ts, message.text, message.meta]);
}
function overlapLength(previous, current) {
  if (!previous) return 0;
  // Match the retained suffix even as the server trims its 200-message window.
  for (let size = Math.min(previous.length, current.length); size > 0; size--) {
    if (previous.slice(-size).every((key, index) => key === current[index])) return size;
  }
  return 0;
}
async function pollMessages() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const url = new URL(POLL_URL, window.location.origin);
    url.searchParams.set("chat_id", CHAT_ID);
    url.searchParams.set("limit", "200");
    const response = await fetch(url.toString(), { credentials: "same-origin" });
    if (!response.ok) throw new Error("Messages unavailable");
    const data = await response.json();
    updateBasketFromMeta(data.metadata);
    const messages = Array.isArray(data.messages) ? data.messages : [];
    const keys = messages.map(messageKey);
    const initial = previousMessageKeys === null;
    const fresh = messages.slice(overlapLength(previousMessageKeys, keys));
    previousMessageKeys = keys;
    for (const message of fresh) {
      if (message.dir === "in") {
        const optimistic = Array.from(chatLog.children).find(node =>
          node.dataset.optimistic === "true" && node.dataset.text === (message.text || ""));
        if (optimistic) { delete optimistic.dataset.optimistic; continue; }
      }
      const who = { in: "You", out: "Assistant", owner: "Owner" }[message.dir];
      if (who) logMsg(who, message.text || "");
    }
    const replies = fresh.filter(message => message.dir === "out").map(message => message.text || "");
    if (!initial && sessionActive && replies.length) speakReply(replies.join("\n"));
  } catch (_) {
    // The next poll retries transient failures without discarding the cursor.
  } finally {
    pollInFlight = false;
  }
}
function ensurePolling() {
  if (!pollHandle) pollHandle = setInterval(pollMessages, 2000);
}

let recognition;
let recognizing = false;
let starting = false;
let listeningRequested = false;
let sessionActive = false;
let sessionGeneration = 0;
let speechGeneration = 0;
let sendController = null;
const GAP_MS = 5000;
const IDLE_MS = 30000;
let gapTimer = null;
let idleTimer = null;
let bufferFinal = "";
let lastInterim = "";
function clearListeningTimers() {
  clearTimeout(gapTimer);
  clearTimeout(idleTimer);
  gapTimer = idleTimer = null;
}
function resetGapTimer() {
  clearTimeout(gapTimer);
  gapTimer = setTimeout(() => { gapTimer = null; sendIfBuffer(); }, GAP_MS);
}
function resetIdleTimer() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => stopListeningDueToIdle(), IDLE_MS);
}
function stopListeningDueToIdle(message = "Idle timeout — click the mic to interact") {
  sessionActive = listeningRequested = starting = false;
  sessionGeneration++;
  speechGeneration++;
  clearListeningTimers();
  bufferFinal = lastInterim = "";
  if (liveTextEl) liveTextEl.textContent = "";
  sendController?.abort();
  if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
  if (recognition && recognizing) recognition.stop();
  recognizing = false;
  micBtn.classList.remove("listening", "processing");
  statusEl.textContent = message;
}
function ensureRecognizer() {
  if (recognition) return;
  recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = getUiLang();
  recognition.onstart = () => {
    starting = false;
    if (!listeningRequested) { recognition.stop(); return; }
    recognizing = true;
    statusEl.textContent = "Listening...";
    micBtn.classList.add("listening");
  };
  recognition.onerror = event => {
    if (event.error === "no-speech" || event.error === "aborted") return;
    stopListeningDueToIdle("Click the mic to start");
    notify(event.error === "not-allowed" ? "Microphone Access Denied" : "Speech Recognition Failed", "error");
  };
  recognition.onresult = event => {
    if (!listeningRequested || !sessionActive) return;
    resetGapTimer();
    resetIdleTimer();
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index++) {
      const result = event.results[index];
      if (result.isFinal) bufferFinal += (bufferFinal ? " " : "") + result[0].transcript.trim();
      else interim += result[0].transcript;
    }
    lastInterim = interim;
    if (liveTextEl) liveTextEl.textContent = interim;
  };
  recognition.onend = () => {
    starting = recognizing = false;
    micBtn.classList.remove("listening");
    if (listeningRequested && sessionActive && autoLoop?.checked) startListening();
  };
}
function startListening() {
  if (!SpeechRecognition || recognizing || starting) return;
  ensureRecognizer();
  sessionActive = listeningRequested = true;
  recognition.lang = getUiLang();
  try {
    starting = true;
    recognition.start();
    statusEl.textContent = "Listening...";
    micBtn.classList.add("listening");
    if (gapTimer === null) resetGapTimer();
    if (idleTimer === null) resetIdleTimer();
  } catch (_) { starting = false; }
}
function stopListening() {
  listeningRequested = false;
  clearListeningTimers();
  if (recognition && (recognizing || starting)) recognition.stop();
  starting = recognizing = false;
  micBtn.classList.remove("listening");
  micBtn.classList.add("processing");
  statusEl.textContent = "Processing...";
}
async function sendIfBuffer() {
  if (!sessionActive || !listeningRequested) return;
  const message = (bufferFinal || lastInterim).trim();
  if (!message) { resetGapTimer(); return; }
  bufferFinal = lastInterim = "";
  if (liveTextEl) liveTextEl.textContent = "";
  stopListening();
  const generation = sessionGeneration;
  const optimistic = logMsg("You", message, true);
  const removeUnconfirmedLine = () => {
    if (optimistic.dataset.optimistic === 'true') optimistic.remove();
  };
  const controller = typeof AbortController === "undefined" ? null : new AbortController();
  sendController = controller;
  try {
    const response = await fetch(WEBHOOK_URL, {
      method: "POST", credentials: "same-origin", signal: controller?.signal,
      headers: { "Content-Type": "application/json", "X-CSRFToken": CFG.csrfToken },
      body: JSON.stringify({ tenant_id: TENANT_ID, chat_id: CHAT_ID, ui_lang: getUiLang(), message: { text: message } }),
    });
    if (!response.ok) throw new Error("Message request failed");
    await response.json();
    if (generation !== sessionGeneration) { removeUnconfirmedLine(); return; }
    ensurePolling();
  } catch (_) {
    if (generation !== sessionGeneration) { removeUnconfirmedLine(); return; }
    delete optimistic.dataset.optimistic;
    sessionActive = false;
    notify("Message Not Sent", "error");
    micBtn.classList.remove("processing");
    statusEl.textContent = "Click the mic to start";
  } finally {
    if (sendController === controller) sendController = null;
  }
}
micBtn.addEventListener("click", () => {
  if (!SpeechRecognition) return;
  if (sessionActive) stopListeningDueToIdle("Click the mic to start");
  else startListening();
});
if (!SpeechRecognition) notify("Speech Recognition Unavailable", "error");
langSelect?.addEventListener("change", () => {
  const resume = listeningRequested;
  stopListeningDueToIdle("Click the mic to start");
  if (recognition) recognition.lang = getUiLang();
  if (resume) startListening();
});

const voiceSelect = document.getElementById("voiceSelect");
const rateCtl = document.getElementById("rateCtl");
const pitchCtl = document.getElementById("pitchCtl");
let availableVoices = [];
let selectedVoice = null;
function voiceKey(voice) { return voice.voiceURI || voice.name; }
function loadVoices() {
  const previous = selectedVoice && voiceKey(selectedVoice);
  const preferredOrder = ["Google US English", "Google UK English Female", "Google UK English Male",
    "Google en-IN", "Microsoft", "Samantha", "Daniel", "Karen", "Veena"];
  const priority = voice => {
    const index = preferredOrder.findIndex(name => voice.name.toLowerCase().includes(name.toLowerCase()));
    return index < 0 ? 999 : index;
  };
  availableVoices = [...speechSynthesis.getVoices()].sort((a, b) =>
    priority(a) - priority(b) || a.name.localeCompare(b.name));
  selectedVoice = availableVoices.find(voice => voiceKey(voice) === previous) || availableVoices[0] || null;
  if (voiceSelect) {
    voiceSelect.replaceChildren();
    availableVoices.forEach(voice => {
      const option = document.createElement("option");
      option.value = voiceKey(voice);
      option.textContent = `${voice.name} — ${voice.lang}`;
      voiceSelect.appendChild(option);
    });
    if (selectedVoice) voiceSelect.value = voiceKey(selectedVoice);
  }
}
voiceSelect?.addEventListener("change", () => {
  selectedVoice = availableVoices.find(voice => voiceKey(voice) === voiceSelect.value) || null;
});
if (typeof speechSynthesis !== "undefined") {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
}

// Split long text into natural clauses and sentences
function chunkText(text, maxLen = 180) {
  // Split on sentence endings but keep punctuation
  const parts = text.split(/([.!?]+)\s+/).reduce((acc, cur, idx, arr) => {
    if (idx % 2 === 0) {
      const ender = arr[idx + 1] || "";
      const piece = (cur + (ender || "")).trim();
      if (piece) acc.push(piece);
    }
    return acc;
  }, []);

  const chunks = [];
  for (const p of (parts.length ? parts : [text])) {
    if (p.length <= maxLen) {
      chunks.push(p);
      continue;
    }
    // Further split long sentences on commas/semicolons/colons
    let buf = "";
    p.split(/([,;:]\s+)/).forEach(seg => {
      const tryStr = buf + seg;
      if (tryStr.length > maxLen && buf) {
        chunks.push(buf.trim());
        buf = seg.trim();
      } else {
        buf = tryStr;
      }
    });
    if (buf.trim()) chunks.push(buf.trim());
  }
  return chunks;
}

function prosodyForChunk(chunk, baseRate, basePitch) {
  let rate = baseRate, pitch = basePitch;

  // Gentle prosody tweaks
  if (/\?\s*$/.test(chunk)) {
    pitch = Math.min(basePitch + 0.08, 1.3);
    rate  = Math.max(baseRate - 0.03, 0.8);
  } else if (/!\s*$/.test(chunk)) {
    pitch = Math.min(basePitch + 0.06, 1.3);
    rate  = Math.min(baseRate + 0.04, 1.2);
  } else if (/,/.test(chunk)) {
    rate = Math.max(baseRate - 0.02, 0.8);
  }

  return { rate, pitch };
}

// A cancelled session invalidates queued speech chunks and automatic restarts.
function speakReply(text) {
  if (!text || typeof speechSynthesis === "undefined") { onReplySpeechEnded(); return; }
  stopListening();
  const generation = ++speechGeneration;
  speechSynthesis.cancel();
  const chunks = chunkText(text);
  const baseRate = parseFloat(rateCtl?.value || "0.95");
  const basePitch = parseFloat(pitchCtl?.value || "1.05");
  let index = 0;
  function speakNext() {
    if (generation !== speechGeneration || !sessionActive) return;
    if (index >= chunks.length) { onReplySpeechEnded(); return; }
    const utterance = new SpeechSynthesisUtterance(chunks[index]);
    if (selectedVoice) utterance.voice = selectedVoice;
    const prosody = prosodyForChunk(chunks[index], baseRate, basePitch);
    utterance.rate = prosody.rate;
    utterance.pitch = prosody.pitch;
    utterance.volume = 1;
    utterance.onend = () => setTimeout(() => { index++; speakNext(); }, 160);
    utterance.onerror = () => setTimeout(() => { index++; speakNext(); }, 80);
    speechSynthesis.resume();
    speechSynthesis.speak(utterance);
  }
  // Give the browser a turn to flush cancel() before starting the new utterance.
  setTimeout(speakNext, 0);
}
function onReplySpeechEnded() {
  if (!sessionActive) return;
  micBtn.classList.remove("processing");
  statusEl.textContent = "Click the mic to start";
  if (autoLoop?.checked) startListening();
  else sessionActive = false;
}
window.addEventListener("pagehide", () => {
  clearInterval(pollHandle);
  pollHandle = null;
  stopListeningDueToIdle("Click the mic to start");
});
window.addEventListener("pageshow", () => { ensurePolling(); pollMessages(); });
ensurePolling();
pollMessages();
