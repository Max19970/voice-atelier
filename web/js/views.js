// View renderers. Each render rebuilds its section and re-binds delegated
// listeners through a single AbortController, so no listener ever leaks.

import * as actions from './state.js';
import {
  state,
  LIMITS,
  modelsOfKind,
  modelById,
  voiceById,
  languages,
  defaultModel,
  designLimit,
  rateBounds,
  isBusy,
  voiceUsedInProject,
  describeError,
} from './state.js';
import { api, fetchBlobUrl, ApiError } from './api.js';
import { t, getLang, hasKey, stageLabel } from './i18n.js';
import { esc, fmtClock, fmtPercent, fmtNumber, fmtTime, clamp, downloadBlob } from './util.js';
import { DESIGN_PRESETS, studioPresets, getPreset, containsCyrillic } from './design-presets.js';
import { parseSeed } from './design-state.js';
import {
  togglePlay,
  currentAudio,
  drawWaveform,
  paintWaveforms,
  decodeBlob,
  VoiceRecorder,
  recordingSupported,
  isSecureForRecording,
} from './audio.js';
import { navigate, confirmDialog, promptDialog, licenseDialog, openModal } from './ui.js';

const ICON = (name, className = 'icon') => `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

let controller = null;
let rootNode = null;
let recorder = null;

const exportOptions = { format: 'wav', normalize: false, allowStale: false };
const generateOptions = { force: false };

function bind(root, handlers) {
  controller?.abort();
  controller = new AbortController();
  rootNode = root;
  for (const [type, handler] of Object.entries(handlers)) {
    root.addEventListener(type, handler, { signal: controller.signal });
  }
}

export function teardown() {
  controller?.abort();
  controller = null;
  if (recorder) {
    recorder.discard();
    recorder = null;
  }
  if (state.voiceForm.recording) {
    state.voiceForm.recording = false;
    state.voiceForm.recordSeconds = 0;
  }
}

/** Re-renders the active view in place after a structural change. */
function rerenderCurrent() {
  if (!rootNode) return;
  const renderer = VIEWS[state.activeTab];
  if (renderer) renderer(rootNode);
}

/* ------------------------------ primitives ------------------------------- */

function languageLabel(code) {
  const value = String(code || '');
  if (value === 'ru') return getLang() === 'ru' ? 'Русский' : 'Russian';
  if (value === 'en') return 'English';
  return value ? value.toUpperCase() : '—';
}

function option(value, label, selected) {
  return `<option value="${esc(value)}"${String(value) === String(selected) ? ' selected' : ''}>${esc(label)}</option>`;
}

function modelOption(model, selectedId) {
  const suffix = model.installed ? '' : ` · ${t('common.notInstalled')}`;
  return option(model.id, `${model.label} · ${fmtNumber(model.size_gb)} GB${suffix}`, selectedId);
}

function qualityBadge(voice) {
  const warnings = voice?.quality?.warnings || [];
  if (!warnings.length) return `<span class="badge ok">${t('voices.qualityGood')}</span>`;
  return `<span class="badge warn">${t('voices.qualityWarn')} · ${warnings.length}</span>`;
}

/** Localized text for a take warning code; unknown codes are shown verbatim. */
function takeWarningText(code) {
  const key = `take.warn.${code}`;
  if (hasKey(key)) return t(key);
  if (String(code).startsWith('engine_sample_rate')) return t('take.warn.engine_sample_rate');
  return String(code);
}

function playBtn(path, { small = false } = {}) {
  const label = t('common.play');
  return `<button type="button" class="icon-btn${small ? ' xs' : ''}" data-act="play" data-audio="${esc(
    path,
  )}" aria-pressed="false" aria-label="${esc(label)}" title="${esc(label)}">${ICON('play')}</button>`;
}

function waveBlock(path) {
  return `<canvas class="wave" data-wave="${esc(path)}" aria-hidden="true"></canvas>`;
}

function emptyState({ title, text, action = '' }) {
  return `<div class="empty-state">
    <div class="empty-mark" aria-hidden="true">${ICON('info', 'icon lg')}</div>
    <h3>${esc(title)}</h3>
    <p>${esc(text)}</p>
    ${action}
  </div>`;
}

export function paintPlayButtons(root = document) {
  const current = currentAudio();
  for (const button of root.querySelectorAll('[data-act="play"][data-audio]')) {
    const playing = current.url === button.dataset.audio && current.playing;
    button.classList.toggle('is-playing', playing);
    button.setAttribute('aria-pressed', playing ? 'true' : 'false');
    const label = playing ? t('common.stop') : t('common.play');
    button.title = label;
    button.setAttribute('aria-label', label);
    const use = button.querySelector('use');
    if (use) use.setAttribute('href', playing ? '#i-stop' : '#i-play');
  }
}

export function paintAutosave(root = document) {
  const element = root.querySelector ? root.querySelector('#autosave') : null;
  if (!element) return;
  const edit = state.edit;
  let key = 'common.saved';
  let name = 'saved';
  if (edit.conflict) {
    key = 'errors.staleProject';
    name = 'error';
  } else if (edit.error) {
    key = 'common.saveError';
    name = 'error';
  } else if (edit.saving) {
    key = 'common.saving';
    name = 'saving';
  } else if (edit.dirty) {
    key = 'common.unsaved';
    name = 'dirty';
  }
  const time = edit.savedAt ? ` · ${fmtTime(edit.savedAt)}` : '';
  element.dataset.state = name;
  element.textContent = name === 'saved' ? `${t(key)}${time}` : t(key);
}

/* --------------------------------- studio -------------------------------- */

