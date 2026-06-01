/* Telegram WebApp adaptation — ADR-0018, 08-frontend.md §10.
 *
 * Loaded on every page via base.html with `defer` (after the official Telegram
 * SDK is fetched from https://telegram.org/js/telegram-web-app.js, also defer).
 * `defer` guarantees execution order: SDK first, then this glue.
 *
 * In a regular browser `window.Telegram` is undefined and this script
 * early-returns — no DOM/CSS mutations, no observable side effects.
 *
 * Inside Telegram WebView:
 *   - tg.ready()  -> Telegram hides its loading splash;
 *   - tg.expand() -> WebApp takes the full screen height (mobile);
 *   - body class `tg-app` is added (CSS rules in main.css §"Telegram WebApp
 *     adaptation" override the palette to use Telegram's themeParams);
 *   - themeParams are mirrored to CSS custom properties (--tg-bg, --tg-text,
 *     --tg-hint, --tg-link, --tg-button, --tg-button-text, --tg-secondary-bg)
 *     on <html>, with re-application on the `themeChanged` event.
 *
 * No `eval`, no `innerHTML`, no `document.write` — only addEventListener,
 * classList.add, and CSSStyleDeclaration.setProperty.
 */
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    // Regular browser: no SDK was loaded (no /js/telegram-web-app.js, or
    // network blocked it, or we're outside Telegram WebView). No-op.
    return;
  }

  // Tell Telegram we've finished mounting; this hides the WebApp splash.
  if (typeof tg.ready === "function") {
    tg.ready();
  }
  // Take the full available height on mobile WebView.
  if (typeof tg.expand === "function") {
    tg.expand();
  }

  document.body.classList.add("tg-app");

  // Map Telegram themeParams -> CSS variables on <html>. The CSS in main.css
  // reads these via `var(--tg-bg, <fallback>)` so missing values fall back
  // to the default light theme.
  var THEME_MAP = {
    bg_color: "--tg-bg",
    secondary_bg_color: "--tg-secondary-bg",
    text_color: "--tg-text",
    hint_color: "--tg-hint",
    link_color: "--tg-link",
    button_color: "--tg-button",
    button_text_color: "--tg-button-text",
  };

  function applyTheme() {
    var params = tg.themeParams || {};
    var root = document.documentElement;
    for (var key in THEME_MAP) {
      if (Object.prototype.hasOwnProperty.call(THEME_MAP, key)) {
        var value = params[key];
        if (value) {
          root.style.setProperty(THEME_MAP[key], value);
        }
      }
    }
  }

  applyTheme();

  // Re-apply when the user toggles light/dark in Telegram itself.
  if (typeof tg.onEvent === "function") {
    tg.onEvent("themeChanged", applyTheme);
  }

  // ADR-0022 §1.3 + §1.6 — Persistent SSO attempt + self-heal of the TG link.
  //
  // round-38: the `data-anonymous` / `isAnonymous` gate has been REMOVED. When we
  // have a non-empty `initData` from the Telegram WebApp we POST it to
  // `/api/telegram/auth` ALWAYS — for anonymous AND for already-logged-in users.
  // The frontend no longer decides "anonymous vs logged-in" (the `mas_session`
  // cookie is HttpOnly and not visible to JS). The backend reads `mas_session`
  // and picks the branch itself:
  //   - no valid session  → SSO (linked/unlinked), as before;
  //   - valid session     → self-heal upsert of `telegram_links` (§1.6), which
  //                          re-creates the link silently so notifications resume.
  //
  // Endpoint is CSRF-exempt (ADR-0022 §1.2) — no `X-CSRF-Token` header needed;
  // protection relies on HMAC of `init_data` + 5-minute `auth_date` TTL.
  // We never log `initData` itself, only the response status.
  //
  // `__masTgSsoTried` guards against duplicate calls (HMR re-execution, repeated
  // DOMContentLoaded handlers, etc.) — set BEFORE the fetch so an in-flight call
  // is never re-fired and we can never enter an infinite reload/POST loop.
  var initData = typeof tg.initData === "string" ? tg.initData : "";

  if (initData && !window.__masTgSsoTried) {
    window.__masTgSsoTried = true;

    fetch("/api/telegram/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: initData }),
      credentials: "same-origin",
    })
      .then(function (response) {
        var status = response.status;
        return response
          .json()
          .then(function (body) {
            return { status: status, body: body };
          })
          .catch(function () {
            // Non-200 with empty/non-JSON body, or malformed JSON from a 200.
            return { status: status, body: null };
          });
      })
      .then(function (result) {
        if (result.status !== 200 || !result.body) {
          // 401 (invalid_init_data / init_data_expired), 429 (rate_limited),
          // 5xx, or malformed body — degrade silently, stay on the current page.
          return;
        }
        var body = result.body;
        if (body.linked === true && body.redirect) {
          // Anonymous visitor just authenticated via SSO: backend set the
          // `mas_session` + `mas_csrf` cookies. Reload into the authenticated app.
          window.location.replace(body.redirect);
          return;
        }
        // body.linked === false:
        //   - anonymous without a link: backend set the short-lived
        //     `mas_tg_pending` cookie; we stay on /login so the normal login flow
        //     can pick it up and create the telegram_links row on success.
        //   - logged-in self-heal (§1.6): backend returned {linked:false,
        //     healed:true} WITHOUT a redirect and WITHOUT mas_tg_pending. We do
        //     NOT reload — the link was restored silently and the page is
        //     unchanged.
      })
      .catch(function () {
        // Network error (offline, CSP block, DNS, etc.) — page still works
        // without SSO/self-heal; user can log in manually. Stay silent.
      });
  }
})();
