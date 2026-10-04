// ---------------- Basket -----------------
const basketItems = [];
function formatMinor(amount, exponent) {
  const negative = amount < 0;
  const digits = String(Math.abs(amount)).padStart((exponent || 0) + 1, "0");
  const text = exponent ? digits.slice(0, -exponent) + "." + digits.slice(-exponent) : digits;
  return (negative ? "-" : "") + text;
}
function moneyPrefix(currency) {
  return currency === "INR" ? "₹" : (currency ? currency + " " : "");
}
function computeTotals(items) {
  const currency = items.length ? items[0].currency : "";
  const exponent = items.length ? items[0].exponent : 2;
  const subtotalMinor = items.reduce((sum, item) => sum + item.lineTotalMinor, 0);
  return { currency, exponent, subtotalMinor };
}

// --- parse metadata (robust) ---
function parseMetaToItems(meta) {
  if (!meta) return [];
  // already an array
  if (Array.isArray(meta)) return meta;
  // object with common keys
  if (typeof meta === "object") return Array.isArray(meta.items) ? meta.items : (Array.isArray(meta.metadata) ? meta.metadata : []);
  // string: try to extract a JSON-like array/object (handles Python single-quotes)
  if (typeof meta === "string") {
    // try to find a bracketed array/object substring
    const m = meta.match(/\[[\s\S]*?\]|\{[\s\S]*?\}/);
    if (m) {
      let s = m[0].trim()
        // normalize single-quotes -> double-quotes for JSON parse
        .replace(/'/g, '"')
        // turn bareword keys into quoted keys: foo: -> "foo":
        .replace(/([{,]\s*)([A-Za-z0-9_]+)\s*:/g, '$1"$2":');
      try { return JSON.parse(s); } catch (e) { /* fallthrough */ }
    }
  }
  return [];
}

// --- map metadata items -> basket item shape and replace basketItems in-place ---
function updateBasketFromMeta(meta) {
  const items = parseMetaToItems(meta);
  if (!items || !items.length) return; // nothing to do

  const mapped = items.map(it => {
    const qty = Number(it.quantity ?? it.qty ?? 1);
    return {
      name: it.name ?? it.title ?? "Item",
      qty,
      size: it.size ?? it.variant ?? "",
      currency: it.currency,
      exponent: it.exponent,
      lineTotalMinor: it.line_total_minor
    };
  });

  // replace basketItems contents (basketItems is const but mutable)
  basketItems.splice(0, basketItems.length, ...mapped);
  renderBasket();
}

function renderBasket() {
  const body = document.getElementById("basketBody");
  body.innerHTML = "";
  basketItems.forEach(it => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${it.name}</td>
      <td>${it.qty}</td>
      <td>${it.size}</td>
      <td class="va-right">${moneyPrefix(it.currency)}${formatMinor(it.lineTotalMinor, it.exponent)}</td>
      <td class="va-right">At checkout</td>
    `;
    body.appendChild(tr);
  });
  const totals = computeTotals(basketItems);
  document.getElementById("basketTotal").textContent =
    `${moneyPrefix(totals.currency)}${formatMinor(totals.subtotalMinor, totals.exponent)}`;
  const taxNoteEl = document.getElementById("taxNote");
  if (taxNoteEl) taxNoteEl.textContent = "Taxes and fees are calculated at checkout.";
}
renderBasket();


const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const micBtn = document.getElementById("micBtn");
const statusEl = document.getElementById("status");
const errorEl = document.getElementById("errorMsg");
const liveTextEl = document.getElementById("liveText");
const chatLog = document.getElementById("chatLog");
const autoLoop = document.getElementById("autoLoop");

const CFG = window.VA_CONFIG || {};
const WEBHOOK_URL = CFG.webhookUrl || "/agent_core/voice/";
const POLL_URL = CFG.pollUrl || "/tenant/api/voice/messages/";
const TENANT_ID = CFG.tenantId || "";
const CHAT_ID = CFG.chatId || "";

// Polling state
let lastRenderedCount = 0;      // how many messages we've already shown
let pollHandle = null;

// put near other DOM lookups
const langSelect = document.getElementById("langSelect");

// helper to read current UI language (fallback to browser)
function getUiLang() {
  return (langSelect?.value || navigator.language || "en-US");
}

// ---------- DEDUPE/OPTIMISTIC LOGGING ----------
// Update logMsg to accept an `optimistic` flag and store message text on the DOM node
function logMsg(who, text, optimistic = false) {
  const div = document.createElement("div");
  div.className = `va-msg ${who === "You" ? "you" : "bot"}`;
  div.innerHTML = `<span class="who">${who}:</span> ${escapeHtml(text || "")}`;
  // store text for dedupe checks and mark optimistic writes
  div.dataset.text = text || "";
  if (optimistic) div.dataset.optimistic = "true";
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Simple helper to pull transcript and render new messages
async function pollMessages() {
  try {
    const url = new URL(POLL_URL, window.location.origin);
    url.searchParams.set("chat_id", CHAT_ID);
    url.searchParams.set("limit", "200");

    const res = await fetch(url.toString(), { method: "GET", credentials: "same-origin" });
    const data = await res.json();
    updateBasketFromMeta(data.metadata);
    const messages = Array.isArray(data.messages) ? data.messages : [];

    // Render only the new tail since last render
    for (let i = lastRenderedCount; i < messages.length; i++) {
      const m = messages[i];

      // If this is an "in" (user) message and the last appended node is an optimistic
      // node with the same text, consider it the same message and skip adding a duplicate.
      if (m.dir === "in") {
        const last = chatLog.lastElementChild;
        if (last && last.dataset && last.dataset.optimistic === "true" && (last.dataset.text || "") === (m.text || "")) {
          // server confirmed it — clear optimistic marker and skip adding duplicate
          delete last.dataset.optimistic;
          continue;
        }
      }

      // directions used by your processor: "in" (user), "out" (bot), "owner" (panel)
      if (m.dir === "in") logMsg("You", m.text || "");
      else if (m.dir === "out") logMsg("Assistant", m.text || "");
      else if (m.dir === "owner") logMsg("Owner", m.text || "");
    }
    lastRenderedCount = messages.length;
  } catch (e) {
    // quiet failures are fine during idle
  }
}

// Start polling loop (every 2s)
function ensurePolling() {
  if (pollHandle) return;
  pollHandle = setInterval(pollMessages, 2000);
}
ensurePolling();
pollMessages(); // prime once

if (!SpeechRecognition) {
  errorEl.textContent = "Your browser doesn't support the Web Speech API (try Chrome/Edge).";
  errorEl.style.display = "block";
}

let recognition;
let recognizing = false;

// Timers/thresholds
const GAP_MS = 5000;        // 5s gap sends to server
const IDLE_MS = 30000;      // 30s idle ends session
let gapTimer = null;
let idleTimer = null;

let bufferFinal = "";       // Accumulate final transcripts to send
let lastInterim = "";       // Show interim to user

function resetGapTimer() {
  clearTimeout(gapTimer);
  gapTimer = setTimeout(sendIfBuffer, GAP_MS);
}
function resetIdleTimer() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(stopListeningDueToIdle, IDLE_MS);
}

function stopListeningDueToIdle() {
  if (recognition && recognizing) {
    recognition.onend = null; // we'll handle UI ourselves
    recognition.stop();
  }
  recognizing = false;
  micBtn.classList.remove("listening", "processing");
  statusEl.textContent = "Idle timeout — click the mic to interact";
}

function ensureRecognizer() {
  if (recognition) return;
  recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = getUiLang();

  recognition.onstart = () => {
    recognizing = true;
    statusEl.textContent = "Listening...";
    errorEl.style.display = "none";
    micBtn.classList.add("listening");
    resetGapTimer();
    resetIdleTimer();
  };

  recognition.onerror = (e) => {
    errorEl.textContent = `Speech error: ${e.error}`;
    errorEl.style.display = "block";
  };

  recognition.onresult = (event) => {
    resetGapTimer();
    resetIdleTimer();

    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const res = event.results[i];
      const txt = res[0].transcript;
      if (res.isFinal) {
        bufferFinal += (bufferFinal ? " " : "") + txt.trim();
      } else {
        interim += txt;
      }
    }
    lastInterim = interim;
    if (liveTextEl) liveTextEl.textContent = interim || "";
  };

  recognition.onend = () => {
    micBtn.classList.remove("listening");
    recognizing = false;
    if (autoLoop?.checked && statusEl.textContent.startsWith("Listening")) {
      startListening();
    }
  };
}

function startListening() {
  if (!SpeechRecognition) return;
  ensureRecognizer();
  try {
    bufferFinal = "";
    lastInterim = "";
    if (liveTextEl) liveTextEl.textContent = "";
    recognition.start();
    statusEl.textContent = "Listening...";
    micBtn.classList.add("listening");
    resetGapTimer();
    resetIdleTimer();
  } catch (_) {}
}

function stopListening() {
  if (recognition && recognizing) {
    recognition.stop();
  }
  recognizing = false;
  micBtn.classList.remove("listening");
  micBtn.classList.add("processing");
  statusEl.textContent = "Processing...";
  clearTimeout(gapTimer);
  clearTimeout(idleTimer);
}

// No speech for 5s → send whatever we have
async function sendIfBuffer() {
  if (!bufferFinal && !lastInterim) {
    resetGapTimer();
    return;
  }

  const message = (bufferFinal || lastInterim || "").trim();
  bufferFinal = "";
  lastInterim = "";
  if (liveTextEl) liveTextEl.textContent = "";

  // Pause recognition while we talk to server
  stopListening();

  // Optimistically show user's message but mark it so poller can dedupe.
  logMsg("You", message, true);

  try {
    // POST to your webhook (async queue)
    const uiLang = getUiLang();
    const res = await fetch(WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.VA_CONFIG.csrfToken },
      credentials: "same-origin",
      body: JSON.stringify({
        tenant_id: TENANT_ID,
        chat_id: CHAT_ID,
        ui_lang:   uiLang,
        message: { text: message }
      })
    });

    // Expect {"status":"queued","chat_id":"..."}
    await res.json();

    // Start/continue polling; when the bot writes "out" messages they'll appear
    ensurePolling();

    // We’ll also speak the last new "out" message when it arrives (see observer below)

  } catch (err) {
    errorEl.textContent = "Error talking to server";
    errorEl.style.display = "block";
    micBtn.classList.remove("processing");
    statusEl.textContent = "Click the mic to start";
    // if POST failed, clear optimistic mark so poller won't suppress real server messages later
    const last = chatLog.lastElementChild;
    if (last && last.dataset && last.dataset.optimistic === "true") {
      delete last.dataset.optimistic;
    }
  }
}

// Observe new bot messages and TTS the latest one
let lastSpokenIdx = -1;
async function speakLatestBotLineIfAny() {
  try {
    const url = new URL(POLL_URL, window.location.origin);
    url.searchParams.set("chat_id", CHAT_ID);
    url.searchParams.set("limit", "200");
    const res = await fetch(url.toString(), { method: "GET", credentials: "same-origin" });
    const data = await res.json();
    const messages = Array.isArray(data.messages) ? data.messages : [];
    // Find last "out" message
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].dir === "out") {
        if (i !== lastSpokenIdx) {
          lastSpokenIdx = i;
          speakReply(messages[i].text || "");
        }
        break;
      }
    }
  } catch (e) {}
}
setInterval(speakLatestBotLineIfAny, 1500);

// UI
micBtn.addEventListener("click", () => {
  if (!SpeechRecognition) return;
  if (recognizing) {
    // Manual stop
    stopListeningDueToIdle();
  } else {
    startListening();
  }
});

// ---------- Natural TTS helpers (less robotic) ----------
const voiceSelect = document.getElementById("voiceSelect"); // optional
const rateCtl = document.getElementById("rateCtl");         // optional
const pitchCtl = document.getElementById("pitchCtl");       // optional

let availableVoices = [];
let selectedVoice = null;

function loadVoices() {
  availableVoices = speechSynthesis.getVoices();

  // Prefer high-quality voices (adjust list to your locale if needed)
  const preferredOrder = [
    "Google US English", "Google UK English Female", "Google UK English Male",
    "Google en-IN", "Microsoft", "Samantha", "Daniel", "Karen", "Veena"
  ];

  // Sort with preferred at top
  const sorted = [...availableVoices].sort((a, b) => {
    const an = a.name.toLowerCase(), bn = b.name.toLowerCase();
    const ap = preferredOrder.findIndex(p => an.includes(p.toLowerCase()));
    const bp = preferredOrder.findIndex(p => bn.includes(p.toLowerCase()));
    return (ap === -1 ? 999 : ap) - (bp === -1 ? 999 : bp) || an.localeCompare(bn);
  });

  // Populate select if present
  if (voiceSelect) {
    voiceSelect.innerHTML = "";
    sorted.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v.name;
      opt.textContent = `${v.name} — ${v.lang}`;
      voiceSelect.appendChild(opt);
    });
  }

  selectedVoice = sorted[0] || null;
  if (voiceSelect && selectedVoice) voiceSelect.value = selectedVoice.name;
}

// Load voices (some browsers async)
if (typeof speechSynthesis !== "undefined") {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
}

langSelect?.addEventListener("change", () => {
  if (recognition) {
    try { recognition.stop(); } catch {}
    recognition.lang = getUiLang();
    if (recognizing) startListening();
  }
});

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

// Speak with micro-pauses between chunks
function speakReply(text) {
  if (!text) return;

  const chunks = chunkText(text);
  const baseRate = parseFloat(rateCtl?.value || "0.95");   // slightly slower than default
  const basePitch = parseFloat(pitchCtl?.value || "1.05"); // slightly brighter

  let idx = 0;

  const speakNext = () => {
    if (idx >= chunks.length) {
      onReplySpeechEnded();
      return;
    }

    const utter = new SpeechSynthesisUtterance(chunks[idx]);
    if (selectedVoice) utter.voice = selectedVoice;

    const { rate, pitch } = prosodyForChunk(chunks[idx], baseRate, basePitch);
    utter.rate = rate;
    utter.pitch = pitch;
    utter.volume = 1;

    utter.onend = () => {
      // Micro pause between chunks
      setTimeout(() => { idx++; speakNext(); }, 160);
    };
    utter.onerror = () => {
      setTimeout(() => { idx++; speakNext(); }, 80);
    };

    // Clear any stuck queue and speak
    if (speechSynthesis.speaking && idx === 0) speechSynthesis.cancel();
    speechSynthesis.speak(utter);
  };

  speakNext();
}

// Resume listening after TTS (respects autoLoop)
function onReplySpeechEnded() {
  micBtn.classList.remove("processing");
  statusEl.textContent = autoLoop?.checked ? "Listening..." : "Click the mic to start";
  if (autoLoop?.checked) startListening();
  else recognizing = false;
}
