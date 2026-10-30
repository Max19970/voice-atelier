// Navigation, modal dialogs and small shared UI primitives (no framework).

import { t } from './i18n.js';
import { esc } from './util.js';

const dialog = document.getElementById('modal');
let activeSession = null;

export function navigate(tab) {
  window.dispatchEvent(new CustomEvent('studio:navigate', { detail: { tab } }));
}

export function isModalOpen() {
  return Boolean(dialog && dialog.open);
}

function cleanup() {
  if (!dialog) return;
  dialog.innerHTML = '';
  dialog.className = 'modal';
  if (activeSession) {
    const session = activeSession;
    activeSession = null;
    if (session.onClose) session.onClose();
    session.resolve(session.value);
  }
}

if (dialog) {
  dialog.addEventListener('close', cleanup);
  dialog.addEventListener('cancel', (event) => {
    if (activeSession && activeSession.dismissible === false) {
      event.preventDefault();
    }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog && activeSession && activeSession.dismissible !== false) {
      dialog.close();
    }
  });
}

/**
 * Opens a modal dialog.
 * @param {{title:string, body:Node|string, actions?:Array<{label:string,kind?:string,onClick?:Function,disabled?:boolean,autofocus?:boolean}>, dismissible?:boolean, onClose?:Function}} options
 * @returns {Promise<any>}
 */
export function openModal(options) {
  if (!dialog) return Promise.resolve(null);
  if (dialog.open) dialog.close();
  const { title, body, actions = [], dismissible = true, onClose } = options;

  dialog.innerHTML = '';
  dialog.className = 'modal';

  const header = document.createElement('header');
  header.className = 'modal-head';
  const heading = document.createElement('h2');
  heading.className = 'modal-title';
  heading.textContent = title;
  heading.id = 'modal-title';
  header.appendChild(heading);

  if (dismissible) {
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'icon-btn';
    close.setAttribute('aria-label', t('common.close'));
    close.title = t('common.close');
    close.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-x"></use></svg>';
    close.addEventListener('click', () => dialog.close());
    header.appendChild(close);
  }
  dialog.appendChild(header);

  const bodyNode = document.createElement('div');
  bodyNode.className = 'modal-body';
  bodyNode.id = 'modal-body';
  if (typeof body === 'string') bodyNode.innerHTML = body;
  else if (body) bodyNode.appendChild(body);
  dialog.appendChild(bodyNode);

  const footer = document.createElement('footer');
  footer.className = 'modal-foot';

  const session = { resolve: null, value: null, dismissible, onClose };
  const promise = new Promise((resolve) => {
    session.resolve = resolve;
  });
  activeSession = session;

  for (const action of actions) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `btn ${action.kind || 'ghost'}`;
    button.textContent = action.label;
    button.disabled = Boolean(action.disabled);
    if (action.autofocus) {
      button.dataset.autofocus = 'true';
    }
    button.addEventListener('click', async () => {
      let result;
      if (action.onClick) {
        result = await action.onClick({ dialog, close: () => dialog.close() });
        if (result === false) return;
      }
      session.value = action.value !== undefined ? action.value : result;
      dialog.close();
    });
    footer.appendChild(button);
  }
  if (footer.childElementCount) dialog.appendChild(footer);

  dialog.setAttribute('aria-labelledby', 'modal-title');
  dialog.showModal();

  const focusTarget =
    footer.querySelector('[data-autofocus="true"]') ||
    bodyNode.querySelector('input:not([type="hidden"]), textarea, select') ||
    footer.querySelector('button') ||
    header.querySelector('button');
  if (focusTarget) focusTarget.focus();

  return promise;
}

export function closeModal() {
  if (dialog && dialog.open) dialog.close();
}

/** Promise<boolean> confirm dialog. */
export function confirmDialog({ title, text, confirmLabel = t('common.delete'), cancelLabel = t('common.cancel'), danger = true }) {
  const body = document.createElement('p');
  body.className = 'modal-text';
  body.textContent = text;
  return openModal({
    title,
    body,
    actions: [
      { label: cancelLabel, kind: 'ghost', value: false },
      { label: confirmLabel, kind: danger ? 'danger' : 'primary', value: true, autofocus: true },
    ],
  }).then((value) => value === true);
}

/** Promise<string|null> single-input dialog. */
export function promptDialog({ title, label, value = '', confirmLabel = t('studio.create'), placeholder = '' }) {
  const wrapper = document.createElement('div');
  wrapper.className = 'field';
  const labelNode = document.createElement('label');
  labelNode.className = 'label';
  labelNode.textContent = label;
  const input = document.createElement('input');
  input.className = 'input';
  input.type = 'text';
  input.value = value;
  input.placeholder = placeholder;
  input.maxLength = 200;
  const id = `prompt-${Date.now()}`;
  input.id = id;
  labelNode.htmlFor = id;
  wrapper.append(labelNode, input);

  return openModal({
    title,
    body: wrapper,
    actions: [
      { label: t('common.cancel'), kind: 'ghost', value: null },
      {
        label: confirmLabel,
        kind: 'primary',
        onClick: () => input.value.trim() || false,
      },
    ],
  }).then((result) => (typeof result === 'string' ? result.trim() || null : null));
}

/** License acceptance dialog. Resolves true only when the box is checked. */
export function licenseDialog({ model }) {
  const wrapper = document.createElement('div');
  wrapper.className = 'stack-sm';
  const text = t('models.licenseBody', {
    license: model.license || '—',
    repo: model.repo || '—',
    revision: model.revision || '—',
  });
  const paragraph = document.createElement('p');
  paragraph.className = 'modal-text';
  paragraph.textContent = text;
  const warning = document.createElement('p');
  warning.className = 'modal-text muted';
  warning.textContent = t('models.notes');

  const checkLabel = document.createElement('label');
  checkLabel.className = 'check';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.id = `license-${model.id}`;
  const span = document.createElement('span');
  span.textContent = t('models.licenseAccept');
  checkLabel.append(checkbox, span);
  wrapper.append(paragraph, warning, checkLabel);

  return openModal({
    title: t('models.licenseTitle', { label: model.label || model.id }),
    body: wrapper,
    actions: [
      { label: t('common.cancel'), kind: 'ghost', onClick: () => false },
      {
        label: t('models.download'),
        kind: 'primary',
        onClick: () => {
          if (!checkbox.checked) {
            checkbox.focus();
            checkLabel.classList.add('shake');
            window.setTimeout(() => checkLabel.classList.remove('shake'), 400);
            return false;
          }
          return true;
        },
      },
    ],
  }).then((value) => value === true);
}

export function errorBlock(error) {
  if (!error) return '';
  const code = esc(error.code || 'error');
  const message = esc(error.message || '');
  return `<p class="error-line"><span class="error-code">${code}</span> ${message}</p>`;
}
