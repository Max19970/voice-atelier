// App shell: tabs, status, banners, jobs drawer, toasts, polling and boot.

import { state, subscribe, describeError } from './js/state.js';
import * as actions from './js/state.js';
import { t, getLang, setLang, applyStatic, initLang, stageLabel } from './js/i18n.js';
import { VIEWS } from './js/views.js';
import * as views from './js/views.js';
import { esc, fmtPercent, isTextEntry, downloadBlob } from './js/util.js';
import { stopPlayback, onPlayerChange, closeAudioContext } from './js/audio.js';
import { navigate, isModalOpen, closeModal } from './js/ui.js';
import { api, fetchBlobUrl, hasToken } from './js/api.js';

const TABS = ['studio', 'voices', 'design', 'models', 'guide'];
const TAB_TITLES = {
  studio: 'nav.studio',
  voices: 'nav.voices',
  design: 'nav.design',
  models: 'nav.models',
  guide: 'nav.guide',
};
const TAB_SUBS = {
  studio: 'studio.sub',
  voices: 'voices.sub',
  design: 'design.sub',
  models: 'models.sub',
  guide: 'guide.sub',
};

const view = document.getElementById('view');
const tabsBar = document.getElementById('tabs');
const statusCard = document.getElementById('status-card');
const pageTitle = document.getElementById('page-title');
const pageSub = document.getElementById('page-sub');
const banners = document.getElementById('banners');
const toastsBox = document.getElementById('toasts');
const jobsBox = document.getElementById('jobs');
const jobsBody = document.getElementById('jobs-body');
const jobsToggle = document.getElementById('jobs-toggle');
const jobsCount = document.getElementById('jobs-count');
const langToggle = document.getElementById('lang-toggle');

let lastFingerprint = '';
let pendingRender = false;
let pollTimer = 0;
let statusTimer = 0;
let booted = false;

/* ------------------------------- rendering ------------------------------- */

function fingerprint() {
  const project = state.project;
  const takes = project
    ? project.segments
        .map((segment) => `${segment.id}:${segment.selected_take_id || ''}:${(segment.takes || []).length}:${
          (segment.takes || []).some((take) => take.stale) ? 1 : 0
        }`)
        .join('|')
    : '';
  return [
    state.activeTab,
    state.status?.engine_available,
    state.status?.offline,
    state.status?.loaded_model || '',
    state.models.map((model) => `${model.id}:${model.installed ? 1 : 0}`).join(','),
    state.voices.map((voice) => voice.id).join(','),
    state.projects.map((projectItem) => `${projectItem.id}:${projectItem.title}`).join(','),
    project?.id || '',
    project?.title || '',
    project?.segments.length || 0,
    takes,
    state.edit.saving ? 1 : 0,
    state.edit.dirty ? 1 : 0,
    state.edit.error?.code || '',
    state.edit.conflict ? 1 : 0,
    state.jobs.map((job) => `${job.id}:${job.state}:${job.stage || ''}`).join(';'),
    state.export.exportId,
    state.export.running ? 1 : 0,
    state.design.result?.take_id || '',
    state.design.running ? 1 : 0,
    state.designs.length,
    state.voicesError?.code || '',
    state.modelsError?.code || '',
    state.projectsError?.code || '',
    booted ? 1 : 0,
  ].join('~');
}

function render({ force = false } = {}) {
  paintTabs();
  paintTopbar();
  paintStatusCard();
  paintBanners();
  paintToasts();
  paintJobs();
  paintLangToggle();
  views.paintAutosave(document);
  views.paintPlayButtons(document);

  const current = document.activeElement;
  const typingHere = isTextEntry(current) && view.contains(current);
  if (!force && typingHere) {
    pendingRender = true;
    return;
  }
  pendingRender = false;
  lastFingerprint = fingerprint();

  if (!booted) {
    view.innerHTML = `<div class="panel loading-panel">${esc(t('common.loading'))}</div>`;
    return;
  }

  const tab = state.activeTab;
  if (view.dataset.tab && view.dataset.tab !== tab) views.teardown();
  view.dataset.tab = tab;
  const renderer = VIEWS[tab];
  if (renderer) renderer(view);
  view.setAttribute('aria-labelledby', `tab-${tab}`);
}