function projectBar(project) {
  const options = state.projects
    .map((item) => option(item.id, item.title || item.id, project?.id))
    .join('');
  return `<div class="panel project-bar">
    <div class="project-bar-main">
      <label class="field-inline">
        <span class="label">${esc(t('studio.project'))}</span>
        <select class="select" id="project-select" aria-label="${esc(t('studio.projects'))}">
          ${project ? '' : option('', t('studio.noProjects'), '')}
          ${options}
        </select>
      </label>
      ${
        project
          ? `<label class="field-inline grow">
               <span class="label sr-only">${esc(t('studio.titleEdit'))}</span>
               <input class="input title-input" id="project-title" type="text" value="${esc(project.title)}"
                 placeholder="${esc(t('studio.newProjectTitle'))}" aria-label="${esc(t('studio.titleEdit'))}" />
             </label>`
          : ''
      }
    </div>
    <div class="project-bar-actions">
      <button type="button" class="btn ghost sm" data-act="new-project">${ICON('plus')}<span>${esc(
        t('studio.newProject'),
      )}</span></button>
      <span class="save-state" id="autosave" data-state="saved" aria-live="polite">${esc(t('common.saved'))}</span>
      ${
        project
          ? `<button type="button" class="icon-btn" data-act="delete-project" aria-label="${esc(
              t('studio.deleteProject'),
            )}" title="${esc(t('studio.deleteProject'))}">${ICON('trash')}</button>`
          : ''
      }
    </div>
  </div>`;
}

function segmentCard(segment, index) {
  const cloneModels = modelsOfKind('clone');
  const designModels = modelsOfKind('design');
  const models = segment.mode === 'design' ? designModels : cloneModels;
  const model = modelById(segment.model_id);
  const voice = voiceById(segment.voice_id);
  const selectedTake = (segment.takes || []).find((take) => take.id === segment.selected_take_id) || null;
  const stale = Boolean(selectedTake && selectedTake.stale);
  const takes = [...(segment.takes || [])].sort(
    (a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0),
  );
  const bounds = rateBounds();
  const designMode = segment.mode === 'design';
  const textMax = designMode ? designLimit() : LIMITS.text;
  const textLength = String(segment.text || '').length;
  const over = textLength > textMax;
  const instructionOver = String(segment.instruction || '').length > LIMITS.instruction;
  const voiceMissing = segment.mode !== 'design' && !segment.voice_id;
  const voiceNoTranscript = segment.mode !== 'design' && voice && !String(voice.transcript || '').trim();
  const modelMissing = !model || !model.installed;

  const voiceOptions = [
    option('', t('studio.voiceNone'), segment.voice_id || ''),
    ...state.voices.map((item) =>
      option(
        item.id,
        `${item.name}${item.source === 'design' ? ` · ${t('studio.voiceSynthetic')}` : ''}${
          String(item.transcript || '').trim() ? '' : ` · ${t('voices.transcriptMissing')}`
        }`,
        segment.voice_id,
      ),
    ),
  ].join('');

  const modelOptions = models.length
    ? models.map((item) => modelOption(item, segment.model_id)).join('')
    : option(segment.model_id, segment.model_id, segment.model_id);

  const takeChips = takes.length
    ? takes
        .map((take, takeIndex) => {
          const letter = String.fromCharCode(65 + (takeIndex % 26));
          const isSelected = take.id === segment.selected_take_id;
          const warnings = Array.isArray(take.warnings) ? take.warnings : [];
          const warningTitle = warnings.map(takeWarningText).join(' · ');
          return `<span class="take${isSelected ? ' is-selected' : ''}${take.stale ? ' is-stale' : ''}">
            ${playBtn(api.takeAudio(take.id), { small: true })}
            <span class="take-name">${esc(t('studio.take'))} ${letter}</span>
            <span class="take-dur">${esc(fmtClock(take.duration))}</span>
            ${
              warnings.length
                ? `<span class="badge warn take-warn" title="${esc(warningTitle)}" aria-label="${esc(
                    `${t('studio.takeWarnings')}: ${warningTitle}`,
                  )}">${ICON('alert', 'icon xs')}<span>${warnings.length}</span></span>`
                : ''
            }
            ${take.stale ? `<span class="badge warn">${esc(t('studio.stale'))}</span>` : ''}
            ${
              isSelected
                ? `<span class="badge ok">${esc(t('studio.selected'))}</span>`
                : `<button type="button" class="link-btn" data-act="select-take" data-take="${esc(
                    take.id,
                  )}">${esc(t('studio.select'))}</button>`
            }
          </span>`;
        })
        .join('')
    : `<span class="muted small">${esc(t('studio.noTakes'))}</span>`;

  const warningRows = takes.flatMap((take, takeIndex) => {
    const warnings = Array.isArray(take.warnings) ? take.warnings : [];
    const letter = String.fromCharCode(65 + (takeIndex % 26));
    return warnings.map((code) => ({ letter, code }));
  });

  return `<li class="segment${stale ? ' is-stale' : ''}" data-seg="${esc(segment.id)}">
    <div class="seg-top">
      <span class="seg-num" aria-hidden="true">${String(index + 1).padStart(2, '0')}</span>
      <div class="seg-badges">
        <span class="badge subtle">${esc(t(segment.mode === 'design' ? 'studio.mode.design' : 'studio.mode.clone'))}</span>
        ${stale ? `<span class="badge warn">${esc(t('studio.stale'))}</span>` : ''}
        ${voiceMissing ? `<span class="badge danger">${esc(t('studio.needsVoice'))}</span>` : ''}
        ${modelMissing ? `<span class="badge danger">${esc(t('studio.needsModel'))}</span>` : ''}
        ${segment.mode === 'design' && !String(segment.instruction || '').trim() ? `<span class="badge danger">${esc(t('studio.designNeedsInstruction'))}</span>` : ''}
        ${designMode && over ? `<span class="badge danger">${esc(
          t('studio.designTextTooLong', { max: textMax }),
        )}</span>` : ''}
      </div>
      <div class="seg-tools">
        <button type="button" class="icon-btn xs" data-act="move-up" aria-label="${esc(t('common.up'))}" title="${esc(
          t('common.up'),
        )}" ${index === 0 ? 'disabled' : ''}>${ICON('up')}</button>
        <button type="button" class="icon-btn xs" data-act="move-down" aria-label="${esc(t('common.down'))}" title="${esc(
          t('common.down'),
        )}">${ICON('down')}</button>
        <button type="button" class="icon-btn xs" data-act="delete-segment" aria-label="${esc(
          t('studio.deleteSegment'),
        )}" title="${esc(t('studio.deleteSegment'))}">${ICON('trash')}</button>
      </div>
    </div>

    <label class="sr-only" for="seg-text-${esc(segment.id)}">${esc(t('studio.text'))}</label>
    <textarea class="textarea seg-text${over ? ' is-over' : ''}" id="seg-text-${esc(segment.id)}"
      data-field="text" rows="3" placeholder="${esc(t('studio.textPlaceholder'))}">${esc(segment.text)}</textarea>
    <div class="seg-under">
      <span class="count${over ? ' over' : ''}" data-role="chars">${esc(
        t('studio.charCount', { n: textLength, max: textMax }),
      )}</span>
      ${
        stale
          ? `<span class="hint warn">${esc(t('studio.staleHint'))}</span>`
          : ''
      }
      ${
        voiceNoTranscript
          ? `<span class="hint danger">${esc(t('voices.transcriptHint'))}</span>`
          : ''
      }
    </div>

    <div class="seg-controls">
      <label class="ctl"><span class="ctl-label">${esc(t('studio.mode'))}</span>
        <select class="select" data-field="mode" aria-label="${esc(t('studio.mode'))}">
          ${option('clone', t('studio.mode.clone'), segment.mode)}
          ${option('design', t('studio.mode.design'), segment.mode)}
        </select>
      </label>
      ${
        segment.mode === 'design'
          ? ''
          : `<label class="ctl"><span class="ctl-label">${esc(t('studio.voice'))}</span>
               <select class="select" data-field="voice_id" aria-label="${esc(t('studio.voice'))}">${voiceOptions}</select>
             </label>`
      }
      <label class="ctl"><span class="ctl-label">${esc(t('studio.model'))}</span>
        <select class="select" data-field="model_id" aria-label="${esc(t('studio.model'))}">${modelOptions}</select>
      </label>
      <label class="ctl"><span class="ctl-label">${esc(t('studio.language'))}</span>
        <select class="select" data-field="language" aria-label="${esc(t('studio.language'))}">
          ${languages().map((code) => option(code, languageLabel(code), segment.language)).join('')}
        </select>
      </label>
      <label class="ctl ctl-num"><span class="ctl-label">${esc(t('studio.rate'))}</span>
        <input class="input" type="number" data-field="rate" value="${esc(segment.rate)}" min="${bounds.min}" max="${bounds.max}" step="0.05" aria-label="${esc(t('studio.rate'))}" />
      </label>
      <label class="ctl ctl-num"><span class="ctl-label">${esc(t('studio.pause'))}</span>
        <input class="input" type="number" data-field="pause_ms" value="${esc(segment.pause_ms)}" min="0" max="5000" step="50" aria-label="${esc(t('studio.pause'))}" />
      </label>
      <label class="ctl ctl-num"><span class="ctl-label">${esc(t('studio.seed'))}</span>
        <input class="input" type="number" data-field="seed" value="${esc(segment.seed)}" step="1" aria-label="${esc(t('studio.seed'))}" />
      </label>
    </div>

    ${
      segment.mode === 'design'
        ? `<div class="seg-instruction">
             <label class="ctl-label" for="seg-ins-${esc(segment.id)}">${esc(t('studio.instruction'))}</label>
             <textarea class="textarea" id="seg-ins-${esc(segment.id)}" data-field="instruction" rows="2"
               placeholder="${esc(t('studio.instructionPlaceholder'))}">${esc(segment.instruction || '')}</textarea>
             <div class="seg-under">
               <span class="count${instructionOver ? ' over' : ''}" data-role="ichars">${esc(
                 t('studio.charCount', {
                   n: String(segment.instruction || '').length,
                   max: LIMITS.instruction,
                 }),
               )}</span>
               <span class="hint">${esc(t('studio.instructionDesignNote'))}</span>
             </div>
             <p class="hint">${esc(t('design.instructionLanguageNote'))}</p>
             <p class="hint warn" data-role="cyrillic" ${containsCyrillic(segment.instruction) ? '' : 'hidden'}>${esc(
               t('design.cyrillicAdvisory'),
             )}</p>
             <p class="hint">${esc(t('design.attributesNote'))}</p>
             <div class="chips">
               ${studioPresets()
                 .map(
                   (preset) =>
                     `<button type="button" class="chip" data-act="seg-preset" data-preset="${esc(
                       preset.id,
                     )}" title="${esc(preset.prompt)}">${esc(t(preset.labelKey))}</button>`,
                 )
                 .join('')}
             </div>
           </div>`
        : `<p class="seg-note">${ICON('info', 'icon xs')}<span>${esc(t('studio.instructionCloneNote'))}</span></p>`
    }

    <div class="takes-row">
      <div class="takes" aria-label="${esc(t('studio.takes'))}">${takeChips}</div>
      <button type="button" class="btn primary sm" data-act="generate-segment" ${
        isBusy() || modelMissing || (segment.mode !== 'design' && voiceMissing) || (designMode && over)
          ? 'disabled'
          : ''
      }>${ICON('wand')}<span>${esc(t('studio.generate'))}</span></button>
    </div>

    ${
      warningRows.length
        ? `<ul class="take-warnings" aria-label="${esc(t('studio.takeWarningsList'))}">
             ${warningRows
               .map(
                 (row) =>
                   `<li>${ICON('alert', 'icon xs')}<span>${esc(t('studio.take'))} ${row.letter} · ${esc(
                     takeWarningText(row.code),
                   )} <code>${esc(row.code)}</code></span></li>`,
               )
               .join('')}
           </ul>`
        : ''
    }
  </li>`;
}

