// Application state, data loading and mutations. Views render from this module;
// nothing here touches the DOM except for tiny indicator hooks.

import { api, ApiError, errorText } from './api.js';
import { t } from './i18n.js';
import { clamp, uuid } from './util.js';
import { designCompletion, newestDesign, parseSeed } from './design-state.js';
import { currentAudio, pausePlayback } from './audio.js';

export const LIMITS = {
  segments: 200,
  text: 10000,
  instruction: 1500,
  designText: 240,
  transcript: 5000,
  uploadBytes: 50 * 1024 * 1024,
};

export const state = {
  activeTab: 'studio',
  status: null,
  statusError: null,
  statusAt: 0,

  models: [],
  modelsError: null,
  modelsAt: 0,

  voices: [],
  voicesError: null,
  voicesAt: 0,

  projects: [],
  projectsError: null,

  project: null,
  projectLoading: false,

  edit: {
    rev: 0,
    dirty: false,
    saving: false,
    error: null,
    conflict: false,
    savedAt: 0,
    epoch: 0,
  },

  jobs: [],
  jobsError: null,
  jobsAt: 0,

  designs: [],
  designsError: null,
  design: { running: false, pending: false, jobId: null, result: null, suppressRestore: false },

  export: { running: false, jobId: null, exportId: '', filename: '', format: '' },

  installJobs: {},

  voiceForm: {
    name: '',
    language: 'ru',
    transcript: '',
    consent: false,
    basis: 'own',
    trimStart: 0,
    trimEnd: '',
    file: null,
    fileUrl: '',
    fileDuration: 0,
    fileError: '',
    busy: false,
    recording: false,
    recordSeconds: 0,
    error: null,
    lastSaved: null,
  },

  designForm: {
    name: '',
    text: '',
    instruction: '',
    language: 'ru',
    seed: 42,
    saveAsVoice: false,
    consent: true,
    busy: false,
    error: null,
  },

  toasts: [],
  ui: { jobsOpen: false, jobsTouched: false, lastRendered: 0 },
};

/* ------------------------------- pub/sub -------------------------------- */

const subscribers = new Set();

