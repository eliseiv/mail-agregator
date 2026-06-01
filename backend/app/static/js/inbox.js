/* =============================================================================
   inbox.js
   Inbox & message-view UX enhancements.

   Hooks (CSP-safe — no inline handlers):
     - <select data-inbox-filter>          : auto-submit form on change.
     - <input data-inbox-unread>           : auto-submit form on toggle.
     - <button data-inbox-refresh>         : reload current URL (preserves filters).
     - <button data-message-mark-unread>   : POST /api/messages/{id}/mark-read
                                              {is_read: false} then redirect home.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return; // csrf.js failed to load — nothing we can do.

  // ---- 1. Inbox filter auto-submit -----------------------------------------

  // Multiple filter selects (account_id, tag_id, ...) — wire each up.
  document.querySelectorAll('[data-inbox-filter]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      const form = sel.closest('form');
      if (form) form.submit();
    });
  });

  const unreadCheckbox = document.querySelector('[data-inbox-unread]');
  if (unreadCheckbox) {
    unreadCheckbox.addEventListener('change', function () {
      const form = unreadCheckbox.closest('form');
      if (form) form.submit();
    });
  }

  // ---- 2. Refresh button ---------------------------------------------------

  const refreshBtn = document.querySelector('[data-inbox-refresh]');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      // Simplest correct behaviour: reload, preserving query string and cursor.
      window.location.reload();
    });
  }

  // ---- 4. Account searchable combobox (typeahead) --------------------------
  //
  // Progressive enhancement of [data-account-combobox]: replaces a plain
  // <select> with an ARIA 1.2 combobox. The user types part of an email OR a
  // display_name (никнейм); we filter the mailbox list (case-insensitive
  // substring on both fields) and render a listbox. Selecting an option writes
  // its id into the hidden [data-account-value] input and submits the GET form
  // (same effect as the old select's change->submit). An "Все почты" option
  // and a clear (×) button reset the filter (account_id="").

  (function setupAccountCombobox() {
    const root = document.querySelector('[data-account-combobox]');
    if (!root) return;

    const input = root.querySelector('[data-account-input]');
    const listbox = root.querySelector('[data-account-listbox]');
    const hidden = root.querySelector('[data-account-value]');
    const clearBtn = root.querySelector('[data-account-clear]');
    const optionsScript = root.querySelector('[data-account-options]');
    if (!input || !listbox || !hidden || !optionsScript) return;

    const emptyLabel = root.getAttribute('data-empty-label') || 'Все почты';

    let accounts = [];
    try {
      const parsed = JSON.parse(optionsScript.textContent || '[]');
      if (Array.isArray(parsed)) accounts = parsed;
    } catch (_e) {
      // Malformed payload — leave the (now visible) noscript-style behaviour:
      // an empty combobox that can still be cleared. Bail out gracefully.
      return;
    }

    // Enable the hidden carrier now that JS is driving the filter; the
    // <noscript> <select> stays inert because scripting is on.
    hidden.removeAttribute('disabled');

    const ID_PREFIX = 'filter-account-opt-';
    let activeIndex = -1; // index into `current` (the rendered subset)
    let current = []; // [{id, label, value}] currently rendered

    function labelFor(acc) {
      // "никнейм — email" when a display_name exists, otherwise just email.
      if (acc.name && acc.name.trim()) {
        return acc.name.trim() + ' — ' + acc.email;
      }
      return acc.email;
    }

    function buildOptions(query) {
      const q = query.trim().toLowerCase();
      const opts = [{ id: '', label: emptyLabel, value: '' }];
      accounts.forEach(function (acc) {
        const email = (acc.email || '').toLowerCase();
        const name = (acc.name || '').toLowerCase();
        if (q === '' || email.indexOf(q) !== -1 || name.indexOf(q) !== -1) {
          opts.push({ id: String(acc.id), label: labelFor(acc), value: String(acc.id) });
        }
      });
      return opts;
    }

    function closeList() {
      listbox.hidden = true;
      listbox.innerHTML = '';
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      activeIndex = -1;
      current = [];
    }

    function setActive(index) {
      const nodes = listbox.querySelectorAll('[role="option"]');
      nodes.forEach(function (n) { n.classList.remove('is-active'); n.setAttribute('aria-selected', 'false'); });
      if (index < 0 || index >= nodes.length) {
        activeIndex = -1;
        input.removeAttribute('aria-activedescendant');
        return;
      }
      activeIndex = index;
      const node = nodes[index];
      node.classList.add('is-active');
      node.setAttribute('aria-selected', 'true');
      input.setAttribute('aria-activedescendant', node.id);
      node.scrollIntoView({ block: 'nearest' });
    }

    function openList(query) {
      current = buildOptions(query);
      listbox.innerHTML = '';
      if (current.length === 0) {
        closeList();
        return;
      }
      current.forEach(function (opt, i) {
        const li = document.createElement('li');
        li.className = 'combobox__option';
        li.id = ID_PREFIX + i;
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', 'false');
        li.setAttribute('data-value', opt.value);
        li.textContent = opt.label;
        li.addEventListener('mousedown', function (ev) {
          // mousedown (not click) so selection happens before input blur.
          ev.preventDefault();
          choose(i);
        });
        listbox.appendChild(li);
      });
      listbox.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      setActive(-1);
    }

    function choose(index) {
      const opt = current[index];
      if (!opt) return;
      hidden.value = opt.value;
      input.value = opt.value === '' ? '' : opt.label;
      if (clearBtn) clearBtn.hidden = opt.value === '';
      closeList();
      submitForm();
    }

    function submitForm() {
      const form = root.closest('form');
      if (form) form.submit();
    }

    function clearSelection() {
      hidden.value = '';
      input.value = '';
      if (clearBtn) clearBtn.hidden = true;
      closeList();
      submitForm();
    }

    input.addEventListener('focus', function () {
      openList(input.value);
    });

    input.addEventListener('input', function () {
      openList(input.value);
    });

    input.addEventListener('keydown', function (ev) {
      switch (ev.key) {
        case 'ArrowDown':
          ev.preventDefault();
          if (listbox.hidden) { openList(input.value); }
          setActive(Math.min(activeIndex + 1, current.length - 1));
          break;
        case 'ArrowUp':
          ev.preventDefault();
          if (listbox.hidden) { openList(input.value); }
          setActive(Math.max(activeIndex - 1, 0));
          break;
        case 'Enter':
          if (!listbox.hidden && activeIndex >= 0) {
            ev.preventDefault();
            choose(activeIndex);
          }
          break;
        case 'Escape':
          if (!listbox.hidden) {
            ev.preventDefault();
            closeList();
          }
          break;
        case 'Tab':
          closeList();
          break;
        default:
          break;
      }
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        clearSelection();
        input.focus();
      });
    }

    // Close on outside click.
    document.addEventListener('click', function (ev) {
      if (!root.contains(ev.target)) closeList();
    });
  })();

  // ---- 3. Mark-as-unread on message view ----------------------------------

  const markUnreadBtn = document.querySelector('[data-message-mark-unread]');
  if (markUnreadBtn) {
    markUnreadBtn.addEventListener('click', async function () {
      const id = markUnreadBtn.getAttribute('data-message-id');
      if (!id) return;
      markUnreadBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(
          '/api/messages/' + encodeURIComponent(id) + '/mark-read',
          {
            method: 'POST',
            body: { is_read: false },
          }
        );
        if (resp.status === 204 || resp.ok) {
          window.MAS.flash('Помечено как непрочитанное.', 'success');
          // Send the user back to inbox so they immediately see the indicator update.
          window.setTimeout(function () { window.location.href = '/'; }, 400);
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось пометить как непрочитанное.', 'error');
        markUnreadBtn.disabled = false;
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        markUnreadBtn.disabled = false;
      }
    });
  }
})();
