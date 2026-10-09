const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function browser() {
  const elements = new Map(), events = {}, notices = [], intervals = new Map();
  let nextTimer = 0;
  function element() {
    const classes = new Set();
    return {
      value: '', textContent: '', dataset: {}, attributes: {}, children: [], events: {},
      disabled: false, hidden: false,
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      focus() { this.focused = true; },
      addEventListener(name, handler) { this.events[name] = handler; },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
      get lastElementChild() { return this.children.at(-1); },
    };
  }
  const get = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const page = {
    addEventListener: (name, handler) => { events[name] = handler; },
    document: {
      getElementById: get, createElement: element, createTextNode: text => ({ textContent: text }),
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: (name, handler) => { events[name] = handler; },
    },
    notify: (...args) => notices.push(args),
    navigator: { language: 'en-US' }, location: { origin: 'https://example.test' }, URL,
    VA_CONFIG: { csrfToken: 'test' }, console: { error() {} },
    setTimeout: () => ++nextTimer, clearTimeout() {},
    setInterval: callback => { intervals.set(++nextTimer, callback); return nextTimer; },
    clearInterval: id => intervals.delete(id),
    fetch: async () => ({ ok: true, json: async () => ({ messages: [] }) }),
  };
  page.window = page;
  vm.createContext(page);
  const run = source => vm.runInContext(source, page);
  const load = path => run(fs.readFileSync(path, 'utf8'));
  return { page, get, events, notices, intervals, run, load };
}

function inlineScript(path) {
  return fs.readFileSync(path, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('Format All preserves preparation and uses arrays for cleared catalog lists', () => {
  const b = browser();
  b.load('users/static/users/js/json_editor.js');
  const template = fs.readFileSync('users/templates/users/menu_item_detail.html', 'utf8');
  b.run(inlineScript('users/templates/users/menu_item_detail.html'));
  const names = ['dietary_preferences', 'allergens', 'preparation', 'nutrition', 'explore_options',
    'ingredients', 'recommendations', 'specialty_items', 'source_quality', 'pairings'];
  const fields = names.map(name => {
    const field = b.get(name);
    field.name = name;
    field.dataset.jsonEmpty = names.slice(5).includes(name) ? '[]' : '{}';
    field.value = name === 'preparation' ? 'Serve hot' : '  ';
    return field;
  });
  b.page.form = { querySelectorAll: selector => fields.filter(field =>
    !selector.includes(':not') || field.name !== 'preparation') };
  b.run('formatAllJSON(form)');
  assert.equal(b.get('preparation').value, 'Serve hot');
  for (const name of names.slice(5)) assert.equal(b.get(name).value, '[]', name);
  assert.equal(b.get('nutrition').value, '{}');
  assert.deepEqual(b.notices, [['JSON Formatted']]);
  b.get('preparation').value = '';
  b.run('formatAllJSON(form)');
  assert.equal(b.get('preparation').value, '');
  b.get('nutrition').value = '{bad';
  b.run('formatAllJSON(form)');
  assert.deepEqual(b.notices.at(-1), ['Invalid JSON', 'error']);
});

function voiceBrowser() {
  const b = browser();
  b.page.SpeechRecognition = class {
    constructor() { b.recognition = this; }
    start() { this.onstart(); }
    stop() { this.onend?.(); }
  };
  b.load('users/static/users/js/voice.js');
  b.run('startListening()');
  return b;
}

test('routine speech recognition events stay quiet while real errors show feedback', () => {
  const b = voiceBrowser();
  for (const error of ['no-speech', 'aborted']) b.recognition.onerror({ error });
  assert.deepEqual(b.notices, []);
  b.recognition.onerror({ error: 'not-allowed' });
  b.recognition.onerror({ error: 'network' });
  assert.deepEqual(b.notices, [
    ['Microphone Access Denied', 'error'], ['Speech Recognition Failed', 'error'],
  ]);
});

test('voice HTTP errors reset processing and report failure even with a valid JSON body', async () => {
  const b = voiceBrowser();
  await settle();
  b.page.fetch = async () => ({ ok: false, json: async () => ({ error: 'Unavailable' }) });
  await b.run('bufferFinal = "A coffee please"; sendIfBuffer()');
  assert.deepEqual(b.notices, [['Message Not Sent', 'error']]);
  assert.equal(b.get('micBtn').classList.contains('processing'), false);
  assert.equal(b.get('status').textContent, 'Click the mic to start');
  assert.equal(b.get('chatLog').lastElementChild.dataset.optimistic, undefined);
});

test('token generation stays disabled for the entire display and uses one countdown', async () => {
  const b = browser();
  let requests = 0;
  b.page.fetch = async () => {
    requests++;
    return { ok: true, json: async () => ({ token: `token-${requests}` }) };
  };
  b.run(inlineScript('users/templates/users/tenant_settings.html'));
  b.events.DOMContentLoaded();
  const button = b.get('generate-token-button');
  button.events.click();
  assert.equal(button.disabled, true);
  await settle();
  button.events.click();
  assert.equal(requests, 1);
  assert.equal(button.disabled, true);
  assert.equal(b.intervals.size, 1);
  const tick = () => [...b.intervals.values()].forEach(callback => callback());
  for (let i = 0; i < 14; i++) tick();
  assert.equal(button.disabled, true);
  assert.equal(b.get('token-container').hidden, false);
  tick();
  assert.equal(button.disabled, false);
  assert.equal(b.get('token-container').hidden, true);
  assert.equal(b.get('jwt-token-display').textContent, '');
  assert.equal(b.intervals.size, 0);
  button.events.click();
  await settle();
  tick();
  assert.equal(b.get('jwt-token-display').textContent, 'token-2');
  assert.equal(b.get('expire-timer').textContent, 14);
  assert.equal(b.intervals.size, 1);
});

test('failed token generation permits retry and leaves no token or countdown', async () => {
  const b = browser();
  b.page.fetch = async () => ({ ok: false });
  b.run(inlineScript('users/templates/users/tenant_settings.html'));
  b.events.DOMContentLoaded();
  b.get('generate-token-button').events.click();
  await settle();
  assert.equal(b.get('generate-token-button').disabled, false);
  assert.equal(b.get('jwt-token-display').textContent, '');
  assert.equal(b.get('token-container').hidden, true);
  assert.equal(b.intervals.size, 0);
  assert.deepEqual(b.notices, [['Token Not Generated', 'error']]);
});
