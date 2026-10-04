const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sandbox = { TextDecoder };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('chatbot_core/static/chatbot_core/js/chat_stream.js', 'utf8'), sandbox);
const readChatStream = sandbox.readChatStream;

function response(text, width = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += width) controller.enqueue(bytes.slice(i, i + width));
      controller.close();
    }
  }));
}

function frame(event, payload) {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

test('decodes split frames, split Unicode, comments and authoritative completion', async () => {
  const events = [];
  const text = ': keep-alive\n\n' + frame('replace', { text: 'First. ' }) +
    frame('delta', { text: 'café ☕' }) + frame('done', { response: 'First. café ☕', basket: [] });
  const result = await readChatStream(response(text), (event, payload) => events.push([event, payload]));
  assert.equal(result.response, 'First. café ☕');
  assert.deepEqual(events.map(([event]) => event), ['replace', 'delta', 'done']);
  assert.equal(events[1][1].text, 'café ☕');
});

test('handles coalesced CRLF frames and clears partial output on replacement', async () => {
  let visible = '';
  const text = frame('delta', { text: 'Failed partial' }) + frame('replace', { text: '' }) +
    frame('done', { response: 'Fallback', basket: [] });
  await readChatStream(response(text.replaceAll('\n', '\r\n'), 4096), (event, data) => {
    if (event === 'delta') visible += data.text;
    if (event === 'replace') visible = data.text;
    if (event === 'done') assert.equal(visible, '');
  });
});

test('rejects truncated streams instead of treating partial text as success', async () => {
  await assert.rejects(readChatStream(response(frame('delta', { text: 'Partial' })), () => {}),
    /before completion/);
});

test('reports server errors and malformed events without retrying', async () => {
  await assert.rejects(readChatStream(response(frame('error', { error: 'Turn failed' })), () => {}), /Turn failed/);
  await assert.rejects(readChatStream(response('event: delta\ndata: invalid\n\n'), () => {}), /JSON/);
});

test('completion releases the reader even when the server keeps the connection open', async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(frame('done', { response: 'Done' }))); },
    cancel() { cancelled = true; }
  });
  await readChatStream(new Response(body), () => {});
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

function chatPage(fetch) {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.disabled = false; this.value = ''; }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    set textContent(value) { this.children = [{ textContent: value }]; }
    get textContent() { return this.children.map(child => child.textContent).join(''); }
    addEventListener(event, handler) { this[event] = handler; }
    focus() {}
  }
  const form = new Element('form'), input = new Element('input'), button = new Element('button');
  const chat = new Element('div');
  form.querySelector = () => button;
  const page = { TextDecoder, ReadableStream, fetch, console: { error() {} },
    localStorage: { getItem: () => JSON.stringify({ token: 'test-token', expiry: Date.now() + 10000 }) },
    document: {
      getElementById: id => ({ 'chat-form': form, 'message-input': input, 'chat-window': chat })[id],
      createElement: tag => new Element(tag),
      createTextNode: text => ({ textContent: text })
    }
  };
  vm.createContext(page);
  vm.runInContext(fs.readFileSync('chatbot_core/static/chatbot_core/js/chat_stream.js', 'utf8'), page);
  const template = fs.readFileSync('chatbot_core/templates/chatbot_core/ai_agent.html', 'utf8');
  vm.runInContext(template.match(/<script>([\s\S]*?)<\/script>/)[1], page);
  return { form, input, button, chat };
}

const nextTask = () => new Promise(resolve => setImmediate(resolve));

test('chat updates one bubble, keeps partial text literal, and enables input after done', async () => {
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  const page = chatPage(async (_url, options) => {
    assert.equal(options.headers.Accept, 'text/event-stream');
    assert.equal(options.credentials, 'include');
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
  });
  page.input.value = 'Hello';
  const finished = page.form.submit({ preventDefault() {} });
  assert.equal(page.input.disabled, true);
  assert.equal(page.chat.children[1].textContent, 'Thinking…');
  controller.enqueue(new TextEncoder().encode(frame('delta', { text: '<img> https://example.com' })));
  await nextTask();
  const bubble = page.chat.children[1];
  assert.equal(bubble.textContent, '<img> https://example.com');
  assert.equal(bubble.children.length, 1);
  assert.equal(page.button.disabled, true);
  controller.enqueue(new TextEncoder().encode(frame('done', { response: 'Final https://example.com', basket: [] })));
  controller.close();
  await finished;
  assert.equal(page.chat.children.length, 2);
  assert.equal(bubble.textContent, 'Final https://example.com');
  assert.equal(bubble.children[1].tagName, 'a');
  assert.equal(bubble.children[1].rel, 'noopener noreferrer');
  assert.equal(page.input.disabled, false);
  assert.equal(page.button.disabled, false);
});

test('chat replaces truncated partial text with an error and does not retry the turn', async () => {
  let calls = 0;
  const page = chatPage(async () => {
    calls++;
    const result = response(frame('delta', { text: 'Misleading partial' }));
    result.headers.set('Content-Type', 'text/event-stream');
    return result;
  });
  page.input.value = 'Checkout';
  await page.form.submit({ preventDefault() {} });
  assert.equal(calls, 1);
  assert.equal(page.chat.children.length, 2);
  assert.match(page.chat.children[1].textContent, /could not be completed/);
  assert.equal(page.input.disabled, false);
});

test('chat accepts legacy JSON responses', async () => {
  const page = chatPage(async () => Response.json({ response: 'Cached reply', basket: [] }));
  page.input.value = 'Hello';
  await page.form.submit({ preventDefault() {} });
  assert.equal(page.chat.children[1].textContent, 'Cached reply');
  assert.equal(page.input.disabled, false);
});
