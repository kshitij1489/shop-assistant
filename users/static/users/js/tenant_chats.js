const chatsConfig = document.getElementById("chats-config").dataset;
const csrfToken = (function(){
    const name = 'csrftoken=';
    const parts = document.cookie.split(';');
    for (let p of parts) { p = p.trim(); if (p.startsWith(name)) return decodeURIComponent(p.substring(name.length)); }
    return '';
  })();

  let selectedChat = null;
  let channel = 'telegram';
  let globalEnabled = true;
  let selectionVersion = 0;
  let messageRequest = 0;
  let listRequest = 0;
  let globalStatusRequest = 0;
  let transcriptSignature = null;
  let togglePending = false;
  let sendPending = false;

  async function fetchJSON(url) {
    const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error('Request failed');
    return await res.json();
  }

  async function postAction(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      const error = new Error(data.error || 'Action failed');
      error.delivered = data.delivered === true;
      throw error;
    }
    return data;
  }

  async function listChats() {
    const version = ++listRequest;
    const data = await fetchJSON(chatsConfig.listUrl + '?channel=' + channel);
    if (version !== listRequest) return;
    const list = document.getElementById('chat-list');
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const existing = new Map(Array.from(list.children).map(node => [node.dataset.chatId, node]));
    const retained = new Set();
    data.chats.forEach((c, index) => {
      const key = String(c.chat_id);
      retained.add(key);
      let div = existing.get(key);
      if (!div) {
        div = document.createElement('div');
        div.dataset.chatId = key;
        div.tabIndex = 0;
        div.setAttribute('role', 'button');
        div.innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div><strong class="chat-name"></strong><div class="chat-meta chat-phone"></div></div>
            <span class="pill"></span>
          </div>
          <div class="chat-meta chat-preview"></div>
          <div class="chat-meta chat-time"></div>`;
      }
      const active = selectedChat && String(selectedChat.chat_id) === key;
      div.className = 'chat-item' + (active ? ' active' : '');
      div.setAttribute('aria-pressed', String(!!active));
      div.querySelector('.chat-name').textContent = c.display_name || 'Guest';
      div.querySelector('.chat-phone').textContent = c.phone || '';
      div.querySelector('.chat-preview').textContent = (c.last_text || '').substring(0, 100);
      div.querySelector('.chat-time').textContent = c.last_activity_ts ? new Date(c.last_activity_ts * 1000).toLocaleString() : '';
      const agent = c.agent_enabled !== '0';
      const pill = div.querySelector('.pill');
      pill.className = 'pill ' + (agent ? 'on' : 'off');
      pill.textContent = agent ? 'ON' : 'OFF';
      div.chatData = c;
      // Remote changes arrive through this refresh. Apply them to the open
      // chat's switch too, unless this page's own toggle has not finished.
      if (active && !togglePending) {
        selectedChat.agent_enabled = c.agent_enabled;
        document.getElementById('toggle-agent').setAttribute('aria-checked', String(agent));
      }
      div.onclick = () => selectChat(div.chatData).catch(() => notify('Chat Unavailable', 'error'));
      div.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          div.onclick();
        }
      };
      if (list.children[index] !== div) list.insertBefore(div, list.children[index] || null);
    });
    existing.forEach((node, key) => { if (!retained.has(key)) node.remove(); });
    if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  }

  async function selectChat(c) {
    const changed = !selectedChat || String(selectedChat.chat_id) !== String(c.chat_id);
    selectedChat = c;
    selectionVersion++;
    if (changed) {
      document.getElementById('transcript').replaceChildren();
      transcriptSignature = null;
      document.getElementById('owner-text').value = '';
    }
    document.getElementById('chat-title').textContent = c.display_name || 'Guest';
    document.getElementById('chat-subtitle').textContent = c.phone ? ('+' + c.phone) : '';

    const btn = document.getElementById('toggle-agent');
    btn.disabled = !globalEnabled || togglePending;
    const enabled = c.agent_enabled !== '0';
    btn.setAttribute('aria-checked', String(enabled));

    document.getElementById('owner-text').disabled = false;
    document.getElementById('send-owner').disabled = sendPending;

    await Promise.all([loadMessages(), listChats()]);
  }

  async function loadMessages() {
    if (!selectedChat) return;
    const selection = selectionVersion;
    const request = ++messageRequest;
    const chatId = String(selectedChat.chat_id);
    const url = chatsConfig.messagesUrl + '?channel=' + channel + '&chat_id=' + encodeURIComponent(chatId);
    const data = await fetchJSON(url);
    if (selection !== selectionVersion || request !== messageRequest) return;
    const signature = JSON.stringify(data.messages);
    if (signature === transcriptSignature) return;
    const transcript = document.getElementById('transcript');
    const follow = transcriptSignature === null || transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 48;
    const oldTop = transcript.scrollTop;
    const oldNodes = Array.from(transcript.children);
    const existing = new Map(oldNodes.map(node => [node.dataset.messageKey, node]));
    const viewportTop = transcript.getBoundingClientRect().top;
    const anchor = oldNodes.find(node => node.getBoundingClientRect().bottom > viewportTop);
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - viewportTop : 0;
    const occurrences = new Map();
    const retained = new Set();
    data.messages.forEach((message, index) => {
      const base = message.id || JSON.stringify([message.dir, message.ts, message.text]);
      const occurrence = (occurrences.get(base) || 0) + 1;
      occurrences.set(base, occurrence);
      const key = JSON.stringify([base, occurrence]);
      retained.add(key);
      let node = existing.get(key);
      if (!node) {
        node = document.createElement('div');
        node.className = 'msg ' + (message.dir || 'in');
        node.dataset.messageKey = key;
        const bubble = document.createElement('div');
        bubble.className = 'bubble';
        bubble.textContent = message.text || '';
        node.appendChild(bubble);
      }
      if (transcript.children[index] !== node) transcript.insertBefore(node, transcript.children[index] || null);
    });
    existing.forEach((node, key) => { if (!retained.has(key)) node.remove(); });
    transcriptSignature = signature;
    if (follow) transcript.scrollTop = transcript.scrollHeight;
    else transcript.scrollTop = anchor?.isConnected
      ? transcript.scrollTop + anchor.getBoundingClientRect().top - viewportTop - anchorOffset : oldTop;
  }

  async function toggleAgent() {
    if (!selectedChat || togglePending || !globalEnabled) return;
    const btn = document.getElementById('toggle-agent');
    const currentlyOn = btn.getAttribute('aria-checked') === 'true';
    const chat = selectedChat;
    togglePending = true;
    btn.disabled = true;
    try {
      const data = await postAction(chatsConfig.toggleUrl, {
        channel, chat_id: chat.chat_id, enabled: !currentlyOn
      });
      chat.agent_enabled = data.enabled ? '1' : '0';
      // Selection and polling can replace the chat object while this request
      // is pending. The confirmed setting belongs to the chat ID, not the object.
      if (String(selectedChat?.chat_id) === String(chat.chat_id)) {
        selectedChat.agent_enabled = chat.agent_enabled;
        btn.setAttribute('aria-checked', String(!!data.enabled));
      }
      const row = Array.from(document.getElementById('chat-list').children)
        .find(node => node.dataset.chatId === String(chat.chat_id));
      if (row) {
        row.chatData.agent_enabled = chat.agent_enabled;
        const pill = row.querySelector('.pill');
        pill.className = 'pill ' + (data.enabled ? 'on' : 'off');
        pill.textContent = data.enabled ? 'ON' : 'OFF';
      }
      notify(data.enabled ? 'Chat Agent Enabled' : 'Chat Agent Disabled');
      listChats().catch(console.error);
    } catch (_error) {
      notify('Chat Agent Not Updated', 'error');
    } finally {
      togglePending = false;
      btn.disabled = !selectedChat || !globalEnabled;
    }
  }

  async function sendOwner() {
    if (!selectedChat || sendPending) return;
    const input = document.getElementById('owner-text');
    const button = document.getElementById('send-owner');
    const text = input.value.trim();
    if (!text) { notify('Enter a Message', 'warning'); return; }
    const chatId = String(selectedChat.chat_id);
    sendPending = true;
    button.disabled = true;
    try {
      await postAction(chatsConfig.sendUrl, { channel, chat_id: chatId, text });
      if (String(selectedChat?.chat_id) === chatId && input.value.trim() === text) input.value = '';
      notify('Message Sent');
      Promise.all([loadMessages(), listChats()]).catch(console.error);
    } catch (error) {
      if (error.delivered) {
        if (String(selectedChat?.chat_id) === chatId && input.value.trim() === text) input.value = '';
        notify('Message Delivered: Transcript Not Saved. Do Not Resend.', 'warning');
      } else {
        notify('Message Not Sent', 'error');
      }
    } finally {
      sendPending = false;
      button.disabled = !selectedChat;
    }
  }

  document.getElementById('toggle-agent').addEventListener('click', toggleAgent);
  document.getElementById('send-owner').addEventListener('click', sendOwner);

  async function fetchGlobalStatus() {
    const request = ++globalStatusRequest;
    const data = await fetchJSON(chatsConfig.globalStatusUrl + '?channel=' + channel);
    if (request !== globalStatusRequest) return;
    globalEnabled = !!data.enabled;

    const b = document.getElementById('toggle-agent-global');
    b.setAttribute('aria-checked', String(globalEnabled));

    // Optional: disable per-chat toggle while global OFF to avoid confusion
    if (selectedChat) {
      document.getElementById('toggle-agent').disabled = !globalEnabled || togglePending;
    }
  }

  async function toggleAgentGlobal() {
    const b = document.getElementById('toggle-agent-global');
    const currentlyOn = b.getAttribute('aria-checked') === 'true';
    b.disabled = true;
    try {
      const data = await postAction(chatsConfig.toggleGlobalUrl, { channel, enabled: !currentlyOn });
      // A status read started before this result can still carry the previous value.
      globalStatusRequest++;
      globalEnabled = !!data.enabled;
      b.setAttribute('aria-checked', String(globalEnabled));
      document.getElementById('toggle-agent').disabled = !selectedChat || !globalEnabled || togglePending;
      notify(globalEnabled ? 'Global Agent Enabled' : 'Global Agent Disabled');
      listChats().catch(console.error);
    } catch (_error) {
      notify('Global Agent Not Updated', 'error');
    } finally {
      b.disabled = false;
    }
  }

  document.getElementById('toggle-agent-global').addEventListener('click', toggleAgentGlobal);

  // initial
  Promise.all([fetchGlobalStatus(), listChats()]).catch(() => notify('Chats Unavailable', 'error'));

  // On load + poll
  setInterval(() => {
    Promise.all([fetchGlobalStatus(), listChats(), selectedChat ? loadMessages() : Promise.resolve()]).catch(console.error);
  }, 5000);