function flushPendingRender() {
  window.setTimeout(() => {
    const current = document.activeElement;
    if (pendingRender && !(isTextEntry(current) && view.contains(current))) {
      render({ force: true });
    }
  }, 260);
}

/* --------------------------------- tabs ---------------------------------- */

function paintTabs() {
  for (const button of tabsBar.querySelectorAll('[data-tab]')) {
    const active = button.dataset.tab === state.activeTab;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  }
}

function activateTab(tab) {
  if (!TABS.includes(tab) || tab === state.activeTab) {
    if (tab === state.activeTab) render({ force: true });
    return;
  }
  views.teardown();
  stopPlayback();
  state.activeTab = tab;
  if (window.location.hash !== `#${tab}`) {
    window.history.replaceState(null, '', `#${tab}`);
  }
  if (tab === 'design') void actions.loadDesigns();
  if (tab === 'voices') void actions.loadVoices();
  if (tab === 'models') {
    void actions.loadModels();
    void actions.loadStatus();
  }
  render({ force: true });
  view.focus({ preventScroll: true });
}

function setupTabs() {
  tabsBar.addEventListener('click', (event) => {
    const button = event.target.closest('[data-tab]');
    if (!button) return;
    activateTab(button.dataset.tab);
  });
  tabsBar.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Home' && event.key !== 'End') {
      return;
    }
    event.preventDefault();
    const index = TABS.indexOf(state.activeTab);
    let next = index;
    if (event.key === 'ArrowDown') next = (index + 1) % TABS.length;
    if (event.key === 'ArrowUp') next = (index - 1 + TABS.length) % TABS.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = TABS.length - 1;
    activateTab(TABS[next]);
    const button = tabsBar.querySelector(`[data-tab="${TABS[next]}"]`);
    if (button) button.focus();
  });
  window.addEventListener('studio:navigate', (event) => {
    const tab = event.detail?.tab;
    if (tab) activateTab(tab);
  });
  window.addEventListener('hashchange', () => {
    const tab = window.location.hash.replace('#', '');
    if (TABS.includes(tab)) activateTab(tab);
  });
}

/* -------------------------------- topbar --------------------------------- */

function paintTopbar() {
  const tab = state.activeTab;
  pageTitle.textContent = t(TAB_TITLES[tab] || 'nav.studio');
  pageSub.textContent = t(TAB_SUBS[tab] || 'studio.sub');
  document.title = `${t(TAB_TITLES[tab] || 'nav.studio')} — Voice Atelier`;
}

function paintLangToggle() {
  const next = getLang() === 'ru' ? 'en' : 'ru';
  langToggle.textContent = next.toUpperCase();
  langToggle.title = next === 'en' ? 'Switch to English' : 'Переключить на русский';
  langToggle.setAttribute('aria-label', langToggle.title);
}

/* -------------------------------- status --------------------------------- */

function statusDot(kind) {
  return `<span class="dot ${kind}" aria-hidden="true"></span>`;
}

function paintStatusCard() {
  if (state.statusError) {
    statusCard.innerHTML = `<p class="status-line">${statusDot('danger')}<span>${esc(t('status.error'))}</span></p>
      <p class="status-meta">${esc(describeError(state.statusError).text)}</p>`;
    return;
  }
  const status = state.status;
  if (!status) {
    statusCard.innerHTML = `<p class="status-line">${statusDot('warn')}<span>${esc(t('common.loading'))}</span></p>`;
    return;
  }
  const device = status.device || {};
  const engineOk = Boolean(status.engine_available);
  const modelLabel = status.loaded_model || t('status.noModel');
  statusCard.innerHTML = `<p class="status-line">${statusDot(engineOk ? 'ok' : 'warn')}<span>${esc(
    engineOk ? t('status.engineOk') : t('status.engineMissing'),
  )}</span></p>
    <p class="status-meta">${esc(device.name || t('status.cpu'))}${
      device.vram_mb ? ` · ${esc(String(device.vram_mb))} MB` : ''
    }</p>
    <p class="status-meta">${esc(t('status.model'))}: ${esc(modelLabel)}</p>
    ${status.offline ? `<p class="status-meta warn">${esc(t('status.offline'))}</p>` : ''}`;
}

