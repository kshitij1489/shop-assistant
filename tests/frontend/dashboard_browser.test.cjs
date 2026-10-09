// Optional native DOM coverage. Set DASHBOARD_BROWSER to a Chromium executable.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const chrome = process.env.DASHBOARD_BROWSER || [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome',
].find(file => fs.existsSync(file));

async function browserChecks(assets) {
  const results = [];
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  async function run(name, callback) {
    document.getElementById('results').textContent = JSON.stringify([...results, { name, ok:false, error:'Check did not complete' }]);
    try { await callback(); results.push({ name, ok: true }); }
    catch (error) { results.push({ name, ok: false, error: error.stack }); }
    document.getElementById('results').textContent = JSON.stringify(results);
  }
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  function page(markup) {
    const frame = document.createElement('iframe');
    document.body.appendChild(frame);
    const win = frame.contentWindow, doc = win.document;
    doc.body.innerHTML = markup;
    const load = source => {
      const script = doc.createElement('script');
      script.textContent = source;
      doc.body.appendChild(script);
    };
    return { win, doc, load, frame, get: id => doc.getElementById(id), eval: source => win.eval(source) };
  }
  function voice() {
    const b = page('<table><tbody id="basketBody"></tbody></table><div id="basketTotal"></div><button id="micBtn"></button><div id="status"></div><div id="liveText"></div><div id="chatLog"></div><input type="checkbox" id="autoLoop"><select id="langSelect"><option>en-US</option></select><select id="voiceSelect"></select><input id="rateCtl"><input id="pitchCtl">');
    b.load(`
      window.VA_CONFIG = { csrfToken: 'test' };
      // The harness is a file page; application URLs normally use an HTTP origin.
      window.URL = class extends URL { constructor(input, base) { super(input, base === 'null' ? 'https://example.test' : base); } };
      window.notices = [];
      window.notify = (...args) => notices.push(args);
      window.turn = 0; window.timerId = 0; window.timers = new Map(); window.intervals = new Map(); window.clock = 0;
      window.setTimeout = (callback, delay) => { const id = ++timerId; timers.set(id, { callback, at: clock + delay }); return id; };
      window.clearTimeout = id => timers.delete(id);
      window.setInterval = callback => { const id = ++timerId; intervals.set(id, callback); return id; };
      window.clearInterval = id => intervals.delete(id);
      window.tick = amount => {
        const end = clock + amount;
        while (true) {
          const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a,b) => a[1].at-b[1].at)[0];
          if (!next) break;
          timers.delete(next[0]); clock = next[1].at; turn++; next[1].callback();
        }
        clock = end;
      };
      window.SpeechRecognition = class {
        constructor() { window.recognizer = this; this.starts = 0; }
        start() { this.starts++; this.onstart?.(); }
        stop() { this.onend?.(); }
      };
      window.spoken = []; window.cancelTurn = -1; window.resumeTurn = -1;
      window.voices = [{ name:'First', voiceURI:'first', lang:'en-US' }, { name:'Second', voiceURI:'second', lang:'en-US' }];
      Object.defineProperty(window, 'speechSynthesis', { value: { getVoices: () => voices,
        cancel() { cancelTurn = turn; }, resume() { resumeTurn = turn; },
        speak(utterance) {
          if (turn === cancelTurn) throw new Error('speak ran in the same turn as cancel');
          if (turn !== resumeTurn) throw new Error('resume was not called before speak');
          spoken.push(utterance);
        } }});
      window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
      window.requests = []; window.messageSnapshot = [];
      window.fetch = async (url, options = {}) => {
        requests.push({ url, options });
        return { ok: true, json: async () => ({ messages: messageSnapshot }) };
      };
    `);
    b.load(assets.voice);
    return b;
  }
  await run('basket text cannot create markup and empty updates clear old rows', async () => {
    const b = voice(); await settle();
    b.eval(`updateBasketFromMeta([{ name:'<img src=x onerror=alert(1)>', size:'<svg onload=alert(2)>', quantity:2, currency:'USD', exponent:2, line_total_minor:1200 }])`);
    check(b.get('basketBody').querySelectorAll('img,svg').length === 0, 'Catalog markup became DOM');
    check(b.get('basketBody').textContent.includes('<img'), 'Catalog name was not rendered literally');
    check(b.get('basketTotal').textContent === 'USD 12.00', 'Total is incorrect');
    b.eval('updateBasketFromMeta(null)');
    check(b.get('basketBody').children.length === 1, 'Missing metadata should preserve the basket');
    b.eval('updateBasketFromMeta([])');
    check(b.get('basketBody').children.length === 0, 'Empty basket retained old rows');
    check(b.get('basketTotal').textContent === '0.00', 'Empty basket retained old total');
    b.frame.remove();
  });
  await run('stopping cancels buffered speech and timers and permits automatic restart later', async () => {
    const b = voice(); await settle();
    b.get('autoLoop').checked = true;
    b.eval('startListening(); bufferFinal = "Coffee"; lastInterim = "Please"');
    b.get('micBtn').click();
    b.win.tick(60000); await settle();
    check(!b.win.requests.some(request => request.options.method === 'POST'), 'Stopped speech submitted later');
    check(b.eval('bufferFinal === "" && lastInterim === "" && gapTimer === null && idleTimer === null'), 'Stop left buffered speech or timers');
    b.win.recognizer.onresult({ resultIndex: 0, results: [{ 0: { transcript: 'Late speech' }, isFinal:true }] });
    check(b.eval('bufferFinal === ""'), 'Late recognition event refilled a stopped buffer');
    b.eval('startListening()');
    const before = b.win.recognizer.starts;
    b.win.recognizer.onend();
    check(b.win.recognizer.starts === before + 1, 'Recognition end handler was lost');
    b.eval('stopListeningDueToIdle()');
    b.win.tick(60000);
    check(!b.win.requests.some(request => request.options.method === 'POST'), 'Idle stop submitted buffered speech');
    b.frame.remove();
  });
  await run('cancelled voice sends remove only unconfirmed rows, including abort and poll races', async () => {
    for (const phase of ['missing', 'stored', 'confirmed', 'late-success']) {
      const b = voice(); await settle();
      const fetch = b.win.fetch;
      let accept;
      b.win.fetch = (url, options = {}) => {
        if (options.method !== 'POST') return fetch(url, options);
        return new Promise((resolve, reject) => {
          accept = resolve;
          if (phase !== 'late-success') options.signal.addEventListener('abort', () => reject(new b.win.DOMException('Cancelled', 'AbortError')));
        });
      };
      b.eval('startListening(); bufferFinal = "Coffee"');
      const pending = b.eval('sendIfBuffer()');
      check(b.get('chatLog').children.length === 1, 'Optimistic row missing');
      if (phase === 'stored' || phase === 'confirmed') {
        b.win.messageSnapshot = [{ id:'saved', dir:'in', text:'Coffee', ts:1 }];
      }
      if (phase === 'confirmed') await b.eval('pollMessages()');
      b.eval('stopListeningDueToIdle()');
      if (phase === 'late-success') accept({ ok:true, json:async () => ({ status:'queued' }) });
      await pending;
      check(b.get('chatLog').children.length === (phase === 'confirmed' ? 1 : 0), 'Cancellation left a phantom or deleted a confirmed row: ' + phase);
      if (phase === 'late-success') b.win.messageSnapshot = [{ id:'saved', dir:'in', text:'Coffee', ts:1 }];
      await b.eval('pollMessages()');
      check(b.get('chatLog').children.length === (phase === 'missing' ? 0 : 1), 'Cancellation duplicated the stored line: ' + phase);
      check(b.win.notices.length === 0, 'Cancellation reported a send failure');
      b.frame.remove();
    }
  });
  await run('voice history advances beyond 200 even for identical replies and uses one poll', async () => {
    const b = voice(); await settle();
    check(b.win.intervals.size === 1, 'Voice created duplicate polling timers');
    b.win.messageSnapshot = Array.from({ length:200 }, (_, i) => ({ id:String(i), dir:i%2 ? 'out':'in', text:'Same text', ts:1 }));
    await b.eval('pollMessages()'); await settle();
    check(b.get('chatLog').children.length === 200, 'Initial history was not rendered: ' + b.get('chatLog').children.length + ' / ' + b.eval('[pollInFlight, previousMessageKeys?.length]'));
    check(b.win.spoken.length === 0, 'Historical replies were spoken');
    b.eval('startListening()');
    b.win.messageSnapshot = b.win.messageSnapshot.slice(2).concat([{ id:'200', dir:'in', text:'Same text', ts:1 }, { id:'201', dir:'out', text:'Same text', ts:1 }]);
    await b.eval('pollMessages()');
    check(b.get('chatLog').children.length === 202, 'Sliding history skipped new messages');
    b.win.tick(0);
    check(b.win.spoken.length === 1, 'Sliding history skipped new speech');
    await b.eval('pollMessages()');
    check(b.win.spoken.length === 1 && b.get('chatLog').children.length === 202, 'Unchanged messages repeated');
    let finish;
    b.win.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = b.eval('pollMessages()');
    await b.eval('pollMessages()');
    finish({ ok:true, json:async () => ({ messages:b.win.messageSnapshot }) }); await pending;
    b.frame.remove();
  });
  await run('voice selector affects utterances and cancellation invalidates queued chunks', async () => {
    const b = voice(); await settle();
    b.get('voiceSelect').value = 'second';
    b.get('voiceSelect').dispatchEvent(new b.win.Event('change'));
    b.eval('loadVoices(); startListening(); speakReply("First sentence. Second sentence.")');
    check(b.win.spoken.length === 0, 'Speech started in the cancel turn');
    b.win.tick(0);
    check(b.win.spoken[0].voice.voiceURI === 'second', 'Selected voice was ignored or reset');
    b.win.spoken[0].onend();
    b.get('micBtn').click();
    b.win.tick(1000);
    check(b.win.spoken.length === 1, 'Stopped session continued speaking');
    check(b.get('status').textContent === 'Click the mic to start', 'Stopped session restarted');
    b.frame.remove();
  });
  await run('late chat responses cannot replace the selected transcript', async () => {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript" style="height:100px;overflow:auto"></div>');
    const chats = [{ chat_id:'A' }, { chat_id:'B' }];
    const pending = new Map();
    b.win.notify = () => {};
    b.win.setInterval = () => 1;
    b.win.fetch = async url => {
      if (url.includes('messages_api')) return new Promise(resolve => pending.set(new URL(url, location.href).searchParams.get('chat_id'), resolve));
      return { ok:true, json:async () => ({ chats, enabled:true }) };
    };
    b.load(assets.chats);
    await settle();
    const a = b.win.selectChat(chats[0]), bb = b.win.selectChat(chats[1]);
    pending.get('B')({ ok:true, json:async () => ({ messages:[{ id:'B1', dir:'out', text:'Transcript B' }] }) }); await bb;
    pending.get('A')({ ok:true, json:async () => ({ messages:[{ id:'A1', dir:'out', text:'Transcript A' }] }) }); await a;
    check(b.get('transcript').textContent === 'Transcript B', 'Stale response overwrote selected chat');
    const item = b.get('chat-list').children[1]; item.focus(); await b.win.listChats();
    check(b.doc.activeElement === item && b.get('chat-list').children[1] === item, 'Polling replaced or unfocused chat rows');
    let messages = Array.from({ length:30 }, (_, index) => ({ id:String(index), dir:'out', text:'Message '+index }));
    b.win.fetch = async url => ({ ok:true, json:async () => url.includes('messages_api') ? { messages } : { chats, enabled:true } });
    await b.win.loadMessages();
    const transcript = b.get('transcript');
    transcript.querySelectorAll('.msg').forEach(node => { node.style.height = '40px'; });
    transcript.scrollTop = 100;
    const first = transcript.firstElementChild;
    await b.win.loadMessages();
    check(transcript.scrollTop === 100 && transcript.firstElementChild === first, 'Unchanged poll rebuilt or scrolled transcript');
    messages = messages.concat({ id:'30', dir:'out', text:'New message' });
    await b.win.loadMessages();
    check(transcript.scrollTop === 100, 'New message interrupted reading');
    b.frame.remove();
  });
  async function ownerChats() {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript"></div>');
    b.win.notices = [];
    b.win.notify = (...args) => b.win.notices.push(args);
    b.win.setInterval = () => 1;
    b.sends = [];
    b.win.fetch = async (url, options = {}) => {
      if (options.method === 'POST') {
        return new Promise(resolve => b.sends.push({ payload: JSON.parse(options.body), resolve }));
      }
      return { ok:true, json:async () => ({ chats:[{ chat_id:'A' }, { chat_id:'B' }], messages:[], enabled:true }) };
    };
    b.load(assets.chats); await settle();
    b.select = async index => {
      b.get('chat-list').children[index].click();
      await settle();
    };
    b.finish = (index, outcome = 'sent') => b.sends[index].resolve({
      ok:outcome === 'sent', json:async () => ({ ok:outcome === 'sent', delivered:outcome === 'delivered' }),
    });
    return b;
  }
  await run('owner drafts stay on the current customer and clear when switching customers', async () => {
    const b = await ownerChats();
    await b.select(0);
    b.get('owner-text').value = 'Reply for A';
    await b.select(0);
    check(b.get('owner-text').value === 'Reply for A', 'Reselecting the current chat discarded its draft');
    await b.select(1);
    check(b.get('owner-text').value === '', 'Switching customers retained the previous customer draft');
    await b.win.sendOwner();
    check(b.sends.length === 0, 'Previous customer draft was submitted to the new customer');
    b.get('owner-text').value = 'Reply for B';
    const pending = b.win.sendOwner();
    check(b.sends.length === 1 && b.sends[0].payload.chat_id === 'B' && b.sends[0].payload.text === 'Reply for B', 'Reply used the wrong customer or draft');
    b.finish(0); await pending;
    await b.select(0);
    check(b.get('owner-text').value === '', 'Switching back restored a different customer draft');
    b.frame.remove();
  });
  await run('reselecting a customer cannot duplicate pending sends and completion handles refreshed chat objects', async () => {
    for (const outcome of ['sent', 'delivered', 'failed']) {
      const b = await ownerChats();
      await b.select(0);
      b.get('owner-text').value = 'Reply for A';
      const pending = b.win.sendOwner();
      check(b.get('send-owner').disabled, 'Send was not disabled while pending');
      await b.select(0);
      check(b.get('send-owner').disabled, 'Reselecting the chat enabled a pending send');
      b.get('send-owner').click();
      await b.win.sendOwner();
      check(b.sends.length === 1, 'Pending send was submitted again');
      b.finish(0, outcome); await pending;
      check(!b.get('send-owner').disabled, 'Completed send did not release Send');
      check(b.get('owner-text').value === (outcome === 'failed' ? 'Reply for A' : ''), 'Completion mishandled the refreshed chat draft: ' + outcome);
      if (outcome === 'delivered') check(b.win.notices.at(-1)[1] === 'warning', 'Delivered message did not report transcript failure');
      if (outcome === 'failed') {
        const retry = b.win.sendOwner();
        check(b.sends.length === 2, 'Failed send could not be retried');
        b.finish(1); await retry;
        check(b.get('owner-text').value === '', 'Retry left a sent draft');
      }
      b.frame.remove();
    }
  });
  await run('changing customers during a pending owner send preserves the new draft and send guard', async () => {
    const b = await ownerChats();
    await b.select(0);
    b.get('owner-text').value = 'Reply for A';
    const a = b.win.sendOwner();
    await b.select(1);
    b.get('owner-text').value = 'Reply for B';
    check(b.get('send-owner').disabled, 'Changing customers enabled Send during a pending request');
    await b.win.sendOwner();
    check(b.sends.length === 1, 'Changing customers bypassed the pending send guard');
    b.finish(0); await a;
    check(b.get('owner-text').value === 'Reply for B', 'Previous send cleared the new customer draft');
    check(!b.get('send-owner').disabled, 'Send stayed disabled after the previous request completed');
    const bb = b.win.sendOwner();
    check(b.sends.length === 2 && b.sends[1].payload.chat_id === 'B', 'Next send used the previous customer');
    b.get('owner-text').value = 'Another reply for B';
    await b.select(1);
    b.finish(1); await bb;
    check(b.get('owner-text').value === 'Another reply for B', 'Completion cleared a newer draft');
    b.frame.remove();
  });
  await run('failed global status polls preserve the switch and delivered messages report storage failure', async () => {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript"></div>');
    b.win.notices = [];
    b.win.notify = (...args) => b.win.notices.push(args);
    b.win.setInterval = () => 1;
    const chats = [{ chat_id:'A' }];
    b.win.fetch = async () => ({ ok:true, json:async () => ({ chats, messages:[], enabled:false }) });
    b.load(assets.chats); await settle();
    await b.win.selectChat(chats[0]);
    check(b.get('toggle-agent-global').getAttribute('aria-checked') === 'false', 'Initial switch should be off');
    b.win.fetch = async () => ({ ok:false, json:async () => ({ ok:false, delivered:true }) });
    let failed = false;
    try { await b.win.fetchGlobalStatus(); } catch (_) { failed = true; }
    check(failed && b.get('toggle-agent-global').getAttribute('aria-checked') === 'false', 'Redis outage reset the switch');
    b.get('owner-text').value = 'Hello';
    await b.win.sendOwner();
    check(b.get('owner-text').value === '', 'Delivered message was left ready to resend');
    check(b.win.notices.at(-1)[1] === 'warning' && b.win.notices.at(-1)[0].includes('Transcript Not Saved'), 'Delivery/storage failure was reported as success or failed delivery');
    b.frame.remove();
  });
  await run('late global status polls cannot undo a newer toggle', async () => {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript"></div>');
    let releaseStatus;
    const chats = [{ chat_id:'A', display_name:'Guest', agent_enabled:'1' }];
    b.win.notify = () => {};
    b.win.setInterval = () => 1;
    b.win.fetch = async (url, options = {}) => {
      if (options.method === 'POST') return { ok:true, json:async () => ({ ok:true, enabled:false }) };
      if (String(url).includes('global_status')) return new Promise(resolve => { releaseStatus = resolve; });
      return { ok:true, json:async () => ({ chats, messages:[], enabled:true }) };
    };
    b.load(assets.chats);
    await settle();
    await b.win.selectChat(chats[0]);
    await b.win.toggleAgentGlobal();
    check(b.get('toggle-agent-global').getAttribute('aria-checked') === 'false', 'Toggle did not turn the global switch off');
    check(b.get('toggle-agent').disabled, 'Per-chat switch stayed available while the global agent was off');
    releaseStatus({ ok:true, json:async () => ({ enabled:true }) });
    await settle();
    check(b.get('toggle-agent-global').getAttribute('aria-checked') === 'false', 'Stale status poll restored the global switch');
    check(b.get('toggle-agent').disabled, 'Stale status poll re-enabled the per-chat switch');
    b.win.fetch = async () => ({ ok:true, json:async () => ({ chats, messages:[], enabled:true }) });
    await b.win.fetchGlobalStatus();
    check(b.get('toggle-agent-global').getAttribute('aria-checked') === 'true', 'A later status poll did not update the switch');
    check(!b.get('toggle-agent').disabled, 'A later status poll left the per-chat switch disabled');
    b.frame.remove();
  });
  await run('chat list refresh keeps the selected switch aligned with its badge', async () => {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript"></div>');
    b.win.notify = () => {};
    b.win.setInterval = () => 1;
    let chats = [{ chat_id:'A', agent_enabled:'1', display_name:'Ada' }];
    let releaseToggle;
    b.win.fetch = async url => {
      if (url.includes('toggle')) return new Promise(resolve => { releaseToggle = resolve; });
      return { ok:true, json:async () => ({ chats, messages:[], enabled:true, ok:true }) };
    };
    b.load(assets.chats); await settle();
    await b.win.selectChat(chats[0]);
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'true', 'Selected switch should start on');
    chats = [{ chat_id:'A', agent_enabled:'0', display_name:'Ada' }];
    await b.win.listChats();
    const pill = b.get('chat-list').querySelector('.pill');
    check(pill.textContent === 'OFF' && pill.className.includes('off'), 'List badge should show the remote off state');
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'false', 'Selected switch stayed on after the list refresh');
    check(b.eval('selectedChat.agent_enabled') === '0', 'Selected chat kept the stale enabled flag');
    chats = [{ chat_id:'A', agent_enabled:'1', display_name:'Ada' }];
    const pending = b.win.toggleAgent();
    await settle();
    await b.win.listChats();
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'false', 'In-flight toggle was overwritten by polling');
    releaseToggle({ ok:true, json:async () => ({ ok:true, enabled:true }) });
    await pending;
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'true', 'Completed toggle did not update the switch');
    b.frame.remove();
  });
  await run('confirmed chat toggles survive reselection and failed follow-up refreshes', async () => {
    const b = page('<div id="chat-list"></div><div id="chat-title"></div><div id="chat-subtitle"></div><button id="toggle-agent"></button><button id="toggle-agent-global"></button><textarea id="owner-text"></textarea><button id="send-owner"></button><div id="transcript"></div>');
    b.win.notify = () => {};
    b.win.setInterval = () => 1;
    b.win.console = { ...console, error: () => {} };
    let releaseToggle, failRefresh = false, posts = 0;
    b.win.fetch = async (url, options = {}) => {
      if (options.method === 'POST') {
        posts++;
        return new Promise(resolve => { releaseToggle = resolve; });
      }
      return { ok:!failRefresh, json:async () => ({ chats:[{ chat_id:'A', agent_enabled:'1' }], messages:[], enabled:true }) };
    };
    b.load(assets.chats); await settle();
    await b.win.selectChat({ chat_id:'A', agent_enabled:'1' });
    const pending = b.win.toggleAgent();
    await b.win.listChats();
    await b.win.selectChat({ chat_id:'A', agent_enabled:'1' });
    await b.win.toggleAgent();
    failRefresh = true;
    releaseToggle({ ok:true, json:async () => ({ ok:true, enabled:false }) });
    await pending; await settle();
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'false', 'Confirmed toggle was lost after reselection');
    check(b.eval('selectedChat.agent_enabled') === '0', 'Selected chat retained the old setting');
    const row = b.get('chat-list').firstElementChild;
    check(row.querySelector('.pill').textContent === 'OFF', 'List badge lost the confirmed setting');
    await row.onclick();
    check(b.get('toggle-agent').getAttribute('aria-checked') === 'false', 'Reselecting the row restored its stale setting');
    check(!b.get('toggle-agent').disabled, 'Completed toggle left its control disabled');
    check(posts === 1, 'Reselection submitted another toggle');
    b.frame.remove();
  });
  await run('shared tabs support arrow keys, Home, End, and labelled panels', async () => {
    const b = page('<div id="tabs" role="tablist"><button role="tab" aria-selected="true" aria-controls="one">One</button><button role="tab" aria-controls="two">Two</button></div><div id="one"></div><div id="two"></div>');
    b.load(assets.ui);
    b.eval('UIComponents.tabs(document.getElementById("tabs"))');
    const buttons = b.get('tabs').children; buttons[0].focus();
    buttons[0].dispatchEvent(new b.win.KeyboardEvent('keydown', { key:'End', bubbles:true }));
    check(b.doc.activeElement === buttons[1] && b.get('one').hidden && !b.get('two').hidden, 'End did not select the last tab');
    check(b.get('two').getAttribute('aria-labelledby') === buttons[1].id, 'Panel is not labelled by its tab');
    buttons[1].dispatchEvent(new b.win.KeyboardEvent('keydown', { key:'ArrowRight', bubbles:true }));
    check(b.doc.activeElement === buttons[0], 'Arrow navigation did not wrap');
    b.frame.remove();
  });
  await run('knowledge switching preserves edits and native dialogs restore focus', async () => {
    const b = page('<script id="docs_json" type="application/json"></script><select id="dtypeSelect"><option>knowledge</option><option>classification</option></select><div id="intentTabs" role="tablist"></div><div id="subIntentTabs" role="tablist"></div><textarea id="payloadTA"></textarea><div id="selectionPath"></div><p id="draftStatus"></p><form id="updateForm"></form><form id="deleteForm"></form><button id="btnFormat"></button><button id="btnValidate"></button><button id="openAddModal"></button><dialog id="addModal"><form id="addForm"><select id="add_dtype"><option>knowledge</option></select><textarea id="add_payload"></textarea><button type="button" id="btnAddCancel"></button><button type="button" id="btnAddFormat"></button><button type="button" id="btnAddValidate"></button></form></dialog>');
    for (const id of ['update_dtype','update_intent','update_sub_intent','update_payload','delete_dtype','delete_intent','delete_sub_intent']) {
      const input = b.doc.createElement('input'); input.id = id; b.doc.body.appendChild(input);
    }
    b.get('docs_json').textContent = JSON.stringify([
      { dtype:'knowledge', intent:'cafe', sub_intent:'a', payload:{ text:'Saved A' } },
      { dtype:'knowledge', intent:'cafe', sub_intent:'b', payload:{ text:'Saved B' } },
      { dtype:'classification', intent:'other', sub_intent:'c', payload:{} },
    ]);
    b.win.notify = () => {};
    b.load(assets.ui); b.load(assets.json); b.load(assets.knowledge);
    const draft = '{"text":"Unsaved A"}';
    b.get('payloadTA').value = draft;
    b.get('payloadTA').dispatchEvent(new b.win.Event('input'));
    b.get('subIntentTabs').children[0].click();
    check(b.get('payloadTA').value === draft, 'Selecting current entry discarded edits');
    b.get('subIntentTabs').children[1].click(); b.get('subIntentTabs').children[0].click();
    check(b.get('payloadTA').value === draft, 'Switching topic discarded edits');
    b.get('dtypeSelect').value = 'classification'; b.get('dtypeSelect').dispatchEvent(new b.win.Event('change'));
    b.get('dtypeSelect').value = 'knowledge'; b.get('dtypeSelect').dispatchEvent(new b.win.Event('change'));
    check(b.get('payloadTA').value === draft, 'Switching dtype discarded edits');
    const unload = new b.win.Event('beforeunload', { cancelable:true }); b.win.dispatchEvent(unload);
    check(unload.defaultPrevented, 'Leaving page did not protect unsaved edits');
    b.get('openAddModal').focus(); b.get('openAddModal').click();
    check(b.get('addModal').open, 'Dialog did not open');
    b.get('btnAddCancel').click(); await settle();
    check(!b.get('addModal').open && b.doc.activeElement === b.get('openAddModal'), 'Dialog failed to restore focus');
    b.frame.remove();
  });
  await run('tenant switch keeps equal touch-target dimensions in both states', async () => {
    const b = page('<button id="switch" class="tenant-switch" aria-checked="false"><span class="tenant-switch-track"></span></button>');
    const style = b.doc.createElement('style'); style.textContent = assets.css; b.doc.head.appendChild(style);
    const button = b.get('switch'), initial = button.getBoundingClientRect();
    button.setAttribute('aria-checked', 'true');
    const active = button.getBoundingClientRect();
    check(active.height >= 44 && active.height === initial.height && active.width === initial.width, 'Switch changed size or lost touch target');
    b.frame.remove();
  });
  await run('modal validation feedback can be focused and dismissed inside the dialog', async () => {
    const b = page('<div id="notifications"></div><button id="open" type="button">Add</button><dialog id="addModal" class="dashboard-dialog"><h3>Add Entry</h3><form id="addForm"><input id="add_intent" required><button type="submit">Save</button></form></dialog>');
    b.frame.style.width = '1200px';
    b.frame.style.height = '900px';
    const style = b.doc.createElement('style');
    style.textContent = assets.notificationsCss + assets.css;
    b.doc.head.appendChild(style);
    b.load(assets.notifications);
    b.load(assets.ui);
    const outside = b.doc.createElement('button');
    outside.type = 'button';
    outside.textContent = 'Outside';
    b.get('notifications').appendChild(outside);
    const dialog = b.get('addModal');
    const modal = b.win.UIComponents.dialog(dialog);
    b.get('open').focus();
    modal.open(b.get('open'));
    outside.focus();
    check(b.doc.activeElement !== outside, 'A control outside the modal received focus');
    b.get('addForm').requestSubmit();
    const notice = dialog.querySelector('.notification');
    check(notice && !b.get('notifications').querySelector('.notification'), 'Validation toast was inserted outside the dialog');
    const close = notice.querySelector('.notification-close');
    close.focus();
    check(b.doc.activeElement === close, 'Dismiss button cannot take keyboard focus while the dialog is open');
    const point = close.getBoundingClientRect();
    const box = dialog.getBoundingClientRect();
    check(point.width > 0 && point.top >= box.top && point.bottom <= box.bottom && point.left >= box.left && point.right <= box.right, 'Validation feedback is outside the dialog');
    const hit = b.doc.elementFromPoint(point.left + point.width / 2, point.top + point.height / 2);
    check(hit === close, 'Dismiss button cannot receive pointer input');
    modal.close();
    check(!dialog.open && b.doc.activeElement === b.get('open'), 'Dialog failed to restore focus');
    check(b.get('notifications').contains(notice) && !dialog.contains(notice), 'Open notification was not returned to the page');
    close.focus();
    check(b.doc.activeElement === close, 'Dismiss button is not focusable after the dialog closes');
    close.click();
    check(notice.classList.contains('notification--leaving'), 'Dismiss did not start');
    b.frame.remove();
  });
  document.getElementById('results').textContent = JSON.stringify(results);
}