export function subscribe(fn) {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

export function emit() {
  for (const fn of subscribers) {
    try {
      fn();
    } catch (error) {
      console.error('subscriber failed', error);
    }
  }
}

/* -------------------------------- helpers ------------------------------- */

export function modelsOfKind(kind) {
  return state.models.filter((model) => model.kind === kind);
}

export function modelById(id) {
  return state.models.find((model) => model.id === id) || null;
}

export function modelLabel(id) {
  const model = modelById(id);
  return model ? model.label : id;
}

export function voiceById(id) {
  return state.voices.find((voice) => voice.id === id) || null;
}

export function languages() {
  const list = state.status?.capabilities?.languages;
  if (Array.isArray(list) && list.length) return list;
  return ['ru', 'en'];
}

export function defaultLanguage() {
  const list = languages();
  return list.includes('ru') ? 'ru' : list[0];
}

export function defaultModel(kind) {
  const models = modelsOfKind(kind);
  const installed = models.find((model) => model.installed);
  if (installed) return installed.id;
  if (models.length) return models[0].id;
  return kind === 'design' ? 'qwen-design-1.7b' : 'qwen-base-1.7b';
}

export function rateBounds() {
  const rate = state.status?.capabilities?.rate;
  return {
    min: Number.isFinite(rate?.min) ? rate.min : 0.5,
    max: Number.isFinite(rate?.max) ? rate.max : 2,
    mode: rate?.mode || 'postprocess',
  };
}

/** VoiceDesign synthesizes short text only: a new identity per chunk would drift. */
export function designLimit() {
  const capabilities = state.status?.capabilities;
  const actual = Number(capabilities?.max_design_chars);
  if (Number.isFinite(actual) && actual > 0) return actual;
  const alias = Number(capabilities?.design_max_chars);
  if (Number.isFinite(alias) && alias > 0) return alias;
  return LIMITS.designText;
}

export function activeJobs() {
  return state.jobs.filter((job) => job.state === 'queued' || job.state === 'running');
}

export function isBusy() {
  return activeJobs().length > 0;
}

export function hasInstalledModel(kind = null) {
  return state.models.some((model) => model.installed && (kind ? model.kind === kind : true));
}

export function voiceUsedInProject(voiceId) {
  if (!state.project) return false;
  return state.project.segments.some((segment) => segment.mode !== 'design' && segment.voice_id === voiceId);
}

const ERROR_KEYS = {
  unauthorized: 'errors.token',
  invalid_token: 'errors.token',
  missing_token: 'errors.token',
  forbidden: 'errors.token',
  forbidden_origin: 'errors.origin',
  bad_origin: 'errors.origin',
  invalid_origin: 'errors.origin',
  not_found: 'errors.notFound',
  validation_error: 'errors.validation',
  invalid_request: 'errors.validation',
  bad_request: 'errors.validation',
  clone_instruction_not_allowed: 'errors.cloneInstruction',
  instruction_not_allowed: 'errors.cloneInstruction',
  transcript_required: 'errors.transcriptMissing',
  missing_transcript: 'errors.transcriptMissing',
  transcript_too_long: 'errors.transcriptTooLong',
  invalid_transcript: 'errors.transcriptTooLong',
  invalid_name: 'errors.nameInvalid',
  name_too_long: 'errors.nameInvalid',
  model_not_installed: 'errors.modelMissing',
  model_missing: 'errors.modelMissing',
  engine_unavailable: 'errors.engine',
  offline: 'errors.offline',
  downloads_disabled: 'errors.offline',
  voice_in_use: 'errors.voiceInUse',
  consent_required: 'errors.consent',
  upload_too_large: 'errors.fileTooBig',
  file_too_large: 'errors.fileTooBig',
  unsupported_media: 'errors.unsupportedMedia',
  too_many_segments: 'errors.tooManySegments',
  text_too_long: 'errors.textTooLong',
  instruction_too_long: 'errors.instructionTooLong',
  seed_invalid: 'errors.seedInvalid',
  design_text_too_long: 'errors.designTextTooLong',
  job_not_cancelable: 'errors.jobNotCancelable',
  export_incomplete: 'errors.exportBlocked',
  stale_takes: 'errors.exportBlocked',
  busy: 'errors.busy',
  conflict: 'errors.staleProject',
  network_error: 'errors.network',
};

/** Localized description for an API error; keeps the machine code visible. */
export function describeError(error) {
  if (!error) return { code: 'error', text: t('errors.generic') };
  const code = error.code || 'error';
  const key = ERROR_KEYS[code];
  const base = key ? t(key) : error.message || t('errors.generic');
  return { code, text: base };
}

/* -------------------------------- toasts -------------------------------- */

let toastSeq = 0;

export function toast(kind, text, { ttl } = {}) {
  toastSeq += 1;
  const item = { id: `toast-${toastSeq}`, kind, text };
  state.toasts.push(item);
  const lifetime = typeof ttl === 'number' ? ttl : kind === 'error' ? 9000 : 4200;
  window.setTimeout(() => dismissToast(item.id), lifetime);
  emit();
}

export function toastError(error) {
  const { code, text } = describeError(error);
  toast('error', `${code} — ${text}`);
}

export function dismissToast(id) {
  const index = state.toasts.findIndex((item) => item.id === id);
  if (index >= 0) {
    state.toasts.splice(index, 1);
    emit();
  }
}

/* ------------------------------- loaders -------------------------------- */

export async function loadStatus() {
  try {
    state.status = await api.status();
    state.statusError = null;
  } catch (error) {
    state.statusError = error;
  }
  state.statusAt = Date.now();
  emit();
}

export async function loadModels() {
  try {
    const payload = await api.models();
    state.models = Array.isArray(payload?.items) ? payload.items : [];
    state.modelsError = null;
  } catch (error) {
    state.modelsError = error;
  }
  state.modelsAt = Date.now();
  emit();
}

export async function loadVoices() {
  try {
    const payload = await api.voices();
    state.voices = Array.isArray(payload?.items) ? payload.items : [];
    state.voicesError = null;
  } catch (error) {
    state.voicesError = error;
  }
  state.voicesAt = Date.now();
  emit();
}

export async function loadProjects() {
  try {
    const payload = await api.projects();
    state.projects = Array.isArray(payload?.items) ? payload.items : [];
    state.projectsError = null;
  } catch (error) {
    state.projectsError = error;
  }
  emit();
}

export async function loadDesigns() {
  try {
    const payload = await api.designs();
    state.designs = Array.isArray(payload?.items) ? payload.items : [];
    state.designsError = null;
    if (!state.design.result && !state.design.running && !state.design.pending && !state.design.jobId && !state.design.suppressRestore) {
      state.design.result = newestDesign(state.designs);
    }
  } catch (error) {
    state.designsError = error;
    state.designs = state.designs || [];
  }
  emit();
}

/* ------------------------------- projects -------------------------------- */

function segmentPayload(segment) {
  return {
    id: segment.id,
    text: segment.text,
    voice_id: segment.voice_id,
    mode: segment.mode,
    model_id: segment.model_id,
    language: segment.language,
    instruction: segment.instruction || '',
    rate: segment.rate,
    pause_ms: segment.pause_ms,
    seed: segment.seed,
    selected_take_id: segment.selected_take_id,
  };
}

function normalizeSegment(raw) {
  return {
    id: raw.id,
    text: raw.text ?? '',
    voice_id: raw.voice_id ?? null,
    mode: raw.mode === 'design' ? 'design' : 'clone',
    model_id: raw.model_id || defaultModel(raw.mode === 'design' ? 'design' : 'clone'),
    language: raw.language || defaultLanguage(),
    instruction: raw.instruction || '',
    rate: Number.isFinite(raw.rate) ? raw.rate : 1,
    pause_ms: Number.isFinite(raw.pause_ms) ? raw.pause_ms : 300,
    seed: Number.isFinite(raw.seed) ? raw.seed : 42,
    selected_take_id: raw.selected_take_id ?? null,
    takes: Array.isArray(raw.takes) ? raw.takes : [],
  };
}

/** Merges server-owned fields (takes, selection) without touching the editor text. */
function mergeProject(updated, { adoptText = false } = {}) {
  if (!updated || !state.project || updated.id !== state.project.id) return;
  const local = state.project;
  const byId = new Map(local.segments.map((segment) => [segment.id, segment]));

  local.title = adoptText ? updated.title : local.title;
  local.updated_at = updated.updated_at;

  if (adoptText) {
    local.segments = (updated.segments || []).map(normalizeSegment);
    return;
  }

  for (const serverSegment of updated.segments || []) {
    const mine = byId.get(serverSegment.id);
    if (!mine) {
      local.segments.push(normalizeSegment(serverSegment));
      continue;
    }
    mine.takes = Array.isArray(serverSegment.takes) ? serverSegment.takes : mine.takes;
    mine.selected_take_id = serverSegment.selected_take_id ?? null;
  }
}

export async function openProject(id) {
  if (!id) {
    state.project = null;
    state.edit.epoch += 1;
    state.edit.dirty = false;
    state.edit.conflict = false;
    state.edit.error = null;
    emit();
    return;
  }
  if (state.edit.dirty && state.project && state.project.id !== id) {
    try {
      await saveProjectNow();
    } catch (_) {
      /* keep going: opening another project must stay possible */
    }
  }
  state.projectLoading = true;
  state.edit.epoch += 1;
  state.edit.saving = false;
  state.edit.error = null;
  const epoch = state.edit.epoch;
  emit();
  try {
    const project = await api.project(id);
    if (epoch !== state.edit.epoch) return;
    state.project = {
      id: project.id,
      title: project.title || '',
      created_at: project.created_at,
      updated_at: project.updated_at,
      segments: (project.segments || []).map(normalizeSegment),
    };
    state.edit.dirty = false;
    state.edit.conflict = false;
    state.edit.error = null;
    state.edit.savedAt = Date.now();
  } catch (error) {
    if (epoch === state.edit.epoch) toastError(error);
  } finally {
    state.projectLoading = false;
    emit();
  }
}

export async function reloadProject({ adoptText = false } = {}) {
  if (!state.project) return;
  const id = state.project.id;
  const epoch = state.edit.epoch;
  try {
    const project = await api.project(id);
    if (epoch !== state.edit.epoch || state.project?.id !== id) return;
    mergeProject(project, { adoptText });
    emit();
  } catch (error) {
    toastError(error);
  }
}

export async function createProject(title) {
  try {
    if (state.project && state.edit.dirty) await saveProjectNow();
    const project = await api.createProject((title || '').trim() || t('studio.newProject'));
    state.projects = [{ id: project.id, title: project.title, updated_at: project.updated_at }, ...state.projects];
    state.project = {
      id: project.id,
      title: project.title,
      created_at: project.created_at,
      updated_at: project.updated_at,
      segments: [],
    };
    state.edit.dirty = false;
    state.edit.conflict = false;
    state.edit.savedAt = Date.now();
    toast('success', t('toast.projectCreated'));
    emit();
    return project;
  } catch (error) {
    toastError(error);
    return null;
  }
}

export async function deleteProject(id) {
  try {
    await api.deleteProject(id);
    state.projects = state.projects.filter((project) => project.id !== id);
    if (state.project?.id === id) {
      state.project = null;
      state.edit.dirty = false;
      state.edit.conflict = false;
    }
    toast('success', t('toast.projectDeleted'));
    emit();
  } catch (error) {
    toastError(error);
  }
}

export function renameProject(title) {
  if (!state.project) return;
  state.project.title = title;
  markDirty();
}

/* ------------------------------- autosave -------------------------------- */

const saveState = { timer: 0, active: null };

export function markDirty() {
  state.edit.rev += 1;
  state.edit.dirty = true;
  state.edit.error = null;
  window.clearTimeout(saveState.timer);
  saveState.timer = window.setTimeout(() => {
    void saveProjectNow();
  }, 900);
  emit();
}

function buildPayload() {
  return {
    title: state.project.title,
    segments: state.project.segments.map(segmentPayload),
  };
}

async function performSave() {
  const project = state.project;
  if (!project) return true;
  if (!state.edit.dirty) return true;
  if (state.edit.conflict) return false;

  const epoch = state.edit.epoch;
  const projectId = project.id;
  const rev = state.edit.rev;
  const payload = buildPayload();

  state.edit.saving = true;
  emit();
  try {
    const updated = await api.saveProject(projectId, payload);
    if (epoch !== state.edit.epoch || state.project?.id !== projectId) return true;
    mergeProject(updated, { adoptText: false });
    state.edit.error = null;
    state.edit.savedAt = Date.now();
    if (state.edit.rev === rev) state.edit.dirty = false;
    return true;
  } catch (error) {
    if (epoch === state.edit.epoch) {
      state.edit.error = error;
      if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
        state.edit.conflict = true;
      }
    }
    return false;
  } finally {
    if (epoch === state.edit.epoch) state.edit.saving = false;
    if (
      epoch === state.edit.epoch &&
      state.edit.dirty &&
      !state.edit.conflict &&
      state.edit.rev !== rev
    ) {
      window.clearTimeout(saveState.timer);
      saveState.timer = window.setTimeout(() => {
        void saveProjectNow();
      }, 300);
    }
    emit();
  }
}

