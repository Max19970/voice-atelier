// Canonical VoiceDesign instructions. Pure module: no DOM, no i18n, no fetch.
//
// The instruction language is independent of the spoken language. These prompts
// stay English regardless of the interface locale so the model sees a stable,
// first-party description; the UI only localizes the button labels.

export const PRESET_PROMPT_LOCALE = 'en';

export const DESIGN_PRESETS = [
  {
    id: 'narrator',
    labelKey: 'design.presetNarrator',
    prompt: 'A low-pitched, calm male voice, 45 years old, with a measured narrator delivery.',
  },
  {
    id: 'warm',
    labelKey: 'design.presetWarm',
    prompt: 'A warm female voice, 30 years old, with a soft smile in the intonation.',
  },
  {
    id: 'news',
    labelKey: 'design.presetNews',
    prompt: 'A neutral news-reader voice with crisp articulation and a steady pace.',
  },
  {
    id: 'child',
    labelKey: 'design.presetChild',
    prompt: 'A bright child voice, 8 years old, fast and joyful.',
  },
  {
    id: 'whisper',
    labelKey: 'design.presetWhisper',
    prompt: 'A quiet whisper, close to the microphone, with a slow delivery.',
  },
  {
    id: 'character',
    labelKey: 'design.presetCharacter',
    prompt: 'A slightly raspy character voice, 60 years old, with an ironic delivery.',
  },
];

/** Presets offered inside Studio for a design-mode segment. */
export const STUDIO_PRESET_IDS = ['narrator', 'warm', 'news', 'child'];

export function getPreset(id) {
  return DESIGN_PRESETS.find((preset) => preset.id === id) || null;
}

export function studioPresets() {
  return STUDIO_PRESET_IDS.map(getPreset).filter(Boolean);
}

/** Advisory only: never used to reject or rewrite input. */
export function containsCyrillic(text) {
  return /[\u0400-\u04FF]/.test(String(text || ''));
}
