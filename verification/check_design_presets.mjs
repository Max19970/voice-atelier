#!/usr/bin/env node
// Regression helper for the Voice Design presets and result-ordering rules.
// Pure Node: imports the shared browser modules (no DOM, no network) and does a
// static wiring check on views.js/state.js. Exit 1 with an inventory on failure.
//
// Usage: node verification/check_design_presets.mjs

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const web = path.join(root, 'web');

const {
  DESIGN_PRESETS,
  STUDIO_PRESET_IDS,
  PRESET_PROMPT_LOCALE,
  studioPresets,
  getPreset,
  containsCyrillic,
} = await import(pathToFileURL(path.join(web, 'js', 'design-presets.js')).href);

const { designCompletion, newestDesign, parseSeed, MAX_SEED } = await import(
  pathToFileURL(path.join(web, 'js', 'design-state.js')).href
);

const failures = [];
function check(condition, message) {
  if (!condition) failures.push(message);
}

/* 1. Presets are canonical English, independent of the UI locale. */
check(PRESET_PROMPT_LOCALE === 'en', 'PRESET_PROMPT_LOCALE must be "en"');
check(DESIGN_PRESETS.length === 6, `expected 6 presets, got ${DESIGN_PRESETS.length}`);

const ids = new Set();
const labels = new Set();
for (const preset of DESIGN_PRESETS) {
  check(!ids.has(preset.id), `duplicate preset id: ${preset.id}`);
  ids.add(preset.id);
  check(!labels.has(preset.labelKey), `duplicate label key: ${preset.labelKey}`);
  labels.add(preset.labelKey);
  check(
    typeof preset.labelKey === 'string' && preset.labelKey.startsWith('design.preset'),
    `labelKey must point at design.preset*, got ${preset.labelKey}`,
  );
  check(
    /^[\x20-\x7E]+$/.test(preset.prompt),
    `prompt for "${preset.id}" is not ASCII/English: ${JSON.stringify(preset.prompt)}`,
  );
  check(!containsCyrillic(preset.prompt), `prompt for "${preset.id}" contains Cyrillic`);
  check(preset.prompt.trim() === preset.prompt, `prompt for "${preset.id}" has stray whitespace`);
  check(preset.prompt.endsWith('.'), `prompt for "${preset.id}" must end with a period`);
}

const narrator = DESIGN_PRESETS.find((preset) => preset.id === 'narrator');
check(
  narrator &&
    narrator.prompt ===
      'A low-pitched, calm male voice, 45 years old, with a measured narrator delivery.',
  'narrator prompt must equal the AB-tested canonical string',
);

/* 2. Studio offers the documented subset through the same shared module. */
check(STUDIO_PRESET_IDS.length === 4, `expected 4 studio presets, got ${STUDIO_PRESET_IDS.length}`);
const studio = studioPresets();
check(studio.length === 4, 'studioPresets() must return 4 presets');
check(
  studio.every((preset) => preset && DESIGN_PRESETS.includes(preset)),
  'studioPresets() must return objects from DESIGN_PRESETS',
);
check(
  JSON.stringify(STUDIO_PRESET_IDS) === JSON.stringify(['narrator', 'warm', 'news', 'child']),
  'studio preset ids/order changed unexpectedly',
);
check(getPreset('narrator') === narrator, 'getPreset("narrator") must return the shared object');
check(getPreset('missing') === null, 'getPreset(unknown) must return null');

/* 3. Result ordering: only the explicitly started job may win, regardless of order. */
const jobOlder = { id: 'job-old', kind: 'design', state: 'done', result: { take_id: 'take-old' } };
const jobCurrent = { id: 'job-new', kind: 'design', state: 'done', result: { take_id: 'take-new', voice_id: 'v1' } };

check(
  designCompletion(jobOlder, 'job-new') === null,
  'an older completed design must not overwrite the current job result',
);
const acceptedNewest = designCompletion(jobCurrent, 'job-new');
check(acceptedNewest?.take_id === 'take-new', 'the explicit current job result must be accepted');
check(acceptedNewest?.voice_id === 'v1', 'voice_id must be carried through');
check(
  designCompletion({ id: 'x', kind: 'generate', result: { take_id: 't' } }, null) === null,
  'non-design jobs must never produce a design result',
);
check(
  designCompletion({ id: 'y', kind: 'design', result: {} }, null) === null,
  'a design job without take_id must be ignored',
);