export function saveProjectNow() {
  if (saveState.active) {
    return saveState.active.then(() => {
      if (state.edit.dirty && !state.edit.conflict) return saveProjectNow();
      return !state.edit.error;
    });
  }
  saveState.active = performSave().finally(() => {
    saveState.active = null;
  });
  return saveState.active;
}

async function ensureSaved() {
  if (!state.project) throw new ApiError({ code: 'no_project', message: t('errors.noProject') });
  if (state.edit.conflict) throw new ApiError({ code: 'conflict', message: t('errors.staleProject') });
  await saveProjectNow();
  if (state.edit.dirty) {
    await saveProjectNow();
  }
  if (state.edit.error) throw state.edit.error;
  if (state.edit.conflict) throw new ApiError({ code: 'conflict', message: t('errors.staleProject') });
}

/* ------------------------------- segments -------------------------------- */

export function addSegment(afterId = null) {
  if (!state.project) return null;
  if (state.project.segments.length >= LIMITS.segments) {
    toastError(new ApiError({ code: 'too_many_segments', message: t('errors.tooManySegments') }));
    return null;
  }
  const segment = {
    id: uuid(),
    text: '',
    voice_id: state.voices[0]?.id ?? null,
    mode: 'clone',
    model_id: defaultModel('clone'),
    language: defaultLanguage(),
    instruction: '',
    rate: 1,
    pause_ms: 300,
    seed: 42,
    selected_take_id: null,
    takes: [],
  };
  const segments = state.project.segments;
  const index = afterId ? segments.findIndex((item) => item.id === afterId) : segments.length - 1;
  segments.splice(index + 1, 0, segment);
  markDirty();
  return segment;
}