function projectStats(project) {
  const segments = project.segments;
  const chars = segments.reduce((sum, segment) => sum + String(segment.text || '').length, 0);
  let duration = 0;
  for (const segment of segments) {
    const take = (segment.takes || []).find((item) => item.id === segment.selected_take_id);
    if (take) duration += Number(take.duration) || 0;
  }
  const voices = new Set(segments.filter((segment) => segment.voice_id).map((segment) => segment.voice_id));
  return `<dl class="stats">
    <div><dt>${esc(t('studio.statsSegments'))}</dt><dd>${segments.length}</dd></div>
    <div><dt>${esc(t('studio.statsChars'))}</dt><dd>${chars}</dd></div>
    <div><dt>${esc(t('studio.statsDuration'))}</dt><dd>${esc(fmtClock(duration))}</dd></div>
    <div><dt>${esc(t('studio.statsVoices'))}</dt><dd>${voices.size}</dd></div>
  </dl>`;
}

function exportReadiness(project) {
  let missing = 0;
  let stale = 0;
  for (const segment of project.segments) {
    if (!String(segment.text || '').trim()) continue;
    const take = (segment.takes || []).find((item) => item.id === segment.selected_take_id);
    if (!take) missing += 1;
    else if (take.stale) stale += 1;
  }
  return { missing, stale };
}

function studioSide(project) {
  const readiness = exportReadiness(project);
  const blocked = (readiness.missing > 0 || readiness.stale > 0) && !exportOptions.allowStale;
  const installedModels = state.models.some((model) => model.installed);
  const exportReady = Boolean(state.export.exportId);

  return `<aside class="studio-side">
    <section class="panel">
      <h2 class="panel-title">${esc(t('studio.stats'))}</h2>
      ${projectStats(project)}
    </section>

    <section class="panel">
      <h2 class="panel-title">${esc(t('studio.export'))}</h2>
      <label class="field">
        <span class="label">${esc(t('studio.exportFormat'))}</span>
        <select class="select" id="export-format">
          ${option('wav', 'WAV', exportOptions.format)}
          ${option('zip', 'ZIP', exportOptions.format)}
        </select>
      </label>
      <label class="check"><input type="checkbox" id="export-normalize" ${
        exportOptions.normalize ? 'checked' : ''
      } /><span>${esc(t('studio.exportNormalize'))}</span></label>
      <label class="check"><input type="checkbox" id="export-allow-stale" ${
        exportOptions.allowStale ? 'checked' : ''
      } /><span>${esc(t('studio.exportAllowStale'))}</span></label>
      ${
        readiness.missing || readiness.stale
          ? `<p class="hint${blocked ? ' danger' : ''}">${esc(t('studio.exportBlocked'))}</p>`
          : ''
      }
      <button type="button" class="btn primary block" data-act="export" ${
        blocked || state.export.running || isBusy() ? 'disabled' : ''
      }>${ICON('download')}<span>${
        state.export.running ? esc(t('studio.exportRunning')) : esc(t('studio.exportRun'))
      }</span></button>
      ${
        exportReady
          ? `<button type="button" class="btn ghost block" data-act="download-export">${ICON('download')}<span>${esc(
              t('common.download'),
            )} ${esc(state.export.filename || state.export.format.toUpperCase())}</span></button>`
          : ''
      }
    </section>

    <section class="panel">
      <h2 class="panel-title">${esc(t('studio.segments'))}</h2>
      <label class="check"><input type="checkbox" id="force-stale" ${
        generateOptions.force ? 'checked' : ''
      } /><span>${esc(t('studio.force'))}</span></label>
      <button type="button" class="btn primary block" data-act="generate-all" ${
        isBusy() || !installedModels ? 'disabled' : ''
      }>${ICON('wand')}<span>${esc(t('studio.generateAll'))}</span></button>
      <button type="button" class="btn ghost block" data-act="split">${ICON('split')}<span>${esc(
        t('studio.split'),
      )}</span></button>
      <p class="hint">${esc(t('studio.splitHint'))}</p>
      ${
        !installedModels
          ? `<p class="hint danger">${esc(t('models.required'))}</p>
             <button type="button" class="btn ghost block" data-act="go-models">${esc(t('nav.models'))}</button>`
          : ''
      }
      <p class="hint">${esc(t('studio.rateHint'))}</p>
      <p class="hint">${esc(t('studio.seedHint'))}</p>
    </section>
  </aside>`;
}

export function renderStudio(root) {
  const project = state.project;
  root.innerHTML = `<div class="stack">
    ${projectBar(project)}
    ${
      state.projectLoading
        ? `<div class="panel loading-panel">${esc(t('studio.loadingProject'))}</div>`
        : project
          ? `<div class="studio-grid">
               <section class="panel script-panel">
                 <div class="panel-head">
                   <h2 class="panel-title">${esc(t('studio.segments'))} <span class="count">${project.segments.length}</span></h2>
                   <div class="head-actions">
                     <button type="button" class="btn primary sm" data-act="generate-all" ${
                       isBusy() || !state.models.some((model) => model.installed) ? 'disabled' : ''
                     }>${ICON('wand')}<span>${esc(t('studio.generateAll'))}</span></button>
                   </div>
                 </div>
                 ${
                   project.segments.length
                     ? `<ol class="segments">${project.segments.map((segment, index) => segmentCard(segment, index)).join('')}</ol>`
                     : emptyState({
                         title: t('studio.noSegments'),
                         text: t('studio.noSegmentsText'),
                         action: `<button type="button" class="btn primary" data-act="add-segment">${esc(
                           t('studio.addSegment'),
                         )}</button>`,
                       })
                 }
                 ${
                   project.segments.length
                     ? `<div class="segments-foot">
                          <button type="button" class="btn ghost" data-act="add-segment">${ICON('plus')}<span>${esc(
                            t('studio.addSegment'),
                          )}</span></button>
                        </div>`
                     : ''
                 }
               </section>
               ${studioSide(project)}
             </div>`
          : emptyState({
              title: t('studio.noProjects'),
              text: t('studio.noProjectsText'),
              action: `<button type="button" class="btn primary" data-act="new-project">${esc(
                t('studio.newProject'),
              )}</button>`,
            })
    }
  </div>`;

  bind(root, {
    click: onStudioClick,
    input: onStudioInput,
    change: onStudioChange,
  });
  paintAutosave(root);
  paintWaveforms(root);
  paintPlayButtons(root);
}

function currentSegment(element) {
  const card = element.closest('[data-seg]');
  if (!card || !state.project) return null;
  const id = card.dataset.seg;
  return state.project.segments.find((segment) => segment.id === id) || null;
}