/* -------------------------------- banners -------------------------------- */

function paintBanners() {
  const items = [];
  if (!hasToken) {
    items.push({ kind: 'danger', text: t('banner.tokenMissing') });
  }
  if (state.statusError) {
    items.push({ kind: 'danger', text: `${t('status.error')}: ${describeError(state.statusError).text}` });
  }
  const warnings = Array.isArray(state.status?.warnings) ? state.status.warnings : [];
  for (const warning of warnings) items.push({ kind: 'warn', text: warning });
  if (state.status?.offline) items.push({ kind: 'warn', text: t('banner.offline') });
  if (state.models.length && !state.models.some((model) => model.installed)) {
    items.push({ kind: 'warn', text: t('banner.noModels') });
  }
  if (state.edit.conflict) {
    items.push({ kind: 'danger', text: t('errors.staleProject') });
  } else if (state.edit.error) {
    items.push({ kind: 'danger', text: t('banner.saveError', { msg: describeError(state.edit.error).text }) });
  }
  if (!items.length) {
    banners.innerHTML = '';
    return;
  }
  banners.innerHTML = items
    .map(
      (item) => `<div class="banner ${item.kind}" role="status">${esc(item.text)}</div>`,
    )
    .join('');
}

/* --------------------------------- toasts -------------------------------- */

function paintToasts() {
  if (!state.toasts.length) {
    toastsBox.innerHTML = '';
    return;
  }
  toastsBox.innerHTML = state.toasts
    .map(
      (item) => `<div class="toast ${item.kind}" role="status">
        <span class="toast-text">${esc(item.text)}</span>
        <button type="button" class="icon-btn xs" data-toast="${esc(item.id)}" aria-label="${esc(
          t('common.close'),
        )}"><svg class="icon" aria-hidden="true"><use href="#i-x"></use></svg></button>
      </div>`,
    )
    .join('');
}

/* ---------------------------------- jobs --------------------------------- */

const JOB_KIND_LABEL = {
  download: 'jobs.kindDownload',
  generate: 'jobs.kindGenerate',
  design: 'jobs.kindDesign',
  export: 'jobs.kindExport',
  unload: 'jobs.kindUnload',
};

function jobStateLabel(jobState) {
  return t(`jobs.${jobState}`);
}

function segmentIndex(segmentId) {
  if (!state.project) return null;
  const index = state.project.segments.findIndex((segment) => segment.id === segmentId);
  return index >= 0 ? index + 1 : null;
}