export function deleteSegment(id) {
  if (!state.project) return;
  state.project.segments = state.project.segments.filter((segment) => segment.id !== id);
  markDirty();
  emit();
}

export function moveSegment(id, direction) {
  if (!state.project) return;
  const segments = state.project.segments;
  const index = segments.findIndex((segment) => segment.id === id);
  if (index < 0) return;
  const target = index + direction;
  if (target < 0 || target >= segments.length) return;
  const [segment] = segments.splice(index, 1);
  segments.splice(target, 0, segment);
  markDirty();
  emit();
}

export function updateSegment(id, patch, { dirty = true } = {}) {
  if (!state.project) return;
  const segment = state.project.segments.find((item) => item.id === id);
  if (!segment) return;
  Object.assign(segment, patch);
  if (dirty) markDirty();
  else emit();
}

/** Splits every segment on blank lines, preserving per-segment settings. */
export function splitParagraphs() {
  if (!state.project) return 0;
  const next = [];
  let created = 0;
  for (const segment of state.project.segments) {
    const paragraphs = String(segment.text || '')
      .split(/\n\s*\n/)
      .map((part) => part.trim())
      .filter(Boolean);
    if (paragraphs.length <= 1) {
      next.push(segment);
      continue;
    }
    paragraphs.forEach((text, index) => {
      const piece = {
        ...segment,
        id: index === 0 ? segment.id : uuid(),
        text: text.slice(0, LIMITS.text),
        selected_take_id: null,
        takes: [],
      };
      next.push(piece);
      if (index > 0) created += 1;
    });
  }
  if (!created) {
    toast('info', t('studio.splitNothing'));
    return 0;
  }
  if (next.length > LIMITS.segments) {
    toastError(new ApiError({ code: 'too_many_segments', message: t('errors.tooManySegments') }));
    return 0;
  }
  state.project.segments = next;
  markDirty();
  emit();
  return created;
}

