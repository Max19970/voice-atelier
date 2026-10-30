// Pure helpers for the Voice Design flow so ordering/seed rules are testable
// without a browser or a running server.

export const MAX_SEED = 2147483647;

/**
 * Parses a seed without silently clamping. Accepts an integer 0..MAX_SEED
 * (numbers or digit-only strings). Returns {ok:true,value} or {ok:false,reason}.
 */
export function parseSeed(input) {
  if (typeof input === 'number') {
    if (!Number.isFinite(input) || !Number.isInteger(input)) return { ok: false, reason: 'integer' };
    if (input < 0) return { ok: false, reason: 'range' };
    if (input > MAX_SEED) return { ok: false, reason: 'range' };
    return { ok: true, value: input };
  }
  const raw = String(input ?? '').trim();
  if (!raw) return { ok: false, reason: 'empty' };
  if (!/^\d+$/.test(raw)) return { ok: false, reason: 'integer' };
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) return { ok: false, reason: 'range' };
  if (value > MAX_SEED) return { ok: false, reason: 'range' };
  return { ok: true, value };
}

/**
 * Returns the design result that may become the visible current result.
 * A completed job is accepted only when it is the explicitly started job, so
 * polling order (newest-first) can never let an older job overwrite the view.
 */
export function designCompletion(job, currentJobId) {
  if (!job || job.kind !== 'design' || job.state !== 'done') return null;
  if (typeof currentJobId !== 'string' || !currentJobId || job.id !== currentJobId) return null;
  if (!job.result || !job.result.take_id) return null;
  return {
    take_id: String(job.result.take_id),
    voice_id: job.result.voice_id ? String(job.result.voice_id) : null,
    job_id: job.id,
  };
}

/** Newest persisted design by created_at; independent of array order. */
export function newestDesign(items) {
  if (!Array.isArray(items)) return null;
  let best = null;
  for (const item of items) {
    if (!item || !item.take_id) continue;
    const stamp = Date.parse(item.created_at || '') || 0;
    if (!best || stamp > best.stamp) {
      best = {
        stamp,
        value: {
          take_id: String(item.take_id),
          voice_id: item.voice_id ? String(item.voice_id) : null,
          created_at: item.created_at || '',
        },
      };
    }
  }
  return best ? best.value : null;
}
