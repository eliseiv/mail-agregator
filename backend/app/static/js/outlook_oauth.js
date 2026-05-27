/* =============================================================================
   outlook_oauth.js
   Подключение личных Outlook-ящиков через OAuth2 + OctoBrowser
   (ADR-0025 / 04-api-contracts.md §4c / 05-modules.md §9.1).

   Особенность flow (ADR-0025 §2.5, alternative 4): consent проходит в нужном
   профиле OctoBrowser, поэтому мы НЕ делаем авто-redirect на Microsoft. Вместо
   этого backend отдаёт authorize_url СТРОКОЙ, а мы показываем пользователю:
     - поле со ссылкой (readonly) + кнопку «Скопировать»,
     - кнопку «Открыть» (target=_blank) — для случая обычного браузера,
     - инструкцию открыть ссылку в нужном профиле OctoBrowser.

   Эндпоинты:
     GET /api/oauth/outlook/authorize  (session cookie)
       -> 200 {authorize_url, state}
       -> 404 not_found            (OUTLOOK_OAUTH_ENABLED=false — фича выключена)
       -> 401 not_authenticated    (сессия истекла)
       -> 429 rate_limited         (10/h на пользователя)
     Callback /api/oauth/outlook/callback обрабатывается backend и редиректит
     на /accounts?outlook=connected при успехе (см. router.py). Ошибки callback
     рендерятся backend как страница 4xx (не query-параметр).

   Хуки на /accounts/new (секция «Подключить Outlook»):
     - [data-outlook-section]        контейнер секции (по умолчанию виден).
     - [data-outlook-connect]        кнопка «Подключить Outlook (OAuth)».
     - [data-outlook-result]         блок результата (hidden до получения URL).
     - [data-outlook-url]            <input readonly> со ссылкой authorize_url.
     - [data-outlook-copy]           кнопка «Скопировать».
     - [data-outlook-open]           <a target=_blank> «Открыть».
     - [data-outlook-error]          блок ошибки (hidden по умолчанию).
     - [data-outlook-unavailable]    блок «недоступно» (hidden по умолчанию).

   Хуки на /accounts (список):
     - [data-outlook-reconnect]      кнопка «Переподключить» (повторный authorize).

   CSP-safe: без inline-обработчиков, всё навешивается по data-* + addEventListener.
   Все запросы — через window.MAS.csrfFetch (GET authorize не меняет состояние,
   но идём через единую обёртку ради credentials + Accept).
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  function flash(text, category) {
    if (typeof window.MAS.flash === 'function') {
      window.MAS.flash(text, category || 'info');
    }
  }

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }

  /* ---- 1. Success flash после callback-редиректа ------------------------ */
  // Backend редиректит на /accounts?outlook=connected (router.py). Показываем
  // подтверждение и убираем query-параметр из URL, чтобы при reload не дублировать.
  (function handleCallbackResult() {
    var params;
    try {
      params = new URLSearchParams(window.location.search);
    } catch (_e) {
      return;
    }
    var outlook = params.get('outlook');
    if (!outlook) return;
    if (outlook === 'connected') {
      flash('Outlook подключён. Аккаунт появился в списке ниже.', 'success');
    }
    // Чистим query-параметр (history.replaceState — CSP-нейтрально).
    if (window.history && typeof window.history.replaceState === 'function') {
      params.delete('outlook');
      var qs = params.toString();
      var clean = window.location.pathname + (qs ? '?' + qs : '');
      window.history.replaceState(null, '', clean);
    }
  })();

  /* ---- 2. Запрос authorize_url ------------------------------------------ */
  // Возвращает Promise. Управляет состояниями loading/404/401/429/network.
  function requestAuthorizeUrl(button, onSuccess, panes) {
    var originalLabel = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Получаем ссылку…';
    if (panes && panes.error) { hide(panes.error); panes.error.textContent = ''; }

    return window.MAS.csrfFetch('/api/oauth/outlook/authorize', { method: 'GET' })
      .then(function (resp) {
        if (resp.ok) {
          return resp.json().then(function (data) {
            if (!data || !data.authorize_url) {
              throw new Error('bad_body');
            }
            onSuccess(data.authorize_url, data.state || '');
          }).catch(function () {
            throw new Error('bad_body');
          });
        }
        // Неуспешные коды — разворачиваем в понятную RU-ошибку.
        if (resp.status === 404) {
          // Фича выключена на сервере (OUTLOOK_OAUTH_ENABLED=false).
          throw { kind: 'unavailable' };
        }
        if (resp.status === 401) {
          throw { kind: 'unauthorized' };
        }
        return window.MAS.readJsonError(resp).then(function (err) {
          if (resp.status === 429) {
            throw { kind: 'message', message: err.message || 'Слишком много попыток. Попробуйте позже.' };
          }
          throw { kind: 'message', message: err.message || 'Не удалось получить ссылку для подключения.' };
        });
      })
      .catch(function (e) {
        if (e && e.kind === 'unavailable') {
          handleUnavailable(panes);
        } else if (e && e.kind === 'unauthorized') {
          // Сессия истекла — flash перед навигацией не виден (DOM очищается),
          // поэтому просто редиректим на /login.
          window.location.href = '/login';
        } else if (e && e.kind === 'message') {
          showError(panes, e.message);
        } else {
          // network / bad_body / прочее
          showError(panes, 'Сетевая ошибка. Попробуйте ещё раз.');
        }
      })
      .then(function () {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        button.textContent = originalLabel;
      });
  }

  function showError(panes, message) {
    if (panes && panes.error) {
      panes.error.textContent = message;
      show(panes.error);
    } else {
      flash(message, 'error');
    }
  }

  function handleUnavailable(panes) {
    // Фича выключена — скрываем кнопку подключения, показываем пояснение.
    if (panes) {
      if (panes.connect) hide(panes.connect);
      if (panes.result) hide(panes.result);
      if (panes.error) hide(panes.error);
      if (panes.unavailable) show(panes.unavailable);
    } else {
      flash('Подключение Outlook временно недоступно.', 'warning');
    }
  }

  /* ---- 3. Секция «Подключить Outlook» на /accounts/new ------------------ */
  var section = document.querySelector('[data-outlook-section]');
  if (section) {
    var connectBtn = section.querySelector('[data-outlook-connect]');
    var resultPane = section.querySelector('[data-outlook-result]');
    var urlInput = section.querySelector('[data-outlook-url]');
    var copyBtn = section.querySelector('[data-outlook-copy]');
    var openLink = section.querySelector('[data-outlook-open]');
    var errorPane = section.querySelector('[data-outlook-error]');
    var unavailablePane = section.querySelector('[data-outlook-unavailable]');

    var panes = {
      connect: connectBtn,
      result: resultPane,
      error: errorPane,
      unavailable: unavailablePane,
    };

    if (connectBtn) {
      connectBtn.addEventListener('click', function () {
        requestAuthorizeUrl(connectBtn, function (url) {
          if (urlInput) urlInput.value = url;
          if (openLink) openLink.setAttribute('href', url);
          if (errorPane) { hide(errorPane); errorPane.textContent = ''; }
          show(resultPane);
          // Перемещаем фокус на поле со ссылкой для удобства/скринридеров.
          if (urlInput) {
            urlInput.focus();
            try { urlInput.select(); } catch (_e) { /* no-op */ }
          }
        }, panes);
      });
    }

    if (copyBtn && urlInput) {
      copyBtn.addEventListener('click', function () {
        copyToClipboard(urlInput, copyBtn);
      });
    }
  }

  /* ---- 4. Кнопки «Переподключить» в списке аккаунтов -------------------- */
  // При oauth_needs_consent=true пользователь переподключает аккаунт — это
  // тот же authorize-flow (новый consent перезапишет токены существующего
  // аккаунта, ADR-0025 §2 шаг 5).
  var reconnectButtons = document.querySelectorAll('[data-outlook-reconnect]');
  Array.prototype.forEach.call(reconnectButtons, function (btn) {
    btn.addEventListener('click', function () {
      requestAuthorizeUrl(btn, function (url, state) {
        showReconnectDialog(url, state);
      }, null);
    });
  });

  // Для reconnect (в списке нет готового блока result) — показываем
  // одноразовую панель прямо под строкой через flash + prompt-блок.
  function showReconnectDialog(url) {
    // Строим контейнер с тем же UX: ссылка + копировать + открыть.
    var holder = document.querySelector('[data-outlook-reconnect-panel]');
    if (!holder) {
      flash('Откройте ссылку в нужном профиле OctoBrowser, чтобы переподключить Outlook.', 'info');
      // Фолбэк без panel — копируем сразу в буфер.
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(url).then(function () {
          flash('Ссылка скопирована в буфер обмена.', 'success');
        }, function () { /* no-op */ });
      }
      return;
    }
    var input = holder.querySelector('[data-outlook-reconnect-url]');
    var openA = holder.querySelector('[data-outlook-reconnect-open]');
    var copyB = holder.querySelector('[data-outlook-reconnect-copy]');
    if (input) input.value = url;
    if (openA) openA.setAttribute('href', url);
    holder.hidden = false;
    if (copyB && input && !copyB.dataset.bound) {
      copyB.dataset.bound = '1';
      copyB.addEventListener('click', function () { copyToClipboard(input, copyB); });
    }
    if (input) {
      input.focus();
      try { input.select(); } catch (_e) { /* no-op */ }
    }
  }

  /* ---- 5. Копирование в буфер (тот же паттерн, что integrations.js) ----- */
  function copyToClipboard(input, button) {
    var value = input.value || '';
    if (!value) return;
    var done = function (ok) {
      if (ok) {
        flash('Ссылка скопирована в буфер обмена.', 'success');
      } else {
        flash('Не удалось скопировать автоматически. Скопируйте вручную.', 'warning');
      }
    };
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      navigator.clipboard.writeText(value).then(function () { done(true); }, function () {
        legacyCopy(input, done);
      });
    } else {
      legacyCopy(input, done);
    }
    if (button) {
      var original = button.textContent;
      button.textContent = 'Скопировано';
      setTimeout(function () { button.textContent = original; }, 1500);
    }
  }

  function legacyCopy(input, done) {
    try {
      var wasReadonly = input.hasAttribute('readonly');
      input.removeAttribute('readonly');
      input.select();
      // eslint-disable-next-line deprecation/deprecation
      var ok = document.execCommand('copy');
      if (wasReadonly) input.setAttribute('readonly', 'readonly');
      done(!!ok);
    } catch (_err) {
      done(false);
    }
  }
})();
