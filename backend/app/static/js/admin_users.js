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

   Group membership (ADR-0030 multi-group):
     - <button data-admin-menu-trigger>           : the «+» button — opens the
                                                    shared actions chooser.
     - <dialog data-admin-actions-dialog>         : actions chooser (Move/Add).
     - <button data-admin-actions-move>           : «Переместить» (hidden for leaders).
     - <button data-admin-actions-add>            : «Добавить в команду».
     - <dialog data-admin-move-dialog> + form     : move-to-group (PATCH).
     - <dialog data-admin-add-dialog>  + form     : add-to-group (POST .../groups).
     - <form data-admin-remove-membership>        : «×» chip — DELETE .../groups/{gid}.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  // ---- Create user ---------------------------------------------------------

  const createBtn    = document.querySelector('[data-admin-create-user]');
  const createDialog = document.querySelector('[data-admin-create-dialog]');
  const createForm   = document.querySelector('[data-admin-create-user-form]');
  const createError  = document.querySelector('[data-admin-create-error]');

  // ADR-0038 §5: additional-teams multiselect (checkboxes) — visible only for
  // role=group_member; the home team (group_id) is excluded from the choices.
  const additionalGroupsField  = document.querySelector('[data-admin-additional-groups-field]');
  const additionalGroupInputs  = document.querySelectorAll('[data-admin-additional-group]');

  function showCreateError(text) {
    if (!createError) return;
    createError.textContent = text || '';
    createError.hidden = !text;
  }

  // Client-side mirror of the ADR-0038 §3 password policy (backend re-validates
  // authoritatively): 12..128 chars, at least one letter and one digit. Returns
  // an error string, or null when acceptable.
  function validatePassword(pw) {
    if (pw.length < 12 || pw.length > 128) {
      return 'Пароль должен быть от 12 до 128 символов.';
    }
    if (!/[A-Za-z]/.test(pw)) {
      return 'Пароль должен содержать хотя бы одну букву.';
    }
    if (!/[0-9]/.test(pw)) {
      return 'Пароль должен содержать хотя бы одну цифру.';
    }
    return null;
  }

  // Keep the "additional teams" checkboxes consistent with the chosen home
  // team: the home team can't also be an additional one (backend dedupes via
  // ON CONFLICT, but the UI hides it for clarity).
  function syncAdditionalGroupWithHome() {
    if (!groupSelect) return;
    const home = (groupSelect.value || '').toString();
    additionalGroupInputs.forEach(function (cb) {
      const gid = cb.getAttribute('data-group-id');
      const isHome = !!home && gid === home;
      cb.disabled = isHome;
      if (isHome) cb.checked = false;
      const label = cb.closest('.checkbox');
      if (label) label.classList.toggle('is-disabled', isHome);
    });
  }

  // Toggle the group select visibility according to the selected role.
  // - role=group_leader  → group select VISIBLE but optional (bug-fix #2):
  //     - empty   → backend auto-creates a new group named «Команда {логин}»
  //     - chosen  → backend assigns the new leader to that orphan group
  //       (400 if the group already has a leader)
  // - role=group_member  → group select visible AND required.
  const roleInputs   = document.querySelectorAll('[data-admin-role-input]');
  const groupField   = document.querySelector('[data-admin-group-field]');
  const groupSelect  = document.querySelector('[data-admin-group-select]');
  const groupHintLeader = document.querySelector('[data-admin-group-hint-leader]');
  const groupHintMember = document.querySelector('[data-admin-group-hint-member]');

  // Parse the orphan-group ids whitelist once (data-orphan-group-ids is a
  // JSON-encoded list of integers — server-side ``tojson`` filter). When
  // role=group_leader is active we hide every <option> whose value isn't
  // in this set; switching back to group_member restores them.
  function parseOrphanGroupIds() {
    if (!groupField) return [];
    const raw = groupField.getAttribute('data-orphan-group-ids');
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.map(function (n) { return parseInt(n, 10); })
                .filter(function (n) { return Number.isFinite(n) && n > 0; });
    } catch (_e) {
      return [];
    }
  }
  const orphanGroupIds = parseOrphanGroupIds();
  const orphanSet = {};
  for (let i = 0; i < orphanGroupIds.length; i++) {
    orphanSet[String(orphanGroupIds[i])] = true;
  }

  function applyRoleVisibility() {
    let role = 'group_member';
    roleInputs.forEach(function (r) { if (r.checked) role = r.value; });
    if (!groupField || !groupSelect) return;
    // Bug-fix #2: group field stays visible for both roles. Required only
    // for group_member (leader can leave it blank → auto-create).
    groupField.hidden = false;
    if (role === 'group_leader') {
      groupSelect.required = false;
      if (groupHintLeader) groupHintLeader.hidden = false;
      if (groupHintMember) groupHintMember.hidden = true;
      // Filter options: leaders can only be assigned to orphan groups
      // (the backend re-validates this — we just guide the UI). The empty
      // "— выберите команду —" entry stays selectable.
      const opts = groupSelect.options;
      let selectedIsOrphan = false;
      for (let i = 0; i < opts.length; i++) {
        const opt = opts[i];
        if (!opt.value) { opt.hidden = false; opt.disabled = false; continue; }
        const eligible = !!orphanSet[opt.value];
        opt.hidden = !eligible;
        opt.disabled = !eligible;
        if (eligible && opt.selected) selectedIsOrphan = true;
      }
      if (!selectedIsOrphan) {
        groupSelect.value = '';
      }
    } else {
      groupSelect.required = true;
      if (groupHintLeader) groupHintLeader.hidden = true;
      if (groupHintMember) groupHintMember.hidden = false;
      // Re-show every option for group_member.
      const opts = groupSelect.options;
      for (let i = 0; i < opts.length; i++) {
        opts[i].hidden = false;
        opts[i].disabled = false;
      }
    }

    // ADR-0038 §5: additional teams only make sense for a group_member.
    // Switching to group_leader hides the block and clears any selection.
    if (additionalGroupsField) {
      if (role === 'group_member') {
        additionalGroupsField.hidden = false;
      } else {
        additionalGroupsField.hidden = true;
        additionalGroupInputs.forEach(function (cb) { cb.checked = false; });
      }
    }
    syncAdditionalGroupWithHome();
  }

  roleInputs.forEach(function (r) {
    r.addEventListener('change', applyRoleVisibility);
  });
  if (groupSelect) {
    groupSelect.addEventListener('change', syncAdditionalGroupWithHome);
  }
  // Initial state on load.
  applyRoleVisibility();

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      // Reset role to default + apply visibility before opening.
      roleInputs.forEach(function (r) {
        r.checked = (r.getAttribute('data-role-value') === 'group_member');
      });
      // Reset optional fields so a re-open never keeps a stale password /
      // additional-team selection / error banner (ADR-0038).
      showCreateError('');
      const pwInput = createForm ? createForm.querySelector('#new-user-password') : null;
      if (pwInput) { pwInput.value = ''; pwInput.type = 'password'; }
      additionalGroupInputs.forEach(function (cb) { cb.checked = false; });
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
      // group_id semantics by role:
      //   - group_member: optional but normally required by the UI; the
      //     backend rejects creation without a group (FE-FIX round-2 #4).
      //   - group_leader (bug-fix #2): optional. When set, backend assigns
      //     the new leader to that existing orphan group; when omitted,
      //     backend auto-creates a new group.
      if (groupIdRaw) {
        const gid = parseInt(groupIdRaw, 10);
        if (Number.isFinite(gid) && gid > 0) {
          payload.group_id = gid;
        }
      }

      showCreateError('');

      // ADR-0038 §3: optional password. Empty → self-set flow (backend leaves
      // password_encrypted NULL → column «—»); set → admin-set reversible copy.
      const pw = (fd.get('password') || '').toString();
      if (pw) {
        const pwErr = validatePassword(pw);
        if (pwErr) { showCreateError(pwErr); return; }
        payload.password = pw;
      }

      // ADR-0038 §5: additional teams — only for group_member; deduped against
      // the home team and each other. Sent as an int array.
      if (payload.role === 'group_member') {
        const seen = {};
        const additional = [];
        fd.getAll('additional_group_ids').forEach(function (raw) {
          const n = parseInt((raw || '').toString(), 10);
          if (!Number.isFinite(n) || n < 1) return;
          if (payload.group_id && n === payload.group_id) return;
          if (seen[n]) return;
          seen[n] = true;
          additional.push(n);
        });
        if (additional.length) payload.additional_group_ids = additional;
      }

      const submitBtn = createForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/users', {
          method: 'POST',
          body: payload,
        });
        if (resp.ok) {
          window.MAS.flash(
            payload.password
              ? 'Пользователь создан. Пароль задан — сообщите его пользователю.'
              : 'Пользователь создан. Сообщите ему логин — при первом входе нужно будет задать пароль.',
            'success'
          );
          if (createDialog && typeof createDialog.close === 'function') createDialog.close();
          window.location.reload();
          return;
        }
        // Inline error (keeps the dialog open so the admin can correct input).
        // 400 group_not_found (field=additional_group_ids) / validation_error
        // (weak password) / conflict (username) all surface here.
        const err = await window.MAS.readJsonError(resp);
        showCreateError(err.message || 'Не удалось создать пользователя.');
      } catch (_e) {
        showCreateError('Сетевая ошибка. Попробуйте ещё раз.');
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
        showGroupError('Укажите название команды.');
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
          window.MAS.flash('Команда создана.', 'success');
          if (createGroupDialog && typeof createGroupDialog.close === 'function') {
            createGroupDialog.close();
          }
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        showGroupError(err.message || 'Не удалось создать команду.');
      } catch (_e) {
        showGroupError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (groupSubmitBtn) groupSubmitBtn.disabled = false;
      }
    });
  }

  // ---- Reset password (ADR-0038: optional admin-set password) --------------
  //
  // Instead of a bare confirm(), the reset action opens a dialog with an
  // OPTIONAL «Новый пароль» field: empty → force self-set (column «—»); set →
  // admin-set reversible copy (value shown in the «Пароль» column).

  const resetDialog        = document.querySelector('[data-admin-reset-dialog]');
  const resetUsernameLabel = document.querySelector('[data-admin-reset-username]');
  const resetConfirmForm   = document.querySelector('[data-admin-reset-confirm-form]');
  const resetPasswordInput = document.querySelector('[data-admin-reset-password]');
  const resetGoBtn         = document.querySelector('[data-admin-reset-go]');
  const resetCancelBtn     = document.querySelector('[data-admin-reset-cancel]');
  const resetError         = document.querySelector('[data-admin-reset-error]');

  let pendingResetAction = '';

  function showResetError(text) {
    if (!resetError) return;
    resetError.textContent = text || '';
    resetError.hidden = !text;
  }

  document.querySelectorAll('[data-admin-reset-form]').forEach(function (f) {
    f.addEventListener('submit', function (event) {
      event.preventDefault();
      pendingResetAction = f.getAttribute('action') || '';
      if (!pendingResetAction) return;
      const username = f.getAttribute('data-username') || '';
      if (resetUsernameLabel) resetUsernameLabel.textContent = '@' + username;
      if (resetPasswordInput) { resetPasswordInput.value = ''; resetPasswordInput.type = 'password'; }
      showResetError('');

      if (!resetDialog) {
        // Defensive no-dialog fallback: plain self-set reset.
        window.MAS.csrfFetch(pendingResetAction, { method: 'POST' }).then(function () {
          window.location.reload();
        });
        return;
      }
      if (typeof resetDialog.showModal === 'function') {
        resetDialog.showModal();
      } else {
        resetDialog.setAttribute('open', 'open');
      }
      if (resetPasswordInput) resetPasswordInput.focus();
    });
  });

  if (resetCancelBtn && resetDialog) {
    resetCancelBtn.addEventListener('click', function () {
      if (typeof resetDialog.close === 'function') resetDialog.close();
      else resetDialog.removeAttribute('open');
    });
  }

  if (resetConfirmForm) {
    resetConfirmForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!pendingResetAction) return;
      showResetError('');
      const pw = resetPasswordInput ? resetPasswordInput.value : '';
      let options = { method: 'POST' };
      if (pw) {
        const pwErr = validatePassword(pw);
        if (pwErr) { showResetError(pwErr); return; }
        options = { method: 'POST', body: { password: pw } };
      }
      if (resetGoBtn) resetGoBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(pendingResetAction, options);
        if (resp.ok) {
          window.MAS.flash(
            pw
              ? 'Пароль изменён. Все сессии пользователя завершены.'
              : 'Пароль сброшен — пользователь задаст новый при входе. Все сессии завершены.',
            'success'
          );
          if (resetDialog && typeof resetDialog.close === 'function') resetDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        showResetError(err.message || 'Не удалось сбросить пароль.');
      } catch (_e) {
        showResetError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (resetGoBtn) resetGoBtn.disabled = false;
      }
    });
  }

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

  // ---- Group actions + move/add/remove membership (ADR-0030) ---------------
  //
  // The «+» button next to a user's name opens a shared actions dialog with
  // two choices: «Переместить в другую команду» (PATCH /api/admin/users/{id})
  // and «Добавить в другую команду» (POST /api/admin/users/{id}/groups). For
  // a group_leader the «Переместить» choice is disabled (a leader's home team
  // can't be moved; backend also rejects with 409). The team chips in the
  // «Команда» column carry a «×» that removes an additional membership
  // (DELETE /api/admin/users/{id}/groups/{gid}).
  //
  // The chooser is a <dialog> (top layer) rather than an inline popup so it
  // is never clipped by the table's overflow:hidden.

  // Context captured when a «+» trigger is clicked, shared by both dialogs.
  let menuUserId = 0;
  let menuUsername = '';
  let menuCurrentGid = '0';
  let menuMemberGids = [];
  let menuIsLeader = false;

  function parseGidList(raw) {
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.map(function (n) { return parseInt(n, 10); })
                .filter(function (n) { return Number.isFinite(n) && n > 0; });
    } catch (_e) {
      return [];
    }
  }

  // -- actions chooser dialog ------------------------------------------------

  const actionsDialog       = document.querySelector('[data-admin-actions-dialog]');
  const actionsUsernameSpan = document.querySelector('[data-admin-actions-username]');
  const actionsMoveBtn      = document.querySelector('[data-admin-actions-move]');
  const actionsMoveDisabled = document.querySelector('[data-admin-actions-move-disabled]');
  const actionsAddBtn       = document.querySelector('[data-admin-actions-add]');

  function closeActionsDialog() {
    if (!actionsDialog) return;
    if (typeof actionsDialog.close === 'function') actionsDialog.close();
    else actionsDialog.removeAttribute('open');
  }

  document.querySelectorAll('[data-admin-menu-trigger]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      menuUserId = parseInt(trigger.getAttribute('data-user-id') || '0', 10);
      menuUsername = trigger.getAttribute('data-username') || '';
      menuCurrentGid = trigger.getAttribute('data-current-gid') || '0';
      menuMemberGids = parseGidList(trigger.getAttribute('data-member-gids'));
      menuIsLeader = trigger.getAttribute('data-is-leader') === '1';
      if (!menuUserId || !actionsDialog) return;

      if (actionsUsernameSpan) actionsUsernameSpan.textContent = '@' + menuUsername;
      // «Переместить» is unavailable for leaders.
      if (actionsMoveBtn) {
        actionsMoveBtn.disabled = menuIsLeader;
        actionsMoveBtn.hidden = menuIsLeader;
      }
      if (actionsMoveDisabled) actionsMoveDisabled.hidden = !menuIsLeader;

      if (typeof actionsDialog.showModal === 'function') {
        actionsDialog.showModal();
      } else {
        actionsDialog.setAttribute('open', 'open');
      }
      // Focus the first available action for keyboard users.
      const firstAction = (actionsMoveBtn && !actionsMoveBtn.hidden)
        ? actionsMoveBtn
        : actionsAddBtn;
      if (firstAction) firstAction.focus();
    });
  });

  if (actionsMoveBtn) {
    actionsMoveBtn.addEventListener('click', function () {
      if (menuIsLeader) return;
      closeActionsDialog();
      openMoveDialog();
    });
  }
  if (actionsAddBtn) {
    actionsAddBtn.addEventListener('click', function () {
      closeActionsDialog();
      openAddDialog();
    });
  }

  // -- Move-to-group dialog (existing flow, now opened from the chooser) -----

  const moveDialog       = document.querySelector('[data-admin-move-dialog]');
  const moveForm         = document.querySelector('[data-admin-move-form]');
  const moveSelect       = document.querySelector('[data-admin-move-select]');
  const moveUsernameSpan = document.querySelector('[data-admin-move-username]');
  const moveCancelBtn    = document.querySelector('[data-admin-move-cancel]');
  const moveGoBtn        = document.querySelector('[data-admin-move-go]');
  const moveError        = document.querySelector('[data-admin-move-error]');

  let pendingMoveUserId = 0;

  function showMoveError(text) {
    if (!moveError) return;
    moveError.textContent = text || '';
    moveError.hidden = !text;
  }

  function openMoveDialog() {
    pendingMoveUserId = menuUserId;
    if (!pendingMoveUserId || !moveDialog || !moveSelect) return;
    if (moveUsernameSpan) moveUsernameSpan.textContent = '@' + menuUsername;
    // Pre-select the current home group (if any) so the admin sees state.
    if (menuCurrentGid && menuCurrentGid !== '0') {
      moveSelect.value = menuCurrentGid;
    } else {
      moveSelect.selectedIndex = 0;
    }
    showMoveError('');
    if (typeof moveDialog.showModal === 'function') {
      moveDialog.showModal();
    } else {
      moveDialog.setAttribute('open', 'open');
    }
  }

  if (moveCancelBtn && moveDialog) {
    moveCancelBtn.addEventListener('click', function () {
      if (typeof moveDialog.close === 'function') moveDialog.close();
      else moveDialog.removeAttribute('open');
    });
  }

  if (moveForm) {
    moveForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!pendingMoveUserId || !moveSelect) return;
      const gid = parseInt((moveSelect.value || '').toString(), 10);
      if (!Number.isFinite(gid) || gid < 1) {
        showMoveError('Выберите команду.');
        return;
      }
      if (moveGoBtn) moveGoBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/users/' + pendingMoveUserId, {
          method: 'PATCH',
          body: { group_id: gid },
        });
        if (resp.ok) {
          window.MAS.flash('Пользователь перенесён в новую команду.', 'success');
          if (typeof moveDialog.close === 'function') moveDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        showMoveError(err.message || 'Не удалось перенести пользователя.');
      } catch (_e) {
        showMoveError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (moveGoBtn) moveGoBtn.disabled = false;
      }
    });
  }

  // -- Add-to-group dialog (ADR-0030 POST .../groups) -----------------------

  const addDialog       = document.querySelector('[data-admin-add-dialog]');
  const addForm         = document.querySelector('[data-admin-add-form]');
  const addSelect       = document.querySelector('[data-admin-add-select]');
  const addField        = document.querySelector('[data-admin-add-field]');
  const addUsernameSpan = document.querySelector('[data-admin-add-username]');
  const addEmptyNote    = document.querySelector('[data-admin-add-empty]');
  const addCancelBtn    = document.querySelector('[data-admin-add-cancel]');
  const addGoBtn        = document.querySelector('[data-admin-add-go]');
  const addError        = document.querySelector('[data-admin-add-error]');

  let pendingAddUserId = 0;

  function showAddError(text) {
    if (!addError) return;
    addError.textContent = text || '';
    addError.hidden = !text;
  }

  // Capture the full team option set once so we can restore it on each open
  // (each user excludes a different subset of teams already joined).
  let allAddOptions = [];
  if (addSelect) {
    allAddOptions = Array.prototype.slice.call(addSelect.options).map(function (opt) {
      return { value: opt.value, label: opt.textContent };
    });
  }

  function openAddDialog() {
    pendingAddUserId = menuUserId;
    if (!pendingAddUserId || !addDialog || !addSelect) return;
    if (addUsernameSpan) addUsernameSpan.textContent = '@' + menuUsername;
    showAddError('');

    // Rebuild the select excluding teams the user already belongs to.
    const joined = {};
    menuMemberGids.forEach(function (g) { joined[String(g)] = true; });
    while (addSelect.firstChild) addSelect.removeChild(addSelect.firstChild);
    let available = 0;
    allAddOptions.forEach(function (o) {
      if (!o.value || joined[o.value]) return;
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      addSelect.appendChild(opt);
      available += 1;
    });

    const hasAvailable = available > 0;
    if (addField) addField.hidden = !hasAvailable;
    if (addEmptyNote) addEmptyNote.hidden = hasAvailable;
    if (addGoBtn) addGoBtn.disabled = !hasAvailable;
    if (hasAvailable) addSelect.selectedIndex = 0;

    if (typeof addDialog.showModal === 'function') {
      addDialog.showModal();
    } else {
      addDialog.setAttribute('open', 'open');
    }
  }

  if (addCancelBtn && addDialog) {
    addCancelBtn.addEventListener('click', function () {
      if (typeof addDialog.close === 'function') addDialog.close();
      else addDialog.removeAttribute('open');
    });
  }

  if (addForm) {
    addForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!pendingAddUserId || !addSelect) return;
      const gid = parseInt((addSelect.value || '').toString(), 10);
      if (!Number.isFinite(gid) || gid < 1) {
        showAddError('Выберите команду.');
        return;
      }
      if (addGoBtn) addGoBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(
          '/api/admin/users/' + pendingAddUserId + '/groups',
          { method: 'POST', body: { group_id: gid } }
        );
        if (resp.ok) {
          window.MAS.flash('Пользователь добавлен в команду.', 'success');
          if (typeof addDialog.close === 'function') addDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        showAddError(err.message || 'Не удалось добавить пользователя в команду.');
      } catch (_e) {
        showAddError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (addGoBtn) addGoBtn.disabled = false;
      }
    });
  }

  // -- Remove additional membership (the «×» on a team chip) ----------------

  document.querySelectorAll('[data-admin-remove-membership]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const username = f.getAttribute('data-username') || '';
      const groupName = f.getAttribute('data-group-name') || '';
      const msg = 'Убрать пользователя @' + username +
                  ' из команды «' + groupName + '»? ' +
                  'Он перестанет видеть письма этой команды.';
      if (!window.confirm(msg)) return;
      // action is the no-JS fallback URL (.../groups/{gid}/delete); strip the
      // trailing /delete and use the canonical DELETE verb for AJAX.
      const action = f.getAttribute('action') || '';
      const url = action.replace(/\/delete$/, '');
      if (!url) return;
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(url, { method: 'DELETE' });
        if (resp.ok || resp.status === 204) {
          window.MAS.flash('Членство в команде удалено.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось удалить членство в команде.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  // ---- Password reveal in the users table (ADR-0038) -----------------------
  //
  // The plaintext is NEVER embedded in the markup. Each reveal fetches it
  // on-demand (GET /api/admin/users/{id}/password) — minimal exposure plus a
  // `user_password_revealed` audit entry per reveal. A second click re-masks
  // it and drops the plaintext from the DOM.

  const PW_MASK = '••••••••';

  document.querySelectorAll('[data-pw-reveal]').forEach(function (btn) {
    const cell = btn.closest('[data-pw-cell]');
    const valueEl = cell ? cell.querySelector('[data-pw-value]') : null;
    const userId = btn.getAttribute('data-user-id');
    let revealed = false;
    let busy = false;

    btn.addEventListener('click', async function () {
      if (busy) return;
      if (revealed) {
        // Re-mask: remove the plaintext from the DOM.
        if (valueEl) valueEl.textContent = PW_MASK;
        revealed = false;
        btn.setAttribute('aria-pressed', 'false');
        btn.setAttribute('title', 'Показать пароль');
        if (cell) cell.classList.remove('is-revealed');
        return;
      }
      if (!userId) return;
      busy = true;
      btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(
          '/api/admin/users/' + encodeURIComponent(userId) + '/password',
          { method: 'GET' }
        );
        if (resp.ok) {
          const data = await resp.json();
          const pw = (data && typeof data.password === 'string') ? data.password : '';
          // textContent (never innerHTML) — server value is rendered as text,
          // never parsed as HTML.
          if (valueEl) valueEl.textContent = pw;
          revealed = true;
          btn.setAttribute('aria-pressed', 'true');
          btn.setAttribute('title', 'Скрыть пароль');
          if (cell) cell.classList.add('is-revealed');
        } else if (resp.status === 404) {
          // password_not_set (has_password desync): show «—», drop the toggle.
          if (valueEl) valueEl.textContent = '—';
          btn.hidden = true;
        } else if (resp.status === 429) {
          window.MAS.flash('Слишком часто. Попробуйте позже.', 'error');
        } else if (resp.status === 403) {
          window.MAS.flash('Недостаточно прав для просмотра пароля.', 'error');
        } else {
          const err = await window.MAS.readJsonError(resp);
          window.MAS.flash(err.message || 'Не удалось показать пароль.', 'error');
        }
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        busy = false;
        btn.disabled = false;
      }
    });
  });

  // ---- Show/hide toggle for password <input>s (create / reset dialogs) -----

  document.querySelectorAll('[data-pw-input-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const wrap = btn.closest('.pw-input');
      const input = wrap ? wrap.querySelector('input') : null;
      if (!input) return;
      const willShow = input.type === 'password';
      input.type = willShow ? 'text' : 'password';
      btn.setAttribute('aria-pressed', willShow ? 'true' : 'false');
      btn.setAttribute('title', willShow ? 'Скрыть пароль' : 'Показать пароль');
      btn.setAttribute('aria-label', willShow ? 'Скрыть пароль' : 'Показать пароль');
    });
  });
})();
