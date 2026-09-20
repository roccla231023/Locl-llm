"use strict";

const state = { status: null, channels: [], credentials: [], accessKeys: [], models: [], logs: [], settings: {}, importChannel: null, importCredential: null, importModels: [], credentialFilter: null, modelChecks: [], modelCheckRunning: false, ignoredModelChanges: new Set() };
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let toastTimer;

async function withLoading(form, fn) {
  const btn = form.querySelector("[type='submit'][value='default'], [type='submit']:not([value='cancel'])");
  if (btn) btn.disabled = true;
  try { await fn(); } finally { if (btn) btn.disabled = false; }
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = "toast", error ? 5000 : 2600);
}

async function api(path, options = {}) {
  const config = { credentials: "same-origin", ...options };
  if (config.body && typeof config.body !== "string") {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("json") ? await response.json() : { error: await response.text() };
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/login")) showAuth(false);
    throw new Error(data.error || data?.error?.message || `HTTP ${response.status}`);
  }
  return data;
}

function showAuth(setup) {
  $("#appView").classList.add("hidden");
  $("#authView").classList.remove("hidden");
  $("#confirmField").classList.toggle("hidden", !setup);
  $("#authTitle").textContent = setup ? "初始化本地网关" : "欢迎回来";
  $("#authHint").textContent = setup ? "首次使用，请设置本地管理密码" : "输入管理员密码进入本地控制台";
  $("#authSubmit").textContent = setup ? "完成初始化" : "登录";
  $("#authPassword").autocomplete = setup ? "new-password" : "current-password";
  state.status = { ...(state.status || {}), setup_required: setup };
  setTimeout(() => $("#authPassword").focus(), 50);
}

function showApp() {
  $("#authView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#versionText").textContent = `v${state.status.version}`;
  loadAll();
}

async function boot() {
  try {
    state.status = await api("/admin/api/status");
    if (state.status.setup_required) showAuth(true);
    else if (!state.status.authenticated) showAuth(false);
    else showApp();
  } catch (error) { toast(`无法连接本地服务：${error.message}`, true); }
}

$("#authForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = $("#authPassword").value;
  const setup = state.status?.setup_required;
  if (setup && password !== $("#authConfirm").value) return toast("两次输入的密码不一致", true);
  try {
    await api(setup ? "/admin/api/setup" : "/admin/api/login", { method: "POST", body: { password } });
    state.status.setup_required = false;
    state.status.authenticated = true;
    event.target.reset();
    showApp();
  } catch (error) { toast(error.message, true); }
});

async function loadAll() {
  await loadChannels();
  await loadCredentials();
  await Promise.all([loadOverview(), loadAccessKeys(), loadModels(), loadSettings()]);
}

async function loadOverview() {
  try {
    const { stats } = await api("/admin/api/overview");
    const cards = [
      ["中转站", stats.channels, `${stats.enabled_channels} 个已启用`],
      ["模型路由", stats.models, `${stats.enabled_models} 个已启用`],
      ["渠道 Key", stats.access_keys, `${stats.enabled_access_keys} 个已启用`],
      ["本地请求", stats.requests, "保留的日志记录"],
    ];
    $("#statsGrid").innerHTML = cards.map(([name, value, detail]) => `<article class="stat"><span>${esc(name)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`).join("");
  } catch (error) { toast(error.message, true); }
}

async function loadChannels() {
  try {
    state.channels = (await api("/admin/api/channels")).channels;
    renderChannels();
    refreshChannelSelect();
    refreshCredentialChannelSelect();
    refreshAccessKeyChannelSelect();
  } catch (error) { toast(error.message, true); }
}

function renderChannels() {
  const list = $("#channelList");
  $("#channelEmpty").classList.toggle("hidden", state.channels.length > 0);
  list.innerHTML = state.channels.map(c => `
    <article class="channel-card">
      <div class="channel-main">
        <div class="channel-top"><span class="status-dot ${c.enabled ? "good" : ""}"></span><h2>${esc(c.name)}</h2><span class="badge ${c.enabled ? "" : "off"}">${c.enabled ? "已启用" : "已禁用"}</span></div>
        <span class="channel-url">${esc(c.base_url)}</span>
        <div class="channel-meta"><span>${c.model_count} 个模型</span><span>${c.credential_count || 0} 个分组</span><span>${c.enabled_credential_count || 0} 个启用</span></div>
      </div>
      <div class="card-actions">
        <button class="action-button" data-manage-credentials="${c.id}">管理分组</button>
        <button class="action-button" data-edit-channel="${c.id}">编辑</button>
        <button class="action-button danger" data-delete-channel="${c.id}">删除</button>
      </div>
    </article>`).join("");
}

async function loadCredentials() {
  try {
    const groups = await Promise.all(state.channels.map(async c => {
      const result = await api(`/admin/api/channels/${c.id}/credentials`);
      return result.credentials || [];
    }));
    state.credentials = groups.flat();
    renderCredentials();
    refreshCredentialChannelSelect();
    refreshModelCredentialSelect();
  } catch (error) { toast(error.message, true); }
}

