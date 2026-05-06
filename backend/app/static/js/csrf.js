/* =============================================================================
   csrf.js
   Universal helpers for CSRF-safe AJAX. Loaded by base.html.

   Per ADR-0010 / 04-api-contracts.md:
     - cookie `mas_csrf` is set by the backend (not HttpOnly so JS can read it).
     - For state-changing AJAX requests, JS must send `X-CSRF-Token` header
       containing the same value as the cookie (double-submit pattern).
     - Backend validates: cookie value must equal header value AND must equal
       the server-side session-bound token.

   Exposes a single global object `window.MAS` with:
     - getCsrfToken(): string  — returns token from cookie, or "" if absent.
     - csrfFetch(url, options): Promise<Response> — wrapper around fetch.
     - flash(text, category): renders a transient flash message at the top of <main>.
     - readJsonError(response): Promise<{code, message}> — parses standard error envelope.

   No third-party dependencies. ES2022 only (no transpilation).
   ========================================================================== */
(function () {
  'use strict';

  /** Read cookie value by name. Returns "" if missing. */
  function readCookie(name) {
    const target = name + '=';
    const parts = document.cookie ? document.cookie.split(';') : [];
    for (let i = 0; i < parts.length; i++) {
      const c = parts[i].trim();
      if (c.indexOf(target) === 0) {
        return decodeURIComponent(c.substring(target.length));
      }
    }
    return '';
  }

  function getCsrfToken() {
    // Primary source: mas_csrf cookie. Fallback: <meta name="csrf-token">.
    const cookieValue = readCookie('mas_csrf');
    if (cookieValue) return cookieValue;
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  /**
   * fetch() wrapper that:
   *   - always sends cookies (`credentials: 'same-origin'`),
   *   - injects `X-CSRF-Token` header for state-changing methods,
   *   - sets `Accept: application/json` by default,
   *   - sets `Content-Type: application/json` when body is a plain object.
   */
  function csrfFetch(url, options) {
    const opts = Object.assign({}, options || {});
    const method = (opts.method || 'GET').toUpperCase();
    const headers = new Headers(opts.headers || {});

    if (!headers.has('Accept')) {
      headers.set('Accept', 'application/json');
    }

    // If body is a plain object (not FormData / not string / not URLSearchParams),
    // serialize as JSON.
    if (
      opts.body &&
      typeof opts.body === 'object' &&
      !(opts.body instanceof FormData) &&
      !(opts.body instanceof URLSearchParams) &&
      !(opts.body instanceof Blob) &&
      typeof opts.body.byteLength !== 'number'
    ) {
      opts.body = JSON.stringify(opts.body);
      if (!headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }
    }

    const isStateChanging = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
    if (isStateChanging) {
      const token = getCsrfToken();
      if (token) headers.set('X-CSRF-Token', token);
    }

    opts.headers = headers;
    opts.credentials = opts.credentials || 'same-origin';
    return fetch(url, opts);
  }

  /**
   * Parse the error envelope used by the backend (04-api-contracts.md sec.
   * "Унифицированный формат ошибок"). Returns {code, message, field, details}.
   * Falls back to a generic message if the body is not JSON.
   */
  async function readJsonError(response) {
    let data = null;
    try {
      data = await response.json();
    } catch (_e) {
      data = null;
    }
    if (data && data.error && typeof data.error === 'object') {
      return {
        code: data.error.code || 'unknown_error',
        message: data.error.message || 'Request failed.',
        field: data.error.field || null,
        details: data.error.details || null,
      };
    }
    return {
      code: 'http_' + response.status,
      message: 'Request failed (HTTP ' + response.status + ').',
      field: null,
      details: null,
    };
  }

  /** Render a transient flash message at the top of <main>. */
  function flash(text, category) {
    const cat = category || 'info';
    const main = document.getElementById('main') || document.querySelector('main');
    if (!main) return;
    let list = main.querySelector('.flashes');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'flashes';
      list.setAttribute('role', 'status');
      list.setAttribute('aria-live', 'polite');
      main.insertBefore(list, main.firstChild);
    }
    const item = document.createElement('li');
    item.className = 'flash flash--' + cat;
    item.textContent = text;
    list.appendChild(item);
    // auto-dismiss after 6s for success/info; keep error/warning until next nav
    if (cat === 'success' || cat === 'info') {
      setTimeout(function () {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, 6000);
    }
  }

  window.MAS = Object.freeze({
    getCsrfToken: getCsrfToken,
    csrfFetch: csrfFetch,
    readJsonError: readJsonError,
    flash: flash,
  });
})();
