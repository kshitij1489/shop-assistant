const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function notificationPage() {
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this.attributes = {};
      this.dataset = {};
      this.events = {};
      this.value = '';
      this.classList = { add: name => { this.className += ' ' + name; } };
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text || this.children.map(child => child.textContent).join(''); }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    append(...children) { children.forEach(child => this.appendChild(child)); }
    appendChild(child) {
      if (child.parent && child.parent !== this) child.remove();
      child.parent = this;
      if (!this.children.includes(child)) this.children.push(child);
    }
    prepend(...nodes) {
      nodes.reduceRight((_, node) => {
        if (node.parent) node.remove();
        node.parent = this;
        this.children.unshift(node);
      }, null);
    }
    after(node) {
      if (node.parent) node.remove();
      node.parent = this.parent;
      const index = this.parent.children.indexOf(this);
      this.parent.children.splice(index + 1, 0, node);
    }
    addEventListener(name, handler) { this.events[name] = handler; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    focus() { this.focused = true; }
    get firstElementChild() { return this.children[0] || null; }
    matches(selector) {
      return selector.split(',').some(part => this.matchesPart(part.trim()));
    }
    matchesPart(selector) {
      if (selector === 'dialog') return this.tagName === 'dialog';
      if (selector.startsWith('[') && selector.endsWith(']')) {
        const name = selector.slice(6, -1).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        return Object.hasOwn(this.dataset, name);
      }
      const [tag, ...classes] = selector.split('.');
      if (tag && this.tagName !== tag) return false;
      return classes.every(name => (this.className || '').split(/\s+/).includes(name));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
      const found = [];
      const walk = node => {
        for (const child of node.children || []) {
          if (!selector || child.matches(selector)) found.push(child);
          walk(child);
        }
      };
      walk(this);
      return found;
    }
    contains(element) { return element === this || this.children.some(child => child.contains?.(element)); }
    closest(selector) { return selector === '[data-json-action]' && this.dataset.jsonAction ? this : null; }
  }
  const region = new Element('div');
  const seeds = new Element('div');
  const elements = { notifications: region, 'notification-messages': seeds };
  const events = {};
  let time = 0, timerId = 0;
  const timers = new Map();
  const page = {
    document: {
      getElementById: id => elements[id],
      createElement: tag => new Element(tag),
      querySelectorAll: () => [],
      addEventListener: (name, handler) => { (events[name] ||= []).push(handler); }
    },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, at: time + delay }); return timerId; },
    clearTimeout: id => timers.delete(id)
  };
  page.window = page;
  vm.createContext(page);
  for (const script of ['notifications', 'json_editor']) {
    vm.runInContext(fs.readFileSync(`users/static/users/js/${script}.js`, 'utf8'), page);
  }
  const fire = (name, event = {}) => (events[name] || []).forEach(handler => handler(event));
  const tick = amount => {
    const end = time + amount;
    while (true) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]);
      time = next[1].at;
      next[1].callback();
    }
    time = end;
  };
  return { page, region, seeds, elements, Element, fire, tick };
}

test('notification stays for five seconds, slides out, then removes itself', () => {
  const { page, region, tick } = notificationPage();
  page.notify('Menu Items Saved');
  const notice = region.children[0];
  assert.equal(notice.attributes.role, 'status');
  assert.equal(notice.children[1].textContent, 'Menu Items Saved');
  tick(4999);
  assert.doesNotMatch(notice.className, /leaving/);
  tick(1);
  assert.match(notice.className, /leaving/);
  assert.equal(region.children.length, 1);
  tick(240);
  assert.equal(region.children.length, 0);
});

test('stacked notifications expire independently and can be dismissed early', () => {
  const { page, region, tick } = notificationPage();
  page.notify('First');
  tick(1000);
  page.notify('Second', 'warning');
  page.notify('Third', 'error');
  assert.equal(region.children[2].attributes.role, 'alert');
  const close = region.children[1].children[2];
  assert.equal(close.attributes['aria-label'], 'Dismiss notification');
  close.events.click();
  tick(240);
  assert.deepEqual(region.children.map(notice => notice.children[1].textContent), ['First', 'Third']);
  tick(4000);
  assert.deepEqual(region.children.map(notice => notice.children[1].textContent), ['Third']);
  tick(1000);
  assert.equal(region.children.length, 0);
});

