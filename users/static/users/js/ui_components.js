/* Shared keyboard navigation for tabs and native modal lifecycle. */
(() => {
  function tabs(list, { onSelect = () => {} } = {}) {
    const buttons = () => Array.from(list.querySelectorAll('[role="tab"]')).filter(tab => !tab.disabled);
    function select(tab, notify = true) {
      if (!tab) return;
      const items = buttons();
      const panelId = tab.getAttribute('aria-controls');
      items.forEach((item, index) => {
        if (!item.id) item.id = `${list.id || 'tabs'}-tab-${index}`;
        const active = item === tab;
        item.setAttribute('aria-selected', String(active));
        item.tabIndex = active ? 0 : -1;
        item.classList.toggle('active', active);
        const panel = document.getElementById(item.getAttribute('aria-controls'));
        if (panel) {
          panel.hidden = panel.id !== panelId;
          panel.setAttribute('aria-hidden', String(panel.hidden));
          if (!panel.hidden) panel.setAttribute('aria-labelledby', tab.id);
        }
      });
      if (notify) onSelect(tab);
    }
    list.addEventListener('click', event => {
      const tab = event.target.closest('[role="tab"]');
      if (!tab || !list.contains(tab) || tab.disabled) return;
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      select(tab);
    });
    list.addEventListener('keydown', event => {
      const items = buttons();
      const current = items.indexOf(document.activeElement);
      if (current < 0) return;
      let next;
      if (event.key === 'Enter' || event.key === ' ') next = current;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = items.length - 1;
      else if (event.key === 'ArrowRight') next = (current + 1) % items.length;
      else if (event.key === 'ArrowLeft') next = (current - 1 + items.length) % items.length;
      else return;
      event.preventDefault();
      select(items[next]);
      buttons().find(item => item.getAttribute('aria-selected') === 'true')?.focus();
    });
    select(buttons().find(tab => tab.getAttribute('aria-selected') === 'true') || buttons()[0], false);
    return { select };
  }

  function dialog(element) {
    let opener = null;
    const releaseNotifications = () => window.DashboardNotifications?.release(element);
    element.addEventListener('beforetoggle', event => {
      if (event.newState === 'closed') releaseNotifications();
    });
    element.addEventListener('close', () => {
      releaseNotifications();
      opener?.focus();
    });
    element.addEventListener('click', event => {
      if (event.target !== element) return;
      const rect = element.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) element.close();
    });
    return {
      open(trigger = document.activeElement) {
        opener = trigger;
        element.showModal();
        // The modal top layer makes the page notification region inert.
        window.DashboardNotifications?.attach(element);
      },
      close() { element.close(); },
    };
  }
  window.UIComponents = { tabs, dialog };

  document.addEventListener('submit', event => {
    const message = event.submitter?.dataset.confirm || event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
})();