function jobItem(job) {
  const active = job.state === 'queued' || job.state === 'running';
  const result = job.result || {};
  const kindLabel = t(JOB_KIND_LABEL[job.kind] || 'jobs.kindOther');
  const progress = Number.isFinite(job.progress) ? job.progress : 0;

  let extra = '';
  if (job.state === 'failed' && job.error) {
    extra = `<p class="error-line"><span class="error-code">${esc(job.error.code)}</span> ${esc(
      job.error.message,
    )}</p>`;
  }
  if (job.kind === 'generate' && Array.isArray(result.failed) && result.failed.length) {
    const rows = result.failed
      .slice(0, 5)
      .map((failure) => {
        const number = segmentIndex(failure.segment_id);
        return `<li>${number ? `#${number} · ` : ''}<span class="error-code">${esc(
          failure.code || 'error',
        )}</span> ${esc(failure.message || '')}</li>`;
      })
      .join('');
    extra += `<ul class="warn-list">${rows}</ul>`;
  }

  let actionsHtml = '';
  if (active) {
    actionsHtml = `<button type="button" class="btn ghost xs" data-act="cancel-job" data-job="${esc(job.id)}" ${
      job.cancel_requested ? 'disabled' : ''
    }>${esc(job.cancel_requested ? t('jobs.canceling') : t('jobs.cancel'))}</button>`;
  } else if (job.state === 'done' && job.kind === 'export' && result.export_id) {
    actionsHtml = `<button type="button" class="btn ghost xs" data-act="download-export" data-export="${esc(
      result.export_id,
    )}" data-filename="${esc(result.filename || '')}" data-format="${esc(result.format || '')}">${esc(
      t('jobs.download'),
    )}</button>`;
  } else if (job.state === 'done' && job.kind === 'design' && result.take_id) {
    actionsHtml = `<button type="button" class="btn ghost xs" data-act="open-design" data-job="${esc(
      job.id,
    )}">${esc(t('jobs.openDesign'))}</button>`;
  }

  return `<article class="job is-${esc(job.state)}" data-job-item="${esc(job.id)}">
    <div class="job-head">
      <span class="job-kind">${esc(kindLabel)}</span>
      <span class="job-state ${esc(job.state)}">${esc(jobStateLabel(job.state))}</span>
    </div>
    ${job.stage ? `<p class="job-stage">${esc(stageLabel(job.stage))}</p>` : ''}
    <div class="job-bar">
      <progress max="1" value="${esc(progress)}" data-job="${esc(job.id)}" aria-label="${esc(
        kindLabel,
      )}"></progress>
      <span class="meta" data-job-percent="${esc(job.id)}">${esc(fmtPercent(progress))}</span>
    </div>
    ${extra}
    <div class="job-foot">${actionsHtml}</div>
  </article>`;
}

function paintJobs() {
  const active = state.jobs.filter((job) => job.state === 'queued' || job.state === 'running').length;
  if (jobsCount) {
    jobsCount.hidden = active === 0;
    jobsCount.textContent = String(active);
  }
  // Historical failures must not cover the workspace: auto-open only while a job
  // is active, and never override an explicit user choice.
  if (!state.ui.jobsTouched) state.ui.jobsOpen = active > 0;
  jobsBox.classList.toggle('is-open', state.ui.jobsOpen);
  jobsToggle.setAttribute('aria-expanded', state.ui.jobsOpen ? 'true' : 'false');

  if (!state.jobs.length) {
    jobsBody.innerHTML = `<p class="empty small">${esc(t('jobs.empty'))}</p>`;
    return;
  }
  const items = state.jobs.slice(0, 30);
  jobsBody.innerHTML = `<p class="hint small">${esc(t('jobs.serial'))}</p>${items
    .map(jobItem)
    .join('')}`;
}

function paintProgress() {
  for (const element of document.querySelectorAll('progress[data-job]')) {
    const job = state.jobs.find((item) => item.id === element.dataset.job);
    if (!job) continue;
    const progress = Number.isFinite(job.progress) ? job.progress : 0;
    element.value = progress;
    element.setAttribute('aria-valuetext', fmtPercent(progress));
  }
  for (const element of document.querySelectorAll('[data-job-percent]')) {
    const job = state.jobs.find((item) => item.id === element.dataset.jobPercent);
    if (job) element.textContent = fmtPercent(job.progress || 0);
  }
}

function setupJobs() {
  jobsToggle.addEventListener('click', () => {
    state.ui.jobsTouched = true;
    state.ui.jobsOpen = !state.ui.jobsOpen;
    paintJobs();
  });
  jobsBody.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-act]');
    if (!button) return;
    const act = button.dataset.act;
    if (act === 'cancel-job') {
      await actions.cancelJob(button.dataset.job);
      return;
    }
    if (act === 'download-export') {
      await downloadExport(button.dataset.export, button.dataset.filename, button.dataset.format);
      return;
    }
    if (act === 'open-design') {
      const job = state.jobs.find((item) => item.id === button.dataset.job);
      if (job && job.result?.take_id) {
        state.design.result = { take_id: job.result.take_id, voice_id: job.result.voice_id || null };
        activateTab('design');
      }
    }
  });
}