function renderCredentials() {
  const list = $("#credentialList");
  const filter = state.credentialFilter;
  const rows = state.credentials.filter(c => !filter || c.channel_id === Number(filter));
  $("#credentialEmpty").classList.toggle("hidden", rows.length > 0);
  const filterBar = $("#credentialFilterBar");
  if (filter) {
    const ch = state.channels.find(c => c.id === Number(filter));
    $("#credentialFilterText").textContent = `筛选中转站：${ch ? ch.name : filter}`;
    filterBar.classList.remove("hidden");
  } else {
    filterBar.classList.add("hidden");
  }
  list.innerHTML = rows.map(c => `
    <article class="channel-card access-key-card">
      <div class="channel-main">
        <div class="channel-top"><span class="status-dot ${c.enabled && c.channel_enabled ? "good" : ""}"></span><h2>${esc(c.name)}</h2><span class="badge ${c.enabled && c.channel_enabled ? "" : "off"}">${c.enabled && c.channel_enabled ? "已启用" : "已停用"}</span></div>
        <div class="channel-meta"><span>中转站：${esc(c.channel_name)}</span><span>${c.model_count} 个模型</span><span>${authLabel(c.auth_type)}</span><span>Key ${esc(c.api_key || "无")}</span></div>
      </div>
      <div class="card-actions"><button class="action-button" data-check-credential="${c.id}">检查变化</button><button class="action-button" data-import-credential="${c.id}">拉取模型</button><button class="action-button" data-edit-credential="${c.id}">编辑</button><button class="action-button danger" data-delete-credential="${c.id}">删除</button></div>
    </article>`).join("");
}

function refreshCredentialChannelSelect() {
  const html = state.channels.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  if ($("#credentialChannel")) $("#credentialChannel").innerHTML = html;
  if ($("#importChannelSelect")) $("#importChannelSelect").innerHTML = html;
}

function refreshModelCredentialSelect(preferred = null) {
  const channelId = Number($("#modelChannel")?.value || 0);
  const groups = state.credentials.filter(c => c.channel_id === channelId);
  const select = $("#modelCredential");
  if (!select) return;
  select.innerHTML = groups.map(c => `<option value="${c.id}">${esc(c.name)}${c.enabled ? "" : "（已停用）"}</option>`).join("");
  if (preferred !== null && groups.some(c => c.id === Number(preferred))) select.value = preferred;
}

async function loadAccessKeys() {
  try {
    state.accessKeys = (await api("/admin/api/access-keys")).access_keys;
    renderAccessKeys();
    refreshAccessKeyChannelSelect();
  } catch (error) { toast(error.message, true); }
}

function renderAccessKeys() {
  const list = $("#accessKeyList");
  $("#accessKeyEmpty").classList.toggle("hidden", state.accessKeys.length > 0);
  list.innerHTML = state.accessKeys.map(k => `
    <article class="channel-card access-key-card">
      <div class="channel-main">
        <div class="channel-top"><span class="status-dot ${k.enabled && k.channel_enabled ? "good" : ""}"></span><h2>${esc(k.name)}</h2><span class="badge ${k.enabled && k.channel_enabled ? "" : "off"}">${k.enabled && k.channel_enabled ? "已启用" : "已停用"}</span></div>
        <div class="channel-meta"><span>中转站：${esc(k.channel_name)}</span><span>Key ${esc(k.api_key || "无")}</span></div>
      </div>
      <div class="card-actions">
        <button class="action-button" data-edit-access-key="${k.id}">编辑</button>
        <button class="action-button danger" data-delete-access-key="${k.id}">删除</button>
      </div>
    </article>`).join("");
}

function refreshAccessKeyChannelSelect() {
  $("#accessKeyChannel").innerHTML = state.channels.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
}

async function loadModels() {
  try {
    state.models = (await api("/admin/api/models")).models;
    renderModels();
  } catch (error) { toast(error.message, true); }
}

function renderModels() {
  const query = $("#modelSearch").value.trim().toLowerCase();
  const rows = state.models.filter(m => [m.public_name, m.upstream_name, m.channel_name, m.credential_name].filter(Boolean).some(x => x.toLowerCase().includes(query)));
  $("#modelCount").textContent = `${rows.length} / ${state.models.length}`;
  $("#modelEmpty").classList.toggle("hidden", rows.length > 0);
  $("#modelTable").innerHTML = rows.map(m => `<tr>
    <td><code>${esc(m.public_name)}</code></td><td>${esc(m.channel_name)}</td><td>${esc(m.credential_name || "默认凭证")}</td><td><code>${esc(m.upstream_name)}</code></td>
    <td><span class="badge ${m.enabled && m.channel_enabled && m.credential_enabled ? "" : "off"}">${m.enabled && m.channel_enabled && m.credential_enabled ? "可用" : "停用"}</span></td>
    <td class="row-actions"><button class="action-button" data-edit-model="${m.id}">编辑</button><button class="action-button danger" data-delete-model="${m.id}">删除</button></td>
  </tr>`).join("");
}

function reportForCredential(credentialId) {
  return state.modelChecks.find(item => item.credential?.id === Number(credentialId));
}

async function fetchModelCheck(credential) {
  try {
    const report = await api(`/admin/api/channels/${credential.channel_id}/credentials/${credential.id}/model-changes`);
    return { ...report, check_ok: true };
  } catch (error) {
    return {
      check_ok: false,
      error: error.message,
      credential: {
        id: credential.id,
        name: credential.name,
        channel_id: credential.channel_id,
        channel_name: credential.channel_name,
      },
    };
  }
}

