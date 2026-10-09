/* Shared feedback for completed actions and validation throughout the dashboard.
   A modal dialog makes the rest of the page inert, so feedback is mounted inside
   the open dialog until that dialog closes. */
(() => {
  const duration = 5000;
  const exitDuration = 240;
  const icons = { success: '✓', error: '!', warning: '!', info: 'i' };

  function pageNotifications() {
    return document.getElementById('notifications');
  }

  function openModal() {
    if (typeof document.querySelectorAll !== 'function') return null;
    let dialogs;
    try {
      dialogs = document.querySelectorAll('dialog:modal');
    } catch (error) {
      if (error.name !== 'SyntaxError' && error.name !== 'DOMException') throw error;
      return null;
    }
    return dialogs[dialogs.length - 1] || null;
  }

  function dialogNotifications(dialog) {
    const existing = dialog.querySelector('[data-dialog-notifications]');
    if (existing) return existing;
    const host = document.createElement('div');
    host.className = 'notifications notifications--dialog';
    host.setAttribute('role', 'region');
    host.setAttribute('aria-label', 'Notifications');
    host.setAttribute('aria-live', 'polite');
    host.setAttribute('aria-relevant', 'additions');
    host.dataset.dialogNotifications = '';
    const heading = dialog.querySelector('h1, h2, h3, h4, h5, h6');
    if (heading) heading.after(host);
    else dialog.prepend(host);
    return host;
  }

  function notificationRegion() {
    const dialog = openModal();
    return dialog ? dialogNotifications(dialog) : pageNotifications();
  }

  function releaseDialogNotifications(dialog) {
    const host = dialog.querySelector?.('[data-dialog-notifications]');
    const page = pageNotifications();
    if (!host || !page || host === page) return;
    while (host.firstElementChild) page.appendChild(host.firstElementChild);
    host.remove();
  }

  window.DashboardNotifications = {
    attach: dialogNotifications,
    release: releaseDialogNotifications,
  };

  window.notify = (message, level = 'success') => {
    const region = notificationRegion();
    if (!region || !String(message).trim()) return;
    if (!Object.hasOwn(icons, level)) level = 'info';

    const notification = document.createElement('div');
    notification.className = `notification notification--${level}`;
    notification.setAttribute('role', level === 'error' ? 'alert' : 'status');
    notification.setAttribute('aria-atomic', 'true');

    const icon = document.createElement('span');
    icon.className = 'notification-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = icons[level];
    const text = document.createElement('span');
    text.className = 'notification-message';
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'notification-close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '×';

    let dismissed = false;
    let timer;
    let hovered = false;
    let focused = false;
    const resume = () => {
      window.clearTimeout(timer);
      if (!dismissed && !hovered && !focused) timer = window.setTimeout(dismiss, duration);
    };
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      window.clearTimeout(timer);
      notification.classList.add('notification--leaving');
      window.setTimeout(() => notification.remove(), exitDuration);
    };
    close.addEventListener('click', dismiss);
    notification.addEventListener('mouseenter', () => { hovered = true; window.clearTimeout(timer); });
    notification.addEventListener('mouseleave', () => { hovered = false; resume(); });
    notification.addEventListener('focusin', () => { focused = true; window.clearTimeout(timer); });
    notification.addEventListener('focusout', event => {
      focused = notification.contains(event.relatedTarget);
      resume();
    });
    notification.append(icon, text, close);
    region.appendChild(notification);
    resume();
    return dismiss;
  };

  document.addEventListener('DOMContentLoaded', () => {
    const messages = document.getElementById('notification-messages');
    if (!messages) return;
    messages.querySelectorAll('[data-notification-level]').forEach(message => {
      window.notify(message.textContent.trim(), message.dataset.notificationLevel);
    });
    messages.remove();
  });

  // Native validation may reject several fields in one submit attempt.
  let validationPending = false;
  document.addEventListener('invalid', () => {
    if (validationPending) return;
    validationPending = true;
    window.notify('Check Form Fields', 'error');
    window.setTimeout(() => { validationPending = false; }, 0);
  }, true);

  // Chrome reports dialog closure on beforetoggle and may not dispatch close.
  document.addEventListener('beforetoggle', event => {
    if (event.target?.matches?.('dialog') && event.newState === 'closed') {
      releaseDialogNotifications(event.target);
    }
  }, true);
  document.addEventListener('close', event => {
    if (event.target?.matches?.('dialog')) releaseDialogNotifications(event.target);
  }, true);
})();