function onStudioClick(event) {
  const button = event.target.closest('[data-act]');
  if (!button || !rootNode || !rootNode.contains(button)) return;
  const act = button.dataset.act;
  const segment = currentSegment(button);
  const takeChip = button.closest('[data-take]');

  switch (act) {
    case 'play':
      togglePlay(button.dataset.audio);
      break;
    case 'new-project':
      void createProjectFlow();
      break;
    case 'delete-project':
      void deleteProjectFlow();
      break;
    case 'add-segment': {
      const created = actions.addSegment(segment ? segment.id : null);
      if (created) {
        window.requestAnimationFrame(() => {
          const textarea = document.querySelector(`[data-seg="${created.id}"] textarea[data-field="text"]`);
          if (textarea) {
            textarea.focus();
            textarea.scrollIntoView({ block: 'center' });
          }
        });
      }
      break;
    }
    case 'delete-segment':
      if (segment)
        void confirmDialog({
          title: t('studio.deleteSegment'),
          text: t('studio.confirmDeleteSegment'),
        }).then((confirmed) => {
          if (confirmed) actions.deleteSegment(segment.id);
        });
      break;
    case 'move-up':
      if (segment) actions.moveSegment(segment.id, -1);
      break;
    case 'move-down':
      if (segment) actions.moveSegment(segment.id, 1);
      break;
    case 'select-take':
      if (segment && takeChip) actions.selectTake(segment.id, takeChip.dataset.take);
      break;
    case 'seg-preset':
      if (segment) {
        const preset = getPreset(button.dataset.preset);
        if (preset) {
          segment.instruction = preset.prompt;
          actions.markDirty();
          rerenderCurrent();
        }
      }
      break;
    case 'generate-segment':
      if (segment) void generateSegment(segment);
      break;
    case 'generate-all':
      void actions.generate({ force: generateOptions.force });
      break;
    case 'split':
      actions.splitParagraphs();
      break;
    case 'export':
      void actions.exportProject(exportOptions);
      break;
    case 'download-export':
      void downloadExportFlow();
      break;
    case 'go-models':
      navigate('models');
      break;
    default:
      break;
  }
}

async function generateSegment(segment) {
  if (segment.mode === 'design') {
    if (!String(segment.instruction || '').trim()) {
      actions.toast('error', `${t('errors.needInstruction')}`);
      return;
    }
    const limit = designLimit();
    if (String(segment.text || '').length > limit) {
      actions.toast('error', t('studio.designTextTooLong', { max: limit }));
      return;
    }
  } else {
    const voice = voiceById(segment.voice_id);
    if (!voice) {
      actions.toast('error', t('studio.needVoice'));
      return;
    }
    if (!String(voice.transcript || '').trim()) {
      actions.toast('error', `${t('errors.transcriptMissing')}`);
      return;
    }
  }
  const model = modelById(segment.model_id);
  if (!model || !model.installed) {
    actions.toast('error', t('studio.modelMissing', { label: model ? model.label : segment.model_id }));
    return;
  }
  await actions.generate({ segmentIds: [segment.id], force: true });
}

function onStudioInput(event) {
  const target = event.target;
  if (target.id === 'project-title' && state.project) {
    actions.renameProject(target.value);
    return;
  }
  const field = target.dataset ? target.dataset.field : null;
  if (!field || !state.project) return;
  const segment = currentSegment(target);
  if (!segment) return;
  const card = target.closest('[data-seg]');

  if (field === 'text') {
    segment.text = target.value;
    const max = segment.mode === 'design' ? designLimit() : LIMITS.text;
    const counter = card?.querySelector('[data-role="chars"]');
    if (counter) {
      counter.textContent = t('studio.charCount', { n: segment.text.length, max });
      counter.classList.toggle('over', segment.text.length > max);
    }
    target.classList.toggle('is-over', segment.text.length > max);
    actions.markDirty();
    return;
  }
  if (field === 'instruction') {
    segment.instruction = target.value;
    const counter = card?.querySelector('[data-role="ichars"]');
    if (counter) {
      counter.textContent = t('studio.charCount', { n: segment.instruction.length, max: LIMITS.instruction });
      counter.classList.toggle('over', segment.instruction.length > LIMITS.instruction);
    }
    const advisory = card?.querySelector('[data-role="cyrillic"]');
    if (advisory) advisory.hidden = !containsCyrillic(segment.instruction);
    actions.markDirty();
    return;
  }
  if (field === 'seed') {
    const parsed = parseSeed(target.value);
    if (parsed.ok) {
      segment.seed = parsed.value;
      actions.markDirty();
    }
    return;
  }
  if (field === 'rate' || field === 'pause_ms') {
    const value = Number(target.value);
    if (Number.isFinite(value)) {
      segment[field] = value;
      actions.markDirty();
    }
  }
}

function onStudioChange(event) {
  const target = event.target;
  if (target.id === 'project-select') {
    actions.openProject(target.value || null);
    return;
  }
  if (target.id === 'export-format') {
    exportOptions.format = target.value === 'zip' ? 'zip' : 'wav';
    rerenderCurrent();
    return;
  }
  if (target.id === 'export-normalize') {
    exportOptions.normalize = target.checked;
    return;
  }
  if (target.id === 'export-allow-stale') {
    exportOptions.allowStale = target.checked;
    rerenderCurrent();
    return;
  }
  if (target.id === 'force-stale') {
    generateOptions.force = target.checked;
    return;
  }
  if (target.id === 'project-title') {
    actions.renameProject(target.value);
    return;
  }
  const field = target.dataset ? target.dataset.field : null;
  if (!field || !state.project) return;
  const segment = currentSegment(target);
  if (!segment) return;

  if (field === 'mode') {
    segment.mode = target.value === 'design' ? 'design' : 'clone';
    segment.model_id = defaultModel(segment.mode === 'design' ? 'design' : 'clone');
    if (segment.mode === 'clone') segment.instruction = '';
    segment.selected_take_id = null;
    actions.markDirty();
    rerenderCurrent();
    return;
  }
  if (field === 'voice_id') {
    segment.voice_id = target.value || null;
    actions.markDirty();
    rerenderCurrent();
    return;
  }
  if (field === 'model_id') {
    segment.model_id = target.value;
    actions.markDirty();
    rerenderCurrent();
    return;
  }
  if (field === 'language') {
    segment.language = target.value;
    actions.markDirty();
    rerenderCurrent();
    return;
  }
  if (field === 'rate') {
    const bounds = rateBounds();
    segment.rate = clamp(Number(target.value) || 1, bounds.min, bounds.max);
    actions.markDirty();
    target.value = String(segment.rate);
    return;
  }
  if (field === 'pause_ms') {
    segment.pause_ms = clamp(Math.round(Number(target.value) || 0), 0, 5000);
    actions.markDirty();
    target.value = String(segment.pause_ms);
    return;
  }
  if (field === 'seed') {
    const parsed = parseSeed(target.value);
    if (!parsed.ok) {
      actions.toastError(new ApiError({ code: 'seed_invalid', message: t('errors.seedInvalid') }));
      target.value = String(segment.seed);
      return;
    }
    segment.seed = parsed.value;
    actions.markDirty();
    target.value = String(parsed.value);
  }
}

async function createProjectFlow() {
  const title = await promptDialog({
    title: t('studio.newProject'),
    label: t('studio.newProjectTitle'),
    placeholder: t('studio.projectTitlePlaceholder'),
  });
  if (!title) return;
  const project = await actions.createProject(title);
  if (project) actions.openProject(project.id);
}

async function deleteProjectFlow() {
  const project = state.project;
  if (!project) return;
  const confirmed = await confirmDialog({
    title: t('studio.deleteProject'),
    text: t('studio.confirmDeleteProject', { title: project.title || project.id }),
  });
  if (confirmed) await actions.deleteProject(project.id);
}

async function downloadExportFlow() {
  const exportId = state.export.exportId;
  if (!exportId) {
    actions.toastError({ code: 'no_export', message: t('errors.noExport') });
    return;
  }
  try {
    const name =
      state.export.filename || `voice-atelier-export.${state.export.format === 'zip' ? 'zip' : 'wav'}`;
    const { blob, filename } = await fetchBlobUrl(api.exportDownload(exportId));
    downloadBlob(blob, filename || name);
  } catch (error) {
    actions.toastError(error);
  }
}

/* --------------------------------- voices -------------------------------- */

function voiceLocalWave() {
  return `<canvas class="wave" id="voice-local-wave" aria-hidden="true"></canvas>`;
}