check(designCompletion(jobCurrent, null) === null, 'no explicit current id must not adopt a completed job');
check(designCompletion(jobCurrent, '') === null, 'empty current id must not adopt a completed job');
for (const state of ['queued', 'running', 'failed', 'canceled']) {
  check(designCompletion({ ...jobCurrent, state }, 'job-new') === null,
    `${state} jobs carrying an old result must not publish it`);
}
for (const pollOrder of [[jobCurrent, jobOlder], [jobOlder, jobCurrent]]) {
  const applied = pollOrder.map((job) => designCompletion(job, 'job-new')).filter(Boolean).map((result) => result.take_id);
  check(applied.length === 1 && applied[0] === 'take-new',
    `completion order applied ${JSON.stringify(applied)} instead of only take-new`);
}

/* 4. Reload restore picks the newest design by created_at, not array order. */
const history = [
  { take_id: 'old', created_at: '2026-10-29T10:00:00Z' },
  { take_id: 'newest', created_at: '2026-10-29T12:00:00Z', voice_id: 'v2' },
  { take_id: 'middle', created_at: '2026-10-29T11:00:00Z' },
  { take_id: '', created_at: '2026-10-29T13:00:00Z' },
];
check(newestDesign(history)?.take_id === 'newest', 'newestDesign must use created_at, not array order');
check(newestDesign([]) === null, 'newestDesign([]) must be null');
check(newestDesign(null) === null, 'newestDesign(null) must be null');

/* 5. Seed parsing preserves 0 and rejects invalid input without clamping. */
check(MAX_SEED === 2147483647, 'seed limit must match the backend 0..2^31-1 contract');
const seeds = [
  [0, true, 0],
  ['0', true, 0],
  [42, true, 42],
  ['42', true, 42],
  [MAX_SEED, true, MAX_SEED],
  ['  17  ', true, 17],
  ['', false],
  ['   ', false],
  ['abc', false],
  ['1.5', false],
  [-1, false],
  ['-1', false],
  [MAX_SEED + 1, false],
  [Number.NaN, false],
  [Number.POSITIVE_INFINITY, false],
  [true, false],
];
for (const [input, ok, value] of seeds) {
  const parsed = parseSeed(input);
  check(parsed.ok === ok, `parseSeed(${JSON.stringify(input)}) ok=${parsed.ok}, expected ${ok}`);
  if (ok) check(parsed.value === value, `parseSeed(${JSON.stringify(input)}) value=${parsed.value}, expected ${value}`);
  if (!ok) check(typeof parsed.reason === 'string', `parseSeed(${JSON.stringify(input)}) must explain the reason`);
}

/* 6. Static wiring: both tabs must write preset.prompt, never a translation. */
const views = await readFile(path.join(web, 'js', 'views.js'), 'utf8');
const stateSource = await readFile(path.join(web, 'js', 'state.js'), 'utf8');
const presetsModule = await readFile(path.join(web, 'js', 'design-presets.js'), 'utf8');

check(views.includes("from './design-presets.js'"), 'views.js must import the shared presets module');
check(
  views.includes('state.designForm.instruction = preset.prompt'),
  'Design tab must put the canonical English prompt into the visible instruction field',
);
check(
  views.includes('segment.instruction = preset.prompt'),
  'Studio design segment must put the canonical English prompt into its instruction field',
);
check(!/instruction\s*=\s*t\(/.test(views), 'instruction must never be assigned a translated t() string');
check(
  (views.match(/preset\.prompt/g) || []).length >= 2,
  'both tabs must reference preset.prompt',
);
check(!views.includes('DESIGN_PRESETS.slice'), 'views.js must use studioPresets(), not a local slice');
check(views.includes('containsCyrillic'), 'views.js must drive the Cyrillic advisory');
check(!/^\s*import\b/m.test(presetsModule), 'presets module must have no locale-dependent imports');
check(!/\bt\(\s*['"]/.test(presetsModule), 'presets module must not call t()');

check(stateSource.includes("from './design-state.js'"), 'state.js must import design-state helpers');
check(stateSource.includes('designCompletion(job, state.design.jobId)'), 'state.js must bind results to the explicit current job');
check(!stateSource.includes('designCompletion(job, null)'), 'state.js must not bypass the explicit job binding');
check(stateSource.includes('!state.design.pending'), 'history restore must not run while POST is pending');
check(stateSource.includes('newestDesign('), 'state.js must restore the newest design on reload');
check(stateSource.includes('parseSeed('), 'state.js must validate seeds with parseSeed');
check(!stateSource.includes('Number(form.seed) || 42'), 'state.js must not coerce seed with || 42');

/* Report */
if (failures.length) {
  console.error(`FAIL: ${failures.length} design-preset/state check(s) failed:`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(
  `OK: ${DESIGN_PRESETS.length} canonical EN presets, ${studio.length} studio presets, ` +
    `ordering + seed rules verified, both tabs wired to preset.prompt.`,
);
