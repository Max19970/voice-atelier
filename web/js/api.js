// Authenticated JSON/blob client for the local Voice Atelier API.
// Every request carries the per-launch token from <meta name="studio-token">.

const TOKEN_PLACEHOLDER = '__STUDIO_TOKEN__';
const meta = document.querySelector('meta[name="studio-token"]');
export const rawToken = meta ? meta.getAttribute('content') || '' : '';
export const hasToken = Boolean(rawToken) && rawToken !== TOKEN_PLACEHOLDER;

export class ApiError extends Error {
  constructor({ code = 'error', message = '', status = 0, details = null } = {}) {
    super(message || code);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

async function readError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  const body = payload && typeof payload === 'object' ? payload.error : null;
  if (body && typeof body === 'object') {
    return new ApiError({
      code: String(body.code || `http_${response.status}`),
      message: String(body.message || response.statusText || 'Request failed'),
      status: response.status,
      details: body.details ?? null,
    });
  }
  return new ApiError({
    code: `http_${response.status}`,
    message: response.statusText || `HTTP ${response.status}`,
    status: response.status,
  });
}

/**
 * Performs a request against the local API.
 * @param {string} path absolute path starting with "/"
 * @param {{method?:string, body?:any, auth?:boolean, signal?:AbortSignal}} options
 */
async function request(path, options = {}) {
  const { method = 'GET', body, auth = true, signal } = options;
  const headers = new Headers({ Accept: 'application/json' });
  if (auth && hasToken) headers.set('X-Studio-Token', rawToken);

  let payload = body;
  if (body !== undefined && body !== null && !(body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
    payload = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: payload,
      credentials: 'same-origin',
      signal,
    });
  } catch (cause) {
    if (cause && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'network_error',
      message: cause && cause.message ? cause.message : 'Network request failed',
      status: 0,
    });
  }

  if (!response.ok) throw await readError(response);
  if (response.status === 204) return null;

  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return response.json();
  return response.text();
}

/** Fetch bytes with the token and return a blob URL plus a server provided name. */
export async function fetchBlobUrl(path) {
  const headers = new Headers();
  if (hasToken) headers.set('X-Studio-Token', rawToken);
  let response;
  try {
    response = await fetch(path, { headers, credentials: 'same-origin' });
  } catch (cause) {
    throw new ApiError({ code: 'network_error', message: String(cause && cause.message ? cause.message : cause) });
  }
  if (!response.ok) throw await readError(response);
  const blob = await response.blob();
  const filename = filenameFromDisposition(response.headers.get('content-disposition'));
  return { blob, filename, objectUrl: URL.createObjectURL(blob) };
}

function filenameFromDisposition(header) {
  if (!header) return '';
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1].trim());
    } catch (_) {
      return utf8[1].trim();
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : '';
}

const enc = encodeURIComponent;

export const api = {
  health: () => request('/api/health', { auth: false }),
  status: () => request('/api/status'),

  models: () => request('/api/models'),
  downloadModel: (id) => request(`/api/models/${enc(id)}/download`, { method: 'POST', body: { accept_license: true } }),
  unloadEngine: () => request('/api/engine/unload', { method: 'POST', body: {} }),

  voices: () => request('/api/voices'),
  createVoice: (formData) => request('/api/voices', { method: 'POST', body: formData }),
  updateVoice: (id, body) => request(`/api/voices/${enc(id)}`, { method: 'PATCH', body }),
  deleteVoice: (id) => request(`/api/voices/${enc(id)}`, { method: 'DELETE' }),
  voiceAudio: (id) => `/api/voices/${enc(id)}/audio`,

  projects: () => request('/api/projects'),
  createProject: (title) => request('/api/projects', { method: 'POST', body: { title } }),
  project: (id) => request(`/api/projects/${enc(id)}`),
  saveProject: (id, body) => request(`/api/projects/${enc(id)}`, { method: 'PUT', body }),
  deleteProject: (id) => request(`/api/projects/${enc(id)}`, { method: 'DELETE' }),

  generate: (id, body) => request(`/api/projects/${enc(id)}/generate`, { method: 'POST', body }),
  selectTake: (id, body) => request(`/api/projects/${enc(id)}/select`, { method: 'POST', body }),
  exportProject: (id, body) => request(`/api/projects/${enc(id)}/export`, { method: 'POST', body }),
  takeAudio: (id) => `/api/takes/${enc(id)}/audio`,
  exportDownload: (id) => `/api/exports/${enc(id)}/download`,

  design: (body) => request('/api/design', { method: 'POST', body }),
  designs: () => request('/api/designs'),

  jobs: () => request('/api/jobs'),
  job: (id) => request(`/api/jobs/${enc(id)}`),
  cancelJob: (id) => request(`/api/jobs/${enc(id)}/cancel`, { method: 'POST', body: {} }),
};

/** Human readable error text: "code: message". */
export function errorText(error) {
  if (!error) return '';
  if (error instanceof ApiError) {
    return error.message && error.message !== error.code ? `${error.code}: ${error.message}` : error.code;
  }
  if (error.code && error.message) return `${error.code}: ${error.message}`;
  return String(error.message || error);
}