/* ------------------------------ generation ------------------------------- */

function upsertJob(job) {
  if (!job || !job.id) return;
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  else state.jobs.unshift(job);
  emit();
}

export async function generate({ segmentIds = null, force = false } = {}) {
  if (!state.project) {
    toastError(new ApiError({ code: 'no_project', message: t('errors.noProject') }));
    return;
  }
  if (isBusy()) {
    toast('info', t('studio.busy'));
    return;
  }
  try {
    await ensureSaved();
    const body = { force };
    if (Array.isArray(segmentIds) && segmentIds.length) body.segment_ids = segmentIds;
    const job = await api.generate(state.project.id, body);
    upsertJob(job);
    toast('info', t('studio.generating'));
  } catch (error) {
    toastError(error);
  }
}

export async function selectTake(segmentId, takeId) {
  if (!state.project) return;
  try {
    if (state.edit.dirty) await saveProjectNow();
    const updated = await api.selectTake(state.project.id, { segment_id: segmentId, take_id: takeId });
    mergeProject(updated, { adoptText: false });
    emit();
  } catch (error) {
    toastError(error);
  }
}

/* --------------------------------- export -------------------------------- */

export async function exportProject({ format = 'wav', normalize = false, allowStale = false } = {}) {
  if (!state.project) {
    toastError(new ApiError({ code: 'no_project', message: t('errors.noProject') }));
    return;
  }
  if (isBusy()) {
    toast('info', t('studio.busy'));
    return;
  }
  try {
    await ensureSaved();
    const job = await api.exportProject(state.project.id, { format, normalize, allow_stale: allowStale });
    state.export.running = true;
    state.export.jobId = job.id;
    state.export.exportId = '';
    state.export.filename = '';
    upsertJob(job);
    emit();
  } catch (error) {
    toastError(error);
  }
}