async function downloadExport(exportId, filename, format) {
  if (!exportId) {
    actions.toastError({ code: 'no_export', message: t('errors.noExport') });
    return;
  }
  try {
    const fallback = `voice-atelier-export.${format === 'zip' ? 'zip' : 'wav'}`;
    const { blob, filename: serverName } = await fetchBlobUrl(api.exportDownload(exportId));
    downloadBlob(blob, serverName || filename || fallback);
  } catch (error) {
    actions.toastError(error);
  }
}

/* --------------------------------- toasts -------------------------------- */

function setupToasts() {
  toastsBox.addEventListener('click', (event) => {
    const button = event.target.closest('[data-toast]');
    if (button) actions.dismissToast(button.dataset.toast);
  });
}

/* ---------------------------------- lang --------------------------------- */

function setupLang() {
  langToggle.addEventListener('click', () => {
    setLang(getLang() === 'ru' ? 'en' : 'ru');
    applyStatic(document);
    render({ force: true });
  });
}

/* -------------------------------- polling -------------------------------- */

function scheduleJobsPoll() {
  window.clearTimeout(pollTimer);
  const active = state.jobs.some((job) => job.state === 'queued' || job.state === 'running');
  const delay = active ? 1000 : 6000;
  pollTimer = window.setTimeout(async () => {
    if (!document.hidden) {
      await actions.refreshJobs();
      paintProgress();
    }
    scheduleJobsPoll();
  }, delay);
}

function setupPolling() {
  statusTimer = window.setInterval(() => {
    if (!document.hidden) void actions.loadStatus();
  }, 20000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      void actions.loadStatus();
      void actions.refreshJobs();
    }
  });
}

function setupRefresh() {
  const button = document.getElementById('refresh-all');
  if (!button) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await Promise.allSettled([
        actions.loadStatus(),
        actions.loadModels(),
        actions.loadVoices(),
        actions.loadProjects(),
        actions.refreshJobs(),
      ]);
      actions.toast('success', t('toast.refreshed'));
    } finally {
      button.disabled = false;
    }
  });
}

/* --------------------------------- boot ---------------------------------- */

async function boot() {
  initLang();
  applyStatic(document);
  setupTabs();
  setupJobs();
  setupToasts();
  setupLang();
  setupRefresh();

  const hash = window.location.hash.replace('#', '');
  if (TABS.includes(hash)) state.activeTab = hash;

  subscribe(() => {
    const next = fingerprint();
    const changed = next !== lastFingerprint;
    if (!changed) {
      paintStatusCard();
      paintBanners();
      paintToasts();
      paintProgress();
      return;
    }
    render();
  });

  onPlayerChange(() => views.paintPlayButtons(document));
  document.addEventListener('focusout', flushPendingRender);

  await Promise.allSettled([
    actions.loadStatus(),
    actions.loadModels(),
    actions.loadVoices(),
    actions.loadProjects(),
    actions.refreshJobs(),
  ]);

  booted = true;
  if (!state.project && state.projects.length) {
    await actions.openProject(state.projects[0].id);
  }
  render({ force: true });
  scheduleJobsPoll();
  setupPolling();

  if (!hasToken) actions.toast('error', t('banner.tokenMissing'), { ttl: 15000 });
}

window.addEventListener('beforeunload', () => {
  views.teardown();
  stopPlayback();
  closeAudioContext();
});

window.addEventListener('keydown', (event) => {
  if (event.altKey && !event.ctrlKey && !event.metaKey) {
    const index = Number(event.key);
    if (index >= 1 && index <= 5) {
      event.preventDefault();
      activateTab(TABS[index - 1]);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (state.activeTab === 'studio' && state.project) void actions.generate({ force: false });
      return;
    }
  }
  if (event.key === 'Escape') {
    if (isModalOpen()) closeModal();
    else if (state.ui.jobsOpen) {
      state.ui.jobsOpen = false;
      paintJobs();
    }
  }
});

void boot();