async function refreshModelCheck(credentialId) {
  const credential = state.credentials.find(item => item.id === Number(credentialId));
  if (!credential) return;
  const report = await fetchModelCheck(credential);
  const index = state.modelChecks.findIndex(item => item.credential?.id === Number(credentialId));
  if (index >= 0) state.modelChecks.splice(index, 1, report);
  else state.modelChecks.push(report);
  renderModelChecks();
}

async function checkModelChanges(credentialId = null) {
  if (state.modelCheckRunning) return;
  let credentials = credentialId === null
    ? state.credentials.filter(item => item.enabled && item.channel_enabled)
    : state.credentials.filter(item => item.id === Number(credentialId));
  if (!credentials.length) return toast(credentialId === null ? "没有启用的分组凭证可检查" : "分组凭证不存在", true);

  state.modelCheckRunning = true;
  state.modelChecks = [];
  state.ignoredModelChanges.clear();
  $("#modelChangesDialog").showModal();
  $("#modelCheckLoading").classList.remove("hidden");
  $("#modelCheckResults").innerHTML = "";
  $("#modelCheckSummary").classList.add("hidden");
  $("#modelCheckProgress").textContent = `准备检查 ${credentials.length} 个分组……`;
  $("#checkAllModelsBtn").disabled = true;
  try {
    for (let index = 0; index < credentials.length; index += 1) {
      const credential = credentials[index];
      $("#modelCheckProgress").textContent = `正在检查 ${index + 1} / ${credentials.length}：${credential.channel_name} / ${credential.name}`;
      state.modelChecks.push(await fetchModelCheck(credential));
      renderModelChecks();
    }
  } finally {
    state.modelCheckRunning = false;
    $("#checkAllModelsBtn").disabled = false;
    $("#modelCheckLoading").classList.add("hidden");
    $("#modelCheckProgress").textContent = `已完成 ${credentials.length} 个分组的检查`;
    renderModelChecks();
  }
}

function renderModelChecks() {
  const reports = state.modelChecks;
  const successful = reports.filter(item => item.check_ok);
  const failed = reports.length - successful.length;
  const totals = successful.reduce((sum, item) => ({
    added: sum.added + item.summary.new_models,
    missing: sum.missing + item.summary.missing_routes,
    normal: sum.normal + item.summary.available_routes,
  }), { added: 0, missing: 0, normal: 0 });

  if (reports.length) {
    $("#modelCheckSummary").classList.remove("hidden");
    $("#modelCheckSummary").innerHTML = [
      ["上游新增", totals.added, "warn"],
      ["疑似下线或改名", totals.missing, "danger"],
      ["正常路由", totals.normal, "good"],
      ["检查失败", failed, failed ? "danger" : "good"],
    ].map(([name, value, kind]) => `<article class="change-stat ${kind}"><span>${name}</span><strong>${value}</strong></article>`).join("");
  }

  $("#modelCheckResults").innerHTML = reports.map(report => {
    const credential = report.credential || {};
    if (!report.check_ok) return `<article class="change-report error-report">
      <div class="change-report-head"><div><h3>${esc(credential.channel_name)} / ${esc(credential.name)}</h3><p>无法读取上游模型列表</p></div><span class="badge error">检查失败</span></div>
      <div class="change-error">${esc(report.error)}</div>
      <button class="action-button" data-recheck-credential="${credential.id}">重试</button>
    </article>`;

    const ignored = state.ignoredModelChanges;
    const missingRoutes = report.missing_routes.filter(route => !ignored.has(route.id));
    const ignoredCount = report.missing_routes.length - missingRoutes.length;
    const importable = report.new_models.filter(item => item.importable);
    const conflicts = report.new_models.filter(item => !item.importable);
    const listId = `replacementModels-${credential.id}`;
    const missingHtml = missingRoutes.map(route => `<div class="missing-route" data-missing-route="${route.id}">
      <div class="route-description"><strong><code>${esc(route.public_name)}</code></strong><span>原上游：<code>${esc(route.upstream_name)}</code></span></div>
      <div class="replace-controls">
        <input data-replacement-input="${route.id}" list="${listId}" placeholder="选择或输入新的上游模型名" autocomplete="off">
        <button class="button primary small" data-replace-route="${route.id}" data-credential-id="${credential.id}">替换并保留对外名</button>
      </div>
      <div class="route-secondary-actions"><button class="text-button danger-text" data-disable-missing-route="${route.id}" data-credential-id="${credential.id}">停用路由</button><button class="text-button" data-ignore-missing-route="${route.id}">本次忽略</button></div>
    </div>`).join("");
    const newHtml = report.new_models.map(item => item.importable
      ? `<label class="check-item"><input type="checkbox" data-new-model="${credential.id}" value="${attr(item.name)}" checked><code>${esc(item.name)}</code></label>`
      : `<div class="check-item conflict-item"><span>!</span><code>${esc(item.name)}</code><small>同站对外名已被 ${esc(item.conflict.credential_name)} 使用，请手动编辑路由</small></div>`
    ).join("");
    const normalHtml = report.available_routes.map(route => `<li><code>${esc(route.public_name)}</code><span>→</span><code>${esc(route.upstream_name)}</code>${route.enabled ? "" : '<span class="badge off">本地已停用</span>'}</li>`).join("");

    return `<article id="modelChangeReport-${credential.id}" class="change-report">
      <div class="change-report-head"><div><h3>${esc(credential.channel_name)} / ${esc(credential.name)}</h3><p>上游 ${report.summary.upstream_models} 个模型，本地 ${report.summary.routes} 条路由 · ${formatTime(report.checked_at)}</p></div><button class="action-button" data-recheck-credential="${credential.id}">重新检查</button></div>
      <datalist id="${listId}">${report.upstream_models.map(name => `<option value="${attr(name)}"></option>`).join("")}</datalist>
      <section class="change-section ${report.summary.missing_routes ? "warning-section" : "good-section"}">
        <div class="change-section-title"><div><strong>疑似下线或改名</strong><small>仅列出仍处于启用状态、但上游列表中已不存在的路由</small></div><span class="badge ${report.summary.missing_routes ? "error" : ""}">${report.summary.missing_routes}</span></div>
        ${missingHtml || `<p class="change-empty">没有发现需要处理的启用路由${ignoredCount ? `，已暂时忽略 ${ignoredCount} 条` : ""}。</p>`}
        ${ignoredCount ? `<p class="change-note">本次已忽略 ${ignoredCount} 条；重新检查后会再次显示。</p>` : ""}
      </section>
      <section class="change-section">
        <div class="change-section-title"><div><strong>上游新增 / 尚未导入</strong><small>导入后默认使用上游原名作为对外模型名</small></div><span class="badge">${report.summary.new_models}</span></div>
        <div class="change-model-list">${newHtml || '<p class="change-empty">没有发现尚未路由的上游模型。</p>'}</div>
        ${importable.length ? `<div class="change-actions"><button class="button primary small" data-import-model-changes="${credential.id}">导入已选的 ${importable.length} 个模型</button></div>` : ""}
        ${conflicts.length ? `<p class="change-note">${conflicts.length} 个模型存在同站名称冲突，不会自动导入或移动分组。</p>` : ""}
      </section>
      <details class="change-section normal-section"><summary>正常路由 ${report.summary.available_routes} 条</summary><ul>${normalHtml || "<li>暂无正常路由</li>"}</ul></details>
      ${report.summary.disabled_missing_routes ? `<p class="change-note">另有 ${report.summary.disabled_missing_routes} 条已停用路由不在上游列表中，无需立即处理。</p>` : ""}
    </article>`;
  }).join("");
}