export async function downloadExport() {
  const id = state.export.exportId;
  if (!id) {
    toastError(new ApiError({ code: 'no_export', message: t('errors.noExport') }));
    return;
  }
  return id;
}

/* --------------------------------- design -------------------------------- */

export async function runDesign() {
  const form = state.designForm;
  if (form.busy) return;
  if (!form.text.trim()) {
    form.error = new ApiError({ code: 'validation_error', message: t('design.needText') });
    emit();
    return;
  }
  if (!form.instruction.trim()) {
    form.error = new ApiError({ code: 'validation_error', message: t('design.needInstruction') });
    emit();
    return;
  }
  if (form.text.length > designLimit()) {
    form.error = new ApiError({
      code: 'design_text_too_long',
      message: t('design.textTooLong', { max: designLimit() }),
    });
    emit();
    return;
  }
  const seed = parseSeed(form.seed);
  if (!seed.ok) {
    form.error = new ApiError({ code: 'seed_invalid', message: t('errors.seedInvalid') });
    emit();
    return;
  }

  // A new run replaces the visible result. The previous clip stays in history,
  // but must not remain playable as if it belonged to the pending job.
  const previous = state.design.result;
  if (previous && previous.take_id) {
    const previousUrl = api.takeAudio(previous.take_id);
    if (currentAudio().url === previousUrl) pausePlayback();
  }
  state.design.result = null;
  state.design.pending = true;
  state.design.jobId = null;
  state.design.suppressRestore = false;

  form.busy = true;
  form.error = null;
  emit();
  try {
    const job = await api.design({
      name: form.name.trim() || t('design.title'),
      text: form.text,
      instruction: form.instruction,
      language: form.language,
      seed: seed.value,
      save_as_voice: Boolean(form.saveAsVoice),
      consent: true,
    });
    state.design.jobId = job.id;
    state.design.running = true;
    state.design.pending = false;
    upsertJob(job);
  } catch (error) {
    state.design.pending = false;
    state.design.running = false;
    state.design.suppressRestore = true;
    form.busy = false;
    form.error = error;
    toastError(error);
    emit();
  }
}

/* --------------------------------- voices -------------------------------- */

export function resetVoiceForm() {
  const language = state.voiceForm.language || defaultLanguage();
  state.voiceForm = {
    name: '',
    language,
    transcript: '',
    consent: false,
    basis: 'own',
    trimStart: 0,
    trimEnd: '',
    file: null,
    fileUrl: '',
    fileDuration: 0,
    fileError: '',
    busy: false,
    recording: false,
    recordSeconds: 0,
    error: null,
    lastSaved: null,
  };
  emit();
}

