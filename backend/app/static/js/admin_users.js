/* =============================================================================
   admin_users.js
   Admin users-page UX:
     - "Create user" dialog (POST /api/admin/users) with display_name,
       role (group_leader|group_member) and group_id select that toggles
       visibility based on the chosen role (ADR-0019).
     - "Create group" dialog (POST /api/admin/groups) — populates leader +
       members selects from GET /api/admin/users/eligible (FE-FIX 7).
     - "Reset password" with confirm.
     - "Delete user" with type-the-username confirmation.

   Hooks (CSP-safe, addEventListener only):
     - <button data-admin-create-user>            : opens create-user dialog.
     - <dialog data-admin-create-dialog>          : the create-user dialog.
     - <form   data-admin-create-user-form>       : form inside that dialog.
     - <input  data-admin-role-input>             : role radio buttons.
     - <div    data-admin-group-field>            : group <select> wrapper.
     - <select data-admin-group-select>           : group_id select.
     - <button data-admin-create-group>           : opens create-group dialog.
     - <dialog data-admin-create-group-dialog>    : the create-group dialog.
     - <form   data-admin-create-group-form>      : form inside that dialog.
     - <select data-admin-group-leader-select>    : leader user_id select.
     - <select data-admin-group-members-select>   : multi-select members.
     - <p      data-admin-create-group-error>     : inline error banner.
     - <form data-admin-reset-form data-confirm>  : reset-password form.
     - <form data-admin-delete-form data-username>: delete form.
     - <dialog data-admin-delete-dialog>          : confirm-deletion dialog.
     - <strong data-admin-delete-username>        : username text inside dialog.
     - <form data-admin-delete-confirm-form>      : confirmation form (text input).
     - <input id="delete-confirm-input">          : the type-username input.
     - <button data-admin-delete-go>              : final delete button.
     - <button data-admin-delete-cancel>          : cancel button.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  // ---- Create user ---------------------------------------------------------

  const createBtn    = document.querySelector('[data-admin-create-user]');
  const createDialog = document.querySelector('[data-admin-create-dialog]');
  const createForm   = document.querySelector('[data-admin-create-user-form]');

  // Toggle the group select visibility according to the selected role.
  // - role=group_leader  → group select hidden + cleared (auto-create on backend).
  // - role=group_member  → group select visible + required.
  const roleInputs   = document.querySelectorAll('[data-admin-role-input]');
  const groupField   = document.querySelector('[data-admin-group-field]');
  const groupSelect  = document.querySelector('[data-admin-group-select]');

  function applyRoleVisibility() {
    let role = 'group_member';
    roleInputs.forEach(function (r) { if (r.checked) role = r.value; });
    if (!groupField || !groupSelect) return;
    if (role === 'group_leader') {
      groupField.hidden = true;
      groupSelect.required = false;
      groupSelect.value = '';
    } else {
      groupField.hidden = false;
      groupSelect.required = true;
    }
  }

  roleInputs.forEach(function (r) {
    r.addEventListener('change', applyRoleVisibility);
  });
  // Initial state on load.
  applyRoleVisibility();

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      // Reset role to default + apply visibility before opening.
      roleInputs.forEach(function (r) {
        r.checked = (r.getAttribute('data-role-value') === 'group_member');
      });
      applyRoleVisibility();
      if (typeof createDialog.showModal === 'function') {
        createDialog.showModal();
      } else {
        createDialog.setAttribute('open', 'open');
      }
    });
  }

  if (createForm) {
    createForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      const fd = new FormData(createForm);
      const payload = {
        username: (fd.get('username') || '').toString().trim(),
      };
      // FE-FIX 6: email removed. FE-FIX round-2 #4: display_name removed too.
      // Only username + role (+ optional group_id) are sent.
      const role = (fd.get('role') || '').toString().trim();
      if (role) payload.role = role;
      const groupIdRaw = (fd.get('group_id') || '').toString().trim();
      // group_id is optional even for group_member (FE-FIX round-2 #4).
      if (role === 'group_member' && groupIdRaw) {
        const gid = parseInt(groupIdRaw, 10);
        if (Number.isFinite(gid) && gid > 0) {
          payload.group_id = gid;
        }
      }
      // For group_leader we deliberately omit group_id — backend auto-creates.

      const submitBtn = createForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/users', {
          method: 'POST',
          body: payload,
        });
        if (resp.ok) {
          window.MAS.flash(
            'Пользователь создан. Сообщите ему логин — при первом входе нужно будет задать пароль.',
            'success'
          );
          if (createDialog && typeof createDialog.close === 'function') createDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось создать пользователя.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // ---- Create group (FE-FIX 7) --------------------------------------------

  const createGroupBtn      = document.querySelector('[data-admin-create-group]');
  const createGroupDialog   = document.querySelector('[data-admin-create-group-dialog]');
  const createGroupForm     = document.querySelector('[data-admin-create-group-form]');
  const groupLeaderSelect   = document.querySelector('[data-admin-group-leader-select]');
  const groupMembersSelect  = document.querySelector('[data-admin-group-members-select]');
  const groupErrorBanner    = document.querySelector('[data-admin-create-group-error]');
  const groupSubmitBtn      = document.querySelector('[data-admin-create-group-submit]');

  let eligibleUsersCache = null;

  function showGroupError(text) {
    if (!groupErrorBanner) return;
    groupErrorBanner.textContent = text || '';
    groupErrorBanner.hidden = !text;
  }

  function clearSelectOptions(sel) {
    if (!sel) return;
    while (sel.firstChild) sel.removeChild(sel.firstChild);
  }

  function userOptionLabel(u) {
    const dn = (u.display_name || '').toString().trim();
    const base = dn ? dn + ' (@' + u.username + ')' : '@' + u.username;
    if (u.group && u.group.name) {
      return base + ' — ' + u.group.name;
    }
    return base;
  }

  async function loadEligibleUsers() {
    if (eligibleUsersCache) return eligibleUsersCache;
    const resp = await window.MAS.csrfFetch('/api/admin/users/eligible', {
      method: 'GET',
    });
    if (!resp.ok) {
      const err = await window.MAS.readJsonError(resp);
      throw new Error(err.message || 'Не удалось загрузить список пользователей.');
    }
    const data = await resp.json();
    eligibleUsersCache = (data && Array.isArray(data.items)) ? data.items : [];
    return eligibleUsersCache;
  }

  function populateLeaderSelect(users) {
    if (!groupLeaderSelect) return;
    clearSelectOptions(groupLeaderSelect);
    const placeholder = document.createElement('option');
    placeholder.value = '';
    if (users.length === 0) {
      placeholder.textContent = '— нет доступных пользователей —';
      placeholder.disabled = true;
    } else {
      placeholder.textContent = '— выберите лидера —';
      placeholder.disabled = true;
    }
    placeholder.selected = true;
    groupLeaderSelect.appendChild(placeholder);
    for (let i = 0; i < users.length; i++) {
      const u = users[i];
      const opt = document.createElement('option');
      opt.value = String(u.id);
      opt.textContent = userOptionLabel(u);
      groupLeaderSelect.appendChild(opt);
    }
  }

  function populateMembersSelect(users) {
    if (!groupMembersSelect) return;
    clearSelectOptions(groupMembersSelect);
    if (users.length === 0) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '— нет доступных пользователей —';
      opt.disabled = true;
      groupMembersSelect.appendChild(opt);
      return;
    }
    for (let i = 0; i < users.length; i++) {
      const u = users[i];
      const opt = document.createElement('option');
      opt.value = String(u.id);
      opt.textContent = userOptionLabel(u);
      groupMembersSelect.appendChild(opt);
    }
  }

  function refreshMembersAvailability() {
    if (!groupLeaderSelect || !groupMembersSelect) return;
    const leaderId = groupLeaderSelect.value;
    const opts = groupMembersSelect.options;
    for (let i = 0; i < opts.length; i++) {
      const opt = opts[i];
      if (!opt.value) continue;
      const isLeader = opt.value === leaderId;
      opt.disabled = isLeader;
      if (isLeader) opt.selected = false;
    }
  }

  if (groupLeaderSelect) {
    groupLeaderSelect.addEventListener('change', refreshMembersAvailability);
  }

  if (createGroupBtn && createGroupDialog) {
    createGroupBtn.addEventListener('click', async function () {
      showGroupError('');
      // Reset form fields.
      if (createGroupForm) {
        const nameInput = createGroupForm.querySelector('[name="name"]');
        if (nameInput) nameInput.value = '';
      }
      if (groupLeaderSelect) {
        clearSelectOptions(groupLeaderSelect);
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '— загрузка списка пользователей… —';
        opt.disabled = true;
        opt.selected = true;
        groupLeaderSelect.appendChild(opt);
      }
      if (groupMembersSelect) {
        clearSelectOptions(groupMembersSelect);
      }
      if (typeof createGroupDialog.showModal === 'function') {
        createGroupDialog.showModal();
      } else {
        createGroupDialog.setAttribute('open', 'open');
      }
      try {
        const users = await loadEligibleUsers();
        populateLeaderSelect(users);
        populateMembersSelect(users);
        refreshMembersAvailability();
      } catch (e) {
        showGroupError(e && e.message ? e.message : 'Не удалось загрузить список пользователей.');
      }
    });
  }

  if (createGroupForm) {
    createGroupForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      showGroupError('');
      const fd = new FormData(createGroupForm);
      const name = (fd.get('name') || '').toString().trim();
      const leaderRaw = (fd.get('leader_user_id') || '').toString().trim();
      if (!name) {
        showGroupError('Укажите название группы.');
        return;
      }
      // FE-FIX round-2 #3: leader is optional. If empty, the first
      // member (if any) becomes the leader; otherwise the group is
      // created leaderless.
      let leaderId = null;
      if (leaderRaw) {
        const parsed = parseInt(leaderRaw, 10);
        if (Number.isFinite(parsed) && parsed > 0) leaderId = parsed;
      }
      // FormData.getAll for the multi-select.
      const memberRaws = fd.getAll('member_ids');
      const memberIds = [];
      for (let i = 0; i < memberRaws.length; i++) {
        const v = (memberRaws[i] || '').toString().trim();
        if (!v) continue;
        const n = parseInt(v, 10);
        if (Number.isFinite(n) && n > 0 && n !== leaderId && memberIds.indexOf(n) === -1) {
          memberIds.push(n);
        }
      }
      const payload = {
        name: name,
        leader_user_id: leaderId,
        member_ids: memberIds,
      };
      if (groupSubmitBtn) groupSubmitBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/groups', {
          method: 'POST',
          body: payload,
        });
        if (resp.ok) {
          window.MAS.flash('Группа создана.', 'success');
          if (createGroupDialog && typeof createGroupDialog.close === 'function') {
            createGroupDialog.close();
          }
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        showGroupError(err.message || 'Не удалось создать группу.');
      } catch (_e) {
        showGroupError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (groupSubmitBtn) groupSubmitBtn.disabled = false;
      }
    });
  }

  // ---- Reset password ------------------------------------------------------

  document.querySelectorAll('[data-admin-reset-form]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const msg = f.getAttribute('data-confirm') ||
                  'Сбросить пароль? Пользователь должен будет задать новый при следующем входе.';
      if (!window.confirm(msg)) return;
      const action = f.getAttribute('action');
      if (!action) return;
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(action, { method: 'POST' });
        if (resp.ok) {
          window.MAS.flash('Пароль сброшен. Все сессии завершены.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось сбросить пароль.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  // ---- Delete user (type-username confirmation) ----------------------------

  const deleteDialog        = document.querySelector('[data-admin-delete-dialog]');
  const deleteUsernameLabel = document.querySelector('[data-admin-delete-username]');
  const deleteConfirmForm   = document.querySelector('[data-admin-delete-confirm-form]');
  const deleteConfirmInput  = document.getElementById('delete-confirm-input');
  const deleteGoBtn         = document.querySelector('[data-admin-delete-go]');
  const deleteCancelBtn     = document.querySelector('[data-admin-delete-cancel]');

  let pendingDeleteAction = '';
  let pendingDeleteUsername = '';

  document.querySelectorAll('[data-admin-delete-form]').forEach(function (f) {
    f.addEventListener('submit', function (event) {
      event.preventDefault();
      pendingDeleteAction = f.getAttribute('action') || '';
      pendingDeleteUsername = f.getAttribute('data-username') || '';
      if (!pendingDeleteAction || !pendingDeleteUsername) return;
      if (deleteUsernameLabel) deleteUsernameLabel.textContent = pendingDeleteUsername;
      if (deleteConfirmInput) {
        deleteConfirmInput.value = '';
        deleteConfirmInput.focus();
      }
      if (deleteGoBtn) deleteGoBtn.disabled = true;
      if (deleteDialog && typeof deleteDialog.showModal === 'function') {
        deleteDialog.showModal();
      } else if (deleteDialog) {
        deleteDialog.setAttribute('open', 'open');
      }
    });
  });

  if (deleteConfirmInput && deleteGoBtn) {
    deleteConfirmInput.addEventListener('input', function () {
      deleteGoBtn.disabled = (deleteConfirmInput.value !== pendingDeleteUsername);
    });
  }

  if (deleteCancelBtn && deleteDialog) {
    deleteCancelBtn.addEventListener('click', function () {
      if (typeof deleteDialog.close === 'function') deleteDialog.close();
      else deleteDialog.removeAttribute('open');
    });
  }

  if (deleteConfirmForm) {
    deleteConfirmForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!pendingDeleteAction || !pendingDeleteUsername) return;
      if (!deleteConfirmInput || deleteConfirmInput.value !== pendingDeleteUsername) return;

      // Strip trailing /delete if present and use the canonical DELETE verb.
      const url = pendingDeleteAction.replace(/\/delete$/, '');
      if (deleteGoBtn) deleteGoBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(url, { method: 'DELETE' });
        if (resp.ok || resp.status === 204) {
          window.MAS.flash('Пользователь удалён.', 'success');
          if (deleteDialog && typeof deleteDialog.close === 'function') deleteDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось удалить пользователя.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (deleteGoBtn) deleteGoBtn.disabled = false;
      }
    });
  }
})();
