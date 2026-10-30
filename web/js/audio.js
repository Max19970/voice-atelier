// Audio helpers: authenticated loading, cached waveforms, a single player, and
// MediaRecorder capture that always releases the microphone tracks.

import { fetchBlobUrl } from './api.js';
import { esc } from './util.js';

const audioCache = new Map(); // api path -> { objectUrl, blob, peaks }
const pending = new Map(); // api path -> Promise

let audioContext = null;
function context() {
  if (!audioContext) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    audioContext = new Ctor();
  }
  if (audioContext.state === 'suspended') audioContext.resume().catch(() => {});
  return audioContext;
}

export function closeAudioContext() {
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

/** Loads audio bytes once, returns a blob URL and cached peak data. */
export async function loadAudio(path) {
  if (!path) throw new Error('empty audio path');
  if (audioCache.has(path)) return audioCache.get(path);
  if (pending.has(path)) return pending.get(path);

  const task = (async () => {
    const { objectUrl, blob } = await fetchBlobUrl(path);
    let peaks = null;
    try {
      const ctx = context();
      if (ctx) {
        const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
        peaks = computePeaks(buffer, 220);
      }
    } catch (_) {
      peaks = null;
    }
    const record = { objectUrl, blob, peaks };
    audioCache.set(path, record);
    pending.delete(path);
    return record;
  })();

  pending.set(path, task);
  try {
    return await task;
  } catch (error) {
    pending.delete(path);
    throw error;
  }
}

export function cachedAudio(path) {
  return audioCache.get(path) || null;
}

/** Decodes a local Blob (upload or recording) for duration and peaks. */
export async function decodeBlob(blob) {
  const ctx = context();
  if (!ctx || !blob) return null;
  try {
    const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    return { duration: buffer.duration, peaks: computePeaks(buffer, 220) };
  } catch (_) {
    return null;
  }
}

export function computePeaks(buffer, buckets = 200) {
  const channel = buffer.getChannelData(0);
  const size = Math.max(1, Math.floor(channel.length / buckets));
  const peaks = new Float32Array(buckets);
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const start = bucket * size;
    const end = Math.min(channel.length, start + size);
    let peak = 0;
    for (let index = start; index < end; index += 1) {
      const value = Math.abs(channel[index]);
      if (value > peak) peak = value;
    }
    peaks[bucket] = peak;
  }
  let max = 0;
  for (let i = 0; i < peaks.length; i += 1) max = Math.max(max, peaks[i]);
  if (max > 0) {
    for (let i = 0; i < peaks.length; i += 1) peaks[i] /= max;
  }
  return peaks;
}

/** Draws a waveform on a canvas sized through width/height attributes (no inline CSS). */
export function drawWaveform(canvas, peaks, options = {}) {
  if (!canvas) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cssWidth = canvas.clientWidth || canvas.parentElement?.clientWidth || 320;
  const cssHeight = canvas.clientHeight || 48;
  canvas.width = Math.max(1, Math.round(cssWidth * dpr));
  canvas.height = Math.max(1, Math.round(cssHeight * dpr));
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const styles = window.getComputedStyle(canvas);
  const idle = options.idleColor || styles.getPropertyValue('--wave-idle').trim() || '#4a3f36';
  const active = options.activeColor || styles.getPropertyValue('--wave-active').trim() || '#e0a860';
  const progress = Number.isFinite(options.progress) ? Math.min(1, Math.max(0, options.progress)) : 0;
  const mid = cssHeight / 2;

  if (!peaks || peaks.length === 0) {
    ctx.strokeStyle = idle;
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(cssWidth, mid);
    ctx.stroke();
    return;
  }

  const bars = Math.min(peaks.length, Math.floor(cssWidth / 3));
  const step = cssWidth / bars;
  const barWidth = Math.max(1, step * 0.55);
  for (let index = 0; index < bars; index += 1) {
    const value = peaks[Math.floor((index / bars) * peaks.length)] || 0;
    const height = Math.max(1.5, value * (cssHeight - 6));
    const x = index * step + (step - barWidth) / 2;
    const played = cssWidth > 0 && x / cssWidth <= progress;
    ctx.fillStyle = played && progress > 0 ? active : idle;
    ctx.globalAlpha = played && progress > 0 ? 1 : 0.85;
    ctx.fillRect(x, mid - height / 2, barWidth, height);
  }
  ctx.globalAlpha = 1;
}

/** Repaints waveforms that are present in the document. */
export async function paintWaveforms(root = document) {
  const canvases = root.querySelectorAll('canvas[data-wave]');
  for (const canvas of canvases) {
    if (!canvas.isConnected) continue;
    const path = canvas.getAttribute('data-wave');
    try {
      const record = await loadAudio(path);
      if (!canvas.isConnected) continue;
      drawWaveform(canvas, record.peaks);
    } catch (_) {
      drawWaveform(canvas, null);
    }
  }
}