export async function submitVoice() {
  const form = state.voiceForm;
  if (form.busy) return;
  if (!form.file) {
    form.error = new ApiError({ code: 'validation_error', message: t('voices.noFile') });
    emit();
    return;
  }
  if (!form.consent) {
    form.error = new ApiError({ code: 'consent_required', message: t('errors.consent') });
    emit();
    return;
  }
  if (!form.transcript.trim()) {
    form.error = new ApiError({ code: 'validation_error', message: t('voices.transcriptHint') });
    emit();
    return;
  }
  if (form.transcript.length > LIMITS.transcript) {
    form.error = new ApiError({
      code: 'transcript_too_long',
      message: t('voices.transcriptTooLong', { max: LIMITS.transcript }),
    });
    emit();
    return;
  }
  if (form.file.size > LIMITS.uploadBytes) {
    form.error = new ApiError({ code: 'upload_too_large', message: t('errors.fileTooBig') });
    emit();
    return;
  }

  const body = new FormData();
  // The local backend reads multipart field "file" (see studio API); SPEC says "audio".
  body.append('file', form.file, form.file.name || 'sample.webm');
  body.append('name', form.name.trim() || 'Voice');
  body.append('language', form.language);
  body.append('transcript', form.transcript);
  body.append('consent', 'true');
  body.append('consent_basis', form.basis);
  body.append('trim_start', String(clamp(form.trimStart || 0, 0, 60)));
  if (form.trimEnd !== '' && Number.isFinite(Number(form.trimEnd))) {
    body.append('trim_end', String(clamp(Number(form.trimEnd), 0, 60)));
  }

  form.busy = true;
  form.error = null;
  emit();
  try {
    const voice = await api.createVoice(body);
    state.voices = [voice, ...state.voices.filter((item) => item.id !== voice.id)];
    if (form.fileUrl) URL.revokeObjectURL(form.fileUrl);
    form.busy = false;
    form.lastSaved = voice;
    state.voiceForm = {
      ...form,
      name: '',
      transcript: '',
      consent: false,
      file: null,
      fileUrl: '',
      fileDuration: 0,
      trimStart: 0,
      trimEnd: '',
      busy: false,
    };
    toast('success', t('voices.added'));
    emit();
  } catch (error) {
    form.busy = false;
    form.error = error;
    emit();
    toastError(error);
  }
}

export async function deleteVoice(id) {
  try {
    await api.deleteVoice(id);
    state.voices = state.voices.filter((voice) => voice.id !== id);
    toast('success', t('voices.removed'));
    emit();
  } catch (error) {
    toastError(error);
  }
}

/**
 * Edits name/transcript only (no audio, no consent). Returns true on success
 * or the ApiError so a dialog can keep itself open.
 */
export async function editVoice(id, patch) {
  try {
    const voice = await api.updateVoice(id, patch);
    state.voices = state.voices.map((item) => (item.id === voice.id ? voice : item));
    toast('success', t('voices.updated'));
    emit();
    // Transcript changes make existing takes stale server-side: refresh selection state.
    if (state.project) await reloadProject();
    return true;
  } catch (error) {
    toastError(error);
    return error;
  }
}

/* --------------------------------- models -------------------------------- */

export async function downloadModel(id) {
  try {
    const job = await api.downloadModel(id);
    state.installJobs[id] = job.id;
    upsertJob(job);
    toast('info', t('models.downloadStarted'));
  } catch (error) {
    toastError(error);
  }
}

export async function unloadEngine() {
  try {
    const job = await api.unloadEngine();
    upsertJob(job);
  } catch (error) {
    toastError(error);
  }
}

/* ---------------------------------- jobs --------------------------------- */

