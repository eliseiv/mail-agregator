/* =============================================================================
   forwarding.js
   /my/integrations — секция «Переадресация писем команды» (ADR-0034,
   04-api-contracts.md §4e, 08-frontend.md §4.13).

   CSP-safe: никаких inline-обработчиков; всё навешивается по data-*-атрибутам.
   Все state-changing запросы идут через window.MAS.csrfFetch (csrf.js, ADR-0010),
   который подставляет заголовок X-CSRF-Token из cookie mas_csrf.

   Текущий конфиг переадресации НЕ приходит в контексте страницы (её рендерит
   webhooks-router). Поэтому этот скрипт при загрузке страницы делает
   GET /api/forwarding/me и заполняет форму:
     - 200 → предзаполнить e-mail + чекбокс, показать статус и кнопку «Удалить».
     - 404 → пустая форма, чекбокс по умолчанию включён, «Удалить» скрыт.

   Прогрессивное улучшение: форма «Сохранить» — рабочий
   <form method="POST" action="/api/forwarding/me"> с _method=PUT. Если JS не
   загрузился, no-JS-путь всё равно работает (upsert через form-override,
   удаление — через <noscript>-форму на /api/forwarding/me/delete). ADR-0015.

   Эндпоинты:
     GET    /api/forwarding/me → {id, group_id, forward_to, is_active, created_at, updated_at} | 404
     PUT    /api/forwarding/me → upsert (200/201)
     DELETE /api/forwarding/me → 204
   super_admin обязан передавать ?group_id=<id>; group_leader — нет.
   ========================================================================== */