function qualityCard(voice) {
  const quality = voice.quality || {};
  const warnings = Array.isArray(quality.warnings) ? quality.warnings : [];
  return `<details class="quality" open>
    <summary>${esc(t('voices.quality'))} ${qualityBadge(voice)}</summary>
    <dl class="stats compact">
      <div><dt>${esc(t('voices.qualityPeak'))}</dt><dd>${esc(fmtNumber(quality.peak_dbfs, 1))} dBFS</dd></div>
      <div><dt>${esc(t('voices.qualityRms'))}</dt><dd>${esc(fmtNumber(quality.rms_dbfs, 1))} dBFS</dd></div>
      <div><dt>${esc(t('voices.qualityClipped'))}</dt><dd>${esc(fmtPercent(quality.clipped_fraction))}</dd></div>
      <div><dt>${esc(t('voices.qualitySilence'))}</dt><dd>${esc(fmtPercent(quality.silence_fraction))}</dd></div>
    </dl>
    ${
      warnings.length
        ? `<ul class="warn-list">${warnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul>`
        : ''
    }
    <p class="hint">${esc(t('voices.qualityNotQuality'))}</p>
  </details>`;
}

function voiceListCard(voice) {
  const used = voiceUsedInProject(voice.id);
  const warnings = voice.quality?.warnings || [];
  return `<article class="voice-card">
    <div class="voice-head">
      <div class="voice-title">
        <h3>${esc(voice.name)}</h3>
        <p class="meta">${esc(languageLabel(voice.language))} · ${esc(fmtClock(voice.duration))} · ${esc(
          fmtNumber(voice.sample_rate, 0),
        )} Hz · ${esc(t(voice.source === 'design' ? 'voices.sourceDesign' : 'voices.sourceUpload'))}</p>
      </div>
      <div class="voice-actions">
        ${playBtn(api.voiceAudio(voice.id))}
        <button type="button" class="icon-btn" data-act="edit-voice" data-voice="${esc(
          voice.id,
        )}" aria-label="${esc(t('common.edit'))}" title="${esc(
          t('voices.editTitle', { name: voice.name || '' }),
        )}">${ICON('edit')}</button>
        <button type="button" class="icon-btn" data-act="delete-voice" data-voice="${esc(voice.id)}"
          ${used ? 'disabled' : ''} aria-label="${esc(t('voices.delete'))}" title="${
          used ? esc(t('voices.inUse')) : esc(t('voices.delete'))
        }">${ICON('trash')}</button>
      </div>
    </div>
    ${waveBlock(api.voiceAudio(voice.id))}
    <p class="transcript">${esc(String(voice.transcript || '').slice(0, 220))}${
      String(voice.transcript || '').length > 220 ? '…' : ''
    }</p>
    <div class="voice-foot">
      ${qualityBadge(voice)}
      ${used ? `<span class="badge subtle">${esc(t('voices.inUse'))}</span>` : ''}
      ${voice.source === 'design' ? `<span class="badge subtle">${esc(t('voices.syntheticNote'))}</span>` : ''}
      ${warnings.length ? `<span class="hint">${esc(warnings.join(' · '))}</span>` : ''}
    </div>
  </article>`;
}

export function renderVoices(root) {
  const form = state.voiceForm;
  const supported = recordingSupported();
  const secure = isSecureForRecording();
  const lastSaved = form.lastSaved;

  root.innerHTML = `<div class="two-col">
    <section class="panel">
      <h2 class="panel-title">${esc(t('voices.add'))}</h2>
      <p class="hint">${esc(t('voices.sub'))}</p>

      <div class="field">
        <span class="label">${esc(t('voices.file'))}</span>
        <div class="file-row">
          <input class="input" type="file" id="voice-file" accept="audio/*" aria-label="${esc(t('voices.file'))}" />
          <button type="button" class="btn ghost sm" data-act="choose-file">${ICON('upload')}<span>${esc(
            t('voices.chooseFile'),
          )}</span></button>
        </div>
        <p class="hint" id="voice-file-hint">${esc(form.file ? form.file.name : t('voices.noFile'))} · ${esc(
          t('voices.fileHint'),
        )}</p>
      </div>

      <div class="rec-block" data-recording="${form.recording ? 'true' : 'false'}">
        <div class="rec-row">
          <button type="button" class="btn ghost sm rec-start" data-act="rec-start" ${
            !supported || form.busy ? 'disabled' : ''
          }>${ICON('record')}<span>${esc(t('voices.recStart'))}</span></button>
          <button type="button" class="btn primary sm rec-stop" data-act="rec-stop">${ICON('stop')}<span>${esc(
            t('voices.recStop'),
          )}</span></button>
          <button type="button" class="btn ghost sm rec-cancel" data-act="rec-cancel">${esc(t('voices.recCancel'))}</button>
          <span class="rec-time" id="rec-time">${esc(fmtClock(form.recordSeconds || 0))}</span>
        </div>
        <p class="hint">${esc(t('voices.recHint'))}</p>
        ${!supported ? `<p class="hint danger">${esc(t('voices.recUnsupported'))}</p>` : ''}
        ${supported && !secure ? `<p class="hint danger">${esc(t('voices.recInsecure'))}</p>` : ''}
      </div>

      ${voiceLocalWave()}

      <div class="grid-2">
        <label class="field"><span class="label">${esc(t('voices.name'))}</span>
          <input class="input" type="text" id="voice-name" value="${esc(form.name)}" placeholder="${esc(
            t('voices.namePlaceholder'),
          )}" maxlength="120" />
        </label>
        <label class="field"><span class="label">${esc(t('studio.language'))}</span>
          <select class="select" id="voice-language">
            ${languages().map((code) => option(code, languageLabel(code), form.language)).join('')}
          </select>
        </label>
      </div>

      <label class="field"><span class="label">${esc(t('voices.transcript'))}</span>
        <textarea class="textarea" id="voice-transcript" rows="3" maxlength="${LIMITS.transcript}" placeholder="${esc(
          t('voices.transcriptPlaceholder'),
        )}">${esc(form.transcript)}</textarea>
        <span class="hint">${esc(t('voices.transcriptHint'))} · ${esc(
          t('voices.transcriptLimit', { max: LIMITS.transcript }),
        )}</span>
      </label>

      <div class="grid-2">
        <label class="field"><span class="label">${esc(t('voices.trimStart'))}</span>
          <input class="input" type="number" id="voice-trim-start" min="0" max="60" step="0.1" value="${esc(
            form.trimStart,
          )}" />
        </label>
        <label class="field"><span class="label">${esc(t('voices.trimEnd'))}</span>
          <input class="input" type="number" id="voice-trim-end" min="0" max="60" step="0.1" value="${esc(
            form.trimEnd,
          )}" placeholder="${esc(t('common.optional'))}" />
        </label>
      </div>
      <p class="hint">${esc(t('voices.trimHint'))}</p>

      <label class="check"><input type="checkbox" id="voice-consent" ${
        form.consent ? 'checked' : ''
      } /><span>${esc(t('voices.consent'))}</span></label>
      <label class="field"><span class="label">${esc(t('voices.basis'))}</span>
        <select class="select" id="voice-basis">
          ${option('own', t('voices.basisOwn'), form.basis)}
          ${option('permission', t('voices.basisPermission'), form.basis)}
          ${option('licensed', t('voices.basisLicensed'), form.basis)}
        </select>
      </label>
      <p class="hint">${esc(t('voices.consentHint'))}</p>

      ${
        form.error
          ? `<p class="error-line"><span class="error-code">${esc(form.error.code || 'error')}</span> ${esc(
              describeError(form.error).text,
            )}</p>`
          : ''
      }

      <button type="button" class="btn primary block" data-act="save-voice" ${form.busy ? 'disabled' : ''}>${
        form.busy ? esc(t('voices.saving')) : esc(t('voices.save'))
      }</button>

      ${lastSaved ? `<div class="saved-voice">${qualityCard(lastSaved)}</div>` : ''}
    </section>

    <section class="panel">
      <h2 class="panel-title">${esc(t('voices.list'))} <span class="count">${state.voices.length}</span></h2>
      ${
        state.voicesError
          ? `<p class="error-line"><span class="error-code">${esc(
              state.voicesError.code || 'error',
            )}</span> ${esc(describeError(state.voicesError).text)}</p>`
          : ''
      }
      ${
        state.voices.length
          ? `<div class="voice-list">${state.voices.map(voiceListCard).join('')}</div>`
          : emptyState({
              title: t('voices.empty'),
              text: t('voices.emptyText'),
            })
      }
    </section>
  </div>`;

  bind(root, {
    click: onVoicesClick,
    input: onVoicesInput,
    change: onVoicesChange,
  });

  paintWaveforms(root);
  paintPlayButtons(root);
  paintLocalWave();
  if (form.recording) paintRecordingState();
}

function paintLocalWave() {
  const canvas = rootNode?.querySelector('#voice-local-wave');
  if (!canvas) return;
  const peaks = state.voiceForm.peaks;
  drawWaveform(canvas, peaks || null);
}

function paintRecordingState() {
  if (!rootNode) return;
  const block = rootNode.querySelector('.rec-block');
  if (block) block.dataset.recording = state.voiceForm.recording ? 'true' : 'false';
  const time = rootNode.querySelector('#rec-time');
  if (time) time.textContent = fmtClock(state.voiceForm.recordSeconds || 0);
}

async function onVoicesClick(event) {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;

  if (act === 'play') {
    togglePlay(button.dataset.audio);
    return;
  }
  if (act === 'edit-voice') {
    const voice = voiceById(button.dataset.voice);
    if (voice) await editVoiceFlow(voice);
    return;
  }
  if (act === 'choose-file') {
    rootNode?.querySelector('#voice-file')?.click();
    return;
  }
  if (act === 'rec-start') {
    await startRecording();
    return;
  }
  if (act === 'rec-stop') {
    await stopRecording();
    return;
  }
  if (act === 'rec-cancel') {
    cancelRecording();
    return;
  }
  if (act === 'save-voice') {
    await actions.submitVoice();
    return;
  }
  if (act === 'delete-voice') {
    const voice = voiceById(button.dataset.voice);
    if (!voice) return;
    if (voiceUsedInProject(voice.id)) {
      actions.toast('error', t('voices.inUse'));
      return;
    }
    const confirmed = await confirmDialog({
      title: t('voices.delete'),
      text: t('voices.confirmDelete', { name: voice.name }),
    });
    if (confirmed) await actions.deleteVoice(voice.id);
  }
}

async function editVoiceFlow(voice) {
  const wrapper = document.createElement('div');
  wrapper.className = 'stack-sm';

  const nameField = document.createElement('label');
  nameField.className = 'field';
  const nameLabel = document.createElement('span');
  nameLabel.className = 'label';
  nameLabel.textContent = t('voices.editName');
  const nameInput = document.createElement('input');
  nameInput.className = 'input';
  nameInput.type = 'text';
  nameInput.maxLength = 120;
  nameInput.value = voice.name || '';
  nameInput.id = `voice-edit-name-${voice.id}`;
  nameLabel.htmlFor = nameInput.id;
  nameField.append(nameLabel, nameInput);

  const transcriptField = document.createElement('label');
  transcriptField.className = 'field';
  const transcriptLabel = document.createElement('span');
  transcriptLabel.className = 'label';
  transcriptLabel.textContent = t('voices.editTranscript');
  const transcriptInput = document.createElement('textarea');
  transcriptInput.className = 'textarea';
  transcriptInput.rows = 4;
  transcriptInput.maxLength = LIMITS.transcript;
  transcriptInput.value = voice.transcript || '';
  transcriptInput.id = `voice-edit-tr-${voice.id}`;
  transcriptLabel.htmlFor = transcriptInput.id;
  transcriptField.append(transcriptLabel, transcriptInput);

  const counter = document.createElement('span');
  counter.className = 'count';
  const limitHint = document.createElement('span');
  limitHint.className = 'hint';
  limitHint.textContent = t('voices.transcriptLimit', { max: LIMITS.transcript });
  const emptyWarn = document.createElement('span');
  emptyWarn.className = 'hint warn';
  emptyWarn.textContent = t('voices.transcriptEmptyWarn');
  emptyWarn.hidden = Boolean(transcriptInput.value.trim());
  const under = document.createElement('div');
  under.className = 'seg-under';
  under.append(counter, limitHint);
  transcriptField.append(under, emptyWarn);

  const syncCounter = () => {
    counter.textContent = t('studio.charCount', { n: transcriptInput.value.length, max: LIMITS.transcript });
    counter.classList.toggle('over', transcriptInput.value.length > LIMITS.transcript);
    emptyWarn.hidden = Boolean(transcriptInput.value.trim());
  };
  syncCounter();
  transcriptInput.addEventListener('input', syncCounter);

  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent = t('voices.editHint');

  const errorNode = document.createElement('p');
  errorNode.className = 'error-line';
  errorNode.hidden = true;

  wrapper.append(nameField, transcriptField, hint, errorNode);

  await openModal({
    title: t('voices.editTitle', { name: voice.name || '' }),
    body: wrapper,
    actions: [
      { label: t('common.cancel'), kind: 'ghost', value: null },
      {
        label: t('common.save'),
        kind: 'primary',
        onClick: async () => {
          const name = nameInput.value.trim();
          if (!name) {
            errorNode.hidden = false;
            errorNode.textContent = t('errors.nameInvalid');
            nameInput.focus();
            return false;
          }
          if (transcriptInput.value.length > LIMITS.transcript) {
            errorNode.hidden = false;
            errorNode.textContent = t('voices.transcriptTooLong', { max: LIMITS.transcript });
            transcriptInput.focus();
            return false;
          }
          const result = await actions.editVoice(voice.id, {
            name,
            transcript: transcriptInput.value,
          });
          if (result !== true) {
            errorNode.hidden = false;
            errorNode.textContent = describeError(result).text;
            return false;
          }
          return true;
        },
      },
    ],
  });
}

async function startRecording() {
  if (!recordingSupported()) {
    actions.toastError({ code: 'recorder', message: t('errors.recorder') });
    return;
  }
  if (recorder) recorder.discard();
  recorder = new VoiceRecorder({
    onTick: (seconds) => {
      state.voiceForm.recordSeconds = seconds;
      const time = rootNode?.querySelector('#rec-time');
      if (time) time.textContent = fmtClock(seconds);
    },
  });
  try {
    await recorder.start();
    state.voiceForm.recording = true;
    state.voiceForm.error = null;
    paintRecordingState();
  } catch (error) {
    recorder = null;
    state.voiceForm.recording = false;
    actions.toastError({ code: 'mic', message: t('errors.mic') });
    paintRecordingState();
  }
}

async function stopRecording() {
  if (!recorder) return;
  const result = await recorder.stop();
  recorder = null;
  state.voiceForm.recording = false;
  paintRecordingState();
  if (!result || !result.blob || result.blob.size === 0) return;
  const extension = result.mime.includes('ogg') ? 'ogg' : 'webm';
  const file = new File([result.blob], `record-${Date.now()}.${extension}`, { type: result.mime });
  await adoptLocalFile(file, result.duration);
}

function cancelRecording() {
  if (recorder) {
    recorder.discard();
    recorder = null;
  }
  state.voiceForm.recording = false;
  state.voiceForm.recordSeconds = 0;
  paintRecordingState();
}

async function adoptLocalFile(file, knownDuration = 0) {
  const form = state.voiceForm;
  if (form.file && form.fileUrl) URL.revokeObjectURL(form.fileUrl);
  form.file = file;
  form.fileUrl = URL.createObjectURL(file);
  form.fileDuration = knownDuration;
  form.fileError = '';
  form.peaks = null;
  form.error = null;
  rerenderCurrent();
  const decoded = await decodeBlob(file);
  if (decoded) {
    form.peaks = decoded.peaks;
    form.fileDuration = decoded.duration;
    paintLocalWave();
  }
}

function onVoicesInput(event) {
  const form = state.voiceForm;
  const target = event.target;
  if (target.id === 'voice-name') form.name = target.value;
  else if (target.id === 'voice-transcript') form.transcript = target.value;
  else if (target.id === 'voice-trim-start') form.trimStart = target.value;
  else if (target.id === 'voice-trim-end') form.trimEnd = target.value;
}

function onVoicesChange(event) {
  const form = state.voiceForm;
  const target = event.target;
  if (target.id === 'voice-file') {
    const file = target.files && target.files[0];
    if (!file) return;
    if (file.size > LIMITS.uploadBytes) {
      actions.toastError({ code: 'upload_too_large', message: t('errors.fileTooBig') });
      return;
    }
    void adoptLocalFile(file);
    return;
  }
  if (target.id === 'voice-language') form.language = target.value;
  else if (target.id === 'voice-basis') form.basis = target.value;
  else if (target.id === 'voice-consent') form.consent = target.checked;
}

/* --------------------------------- design -------------------------------- */

function designResultCard() {
  const result = state.design.result;
  if (!result || !result.take_id) {
    if (state.design.running || state.design.pending) {
      return `<div class="design-result">
        <p class="hint">${esc(t('design.running'))}</p>
      </div>`;
    }
    return emptyState({ title: t('design.empty'), text: t('design.emptyText') });
  }
  return `<div class="design-result">
    <div class="result-row">
      ${playBtn(api.takeAudio(result.take_id))}
      <span class="result-label">${esc(t('design.result'))}</span>
      ${result.voice_id ? `<span class="badge ok">${esc(t('design.savedVoice'))}</span>` : ''}
    </div>
    ${waveBlock(api.takeAudio(result.take_id))}
  </div>`;
}

function designHistory() {
  if (!state.designs.length) return `<p class="hint">${esc(t('design.historyEmpty'))}</p>`;
  return `<div class="history-list">${state.designs
    .map((item) => {
      const date = item.created_at ? new Date(item.created_at) : null;
      const stamp = date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : '';
      return `<article class="history-item">
        <div class="history-head">
          ${playBtn(api.takeAudio(item.take_id), { small: true })}
          <span class="history-meta">${esc(stamp)} · ${esc(fmtClock(item.duration))} · ${esc(
            languageLabel(item.language),
          )}</span>
          ${item.voice_id ? `<span class="badge subtle">${esc(t('design.savedVoice'))}</span>` : ''}
        </div>
        <p class="history-text">${esc(String(item.text || '').slice(0, 180))}</p>
        <p class="history-ins">${esc(String(item.instruction || '').slice(0, 180))}</p>
      </article>`;
    })
    .join('')}</div>`;
}

export function renderDesign(root) {
  const form = state.designForm;
  const busy = form.busy || state.design.running;
  const limit = designLimit();
  const over = form.text.length > limit;
  const seedOk = parseSeed(form.seed).ok;
  const languagesOptions = languages()
    .map((code) => option(code, languageLabel(code), form.language))
    .join('');

  root.innerHTML = `<div class="two-col design-layout">
    <section class="panel">
      <h2 class="panel-title">${esc(t('design.title'))}</h2>
      <p class="callout">${ICON('info', 'icon xs')}<span>${esc(t('design.banner'))}</span></p>

      <div class="grid-2">
        <label class="field"><span class="label">${esc(t('design.name'))}</span>
          <input class="input" type="text" id="design-name" value="${esc(form.name)}" placeholder="${esc(
            t('design.namePlaceholder'),
          )}" maxlength="120" />
        </label>
        <label class="field"><span class="label">${esc(t('design.language'))}</span>
          <select class="select" id="design-language">${languagesOptions}</select>
        </label>
      </div>

      <label class="field"><span class="label">${esc(t('design.text'))}</span>
        <textarea class="textarea${over ? ' is-over' : ''}" id="design-text" rows="4" placeholder="${esc(
          t('design.textPlaceholder'),
        )}">${esc(form.text)}</textarea>
        <div class="seg-under">
          <span class="count${over ? ' over' : ''}" id="design-chars">${esc(
            t('design.charCount', { n: form.text.length, max: limit }),
          )}</span>
          <span class="hint">${esc(t('design.textLimitHint', { max: limit }))}</span>
        </div>
      </label>

      <label class="field"><span class="label">${esc(t('design.instruction'))}</span>
        <textarea class="textarea" id="design-instruction" rows="3" placeholder="${esc(
          t('design.instructionPlaceholder'),
        )}">${esc(form.instruction)}</textarea>
        <span class="hint">${esc(t('design.instructionLanguageNote'))}</span>
        <span class="hint warn" id="design-cyrillic" ${containsCyrillic(form.instruction) ? '' : 'hidden'}>${esc(
          t('design.cyrillicAdvisory'),
        )}</span>
        <span class="hint">${esc(t('design.attributesNote'))}</span>
      </label>

      <div class="field">
        <span class="label">${esc(t('design.presets'))}</span>
        <div class="chips">${DESIGN_PRESETS.map(
          (preset) =>
            `<button type="button" class="chip" data-act="preset" data-preset="${esc(preset.id)}" title="${esc(
              preset.prompt,
            )}">${esc(t(preset.labelKey))}</button>`,
        ).join('')}</div>
      </div>

      <div class="grid-2">
        <label class="field"><span class="label">${esc(t('design.seed'))}</span>
          <input class="input${seedOk ? '' : ' is-over'}" type="number" id="design-seed" value="${esc(form.seed)}" step="1" />
          <span class="hint danger" id="design-seed-hint" ${seedOk ? 'hidden' : ''}>${esc(t('design.seedInvalid'))}</span>
        </label>
        <label class="check"><input type="checkbox" id="design-save-voice" ${
          form.saveAsVoice ? 'checked' : ''
        } /><span>${esc(t('design.saveAsVoice'))}</span></label>
      </div>
      <p class="hint">${esc(t('design.saveAsVoiceHint'))}</p>

      ${
        form.error
          ? `<p class="error-line"><span class="error-code">${esc(form.error.code || 'error')}</span> ${esc(
              describeError(form.error).text,
            )}</p>`
          : ''
      }

      <button type="button" class="btn primary block" data-act="design-run" ${
        busy || over || !seedOk ? 'disabled' : ''
      }>${busy ? esc(t('design.running')) : esc(t('design.run'))}</button>
    </section>

    <section class="panel">
      <h2 class="panel-title">${esc(t('design.result'))}</h2>
      ${designResultCard()}
      <h2 class="panel-title spaced">${esc(t('design.history'))}</h2>
      ${
        state.designsError
          ? `<p class="error-line"><span class="error-code">${esc(
              state.designsError.code || 'error',
            )}</span> ${esc(describeError(state.designsError).text)}</p>`
          : designHistory()
      }
    </section>
  </div>`;

  bind(root, { click: onDesignClick, input: onDesignInput, change: onDesignChange });
  paintWaveforms(root);
  paintPlayButtons(root);
}

function onDesignClick(event) {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'play') {
    togglePlay(button.dataset.audio);
    return;
  }
  if (act === 'preset') {
    const preset = getPreset(button.dataset.preset);
    if (preset) {
      state.designForm.instruction = preset.prompt;
      rerenderCurrent();
    }
    return;
  }
  if (act === 'design-run') {
    void actions.runDesign();
  }
}

function onDesignInput(event) {
  const form = state.designForm;
  const target = event.target;
  if (target.id === 'design-name') form.name = target.value;
  else if (target.id === 'design-text') {
    form.text = target.value;
    const limit = designLimit();
    const over = form.text.length > limit;
    const counter = rootNode?.querySelector('#design-chars');
    if (counter) {
      counter.textContent = t('design.charCount', { n: form.text.length, max: limit });
      counter.classList.toggle('over', over);
    }
    target.classList.toggle('is-over', over);
    const run = rootNode?.querySelector('[data-act="design-run"]');
    if (run) run.disabled = over || form.busy || state.design.running;
  } else if (target.id === 'design-instruction') {
    form.instruction = target.value;
    const advisory = rootNode?.querySelector('#design-cyrillic');
    if (advisory) advisory.hidden = !containsCyrillic(form.instruction);
  } else if (target.id === 'design-seed') {
    form.seed = target.value;
    const valid = parseSeed(form.seed).ok;
    target.classList.toggle('is-over', !valid);
    const hint = rootNode?.querySelector('#design-seed-hint');
    if (hint) hint.hidden = valid;
    const run = rootNode?.querySelector('[data-act="design-run"]');
    if (run) run.disabled = !valid || form.busy || state.design.running;
  }
}

function onDesignChange(event) {
  const form = state.designForm;
  const target = event.target;
  if (target.id === 'design-language') form.language = target.value;
  else if (target.id === 'design-save-voice') form.saveAsVoice = target.checked;
  else if (target.id === 'design-seed') {
    // Do not replace the Generate button during the seed field's blur event:
    // that would swallow the user's first click after editing this field.
    onDesignInput(event);
  }
}

/* --------------------------------- models -------------------------------- */

function installJobFor(modelId) {
  const jobId = state.installJobs[modelId];
  if (!jobId) return null;
  return state.jobs.find((job) => job.id === jobId) || null;
}

function anyActiveDownload() {
  return state.jobs.some(
    (job) => job.kind === 'download' && (job.state === 'queued' || job.state === 'running'),
  );
}

/** Catalog prose is ours: localize the three known ids, fall back to server text. */
function modelDescription(model) {
  const key = `models.desc.${model.id}`;
  return hasKey(key) ? t(key) : model.description || '';
}

const MODEL_STATUS_KEYS = {
  installed: 'models.installed',
  partial: 'models.statusPartial',
  missing: 'models.notInstalled',
};

/** Localizes the backend status tokens; unknown/free-text values pass through. */
function modelStatusLabel(status) {
  if (!status) return '—';
  const key = MODEL_STATUS_KEYS[String(status).toLowerCase()];
  return key ? t(key) : String(status);
}

function modelCard(model) {
  const offline = Boolean(state.status?.offline);
  const job = installJobFor(model.id);
  const running = job && (job.state === 'queued' || job.state === 'running');
  return `<article class="model-card${model.installed ? ' is-installed' : ''}">
    <div class="model-head">
      <div>
        <h3>${esc(model.label || model.id)}</h3>
        <p class="meta">${esc(t(model.kind === 'design' ? 'models.kindDesign' : 'models.kindClone'))} · ${esc(
          fmtNumber(model.size_gb),
        )} GB · ${esc(model.license || '—')}</p>
      </div>
      <span class="badge ${model.installed ? 'ok' : 'muted'}">${esc(
        model.installed ? t('models.installed') : t('models.notInstalled'),
      )}</span>
    </div>
    <p class="model-desc">${esc(modelDescription(model))}</p>
    <dl class="meta-grid">
      <div><dt>${esc(t('models.repo'))}</dt><dd>${esc(model.repo || '—')}</dd></div>
      <div><dt>${esc(t('models.revision'))}</dt><dd>${esc(model.revision || '—')}</dd></div>
      <div><dt>${esc(t('models.size'))}</dt><dd>${esc(fmtNumber(model.size_gb))} GB</dd></div>
      <div><dt>${esc(t('models.statusLabel'))}</dt><dd>${esc(modelStatusLabel(model.status))}</dd></div>
    </dl>
    ${
      running
        ? `<div class="model-progress">
             <progress max="1" value="${esc(job.progress || 0)}" data-job="${esc(job.id)}" aria-label="${esc(
               t('jobs.kindDownload'),
             )}"></progress>
             <span class="meta">${esc(stageLabel(job.stage) || t('jobs.running'))} · <span data-job-percent="${esc(
               job.id,
             )}">${esc(fmtPercent(job.progress || 0))}</span></span>
             <button type="button" class="btn ghost sm" data-act="cancel-job" data-job="${esc(job.id)}">${esc(
               t('jobs.cancel'),
             )}</button>
           </div>`
        : model.installed
          ? ''
          : `<button type="button" class="btn primary sm" data-act="install-model" data-model="${esc(model.id)}" ${
              offline || anyActiveDownload() ? 'disabled' : ''
            }>${ICON('download')}<span>${esc(t('models.install'))}</span></button>
             ${offline ? `<p class="hint danger">${esc(t('models.offline'))}</p>` : ''}`
    }
  </article>`;
}

export function renderModels(root) {
  const offline = Boolean(state.status?.offline);
  const loaded = state.status?.loaded_model || '';
  const installed = state.models.filter((model) => model.installed).length;

  root.innerHTML = `<div class="stack">
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">${esc(t('models.engine'))}</h2>
        <span class="badge ${state.status?.engine_available ? 'ok' : 'warn'}">${esc(
          state.status?.engine_available ? t('status.engineOk') : t('status.engineMissing'),
        )}</span>
      </div>
      <dl class="meta-grid">
        <div><dt>${esc(t('status.model'))}</dt><dd>${esc(loaded || t('status.noModel'))}</dd></div>
        <div><dt>${esc(t('models.installedModels'))}</dt><dd>${installed}</dd></div>
        <div><dt>${esc(t('status.device'))}</dt><dd>${esc(state.status?.device?.name || t('status.cpu'))}</dd></div>
        <div><dt>${esc(t('status.vram'))}</dt><dd>${esc(
          state.status?.device?.vram_mb ? `${state.status.device.vram_mb} MB` : '—',
        )}</dd></div>
      </dl>
      <button type="button" class="btn ghost sm" data-act="unload" ${
        !loaded || isBusy() ? 'disabled' : ''
      }>${ICON('refresh')}<span>${esc(t('models.unload'))}</span></button>
      <p class="hint">${esc(t('status.unloadHint'))}</p>
      ${offline ? `<p class="hint danger">${esc(t('models.offline'))}</p>` : ''}
    </section>

    <section class="panel">
      <h2 class="panel-title">${esc(t('models.title'))} <span class="count">${state.models.length}</span></h2>
      <p class="hint">${esc(t('models.sub'))}</p>
      ${
        state.modelsError
          ? `<p class="error-line"><span class="error-code">${esc(
              state.modelsError.code || 'error',
            )}</span> ${esc(describeError(state.modelsError).text)}</p>`
          : ''
      }
      ${
        state.models.length
          ? `<div class="model-list">${state.models.map(modelCard).join('')}</div>`
          : emptyState({ title: t('models.empty'), text: t('models.emptyHint') })
      }
      <p class="hint">${esc(t('models.notes'))}</p>
    </section>
  </div>`;

  bind(root, { click: onModelsClick });
}

async function onModelsClick(event) {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'install-model') {
    const model = modelById(button.dataset.model);
    if (!model) return;
    if (state.status?.offline) {
      actions.toastError({ code: 'offline', message: t('models.offline') });
      return;
    }
    const accepted = await licenseDialog({ model });
    if (accepted) await actions.downloadModel(model.id);
    return;
  }
  if (act === 'unload') {
    await actions.unloadEngine();
    return;
  }
  if (act === 'cancel-job') {
    await actions.cancelJob(button.dataset.job);
  }
}

/* ---------------------------------- guide -------------------------------- */

export function renderGuide(root) {
  const sections = [
    ['guide.howTitle', 'guide.howBody'],
    ['guide.limitsTitle', 'guide.limitsBody'],
    ['guide.staleTitle', 'guide.staleBody'],
    ['guide.consentTitle', 'guide.consentBody'],
    ['guide.dataTitle', 'guide.dataBody'],
    ['guide.exportTitle', 'guide.exportBody'],
    ['guide.keysTitle', 'guide.keysBody'],
    ['guide.licenseTitle', 'guide.licenseBody'],
  ];
  root.innerHTML = `<div class="stack guide">
    <section class="panel">
      <h2 class="panel-title">${esc(t('guide.title'))}</h2>
      <p class="hint">${esc(t('guide.sub'))}</p>
      <div class="guide-sections">
        ${sections
          .map(
            ([titleKey, bodyKey]) => `<article class="guide-section">
              <h3>${esc(t(titleKey))}</h3>
              <p>${esc(t(bodyKey))}</p>
            </article>`,
          )
          .join('')}
      </div>
      <div class="guide-actions">
        <button type="button" class="btn ghost" data-act="go-models">${esc(t('nav.models'))}</button>
        <button type="button" class="btn ghost" data-act="go-studio">${esc(t('nav.studio'))}</button>
        <button type="button" class="btn ghost" data-act="go-voices">${esc(t('nav.voices'))}</button>
      </div>
    </section>
  </div>`;

  bind(root, {
    click: (event) => {
      const button = event.target.closest('[data-act]');
      if (!button) return;
      if (button.dataset.act === 'go-models') navigate('models');
      if (button.dataset.act === 'go-studio') navigate('studio');
      if (button.dataset.act === 'go-voices') navigate('voices');
    },
  });
}

export const VIEWS = {
  studio: renderStudio,
  voices: renderVoices,
  design: renderDesign,
  models: renderModels,
  guide: renderGuide,
};