export async function cancelJob(id) {
  try {
    const job = await api.cancelJob(id);
    upsertJob(job);
  } catch (error) {
    toastError(error);
  }
}

function jobFailedSummary(job) {
  if (job.error) return errorText(job.error);
  return '';
}

async function onJobDone(job) {
  const result = job.result || {};
  switch (job.kind) {
    case 'download': {
      delete state.installJobs[result?.model_id];
      await loadModels();
      await loadStatus();
      toast('success', t('toast.modelReady'));
      break;
    }
    case 'generate': {
      await reloadProject({ adoptText: false });
      const failed = Array.isArray(result.failed) ? result.failed : [];
      const generated = Array.isArray(result.generated) ? result.generated : [];
      const skipped = Array.isArray(result.skipped) ? result.skipped : [];
      if (failed.length) {
        toast('error', t('studio.partialFailure', { failed: failed.length, generated: generated.length }), {
          ttl: 12000,
        });
      } else {
        toast('success', t('toast.generated', { generated: generated.length, skipped: skipped.length }));
      }
      break;
    }
    case 'design': {
      const completion = designCompletion(job, state.design.jobId);
      // Other tabs/history must not clear pending state, replace a result, or
      // adopt a job while our POST is still waiting for its explicit id.
      if (!completion) {
        await loadDesigns();
        break;
      }
      state.design.running = false;
      state.design.pending = false;
      state.designForm.busy = false;
      state.design.result = completion;
      state.design.suppressRestore = false;
      if (completion.voice_id) await loadVoices();
      toast('success', t('toast.designReady'));
      await loadDesigns();
      break;
    }
    case 'export': {
      state.export.running = false;
      const exportId = result?.export_id || '';
      if (!exportId) {
        toastError(new ApiError({ code: 'no_export', message: t('errors.noExport') }));
      } else {
        state.export.exportId = exportId;
        state.export.filename = result?.filename || '';
        state.export.format = result?.format || '';
        toast('success', t('toast.exportReady'));
      }
      break;
    }
    case 'unload': {
      await loadStatus();
      await loadModels();
      toast('success', t('toast.unloaded'));
      break;
    }
    default:
      break;
  }
  emit();
}

async function onJobFailed(job) {
  if (job.kind === 'design') {
    const currentId = state.design.jobId;
    const mine = Boolean(currentId) && job.id === currentId;
    if (mine) {
      state.design.running = false;
      state.design.pending = false;
      state.design.jobId = job.id;
      state.design.result = null;
      state.design.suppressRestore = true;
      state.designForm.busy = false;
    }
  }
  if (job.kind === 'export') state.export.running = false;
  if (job.kind === 'download') delete state.installJobs[job.result?.model_id];
  const detail = jobFailedSummary(job);
  toast('error', `${job.error?.code || 'failed'} — ${job.error?.message || detail || t('errors.generic')}`, {
    ttl: 12000,
  });
  emit();
}

export async function refreshJobs() {
  let payload;
  try {
    payload = await api.jobs();
  } catch (error) {
    state.jobsError = error;
    return;
  }
  state.jobsError = null;
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const previous = new Map(state.jobs.map((job) => [job.id, job]));
  const additions = items.filter((job) => !previous.has(job.id));

  state.jobs = items;
  state.jobsAt = Date.now();

  for (const job of items) {
    const before = previous.get(job.id);
    if (!before) continue;
    if (before.state === job.state) continue;
    if (job.state === 'done') await onJobDone(job);
    else if (job.state === 'failed') await onJobFailed(job);
    else if (job.state === 'canceled') {
      if (job.kind === 'design') await onJobFailed(job);
      else toast('info', t('toast.jobCanceled'));
    }
  }
  if (additions.length) {
    const installing = Object.keys(state.installJobs).length > 0;
    if (installing) await loadModels();
  }
  emit();
}

export function jobProgressLabel(job) {
  return typeof job.progress === 'number' ? job.progress : 0;
}