test('native browser dashboard regressions', { skip: !chrome }, async t => {
  const read = file => fs.readFileSync(file, 'utf8');
  const assets = {
    voice:read('users/static/users/js/voice.js'), ui:read('users/static/users/js/ui_components.js'),
    notifications:read('users/static/users/js/notifications.js'),
    notificationsCss:read('users/static/users/css/notifications.css'),
    knowledge:read('users/static/users/js/knowledge.js'), json:read('users/static/users/js/json_editor.js'),
    chats:read('users/templates/users/tenant_chats.html').match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\{% url "([^"]+)" %\}/g, '/test/$1'),
    css:read('users/static/users/css/dashboard.css') + read('users/static/users/css/master_tenants.css'),
  };
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-dashboard-browser-'));
  try {
    const html = path.join(directory, 'checks.html');
    fs.writeFileSync(html, '<!doctype html><meta charset="utf-8"><pre id="results"></pre><script>(' + browserChecks.toString().replace(/<\/script/gi, '<\\/script') + ')(' + JSON.stringify(assets).replace(/</g, '\\u003c') + ')</script>');
    const browser = spawn(chrome, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      ...(process.getuid?.() === 0 ? ['--no-sandbox'] : []),
      '--disable-background-networking', '--disable-component-update', '--host-resolver-rules=MAP * ~NOTFOUND',
      '--user-data-dir=' + path.join(directory, 'profile'), '--timeout=10000', '--virtual-time-budget=5000', '--dump-dom', 'file://' + html],
      { stdio:['ignore', 'pipe', 'pipe'] });
    const exited = new Promise(resolve => browser.once('close', resolve));
    let stdout = '', stderr = '';
    try {
      await new Promise((resolve, reject) => {
        const deadline = setTimeout(() => reject(new Error('Browser did not produce a complete document: ' + stderr.slice(-1500))), 20000);
        const finish = () => { clearTimeout(deadline); resolve(); };
        browser.on('error', error => { clearTimeout(deadline); reject(error); });
        browser.stdout.on('data', chunk => {
          stdout += chunk;
          if (/<\/html>\s*$/.test(stdout)) finish();
        });
        browser.stderr.on('data', chunk => { stderr += chunk; });
        browser.on('exit', code => {
          if (/<\/html>\s*$/.test(stdout)) finish();
          else if (code !== 0) { clearTimeout(deadline); reject(new Error(stderr)); }
        });
      });
    } finally {
      browser.kill();
      browser.stdout.destroy(); browser.stderr.destroy();
      await exited;
    }
    const raw = stdout.match(/<pre id="results">([\s\S]*?)<\/pre>/)?.[1];
    assert.ok(raw, 'Browser did not finish its checks: ' + stderr.slice(-1500));
    const results = JSON.parse(raw.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
    assert.equal(results.length, 17);
    for (const result of results) await t.test(result.name, () => assert.ok(result.ok, result.error));
  } finally {
    fs.rmSync(directory, { recursive:true, force:true });
  }
});
