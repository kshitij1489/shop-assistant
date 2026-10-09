const chatConfig = document.getElementById("chat-config").dataset;
const TENANT_SLUG = chatConfig.tenantSlug;
const TOKEN_ENDPOINT = chatConfig.tokenUrl + "?tenant=" + encodeURIComponent(TENANT_SLUG);
const CHATBOT_ENDPOINT = chatConfig.chatbotEndpoint;
const TENANT_API_KEY = chatConfig.tenantApiKey;
const TOKEN_STORAGE_KEY = `jwt_${TENANT_SLUG}`;

function storeToken(token) {
  const expiry = parseJwt(token).exp * 1000;
  localStorage.setItem(TOKEN_STORAGE_KEY, JSON.stringify({ token, expiry }));
}

function getStoredToken() {
  const item = localStorage.getItem(TOKEN_STORAGE_KEY);
  if (!item) return null;
  try {
    const { token, expiry } = JSON.parse(item);
    if (Date.now() < expiry) return token;
  } catch (_) {}
  return null;
}

function parseJwt(token) {
  try {
    const base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(atob(base64).split('').map(c =>
      '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)).join(''));
    return JSON.parse(json);
  } catch (e) {
    return {};
  }
}

async function fetchTokenIfNeeded() {
  let token = getStoredToken();
  if (!token) {
    const res = await fetch(TOKEN_ENDPOINT, {
      credentials: 'include',
      headers: {
        'X-API-KEY': TENANT_API_KEY
      }
    });
    if (!res.ok) throw new Error("Failed to get token");
    const data = await res.json();
    token = data.token;
    storeToken(token);
  }
  return token;
}

const form = document.getElementById('chat-form');
const input = document.getElementById('message-input');
const chatWindow = document.getElementById('chat-window');
const submitButton = form.querySelector('button[type="submit"]');

form.addEventListener('submit', async function (e) {
  e.preventDefault();
  if (input.disabled) return;
  const message = input.value.trim();
  if (!message) return;

  appendMessage('user', message);
  input.value = '';
  input.disabled = true;
  submitButton.disabled = true;
  const botMessage = appendMessage('bot', 'Thinking…');
  document.getElementById('reply-announcement').textContent = '';
  botMessage.setAttribute('aria-busy', 'true');
  let streamedText = '';

  try {
    const token = await fetchTokenIfNeeded();
    const response = await fetch(CHATBOT_ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'Accept': typeof ReadableStream === 'undefined' ? 'application/json' : 'text/event-stream',
        'Authorization': `Bearer ${token}`
      },
      body: JSON.stringify({ message })
    });

    if (!response.ok) {
      throw new Error(`Server responded with status ${response.status}`);
    }

    const contentType = response.headers.get("Content-Type") || "";
    let data;
    if (contentType.includes('text/event-stream')) {
      data = await readChatStream(response, (event, payload) => {
        if (event === 'replace') streamedText = payload.text;
        if (event === 'delta') streamedText += payload.text;
        if (event !== 'done') {
          // Partial URLs stay plain text until the complete reply arrives.
          botMessage.textContent = streamedText || 'Thinking…';
          chatWindow.scrollTop = chatWindow.scrollHeight;
        }
      });
    } else if (contentType.includes('application/json')) {
      data = await response.json();
    } else {
      throw new Error('Unexpected response type');
    }
    const reply = data.response || "Sorry, I didn't get that.";
    renderMessage(botMessage, reply);
  } catch (err) {
    console.error("Chat error:", err);
    renderMessage(botMessage, 'The reply could not be completed. Please check your basket before trying again.');
  } finally {
    botMessage.removeAttribute('aria-busy');
    document.getElementById('reply-announcement').textContent = botMessage.textContent;
    input.disabled = false;
    submitButton.disabled = false;
    input.focus();
  }
});

function appendMessage(role, text) {
  const msg = document.createElement('div');
  msg.className = `message ${role}`;
  chatWindow.appendChild(msg);
  renderMessage(msg, text);
  return msg;
}

function renderMessage(msg, text) {
  msg.replaceChildren();
  // Render user/model text literally; only HTTP(S) URLs become links.
  const parts = text.split(/(https?:\/\/[^\s]+)/g);
  for (const part of parts) {
    if (/^https?:\/\//.test(part)) {
      const link = document.createElement('a');
      link.href = part;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = part;
      msg.appendChild(link);
    } else {
      msg.appendChild(document.createTextNode(part));
    }
  }
  chatWindow.scrollTop = chatWindow.scrollHeight;
}
