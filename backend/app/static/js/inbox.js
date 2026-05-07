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
          window.MAS.flash('Marked as unread.', 'success');
          // Send the user back to inbox so they immediately see the indicator update.
          window.setTimeout(function () { window.location.href = '/'; }, 400);
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Could not mark as unread.', 'error');
        markUnreadBtn.disabled = false;
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
        markUnreadBtn.disabled = false;
      }
    });
  }
})();