async function importCheckedModelChanges(credentialId, button) {
  const report = reportForCredential(credentialId);
  if (!report?.check_ok) return;
  const models = $$(`#modelChangeReport-${credentialId} input[data-new-model]:checked`).map(input => input.value);
  if (!models.length) return toast("请至少选择一个新增模型", true);
  button.disabled = true;
  try {
    const result = await api(`/admin/api/channels/${report.credential.channel_id}/credentials/${credentialId}/import`, { method: "POST", body: { models } });
    await Promise.all([loadModels(), loadChannels(), loadOverview()]);
    await refreshModelCheck(credentialId);
    toast(`已导入 ${result.imported.length} 个模型${result.conflicts.length ? `，冲突 ${result.conflicts.length} 个` : ""}`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function replaceMissingRoute(routeId, credentialId, button) {
  const report = reportForCredential(credentialId);
  const route = report?.missing_routes?.find(item => item.id === Number(routeId));
  const replacement = $(`[data-replacement-input="${routeId}"]`)?.value.trim();
  if (!route || !replacement) return toast("请先选择或输入新的上游模型名", true);
  if (!report.upstream_models.includes(replacement)) return toast("新上游模型名不在本次拉取列表中，请重新选择", true);
  if (!confirm(`确定把“${route.public_name}”的上游模型改为“${replacement}”吗？\n\n客户端继续使用原对外模型名。`)) return;
  button.disabled = true;
  try {
    await api(`/admin/api/models/${route.id}`, { method: "PUT", body: {
      channel_id: route.channel_id,
      credential_id: route.credential_id,
      public_name: route.public_name,
      upstream_name: replacement,
      enabled: true,
    }});
    await Promise.all([loadModels(), loadChannels(), loadOverview()]);
    await refreshModelCheck(credentialId);
    toast(`已更新 ${route.public_name}，客户端配置无需修改`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function disableMissingRoute(routeId, credentialId, button) {
  const report = reportForCredential(credentialId);
  const route = report?.missing_routes?.find(item => item.id === Number(routeId));
  if (!route || !confirm(`确定停用模型路由“${route.public_name}”吗？`)) return;
  button.disabled = true;
  try {
    await api(`/admin/api/models/${route.id}`, { method: "PUT", body: {
      channel_id: route.channel_id,
      credential_id: route.credential_id,
      public_name: route.public_name,
      upstream_name: route.upstream_name,
      enabled: false,
    }});
    await Promise.all([loadModels(), loadChannels(), loadOverview()]);
    await refreshModelCheck(credentialId);
    toast(`已停用 ${route.public_name}`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function loadLogs() {
  try {
    state.logs = (await api("/admin/api/logs")).logs;
    $("#logEmpty").classList.toggle("hidden", state.logs.length > 0);
    $("#logTable").innerHTML = state.logs.map(log => `<tr>
      <td>${formatTime(log.created_at)}</td><td><code>${esc(log.public_model || "—")}</code></td><td>${esc(log.channel_name || "—")}</td><td>${esc(log.credential_name || "—")}</td>
      <td><span class="badge ${log.status >= 200 && log.status < 400 ? "" : "error"}">${log.status || "—"}</span></td>
      <td class="latency"><strong>${formatDuration(log.first_token_ms)}</strong><small>总 ${formatDuration(log.duration_ms)}</small></td><td class="error-text" title="${esc(log.error)}">${esc(log.error || "—")}</td>
    </tr>`).join("");
  } catch (error) { toast(error.message, true); }
}

async function loadSettings() {
  try {
    state.settings = (await api("/admin/api/settings")).settings;
    $("#settingAppName").value = state.settings.app_name;
    $("#settingApiKey").value = state.settings.api_key;
    $("#settingLogLimit").value = state.settings.log_limit;
    $("#brandName").textContent = state.settings.app_name;
    $("#apiKeyText").textContent = state.settings.api_key;
    $("#baseUrlText").textContent = `${location.origin}/v1`;
  } catch (error) { toast(error.message, true); }
}

function refreshChannelSelect() {
  $("#modelChannel").innerHTML = state.channels.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  refreshModelCredentialSelect();
}

function switchPage(page) {
  $$(".page").forEach(p => p.classList.toggle("active", p.id === `page-${page}`));
  $$("#mainNav button").forEach(b => b.classList.toggle("active", b.dataset.page === page));
  const button = $(`#mainNav button[data-page="${page}"]`);
  $("#mobileTitle").textContent = button?.textContent.trim() || page;
  $(".sidebar").classList.remove("open");
  if (page === "logs") loadLogs();
  if (page === "access-keys") loadAccessKeys();
  if (page === "credentials") loadCredentials();
  if (page === "overview") loadOverview();
}

$("#mainNav").addEventListener("click", e => { const b = e.target.closest("button[data-page]"); if (b) { if (b.dataset.page === "credentials") state.credentialFilter = null; switchPage(b.dataset.page); } });
function closeSidebar() { $(".sidebar").classList.remove("open"); }
$("#mobileMenuBtn").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
$("#sidebarOverlay").addEventListener("click", closeSidebar);
$("#modelSearch").addEventListener("input", renderModels);
$("#clearCredentialFilter").addEventListener("click", () => { state.credentialFilter = null; renderCredentials(); });
$("#checkAllModelsBtn").addEventListener("click", () => checkModelChanges());
$("#closeModelChangesBtn").addEventListener("click", () => $("#modelChangesDialog").close());
$("#closeModelChangesFooterBtn").addEventListener("click", () => $("#modelChangesDialog").close());

$("#logoutBtn").addEventListener("click", async () => { await api("/admin/api/logout", { method: "POST" }); showAuth(false); });

$("#settingsForm").addEventListener("submit", async e => {
  e.preventDefault();
  await withLoading(e.target, async () => {
    try {
      await api("/admin/api/settings", { method: "PUT", body: {
        app_name: $("#settingAppName").value,
        api_key: $("#settingApiKey").value,
        log_limit: Number($("#settingLogLimit").value),
        new_password: $("#settingPassword").value,
      }});
      $("#settingPassword").value = "";
      await loadSettings();
      toast("设置已保存");
    } catch (error) { toast(error.message, true); }
  });
});

$("#generateKeyBtn").addEventListener("click", () => { $("#settingApiKey").value = `sk-local-${randomKey(32)}`; });
$("#toggleCredentialKey").addEventListener("click", e => { const input = $("#credentialApiKey"); input.type = input.type === "password" ? "text" : "password"; e.target.textContent = input.type === "password" ? "显示" : "隐藏"; });
$("#toggleAccessKey").addEventListener("click", e => { const input = $("#accessKeyValue"); input.type = input.type === "password" ? "text" : "password"; e.target.textContent = input.type === "password" ? "显示" : "隐藏"; });
$("#generateAccessKeyBtn").addEventListener("click", () => { $("#accessKeyValue").value = `sk-channel-${randomKey(32)}`; });
$("#copyAccessKeyBtn").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("#accessKeyValue").value); toast("已复制渠道 Key"); } catch { toast("浏览器未允许复制，请长按文本复制", true); } });

function openNewChannel() {
  $("#channelForm").reset(); $("#channelId").value = ""; $("#channelDialogTitle").textContent = "添加中转站";
  $("#channelEnabled").checked = true;
  $("#channelDialog").showModal();
}

async function editChannel(id) {
  try {
    const { channel: c } = await api(`/admin/api/channels/${id}`);
    $("#channelId").value = c.id; $("#channelName").value = c.name; $("#channelBaseUrl").value = c.base_url;
    $("#channelEnabled").checked = c.enabled;
    $("#channelDialogTitle").textContent = "编辑中转站"; $("#channelDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

$("#channelForm").addEventListener("submit", async e => {
  e.preventDefault();
  if (e.submitter?.value === "cancel") return $("#channelDialog").close();
  const id = $("#channelId").value;
  await withLoading(e.target, async () => {
    try {
      await api(id ? `/admin/api/channels/${id}` : "/admin/api/channels", { method: id ? "PUT" : "POST", body: {
        name: $("#channelName").value, base_url: $("#channelBaseUrl").value, enabled: $("#channelEnabled").checked,
      }});
      $("#channelDialog").close(); await loadChannels(); await loadCredentials(); await Promise.all([loadModels(), loadOverview()]); toast(id ? "中转站已更新" : "中转站已添加");
    } catch (error) { toast(error.message, true); }
  });
});

function openNewAccessKey() {
  if (!state.channels.length) return toast("请先添加一个中转站", true);
  $("#accessKeyForm").reset();
  $("#accessKeyId").value = "";
  $("#accessKeyValue").value = `sk-channel-${randomKey(32)}`;
  $("#accessKeyValue").type = "password";
  $("#toggleAccessKey").textContent = "显示";
  $("#accessKeyEnabled").checked = true;
  refreshAccessKeyChannelSelect();
  $("#accessKeyDialogTitle").textContent = "添加渠道 Key";
  $("#accessKeyDialog").showModal();
}

async function editAccessKey(id) {
  try {
    const { access_key: key } = await api(`/admin/api/access-keys/${id}`);
    refreshAccessKeyChannelSelect();
    $("#accessKeyId").value = key.id;
    $("#accessKeyName").value = key.name;
    $("#accessKeyChannel").value = key.channel_id;
    $("#accessKeyValue").value = key.api_key;
    $("#accessKeyValue").type = "password";
    $("#toggleAccessKey").textContent = "显示";
    $("#accessKeyEnabled").checked = key.enabled;
    $("#accessKeyDialogTitle").textContent = "编辑渠道 Key";
    $("#accessKeyDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

$("#accessKeyForm").addEventListener("submit", async e => {
  e.preventDefault();
  if (e.submitter?.value === "cancel") return $("#accessKeyDialog").close();
  const id = $("#accessKeyId").value;
  await withLoading(e.target, async () => {
    try {
      await api(id ? `/admin/api/access-keys/${id}` : "/admin/api/access-keys", {
        method: id ? "PUT" : "POST",
        body: {
          id: id ? Number(id) : undefined,
          name: $("#accessKeyName").value,
          channel_id: Number($("#accessKeyChannel").value),
          api_key: $("#accessKeyValue").value,
          enabled: $("#accessKeyEnabled").checked,
        },
      });
      $("#accessKeyDialog").close();
      await Promise.all([loadAccessKeys(), loadOverview()]);
      toast(id ? "渠道 Key 已更新" : "渠道 Key 已创建，请复制到客户端");
    } catch (error) { toast(error.message, true); }
  });
});

function openNewModel() {
  if (!state.channels.length) return toast("请先添加一个中转站", true);
  $("#modelForm").reset(); $("#modelId").value = ""; $("#modelEnabled").checked = true; $("#modelDialogTitle").textContent = "添加模型路由"; refreshChannelSelect(); refreshModelCredentialSelect(); $("#modelDialog").showModal();
}

function editModel(id) {
  const m = state.models.find(x => x.id === Number(id)); if (!m) return;
  refreshChannelSelect(); $("#modelId").value = m.id; $("#modelChannel").value = m.channel_id; refreshModelCredentialSelect(m.credential_id); $("#modelPublicName").value = m.public_name;
  $("#modelUpstreamName").value = m.upstream_name; $("#modelEnabled").checked = m.enabled; $("#modelDialogTitle").textContent = "编辑模型路由"; $("#modelDialog").showModal();
}

$("#modelChannel").addEventListener("change", () => refreshModelCredentialSelect());
$("#modelForm").addEventListener("submit", async e => {
  e.preventDefault(); if (e.submitter?.value === "cancel") return $("#modelDialog").close();
  const id = $("#modelId").value;
  await withLoading(e.target, async () => {
    try {
      await api(id ? `/admin/api/models/${id}` : "/admin/api/models", { method: id ? "PUT" : "POST", body: {
        channel_id: Number($("#modelChannel").value), credential_id: Number($("#modelCredential").value), public_name: $("#modelPublicName").value,
        upstream_name: $("#modelUpstreamName").value, enabled: $("#modelEnabled").checked,
      }});
      $("#modelDialog").close(); await Promise.all([loadModels(), loadChannels(), loadOverview()]); toast(id ? "模型路由已更新" : "模型路由已添加");
    } catch (error) { toast(error.message, true); }
  });
});

async function fetchCredentialModels(channelId, credentialId) {
  state.importCredential = state.credentials.find(c => c.id === Number(credentialId));
  if (!state.importCredential) return;
  $("#importLoading").classList.remove("hidden");
  try {
    state.importModels = (await api(`/admin/api/channels/${channelId}/credentials/${credentialId}/models`)).models;
    renderImportModels();
  } catch (error) {
    $("#importModelList").innerHTML = `<div class="empty compact"><h2>拉取失败</h2><p>${esc(error.message)}</p></div>`;
    toast(error.message, true);
  } finally { $("#importLoading").classList.add("hidden"); }
}

async function openImport(channelId, credentialId = null) {
  state.importChannel = state.channels.find(c => c.id === Number(channelId));
  if (!state.importChannel) return;
  const groups = state.credentials.filter(c => c.channel_id === Number(channelId));
  if (!groups.length) return toast("请先为该中转站添加分组凭证", true);
  state.importModels = []; $("#importChannelName").textContent = state.importChannel.name; $("#importModelList").innerHTML = "";
  $("#importCredential").innerHTML = groups.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("#importCredential").value = credentialId && groups.some(c => c.id === Number(credentialId)) ? credentialId : groups[0].id;
  $("#importDialog").showModal();
  await fetchCredentialModels(channelId, $("#importCredential").value);
}

function renderImportModels() {
  const q = $("#importSearch").value.toLowerCase();
  $("#importModelList").innerHTML = state.importModels.filter(x => x.toLowerCase().includes(q)).map(name => `<label class="check-item"><input type="checkbox" value="${attr(name)}" checked><code>${esc(name)}</code></label>`).join("") || `<div class="empty compact"><p>没有找到模型</p></div>`;
}
$("#importSearch").addEventListener("input", renderImportModels);
$("#importCredential").addEventListener("change", async e => { if (state.importChannel) await fetchCredentialModels(state.importChannel.id, e.target.value); });
$("#toggleAllModels").addEventListener("click", () => { const boxes = $$("#importModelList input"); const value = boxes.some(x => !x.checked); boxes.forEach(x => x.checked = value); });
$("#closeImportBtn").addEventListener("click", () => $("#importDialog").close());
$("#cancelImportBtn").addEventListener("click", () => $("#importDialog").close());
$("#confirmImportBtn").addEventListener("click", async () => {
  const models = $$("#importModelList input:checked").map(x => x.value); if (!models.length) return toast("请选择至少一个模型", true);
  if (!state.importChannel || !state.importCredential) return toast("请选择分组凭证", true);
  try {
    const result = await api(`/admin/api/channels/${state.importChannel.id}/credentials/${state.importCredential.id}/import`, { method: "POST", body: { models } });
    $("#importDialog").close(); await Promise.all([loadModels(), loadChannels(), loadOverview()]); toast(`已导入 ${result.imported.length} 个模型${result.conflicts?.length ? `，冲突 ${result.conflicts.length} 个` : ""}`);
  } catch (error) { toast(error.message, true); }
});

$("#clearLogsBtn").addEventListener("click", async () => { if (!confirm("确定清空全部请求日志吗？")) return; await api("/admin/api/logs", { method: "DELETE" }); await Promise.all([loadLogs(), loadOverview()]); toast("日志已清空"); });

document.querySelectorAll("dialog").forEach(dialog => {
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
});

function openNewCredential() {
  if (!state.channels.length) return toast("请先添加一个中转站", true);
  $("#credentialForm").reset(); $("#credentialId").value = ""; $("#credentialChannel").disabled = false; $("#credentialApiKey").value = ""; $("#credentialApiKey").type = "password"; $("#toggleCredentialKey").textContent = "显示"; $("#credentialHeaders").value = "{}"; $("#credentialEnabled").checked = true;
  refreshCredentialChannelSelect(); $("#credentialApiKey").required = true; $("#credentialDialogTitle").textContent = "添加分组凭证"; $("#credentialDialog").showModal();
}

async function editCredential(id) {
  try {
    const { credential: c } = await api(`/admin/api/credentials/${id}`);
    refreshCredentialChannelSelect(); $("#credentialId").value = c.id; $("#credentialChannel").value = c.channel_id; $("#credentialName").value = c.name; $("#credentialApiKey").value = c.api_key; $("#credentialApiKey").type = "password"; $("#toggleCredentialKey").textContent = "显示"; $("#credentialAuthType").value = c.auth_type; $("#credentialHeaders").value = JSON.stringify(c.extra_headers || {}, null, 2); $("#credentialEnabled").checked = c.enabled;
    $("#credentialApiKey").required = c.auth_type !== "none"; $("#credentialDialogTitle").textContent = "编辑分组凭证"; $("#credentialChannel").disabled = true; $("#credentialDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

$("#credentialAuthType").addEventListener("change", e => { $("#credentialApiKey").required = e.target.value !== "none"; });
$("#credentialForm").addEventListener("submit", async e => {
  e.preventDefault(); if (e.submitter?.value === "cancel") return $("#credentialDialog").close();
  const id = $("#credentialId").value;
  await withLoading(e.target, async () => {
    try {
      let headers; try { headers = JSON.parse($("#credentialHeaders").value || "{}"); } catch { throw new Error("额外请求头不是有效 JSON"); }
      const channelId = Number($("#credentialChannel").value);
      await api(id ? `/admin/api/credentials/${id}` : `/admin/api/channels/${channelId}/credentials`, { method: id ? "PUT" : "POST", body: { channel_id: channelId, name: $("#credentialName").value, api_key: $("#credentialApiKey").value, auth_type: $("#credentialAuthType").value, extra_headers: headers, enabled: $("#credentialEnabled").checked } });
      $("#credentialChannel").disabled = false; $("#credentialDialog").close(); await loadCredentials(); await Promise.all([loadChannels(), loadModels(), loadOverview()]); toast(id ? "分组凭证已更新" : "分组凭证已创建");
    } catch (error) { toast(error.message, true); }
  });
});

document.addEventListener("click", async e => {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "new-channel") openNewChannel();
  if (action === "new-access-key") openNewAccessKey();
  if (action === "new-credential") openNewCredential();
  if (action === "new-model") openNewModel();
  const editC = e.target.closest("[data-edit-channel]")?.dataset.editChannel; if (editC) editChannel(editC);
  const importC = e.target.closest("[data-import-channel]")?.dataset.importChannel; if (importC) openImport(importC);
  const manageC = e.target.closest("[data-manage-credentials]")?.dataset.manageCredentials; if (manageC) { state.credentialFilter = Number(manageC); switchPage("credentials"); renderCredentials(); }
  const importCredential = e.target.closest("[data-import-credential]")?.dataset.importCredential; if (importCredential) { const c = state.credentials.find(x => x.id === Number(importCredential)); if (c) openImport(c.channel_id, c.id); }
  const checkCredential = e.target.closest("[data-check-credential]")?.dataset.checkCredential; if (checkCredential) checkModelChanges(Number(checkCredential));
  const recheckCredential = e.target.closest("[data-recheck-credential]")?.dataset.recheckCredential;
  if (recheckCredential && !state.modelCheckRunning) { e.target.closest("button").disabled = true; try { await refreshModelCheck(Number(recheckCredential)); } finally { const btn = document.querySelector(`[data-recheck-credential="${recheckCredential}"]`); if (btn) btn.disabled = false; } }
  const importChanges = e.target.closest("[data-import-model-changes]"); if (importChanges) await importCheckedModelChanges(Number(importChanges.dataset.importModelChanges), importChanges);
  const replaceRoute = e.target.closest("[data-replace-route]"); if (replaceRoute) await replaceMissingRoute(Number(replaceRoute.dataset.replaceRoute), Number(replaceRoute.dataset.credentialId), replaceRoute);
  const disableRoute = e.target.closest("[data-disable-missing-route]"); if (disableRoute) await disableMissingRoute(Number(disableRoute.dataset.disableMissingRoute), Number(disableRoute.dataset.credentialId), disableRoute);
  const ignoreRoute = e.target.closest("[data-ignore-missing-route]")?.dataset.ignoreMissingRoute; if (ignoreRoute) { state.ignoredModelChanges.add(Number(ignoreRoute)); renderModelChecks(); }
  const editCredentialId = e.target.closest("[data-edit-credential]")?.dataset.editCredential; if (editCredentialId) editCredential(editCredentialId);
  const deleteCredentialId = e.target.closest("[data-delete-credential]")?.dataset.deleteCredential;
  if (deleteCredentialId && confirm("删除分组凭证后，绑定它的模型路由也需要先处理，确定继续吗？")) { try { await api(`/admin/api/credentials/${deleteCredentialId}`, { method: "DELETE" }); await loadCredentials(); await Promise.all([loadChannels(), loadModels(), loadOverview()]); toast("分组凭证已删除"); } catch (error) { toast(error.message, true); } }
  const deleteC = e.target.closest("[data-delete-channel]")?.dataset.deleteChannel;
  if (deleteC && confirm("删除中转站会同时删除它的全部模型路由，确定继续吗？")) { try { await api(`/admin/api/channels/${deleteC}`, { method: "DELETE" }); await loadChannels(); await loadCredentials(); await Promise.all([loadAccessKeys(), loadModels(), loadOverview()]); toast("中转站已删除"); } catch (error) { toast(error.message, true); } }
  const editK = e.target.closest("[data-edit-access-key]")?.dataset.editAccessKey; if (editK) editAccessKey(editK);
  const deleteK = e.target.closest("[data-delete-access-key]")?.dataset.deleteAccessKey;
  if (deleteK && confirm("删除后，使用这个渠道 Key 的客户端将无法继续访问，确定继续吗？")) { try { await api(`/admin/api/access-keys/${deleteK}`, { method: "DELETE" }); await Promise.all([loadAccessKeys(), loadOverview()]); toast("渠道 Key 已删除"); } catch (error) { toast(error.message, true); } }
  const editM = e.target.closest("[data-edit-model]")?.dataset.editModel; if (editM) editModel(editM);
  const deleteM = e.target.closest("[data-delete-model]")?.dataset.deleteModel;
  if (deleteM && confirm("确定删除这个模型路由吗？")) { try { await api(`/admin/api/models/${deleteM}`, { method: "DELETE" }); await Promise.all([loadModels(), loadChannels(), loadOverview()]); toast("模型路由已删除"); } catch (error) { toast(error.message, true); } }
  const copy = e.target.closest("[data-copy]")?.dataset.copy;
  if (copy) { const text = copy === "base" ? $("#baseUrlText").textContent : $("#apiKeyText").textContent; try { await navigator.clipboard.writeText(text); toast("已复制"); } catch { toast("浏览器未允许复制，请长按文本复制", true); } }
});

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
function attr(value) { return esc(value).replace(/`/g, "&#96;"); }
function authLabel(type) { return ({bearer:"Bearer", "x-api-key":"x-api-key", none:"无认证"})[type] || type; }
function randomKey(length) { const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"; const bytes = crypto.getRandomValues(new Uint8Array(length)); return [...bytes].map(x => chars[x % chars.length]).join(""); }
function formatTime(value) { try { return new Date(value).toLocaleString("zh-CN", { hour12:false }); } catch { return value; } }
function formatDuration(value) { return value === null || value === undefined ? "—" : `${value} ms`; }

boot();