test('server messages use the same renderer and markup stays literal', () => {
  const { region, seeds, Element, fire } = notificationPage();
  const seed = new Element('span');
  seed.dataset.notificationLevel = 'error';
  seed.textContent = '<img src=x onerror=alert(1)>';
  seeds.appendChild(seed);
  fire('DOMContentLoaded');
  const notice = region.children[0];
  assert.match(notice.className, /notification--error/);
  assert.equal(notice.children[1].textContent, seed.textContent);
  assert.equal(notice.children[1].children.length, 0);
});

test('JSON validation and formatting report success and preserve malformed input', () => {
  const { page, region, Element } = notificationPage();
  const field = new Element('textarea');
  field.value = '{"enabled":true}';
  assert.equal(page.JSONEditor.check(field), true);
  assert.equal(region.children[0].children[1].textContent, 'JSON Valid');
  assert.equal(page.JSONEditor.check(field, { format: true }), true);
  assert.equal(field.value, '{\n  "enabled": true\n}');
  field.value = '{invalid';
  assert.equal(page.JSONEditor.check(field, { format: true }), false);
  assert.equal(field.value, '{invalid');
  assert.equal(field.attributes['aria-invalid'], 'true');
  assert.equal(field.focused, true);
  assert.equal(region.children.at(-1).children[1].textContent, 'Invalid JSON');
});

test('declarative validation buttons work and invalid JSON blocks submission', () => {
  const { region, elements, Element, fire } = notificationPage();
  const field = new Element('textarea');
  field.value = '{}';
  elements.editor = field;
  const button = new Element('button');
  button.dataset = { jsonAction: 'validate', jsonTarget: 'editor' };
  fire('click', { target: button });
  assert.equal(region.children[0].children[1].textContent, 'JSON Valid');
  const form = new Element('form');
  form.dataset.jsonValidate = 'textarea';
  form.appendChild(field);
  let prevented = false;
  fire('submit', { target: form, preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
  assert.equal(region.children.length, 1, 'submission must not announce success before saving');
  field.value = '[';
  fire('submit', { target: form, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.match(region.children.at(-1).className, /notification--error/);
});

test('modal validation feedback is inside the dialog and returns on close', () => {
  const { page, region, Element, fire, tick } = notificationPage();
  const dialog = new Element('dialog');
  dialog.open = true;
  const heading = new Element('h3');
  const field = new Element('input');
  dialog.append(heading, field);
  page.document.querySelectorAll = selector => (
    selector === 'dialog:modal' && dialog.open ? [dialog] : []
  );
  fire('invalid', { target: field });
  const host = dialog.querySelector('[data-dialog-notifications]');
  assert.equal(host.parent, dialog);
  assert.equal(dialog.children[1], host);
  assert.equal(region.children.length, 0);
  const notice = host.children[0];
  assert.equal(notice.children[1].textContent, 'Check Form Fields');
  const close = notice.children[2];
  close.focus();
  assert.equal(close.focused, true);
  dialog.open = false;
  fire('beforetoggle', { target: dialog, newState: 'closed' });
  assert.equal(dialog.querySelector('[data-dialog-notifications]'), null);
  assert.equal(region.children[0], notice);
  close.events.click();
  tick(240);
  assert.equal(region.children.length, 0);
});

test('native form validation emits one notification per attempt', () => {
  const { region, fire, tick } = notificationPage();
  fire('invalid');
  fire('invalid');
  assert.equal(region.children.length, 1);
  assert.equal(region.children[0].children[1].textContent, 'Check Form Fields');
  tick(1);
  fire('invalid');
  assert.equal(region.children.length, 2);
});


test('hover and keyboard focus both hold notifications until interaction ends', () => {
  const { page, region, tick } = notificationPage();
  page.notify('Needs attention');
  const notice = region.children[0];
  tick(4000);
  notice.events.mouseenter();
  tick(10000);
  assert.equal(region.children.length, 1);
  notice.events.focusin();
  notice.events.mouseleave();
  tick(10000);
  assert.equal(region.children.length, 1);
  notice.events.focusout({ relatedTarget: notice.children[2] });
  tick(10000);
  assert.equal(region.children.length, 1);
  notice.events.focusout({ relatedTarget: null });
  tick(4999);
  assert.doesNotMatch(notice.className, /leaving/);
  tick(241);
  assert.equal(region.children.length, 0);
});