(function () {
  'use strict';

  var section = document.querySelector('[data-forwarding-section]');
  if (!section) return;

  var form = section.querySelector('[data-forwarding-form]');
  // Если формы нет — это режим «super_admin без выбранной команды»
  // (шаблон показал подсказку выбрать команду). Ничего не делаем.
  if (!form) return;

  var MAS = window.MAS;
  if (!MAS || typeof MAS.csrfFetch !== 'function') return;

  var emailInput = section.querySelector('[data-forwarding-email]');
  var activeCheckbox = section.querySelector('[data-forwarding-active-checkbox]');
  var saveBtn = section.querySelector('[data-forwarding-save]');
  var deleteForm = section.querySelector('[data-forwarding-delete-form]');
  var statusEl = section.querySelector('[data-forwarding-status]');
  var loadingEl = section.querySelector('[data-forwarding-loading]');
  var errorEl = section.querySelector('[data-forwarding-error]');
  var fieldErrorEl = section.querySelector('[data-forwarding-field-error]');

  /* ---- Утилиты ---------------------------------------------------------- */

  function flash(text, category) {
    if (typeof MAS.flash === 'function') {
      MAS.flash(text, category || 'info');
    }
  }

  function readError(response) {
    if (typeof MAS.readJsonError === 'function') {
      return MAS.readJsonError(response);
    }
    return Promise.resolve({ code: 'http_' + response.status, message: 'Запрос не выполнен.', field: null });
  }

  function buildUrl(base) {
    // Пробрасываем ?group_id= из текущего URL (поток super_admin). backend
    // требует его для super_admin и запрещает для group_leader — мы просто
    // передаём то, с чем страница была открыта.
    var params = new URLSearchParams(window.location.search);
    var gid = params.get('group_id');
    if (!gid) return base;
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    return base + sep + 'group_id=' + encodeURIComponent(gid);
  }

  function disableButton(btn, busyLabel) {
    if (!btn) return;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    if (busyLabel) {
      if (!btn.hasAttribute('data-original-label')) {
        btn.setAttribute('data-original-label', btn.textContent || '');
      }
      btn.textContent = busyLabel;
    }
  }

  function restoreButton(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    var original = btn.getAttribute('data-original-label');
    if (original !== null) {
      btn.textContent = original;
      btn.removeAttribute('data-original-label');
    }
  }

  function setError(msg) {
    if (!errorEl) return;
    if (msg) {
      errorEl.textContent = msg;
      errorEl.hidden = false;
    } else {
      errorEl.textContent = '';
      errorEl.hidden = true;
    }
  }

  function setFieldError(msg) {
    if (fieldErrorEl) {
      if (msg) {
        fieldErrorEl.textContent = msg;
        fieldErrorEl.hidden = false;
      } else {
        fieldErrorEl.textContent = '';
        fieldErrorEl.hidden = true;
      }
    }
    if (emailInput) {
      if (msg) {
        emailInput.setAttribute('aria-invalid', 'true');
      } else {
        emailInput.removeAttribute('aria-invalid');
      }
    }
  }

  // Клиентская проверка e-mail — зеркалит backend
  // (accounts/schemas.py: ровно один @, домен с точкой, без .., длина 3..254).
  function isValidEmail(raw) {
    var email = (raw || '').trim();
    if (email.length < 3 || email.length > 254) return false;
    var at = email.indexOf('@');
    if (at < 0) return false;
    if (email.indexOf('@', at + 1) >= 0) return false; // более одного @
    var local = email.slice(0, at);
    var domain = email.slice(at + 1);
    if (!local) return false;
    if (domain.indexOf('.') < 0) return false;
    if (domain.charAt(0) === '.' || domain.charAt(domain.length - 1) === '.') return false;
    if (domain.indexOf('..') >= 0) return false;
    return true;
  }

  function formatDate(iso) {
    if (!iso) return '';
    // "2026-07-03T10:00:00Z" → "2026-07-03 10:00"
    return String(iso).replace('T', ' ').slice(0, 16);
  }

  function renderStatus(dto) {
    if (!statusEl) return;
    if (dto) {
      var on = !!dto.is_active;
      var date = formatDate(dto.created_at);
      statusEl.textContent = 'Статус: ' + (on ? 'включена' : 'выключена') +
        (date ? ' • настроена ' + date : '');
      statusEl.className = 'forwarding__status ' + (on ? 'forwarding__status--on' : 'forwarding__status--off');
    } else {
      statusEl.textContent = 'Переадресация не настроена.';
      statusEl.className = 'forwarding__status forwarding__status--off';
    }
    statusEl.hidden = false;
  }

  // Привести UI к состоянию конфига: dto — объект (есть запись) или null (нет).
  function populate(dto) {
    if (dto) {
      if (emailInput) emailInput.value = dto.forward_to || '';
      if (activeCheckbox) activeCheckbox.checked = !!dto.is_active;
      if (deleteForm) deleteForm.hidden = false;
    } else {
      if (activeCheckbox) activeCheckbox.checked = true;
      if (deleteForm) deleteForm.hidden = true;
    }
    renderStatus(dto);
  }

  /* ---- 1. Загрузка текущего конфига ------------------------------------- */

  function loadConfig() {
    if (loadingEl) loadingEl.hidden = false;
    setError('');
    MAS.csrfFetch(buildUrl('/api/forwarding/me'), { method: 'GET' })
      .then(function (response) {
        if (loadingEl) loadingEl.hidden = true;
        if (response.status === 200) {
          return response.json().then(function (dto) { populate(dto); });
        }
        if (response.status === 404) {
          populate(null);
          return undefined;
        }
        return readError(response).then(function (err) {
          populate(null);
          if (err.field === 'group_id') {
            setError('Выберите команду.');
          } else {
            setError(err.message || 'Не удалось загрузить настройки переадресации.');
          }
        });
      })
      .catch(function () {
        if (loadingEl) loadingEl.hidden = true;
        setError('Сетевая ошибка при загрузке настроек. Форма всё ещё доступна.');
      });
  }

  /* ---- 2. Сохранить (upsert PUT) ---------------------------------------- */

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    setError('');
    setFieldError('');
    var email = emailInput ? (emailInput.value || '').trim() : '';
    if (!isValidEmail(email)) {
      setFieldError('Введите корректный e-mail');
      if (emailInput) emailInput.focus();
      return;
    }
    var isActive = activeCheckbox ? !!activeCheckbox.checked : true;
    disableButton(saveBtn, 'Сохраняем…');
    MAS.csrfFetch(buildUrl('/api/forwarding/me'), {
      method: 'PUT',
      body: { forward_to: email, is_active: isActive }
    }).then(function (response) {
      if (response.ok) { // 200 (обновлено) или 201 (создано)
        return response.json().then(function (dto) {
          populate(dto);
          restoreButton(saveBtn);
          flash('Переадресация сохранена.', 'success');
        });
      }
      return readError(response).then(function (err) {
        restoreButton(saveBtn);
        if (err.field === 'forward_to') {
          setFieldError('Введите корректный e-mail');
        } else if (err.field === 'group_id') {
          setError('Выберите команду.');
        } else {
          setError(err.message || 'Не удалось сохранить переадресацию.');
        }
      });
    }).catch(function () {
      restoreButton(saveBtn);
      setError('Сетевая ошибка. Попробуйте ещё раз.');
    });
  });

  // Сбрасываем inline-ошибку поля, как только пользователь правит адрес.
  if (emailInput) {
    emailInput.addEventListener('input', function () { setFieldError(''); });
  }

  /* ---- 3. Удалить (DELETE) ---------------------------------------------- */

  if (deleteForm) {
    deleteForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var msg = deleteForm.getAttribute('data-confirm');
      // eslint-disable-next-line no-alert
      if (msg && !window.confirm(msg)) return;
      var delBtn = deleteForm.querySelector('button[type="submit"]');
      setError('');
      disableButton(delBtn, 'Удаляем…');
      MAS.csrfFetch(buildUrl('/api/forwarding/me'), { method: 'DELETE' })
        .then(function (response) {
          if (response.status === 204 || response.ok) {
            restoreButton(delBtn);
            if (emailInput) emailInput.value = '';
            populate(null);
            flash('Переадресация удалена.', 'success');
            return undefined;
          }
          return readError(response).then(function (err) {
            restoreButton(delBtn);
            setError(err.message || 'Не удалось удалить переадресацию.');
          });
        })
        .catch(function () {
          restoreButton(delBtn);
          setError('Сетевая ошибка. Попробуйте ещё раз.');
        });
    });
  }

  /* ---- 4. Старт --------------------------------------------------------- */

  loadConfig();
})();