/* ------------------------------- player -------------------------------- */

const playerListeners = new Set();
const player = {
  element: null,
  url: null,
  playing: false,
};

export function onPlayerChange(fn) {
  playerListeners.add(fn);
  return () => playerListeners.delete(fn);
}

function emitPlayer() {
  for (const fn of playerListeners) fn({ url: player.url, playing: player.playing });
}

function ensureElement() {
  if (player.element) return player.element;
  const element = new Audio();
  element.preload = 'auto';
  element.addEventListener('ended', () => {
    player.playing = false;
    emitPlayer();
  });
  element.addEventListener('pause', () => {
    if (player.playing) {
      player.playing = false;
      emitPlayer();
    }
  });
  element.addEventListener('play', () => {
    player.playing = true;
    emitPlayer();
  });
  player.element = element;
  return element;
}

export function currentAudio() {
  return { url: player.url, playing: player.playing };
}

export function isPlaying(path) {
  return player.url === path && player.playing;
}

export function pausePlayback() {
  if (player.element) player.element.pause();
  player.playing = false;
  emitPlayer();
}

export function stopPlayback() {
  if (player.element) {
    player.element.pause();
    player.element.removeAttribute('src');
    player.element.load();
  }
  player.url = null;
  player.playing = false;
  emitPlayer();
}

/** Plays path, or stops it when it is already the current track. */
export async function togglePlay(path) {
  if (!path) return;
  if (player.url === path && player.playing) {
    pausePlayback();
    return;
  }
  const element = ensureElement();
  const record = await loadAudio(path);
  if (player.url !== path) {
    element.src = record.objectUrl;
    element.currentTime = 0;
  }
  player.url = path;
  try {
    await element.play();
    player.playing = true;
  } catch (_) {
    player.playing = false;
  }
  emitPlayer();
}

/* ------------------------------ recorder ------------------------------- */

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

export function recordingSupported() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

export function isSecureForRecording() {
  return window.isSecureContext === true;
}

function pickMime() {
  if (!window.MediaRecorder || typeof window.MediaRecorder.isTypeSupported !== 'function') return '';
  for (const candidate of MIME_CANDIDATES) {
    if (window.MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return '';
}

export class VoiceRecorder {
  constructor({ onTick } = {}) {
    this.onTick = onTick || (() => {});
    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.startedAt = 0;
    this.timer = 0;
    this.mime = '';
  }

  get active() {
    return Boolean(this.recorder) && this.recorder.state !== 'inactive';
  }

  async start() {
    if (this.active) return;
    if (!recordingSupported()) throw new Error('unsupported');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    this.stream = stream;
    this.mime = pickMime();
    this.chunks = [];
    const recorder = this.mime ? new MediaRecorder(stream, { mimeType: this.mime }) : new MediaRecorder(stream);
    this.recorder = recorder;
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) this.chunks.push(event.data);
    });
    recorder.start(250);
    this.startedAt = performance.now();
    this.timer = window.setInterval(() => {
      this.onTick((performance.now() - this.startedAt) / 1000);
    }, 200);
  }

  stop() {
    const recorder = this.recorder;
    if (!recorder) return Promise.resolve(null);
    return new Promise((resolve) => {
      const finish = () => {
        const duration = (performance.now() - this.startedAt) / 1000;
        const blob = new Blob(this.chunks, { type: this.mime || 'audio/webm' });
        const mime = this.mime || 'audio/webm';
        this.dispose();
        resolve({ blob, duration, mime });
      };
      recorder.addEventListener('stop', finish, { once: true });
      try {
        recorder.stop();
      } catch (_) {
        finish();
      }
    });
  }

  /** Stops the recorder and releases the microphone without keeping audio. */
  discard() {
    try {
      if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    } catch (_) {
      /* ignore */
    }
    this.chunks = [];
    this.dispose();
  }

  dispose() {
    window.clearInterval(this.timer);
    this.timer = 0;
    this.recorder = null;
    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        try {
          track.stop();
        } catch (_) {
          /* ignore */
        }
      }
    }
    this.stream = null;
    this.chunks = [];
  }
}

/** Small helper for markup: a play/stop button bound to a data-audio attribute. */
export function playButtonHtml(path, { label } = {}) {
  const title = label || '';
  return `<button type="button" class="icon-btn" data-act="play" data-audio="${esc(path)}" aria-label="${esc(
    title,
  )}" title="${esc(title)}"><svg class="icon" aria-hidden="true"><use href="#i-play"></use></svg></button>`;
}
