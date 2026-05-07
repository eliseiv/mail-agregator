/* =============================================================================
   tags.js
   Tag form + tags list UX enhancements (ADR-0017, 08-frontend.md §3 / §4.11).

   CSP-safe: no inline handlers, all wiring via data-* attributes. Loaded only
   on /tags, /tags/new, /tags/{id}/edit (see tags/list.html and tags/form.html
   `extra_js` blocks).

   Hooks:
     - [data-add-rule]              : reveal + clone empty rule-row template.
     - [data-rules-container]       : container holding rule-rows.
     - [data-remove-rule]           : remove the closest .rule-row (delegated).
     - [data-tag-delete-form]       : wrap submit with native confirm() —
                                      data-confirm attribute provides the text.
     - [data-rule-delete-form]      : same.
     - [data-tag-apply-form]        : same (apply-to-existing on edit page).
     - [data-rule-add-form]         : extra client-side check that both type
                                      and pattern are non-empty before submit.
   ========================================================================== */
(function () {
  'use strict';

  /* ---- 1. Confirm dialogs for destructive / heavy actions --------------- */

  function wireConfirm(form) {
    if (!form || form.dataset.confirmWired === '1') return;
    form.dataset.confirmWired = '1';
    form.addEventListener('submit', function (e) {
      var msg = form.getAttribute('data-confirm');
      if (!msg) return;
      // window.confirm is synchronous and blocks until user answers — exactly
      // what we want for irreversible actions.
      // eslint-disable-next-line no-alert
      if (!window.confirm(msg)) {
        e.preventDefault();
      }
    });
  }

  var confirmSelectors = [
    '[data-tag-delete-form]',
    '[data-rule-delete-form]',
    '[data-tag-apply-form]'
  ];
  confirmSelectors.forEach(function (sel) {
    var nodes = document.querySelectorAll(sel);
    for (var i = 0; i < nodes.length; i++) wireConfirm(nodes[i]);
  });

  /* ---- 2. Add-rule / remove-rule on the tag form ------------------------ */

  var rulesContainer = document.querySelector('[data-rules-container]');
  var addBtn = document.querySelector('[data-add-rule]');

  if (rulesContainer) {
    // Reveal the JS-only add button (it's hidden by default so no-JS users
    // never see a non-functional control).
    if (addBtn) {
      addBtn.hidden = false;
    }

    // Build a fresh rule row by cloning the first existing one and clearing
    // its inputs. We avoid <template> because the container is already on the
    // page with at least 5 working rows for no-JS users — duplicating the
    // first one keeps any future markup change in one place (the template).
    function buildEmptyRuleRow() {
      var first = rulesContainer.querySelector('.rule-row');
      if (!first) return null;
      var clone = first.cloneNode(true);
      var inputs = clone.querySelectorAll('input, select');
      for (var i = 0; i < inputs.length; i++) {
        var el = inputs[i];
        if (el.tagName === 'SELECT') {
          el.selectedIndex = 0;
        } else if (el.type === 'checkbox' || el.type === 'radio') {
          el.checked = false;
        } else {
          el.value = '';
        }
        // Strip any id to avoid duplicate ids in the DOM (label `for=` only
        // referenced visually-hidden labels — losing the link is acceptable
        // since each input still has a placeholder + visually-hidden sibling).
        if (el.id) el.removeAttribute('id');
      }
      // Drop any visually-hidden <label for=...> ids inside the cloned row to
      // avoid stale references.
      var labels = clone.querySelectorAll('label[for]');
      for (var j = 0; j < labels.length; j++) labels[j].removeAttribute('for');
      return clone;
    }

    if (addBtn) {
      addBtn.addEventListener('click', function () {
        var existing = rulesContainer.querySelectorAll('.rule-row').length;
        // Backend hard-limits at 32 rules per tag (ADR-0017).
        if (existing >= 32) {
          if (window.MAS && window.MAS.flash) {
            window.MAS.flash('Достигнут лимит 32 условий на тег.', 'warning');
          }
          return;
        }
        var row = buildEmptyRuleRow();
        if (row) rulesContainer.appendChild(row);
      });
    }

    // Delegated remove-rule click.
    rulesContainer.addEventListener('click', function (e) {
      var target = e.target;
      if (!target || !target.closest) return;
      var btn = target.closest('[data-remove-rule]');
      if (!btn || !rulesContainer.contains(btn)) return;
      e.preventDefault();
      var row = btn.closest('.rule-row');
      if (!row) return;
      // Always keep at least one row visible so the user has a place to type.
      var rows = rulesContainer.querySelectorAll('.rule-row');
      if (rows.length <= 1) {
        var inputs = row.querySelectorAll('input, select');
        for (var i = 0; i < inputs.length; i++) {
          var el = inputs[i];
          if (el.tagName === 'SELECT') el.selectedIndex = 0;
          else el.value = '';
        }
        return;
      }
      row.parentNode.removeChild(row);
    });
  }

  /* ---- 3. Add-rule form (edit page) — client-side guard ----------------- */

  var addRuleForm = document.querySelector('[data-rule-add-form]');
  if (addRuleForm) {
    addRuleForm.addEventListener('submit', function (e) {
      var typeEl = addRuleForm.querySelector('select[name="type"]');
      var patternEl = addRuleForm.querySelector('input[name="pattern"]');
      var typeVal = typeEl ? (typeEl.value || '').trim() : '';
      var patternVal = patternEl ? (patternEl.value || '').trim() : '';
      if (!typeVal || !patternVal) {
        e.preventDefault();
        if (window.MAS && window.MAS.flash) {
          window.MAS.flash('Выберите тип условия и заполните шаблон.', 'error');
        }
      }
    });
  }
})();
