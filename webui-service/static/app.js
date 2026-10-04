/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", musicbox: "网易云音乐盒子", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", musicbox: "musicbox", lxmusic: "lxmusic", webui: "WebUI" };

let configValues = {};   // GET /api/config 的 values
let platforms = { enabled: [], registered: [] };
let dirty = false;
let qrTimer = null;
let lxVerifiedUrl = null; // 已通过测试的 lx URL（保存时免二次校验提示用）

async function api(path, options) {
  const resp = await fetch(APP_BASE + path, options);
  let body = {};
  try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
  return body;
}

function toast(message, kind) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + (kind || "");
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 3800);
}

function markDirty(note) {
  dirty = true;
  const bar = $("#save-bar");
  if (bar) bar.classList.add("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = note || "有未保存的修改";
}

function clearDirty() {
  dirty = false;
  const bar = $("#save-bar");
  if (bar) bar.classList.remove("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = "";
}

/* -------------------------------------------------------------- 导航 */
function switchPage(page) {
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === "page-" + page));
  $$("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
}
$$("[data-page]").forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));

/* -------------------------------------------------------------- 概览 */
async function loadStatus() {
  try {
    const st = await api("/api/status");
    $("#brand-version").textContent = `v${st.version}`;
    $("#sidebar-foot").textContent = `${PROVIDER_LABEL[st.current_provider] || st.current_provider}`;
    $("#ov-provider-body").innerHTML =
      `<span class="state-line"><span class="dot ok"></span>${PROVIDER_LABEL[st.current_provider] || st.current_provider}</span>`;
    $("#ov-processes").innerHTML = Object.entries(st.processes).map(([name, p]) => {
      const ok = p.state === "RUNNING";
      const cls = ok ? "ok" : p.state === "FATAL" ? "err" : "";
      return `<span class="chip"><span class="dot ${cls}"></span>${PROC_LABEL[name] || name} · ${p.state}</span>`;
    }).join("");
    $("#ov-services").innerHTML = Object.entries(st.services).map(([name, s]) => {
      if (!s.reachable && s.note) return `<span class="state-line"><span class="dot"></span>${PROC_LABEL[name]}：${s.note}</span>`;
      return `<span class="state-line"><span class="dot ${s.reachable ? "ok" : "err"}"></span>${PROC_LABEL[name]}：${s.reachable ? "正常" : "不可达"}</span>`;
    }).join("");
    const lxCard = $("#ov-lx-card");
    if (st.current_provider === "lxmusic" && st.lx_source) {
      lxCard.hidden = false;
      const s = st.lx_source.source;
      const rows = [];
      rows.push(`<div class="kv"><b>状态</b>${st.lx_source.initialized ? "已加载" : "未加载"}</div>`);
      if (s) {
        rows.push(`<div class="kv"><b>源名称</b>${s.name || "-"} ${s.version ? "v" + s.version : ""}</div>`);
        rows.push(`<div class="kv"><b>平台</b>${Object.keys(s.platforms || {}).join("、") || "-"}</div>`);
        rows.push(`<div class="kv"><b>运行</b>${s.running ? "是" : "否"}</div>`);
      }
      if (st.lx_source.last_error) rows.push(`<div class="kv"><b>错误</b>${st.lx_source.last_error}</div>`);
      $("#ov-lx").innerHTML = rows.join("");
    } else {
      lxCard.hidden = true;
    }
  } catch (exc) {
    $("#ov-provider-body").textContent = "状态加载失败：" + exc.message;
  }
}

/* -------------------------------------------------------------- 配置读写 */
async function loadConfig() {
  const cfg = await api("/api/config");
  configValues = cfg.values;
  applyConfigToForm();
  clearDirty();
}

function applyConfigToForm() {
  const v = configValues;
  const provider = v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
  $$("input[name=provider]").forEach((el) => { el.checked = el.value === provider; });
  syncProviderPanels(provider);
  if (provider === "musicbox") syncNeteaseAccount();
  const quality = v.FNMUSIC_QUALITY_MODE || "high";
  $$("input[name=quality]").forEach((el) => { el.checked = el.value === quality; });
  $("#recommend-hot").checked = v.FNMUSIC_RECOMMEND_HOT === "true";
  $("#recommend-daily").checked = v.FNMUSIC_RECOMMEND_DAILY === "true";
  $("#tee-enabled").checked = v.FNMUSIC_TEE_SAVE_ENABLED === "true";
  $("#auto-cover").checked = v.FNMUSIC_AUTO_COVER !== "false";
  $("#lyric-auto-dl").checked = v.FNMUSIC_LYRIC_AUTO_DL === "true";
  $("#fav-autobind").checked = v.FNMUSIC_FAV_AUTO_BIND === "true";
  $("#tee-dir").value = v.FNMUSIC_TEE_SAVE_DIR || "";
  $("#tee-max").value = v.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#bind-timeout").value = v.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#handoff-max").value = v.FNMUSIC_TEE_HANDOFF_MAX != null ? v.FNMUSIC_TEE_HANDOFF_MAX : "3";
  $("#scan-path").value = v.FNMUSIC_LIBRARY_SCAN_PATH || "";
  updateTeeCountLabel();
  updateBindTimeoutLabel();
  $("#llm-base").value = v.FNMUSIC_LLM_BASE_URL || "";
  $("#llm-key").value = v.FNMUSIC_LLM_API_KEY || "";
  $("#llm-model").value = v.FNMUSIC_LLM_MODEL || "";
  $("#search-timeout").value = v.FNMUSIC_SEARCH_TIMEOUT || "15";
  $("#search-probe").checked = v.FNMUSIC_SEARCH_PROBE === "true";
  $("#netease-my-playlists").checked = v.FNMUSIC_NETEASE_MY_PLAYLISTS === "true";
  $("#lx-url").value = v.LX_SOURCE_URL || "";
  lxVerifiedUrl = v.LX_SOURCE_URL || null;
  renderPlatformChips();
}

function collectConfig() {
  const provider = ($$("input[name=provider]").find((el) => el.checked) || {}).value || "";
  const values = {
    FNMUSIC_MUSICDL_ENABLED: provider === "musicdl",
    FNMUSIC_NETEASE_ENABLED: provider === "musicbox",
    FNMUSIC_LX_ENABLED: provider === "lxmusic",
    FNMUSIC_QUALITY_MODE: ($$("input[name=quality]").find((el) => el.checked) || {}).value || "high",
    FNMUSIC_RECOMMEND_HOT: $("#recommend-hot").checked,
    FNMUSIC_RECOMMEND_DAILY: $("#recommend-daily").checked,
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_AUTO_COVER: $("#auto-cover").checked,
    FNMUSIC_LYRIC_AUTO_DL: $("#lyric-auto-dl").checked,
    FNMUSIC_FAV_AUTO_BIND: $("#fav-autobind").checked,
    FNMUSIC_TEE_SAVE_DIR: $("#tee-dir").value.trim(),
    FNMUSIC_TEE_CACHE_MAX: parseInt($("#tee-max").value || "2", 10),
    FNMUSIC_OFFICIAL_BIND_TIMEOUT_S: parseInt($("#bind-timeout").value || "120", 10) || 120,
    FNMUSIC_TEE_HANDOFF_MAX: parseInt($("#handoff-max").value || "3", 10) || 0,
    FNMUSIC_LIBRARY_SCAN_PATH: $("#scan-path").value.trim(),
    FNMUSIC_LLM_BASE_URL: $("#llm-base").value.trim(),
    FNMUSIC_LLM_API_KEY: $("#llm-key").value.trim(),
    FNMUSIC_LLM_MODEL: $("#llm-model").value.trim(),
    FNMUSIC_SEARCH_TIMEOUT: parseInt($("#search-timeout").value || "15", 10) || 15,
    FNMUSIC_SEARCH_PROBE: $("#search-probe").checked,
    FNMUSIC_NETEASE_MY_PLAYLISTS: $("#netease-my-playlists").checked,
  };
  if (provider === "musicdl") {
    values.FNMUSIC_ONLINE_SOURCES = platforms.enabled.join(",");
    values.MUSICDL_SOURCES = platforms.enabled.join(",");
  }
  if (provider === "lxmusic") {
    const url = $("#lx-url").value.trim();
    if (!url) throw new Error("洛雪源需要填写脚本 URL");
    values.LX_SOURCE_URL = url;
  }
  return values;
}

async function saveConfig() {
  let values;
  try { values = collectConfig(); } catch (exc) { toast(exc.message, "fail"); return; }
  const btn = $("#save-btn");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const result = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const failed = (result.actions || []).filter((a) => !a.ok);
    const parts = [];
    if (result.changed && result.changed.length) parts.push(`已保存 ${result.changed.length} 项`);
    (result.actions || []).forEach((a) => {
      if (a.kind === "process") parts.push(`进程 ${a.program} ${a.op} ${a.ok ? "成功" : "失败"}`);
      if (a.kind === "lx_activate") parts.push(`洛雪源激活${a.ok ? "成功" : "失败"}`);
    });
    if (failed.length) {
      toast((parts.join("；") || "") + ` —— ${failed.map((f) => f.error).join("；")}`, "fail");
    } else {
      toast(parts.join("；") || "配置无变化", "ok");
    }
    clearDirty();
    await loadConfig();
    await loadStatus();
  } catch (exc) {
    toast("保存失败：" + exc.message, "fail");
  } finally {
    btn.disabled = false;
    btn.textContent = "保存并生效";
  }
}
$("#save-btn").addEventListener("click", saveConfig);

/* -------------------------------------------------------------- 大模型配置测试 */
$("#llm-test").addEventListener("click", async () => {
  const btn = $("#llm-test");
  const box = $("#llm-report");
  box.hidden = false;
  box.className = "report";
  box.textContent = "测试中（向接口发一次最小请求）…";
  btn.disabled = true;
  btn.textContent = "测试中…";
  let r = null;
  let fatal = "";
  try {
    r = await api("/api/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        values: {
          base_url: $("#llm-base").value.trim(),
          api_key: $("#llm-key").value.trim(),
          model: $("#llm-model").value.trim(),
        },
      }),
    });
  } catch (exc) {
    fatal = exc.message || String(exc);
  }
  btn.disabled = false;
  btn.textContent = "测试连接";

  if (fatal) {
    box.className = "report fail";
    box.innerHTML = `<div class="kv"><b>结论</b>测试失败 ✗</div>` +
      `<div class="kv"><b>原因</b>${esc(fatal)}</div>`;
    return;
  }
  const echo = r.model_echo && r.model_echo !== r.model
    ? `<div class="kv"><b>实际模型</b>${esc(r.model_echo)}（与填写不一致，服务端可能忽略了 model）</div>` : "";
  if (r.ok) {
    box.className = "report ok";
    box.innerHTML =
      `<div class="kv"><b>结论</b>配置正确 ✓ 保存后即可生效</div>` +
      `<div class="kv"><b>接口</b>${esc(r.url)}</div>` +
      `<div class="kv"><b>模型</b>${esc(r.model)}</div>` +
      echo +
      `<div class="kv"><b>耗时</b>${r.elapsed_ms} ms</div>` +
      (r.reply ? `<div class="kv"><b>回应</b>${esc(r.reply)}</div>` : "");
    return;
  }
  box.className = "report fail";
  box.innerHTML =
    `<div class="kv"><b>结论</b>配置不可用 ✗</div>` +
    `<div class="kv"><b>接口</b>${esc(r.url)}</div>` +
    `<div class="kv"><b>模型</b>${esc(r.model)}</div>` +
    (r.status ? `<div class="kv"><b>状态</b>HTTP ${r.status}</div>`
              : `<div class="kv"><b>阶段</b>连接${r.timeout_s ? `（超时上限 ${r.timeout_s}s）` : ""}</div>`) +
    `<div class="kv"><b>耗时</b>${r.elapsed_ms} ms</div>` +
    (r.message ? `<div class="kv"><b>返回</b>${esc(r.message)}</div>` : "") +
    (r.hint ? `<div class="kv"><b>可能原因</b>${esc(r.hint)}</div>` : "");
});

/* -------------------------------------------------------------- 音源选择 */
function savedProvider() {
  const v = configValues;
  return v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
}

// 点选未启用的音源：临时拉起其进程供预览（不写配置；5 分钟内未保存自动停止）
async function startPreview(provider) {
  try {
    const r = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
    if (r.preview) toast(`已临时启动 ${PROVIDER_LABEL[provider]}（预览）：5 分钟内未保存将自动停止`);
    return true;
  } catch (exc) {
    toast(`临时启动 ${PROVIDER_LABEL[provider]} 失败：${exc.message}`, "fail");
    return false;
  }
}

function syncProviderPanels(provider) {
  $$(".provider-card").forEach((el) => el.classList.toggle("selected", el.dataset.provider === provider));
  $("#panel-musicbox").hidden = provider !== "musicbox";
  $("#panel-musicdl").hidden = provider !== "musicdl";
  $("#panel-lxmusic").hidden = provider !== "lxmusic";
}
$$("input[name=provider]").forEach((el) =>
  el.addEventListener("change", async () => {
    syncProviderPanels(el.value);
    if (el.value === "musicbox") syncNeteaseAccount();
    markDirty("音源切换需保存后生效");
    if (el.value && el.value !== savedProvider() && await startPreview(el.value)) {
      if (el.value === "musicdl") await loadPlatforms(false);
    }
  }));

/* -------------------------------------------------------------- musicdl 平台 */
function setPlatformFallback() {
  platforms = { enabled: (configValues.FNMUSIC_ONLINE_SOURCES || "").split(",").filter(Boolean), registered: [] };
  $("#platform-note").textContent = "musicdl 进程未运行，暂无法获取平台列表（点选 musicdl 音源可临时启动预览）";
  renderPlatformChips();
}

async function fetchPlatforms() {
  const data = await api("/api/platforms");
  platforms = { enabled: data.enabled || [], registered: data.registered || [] };
  $("#platform-note").textContent = `共 ${platforms.registered.length} 个注册平台，已启用 ${platforms.enabled.length} 个`;
  renderPlatformChips();
}

async function loadPlatforms(autoPreview = true) {
  try {
    await fetchPlatforms();
  } catch (_) {
    // 进程未运行：autoPreview（用户点选/刷新触发）时临时拉起后重试；页面加载不自动拉起
    if (!autoPreview) { setPlatformFallback(); return; }
    try {
      if (await startPreview("musicdl")) await fetchPlatforms();
      else setPlatformFallback();
    } catch (_) {
      setPlatformFallback();
    }
  }
}

function renderPlatformChips() {
  const keyword = $("#platform-search").value.trim().toLowerCase();
  const container = $("#platform-list");
  const enabledSet = new Set(platforms.enabled);
  const items = platforms.registered.filter((p) => !keyword || p.toLowerCase().includes(keyword));
  container.innerHTML = items.length
    ? items.map((p) => `<span class="chip ${enabledSet.has(p) ? "on" : ""}" data-platform="${p}">${p}</span>`).join("")
    : `<span class="muted">无匹配平台</span>`;
  container.querySelectorAll(".chip[data-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const p = chip.dataset.platform;
      const set = new Set(platforms.enabled);
      if (set.has(p)) { set.delete(p); chip.classList.remove("on"); }
      else { set.add(p); chip.classList.add("on"); }
      platforms.enabled = Array.from(set);
      markDirty("musicdl 平台已修改");
    }));
}
$("#platform-search").addEventListener("input", renderPlatformChips);
$("#platform-reload").addEventListener("click", loadPlatforms);

/* -------------------------------------------------------------- 网易扫码 */
async function syncNeteaseAccount(retries = 0) {
  // 把网易账号态同步到 #qr-check（已登录显示昵称；未登录/失败清空）
  for (let i = 0; ; i++) {
    try {
      const st = await api("/api/netease/auth/status");
      const d = st.data || st;
      if (d.logged_in) {
        $("#qr-check").textContent = `当前登录：${d.nickname || d.user_id || "已登录用户"}`;
        return;
      }
    } catch (_) { /* status 不可达视为未登录 */ }
    if (i >= retries) { $("#qr-check").textContent = ""; return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

async function startQrLogin() {
  stopQrPolling();
  $("#qr-status").textContent = "正在生成二维码…";
  $("#qr-img").hidden = true;
  syncNeteaseAccount(); // 生成前同步当前账号态（非致命，不阻塞出码）
  const callLogin = () => api("/api/netease/auth/login", { method: "POST" });
  try {
    let data;
    try {
      data = await callLogin();
    } catch (exc) {
      // musicbox 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("musicbox")) throw exc;
      data = await callLogin();
    }
    const unikey = data.unikey || data.codekey || (data.data && (data.data.unikey || data.data.codekey)) || "";
    if (!unikey) throw new Error("未获取到 unikey");
    $("#qr-img").src = `${APP_BASE}/api/netease/qr?unikey=${encodeURIComponent(unikey)}`;
    $("#qr-img").hidden = false;
    $("#qr-status").textContent = "请用手机网易云音乐 App 扫码";
    pollQr(unikey);
  } catch (exc) {
    $("#qr-status").textContent = "生成失败：" + exc.message;
  }
}

async function checkQrStatus(unikey) {
  const st = await api(`/api/netease/auth/login/check?unikey=${encodeURIComponent(unikey)}`);
  // musicbox CLI 返回 {ok, data:{code}} 信封结构；兼容扁平 {code}
  const code = st?.data?.code ?? st?.code;
  if (code === 803) {
    stopQrPolling();
    $("#qr-status").textContent = "登录成功 ✓";
    syncNeteaseAccount(2); // 803 后 cookie 落盘需要一点时间，带重试取昵称
    toast("网易账号登录成功", "ok");
  } else if (code === 802) {
    $("#qr-status").textContent = "已扫码，请在手机上确认";
  } else if (code === 800) {
    stopQrPolling();
    $("#qr-status").textContent = "二维码已过期，请重新生成";
  } else {
    $("#qr-status").textContent = "等待扫码…";
  }
}

function pollQr(unikey, intervalMs = 2000) {
  stopQrPolling();
  qrTimer = setInterval(() => { checkQrStatus(unikey).catch(() => {}); }, intervalMs);
}

function stopQrPolling() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}
$("#qr-btn").addEventListener("click", startQrLogin);

/* -------------------------------------------------------------- lx 源测试 */
$("#lx-test").addEventListener("click", async () => {
  const url = $("#lx-url").value.trim();
  const box = $("#lx-report");
  if (!url) { toast("请先填写源 URL", "fail"); return; }
  box.hidden = false;
  box.className = "report";
  box.textContent = "测试中（下载脚本 → 沙箱初始化 → 多首抽样搜索/解析/探活）…";
  const callVerify = () => api("/api/lx/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: { url } }),
  });
  try {
    let r;
    try {
      r = await callVerify();
    } catch (exc) {
      // lxmusic 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("lxmusic")) throw exc;
      r = await callVerify();
    }
    if (r.ok) {
      const d = r.data || {};
      const meta = d.meta || {};
      const probe = d.probe || {};
      lxVerifiedUrl = url;
      box.className = "report ok";
      box.innerHTML =
        `<div class="kv"><b>源名称</b>${meta.name || "-"} ${meta.version ? "v" + meta.version : ""}（${meta.author || "未知作者"}）</div>` +
        `<div class="kv"><b>可用平台</b>${(d.platforms || []).join("、")}</div>` +
        (probe.title ? `<div class="kv"><b>实测</b>${probe.title} - ${probe.artist} [${probe.platform}/${probe.quality}] ${probe.content_type || ""}</div>` : "") +
        `<div class="kv"><b>结论</b>可用 ✓（保存后激活）</div>`;
    } else {
      const d = r.data || {};
      box.className = "report fail";
      box.innerHTML = `<div class="kv"><b>不可用</b>${d.message || r.error || "校验失败"}</div>`;
    }
  } catch (exc) {
    box.className = "report fail";
    box.textContent = "测试失败：" + exc.message;
  }
});

/* -------------------------------------------------- lx 源：文件上传 / NAS 选择 */
async function lxUploadScript(filename, script) {
  // 先确保 lxmusic 进程可用（预览拉起），再转发落盘
  const callUpload = () => api("/api/lx/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, script }),
  });
  try {
    return await callUpload();
  } catch (exc) {
    if (!await startPreview("lxmusic")) throw exc;
    return await callUpload();
  }
}

async function lxAfterUpload(r) {
  const d = r.data || {};
  $("#lx-url").value = d.url || "";
  lxVerifiedUrl = null; // 上传地址仍需走一次"测试"
  $("#lx-upload-note").textContent = d.meta && d.meta.name ? `已上传：${d.meta.name}` : "已上传";
  markDirty("洛雪源已更新为上传脚本，测试后保存生效");
  toast("脚本已上传，请点「测试」验证后保存", "ok");
}

$("#lx-upload").addEventListener("click", () => $("#lx-file").click());
$("#lx-file").addEventListener("change", async () => {
  const file = $("#lx-file").files && $("#lx-file").files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".js")) { toast("只支持 .js 后缀文件", "fail"); return; }
  if (file.size > 9_000_000) { toast("脚本超过 9MB 上限", "fail"); return; }
  $("#lx-upload-note").textContent = "读取并上传中…";
  try {
    const script = await file.text();
    const r = await lxUploadScript(file.name, script);
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("上传失败：" + exc.message, "fail");
  } finally {
    $("#lx-file").value = ""; // 允许重复选择同一文件
  }
});

/* NAS 文件选择：仅桌面环境（统一网关 /app/fnmusic-ext 内）可用。
   选中的主机路径经 /api/host-file 代读（webui_gateway 本地处理），再走上传落盘。 */
let lxTrimSdk = null;
async function lxLoadTrimSdk() {
  if (lxTrimSdk !== null) return lxTrimSdk;
  try {
    const mod = await import("/app/fnmusic-ext/static/vendor/trim-web-app.js");
    lxTrimSdk = new mod.TrimApp();
  } catch (_) {
    lxTrimSdk = false;
  }
  return lxTrimSdk;
}

async function lxPickFromNas() {
  const sdk = await lxLoadTrimSdk();
  if (!sdk) { toast("当前环境不支持 NAS 文件选择（直连 8774 时请用上传或 URL）", "fail"); return; }
  try {
    const result = await sdk.pickUserFile({
      directory: false,
      accept: [".js"],
      title: "选择洛雪源脚本",
      okText: "选择",
      sidebarGroup: ["myFiles", "otherShare", "favorites"],
    });
    const paths = (result && result.data) || [];
    if (!paths.length) return;
    const hostPath = paths[0];
    $("#lx-upload-note").textContent = "读取 NAS 文件中…";
    const resp = await fetch(APP_BASE + "/api/host-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: hostPath }),
    });
    let body = {};
    try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    const filename = hostPath.split("/").pop() || "source.js";
    const r = await lxUploadScript(filename, body.script || "");
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("NAS 选择失败：" + exc.message, "fail");
  }
}
$("#lx-pick").addEventListener("click", lxPickFromNas);

(async function detectNasPicker() {
  // 桌面网关路径下才尝试加载 SDK；探测失败（直连 8774）保持隐藏
  const pathname = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
  if (pathname.startsWith("/app/")) {
    const sdk = await lxLoadTrimSdk();
    if (sdk) $("#lx-pick").hidden = false;
  }
})();

/* -------------------------------------------------------------- 表单脏标记 */
["#tee-dir", "#tee-max", "#llm-base", "#llm-key", "#llm-model", "#lx-url", "#search-timeout", "#bind-timeout", "#handoff-max", "#scan-path"].forEach((sel) =>
  $(sel).addEventListener("input", () => markDirty()));
$$("input[name=quality]").forEach((el) => el.addEventListener("change", () => markDirty("音质偏好需保存后生效")));
["#recommend-hot", "#recommend-daily", "#search-probe", "#tee-enabled", "#fav-autobind", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists"].forEach((sel) =>
  $(sel).addEventListener("change", () => markDirty()));

function updateTeeCountLabel() {
  const n = $("#tee-max").value || configValues.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#tee-count-label").textContent = `（缓存数 ${n} 首）`;
}
$("#tee-max").addEventListener("input", updateTeeCountLabel);

function updateBindTimeoutLabel() {
  const n = $("#bind-timeout").value || configValues.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#bind-timeout-label").textContent = `（${n} 秒）`;
}
$("#bind-timeout").addEventListener("input", updateBindTimeoutLabel);

window.addEventListener("beforeunload", (ev) => {
  if (dirty) ev.preventDefault();
});

/* -------------------------------------------------------------- 版本与升级
 *
 * 纯提示型：检测失败、新版本不下载、页面不跳转，功能完全不受影响。
 * 唯一的"动作"是用户主动点按钮打开发行页（target=_blank 交给系统浏览器）。
 */
let versionInfo = null;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (!v) return "";
  if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + " MB";
  if (v >= 1024) return (v / 1024).toFixed(0) + " KB";
  return v + " B";
}

function openExternal(url) {
  if (!url) return toast("没有可用的链接", "fail");
  // 交给系统浏览器打开；App/WebView 场景下新标签最稳，失败则退回当前页跳转
  const w = window.open(url, "_blank", "noopener");
  if (!w) window.location.href = url;
}

/* 选安装包。
 *
 * 踩坑：远端同时挂 `fnmusic-ext-2.6.17.fpk`（离线完整包，约 405MB）
 * 和 `fnmusic-ext-2.6.17-online.fpk`（在线包，约 5MB，装机时现拉 Docker 镜像）。
 * 早先写的是 assets.find(name.endsWith(".fpk"))，而 "-online.fpk" 同样以 .fpk 结尾，
 * find() 取第一个就会命中 online 包 —— 用户的 NAS 拉不到 Docker 仓库时装不上。
 * 这里改成显式分类，并把"离线完整包"作为默认（唯一在无外网镜像时仍能装的选项）。
 */
function pickFpk(assets, version) {
  const list = (assets || []).filter((a) => /\.fpk$/i.test(String(a.name)));
  if (!list.length) return { offline: null, online: null };
  const isOnline = (a) => /-online\.fpk$/i.test(String(a.name));
  const matchesVer = (a) => !version || String(a.name).includes(String(version));
  // 离线包 = 名字里没有 -online 标记，且文件名里就是目标版本号
  const offline = list.find((a) => !isOnline(a) && matchesVer(a))
    || list.find((a) => !isOnline(a))
    || null;
  const online = list.find((a) => isOnline(a) && matchesVer(a))
    || list.find(isOnline)
    || null;
  return { offline, online };
}

function fpkLabel(a) {
  if (!a) return "";
  const name = String(a.name);
  return /-online\.fpk$/i.test(name) ? "在线包" : "离线完整包";
}

async function loadVersion(silent) {
  const btn = $("#ver-check");
  if (btn) { btn.disabled = true; btn.textContent = "检查中…"; }
  try {
    const v = await api("/api/version");
    versionInfo = v;
    renderVersion(v);
  } catch (exc) {
    if (!silent) toast("版本检查失败：" + exc.message, "fail");
    const state = $("#ver-state");
    if (state) state.innerHTML = `<span class="state-line"><span class="dot err"></span>检查失败：${esc(exc.message)}（不影响使用）</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "检查更新"; }
  }
}

function renderVersion(v) {
  // 当前版本
  const cur = $("#ver-current");
  if (cur) {
    cur.innerHTML = `<div class="kv"><b>已安装</b>v${esc(v.current)}</div>`;
  }

  // 侧边栏版本号旁的升级角标（点击直达发行页看升级点）
  const badge = $("#brand-upgrade");
  if (badge) {
    badge.hidden = !v.has_update;
    if (v.has_update) {
      badge.innerHTML = `<span class="ver-dot"></span>新版本 ${esc(v.latest)}`;
      badge.title = `发现新版本 v${v.latest}，点击查看升级内容`;
    }
  }
  // 窄屏侧边栏隐藏，用底部导航的「版本」项小圆点兜底提示
  const navDot = $("#nav-upgrade");
  if (navDot) navDot.hidden = !v.has_update;

  const state = $("#ver-state");
  if (state) {
    if (!v.checked) {
      state.innerHTML = `<span class="state-line"><span class="dot warn"></span>${esc(v.error || "未检查到版本源")}</span>`;
    } else if (v.has_update) {
      state.innerHTML =
        `<span class="state-line"><span class="dot warn"></span>发现新版本 v${esc(v.latest)}（当前 v${esc(v.current)}）</span>`;
    } else {
      state.innerHTML =
        `<span class="state-line"><span class="dot ok"></span>已是最新版本（v${esc(v.current)}）</span>`;
    }
    // 主版本源不可用时必须如实说明。
    // 这不是可选的礼貌提示：GitHub 匿名 API 很容易限流，若此时镜像又落后，
    // 用户会看到"已是最新"却其实有新版 —— 那是在拿错误的结论误导人。
    const notices = v.notices || [];
    if (notices.length) {
      state.innerHTML += notices
        .map((n) => `<span class="state-line"><span class="dot warn"></span>${esc(n)}</span>`)
        .join("");
    }
  }

  // 有更新才显示更新卡片
  const card = $("#ver-update-card");
  if (card) {
    card.hidden = !v.has_update;
    if (v.has_update) {
      const rows = [`<div class="kv"><b>最新版本</b>v${esc(v.latest)}</div>`];
      if (v.published) rows.push(`<div class="kv"><b>发布时间</b>${esc(String(v.published).slice(0, 10))}</div>`);
      if (v.source) rows.push(`<div class="kv"><b>版本源</b>${esc(v.source)}</div>`);
      const { offline, online } = pickFpk(v.assets, v.latest);
      // 离线包优先：唯一在拉不到 Docker 镜像时仍能装成功的选项
      if (offline) {
        const size = fmtBytes(offline.size);
        rows.push(`<div class="kv"><b>安装包</b>${esc(offline.name)}${size ? "（" + size + "，" + fpkLabel(offline) + "）" : "（" + fpkLabel(offline) + "）"}</div>`);
      }
      if (online) {
        const size = fmtBytes(online.size);
        rows.push(`<div class="kv"><b>备选</b>${esc(online.name)}${size ? "（" + size + "，" + fpkLabel(online) + "）" : "（" + fpkLabel(online) + "）"}</div>`);
      }
      if (!offline && !online) rows.push(`<div class="kv"><b>安装包</b>远端未附带 .fpk，请前往发行页手动下载</div>`);
      $("#ver-update").innerHTML = rows.join("");

      // 只有存在离线完整包时才允许一键升级：online 包要现拉 Docker 镜像，装不上
      const btn = $("#ver-install");
      if (btn) {
        btn.disabled = !offline;
        btn.title = offline ? "" : "远端未提供离线完整安装包，请前往发行页手动下载安装";
      }
    }
  }

  // 升级点明细（来自本地 CHANGELOG，仅含高于当前版本的段落）
  const clCard = $("#ver-changelog-card");
  const cl = $("#ver-changelog");
  if (clCard && cl) {
    const groups = v.changelog || [];
    clCard.hidden = groups.length === 0;
    cl.innerHTML = groups.map((g) => {
      const items = g.items.map((it) => `<li>${esc(it.text)}</li>`).join("");
      const more = g.total > g.items.length ? `<li class="muted">…… 另有 ${g.total - g.items.length} 项，见发行页</li>` : "";
      return `<div class="subpanel"><div class="kv"><b>v${esc(g.version)}</b>${esc(g.date || "")}</div><ul>${items}${more}</ul></div>`;
    }).join("");
  }
}

$("#ver-check") && $("#ver-check").addEventListener("click", () => loadVersion(false));
$("#brand-upgrade") && $("#brand-upgrade").addEventListener("click", () => {
  const v = versionInfo || {};
  openExternal(v.url || v.releases_page || "https://github.com/haonanren118/fnos_music_ext/releases");
});
$("#ver-releases") && $("#ver-releases").addEventListener("click", () => {
  openExternal((versionInfo && versionInfo.releases_page) || "https://github.com/haonanren118/fnos_music_ext/releases");
});
$("#ver-notes-btn") && $("#ver-notes-btn").addEventListener("click", () => {
  const v = versionInfo || {};
  openExternal(v.url || v.releases_page || "https://github.com/haonanren118/fnos_music_ext/releases");
});
$("#ver-install") && $("#ver-install").addEventListener("click", startUpgrade);

/* -------------------------------------------------------------- 一键升级 */
let upgradePoll = null;

function upgradeUI(stage, text, percent) {
  const hint = $("#ver-install-hint");
  if (!hint) return;
  const st = window.__upgradeState || {};
  if (stage === "downloading" || stage === "verifying") {
    // 整百分比在慢速下长时间不动，看着像卡死。补上已下载量、速度、剩余时间，
    // 让"在动"这件事看得见。
    const p = Number(percent || 0);
    const got = Number(st.received || 0), total = Number(st.total || 0);
    const spd = Number(st.speed || 0), eta = Number(st.eta || 0);
    const mb = (n) => (n / 1048576).toFixed(1);
    const parts = [`正在下载并校验安装包… ${p}%`];
    if (total) parts.push(`${mb(got)} / ${mb(total)} MB`);
    if (spd > 0) parts.push(`${Math.round(spd / 1024)} KB/s`);
    if (eta > 0) parts.push(`约剩 ${eta < 60 ? eta + " 秒" : Math.ceil(eta / 60) + " 分钟"}`);
    if (st.source) parts.push(`来源：${st.source}`);
    hint.innerHTML = `${esc(parts.join("　"))}　请勿关闭页面`;
  } else if (stage === "ready") {
    // 包已完整落在宿主目录。**路径必须原样显示**：后端下发的 host_dir/file_name
    // 才是真实落盘位置，之前这里自己拼了个文件名，与实际落盘不一致，
    // 用户照着指引找会找不到文件。改成直接用后端给的路径。
    const dir = st.host_dir || "";
    const fname = st.file_name || "";
    hint.innerHTML = `<div class="up-ready">${esc(text || "安装包已就位")}
      <div class="up-ready-path">安装包位置：<code>${esc(dir ? dir + "/" + fname : fname)}</code></div>
      <ol>
        <li>打开飞牛「<b>应用中心</b>」→「我的应用」</li>
        <li>点右上角<b>「手动安装」</b>（需先在侧边栏底部开启该功能）</li>
        <li>选择上面这个文件完成升级</li>
      </ol>
      <div class="up-ready-tip">已通过官方 sha256 校验，可放心安装；用「文件」App 打开上面这个目录也能直接找到它。</div>
    </div>`;
  } else if (stage === "failed") {
    hint.innerHTML = `<span style="color:var(--err)">下载失败：${esc(text || "未知错误")}</span>　可前往<a href="${esc(fallbackUrl())}" target="_blank" rel="noopener">发行页</a>手动下载`;
  } else if (stage === "interrupted") {
    hint.innerHTML = `<span style="color:var(--warn)">${esc(text || "上次下载被中断")}</span>　可重新发起`;
  } else {
    hint.innerHTML = "「下载安装包」会自动从官方发行页拉取并校验安装包到本机；"
      + "飞牛系统未提供自动安装接口，最后一步需要在应用中心点「手动安装」完成。"
      + "不升级的话当前版本功能完全不受影响。";
  }
}

function fallbackUrl() {
  const v = versionInfo || {};
  const { offline, online } = pickFpk(v.assets, v.latest);
  return (offline || online || {}).url || v.url || v.releases_page || "";
}

function stopUpgradePoll() {
  if (upgradePoll) { clearInterval(upgradePoll); upgradePoll = null; }
}

async function pollUpgrade() {
  let s;
  try {
    s = await api("/api/upgrade/state");
  } catch (e) {
    // 安装期间 WebUI 会被重启打断，这里静默重试即可
    return;
  }
  if (!s || !s.stage) return;
  // upgradeUI 要用完整状态（received/total/speed/eta/host_dir/file_name/source），
  // 签名只传了三个参数，其余字段挂在这里取。
  window.__upgradeState = s;
  upgradeUI(s.stage, s.error || s.message, s.percent);
  if (s.stage === "ready") {
    stopUpgradePoll();
    toast("安装包已下载完成", "ok");
    const btn = $("#ver-install");
    if (btn) { btn.disabled = false; btn.textContent = "重新下载安装包"; }
  } else if (s.stage === "failed" || s.stage === "interrupted") {
    stopUpgradePoll();
    const btn = $("#ver-install");
    if (btn) { btn.disabled = false; btn.textContent = "下载安装包"; }
  } else if (s.stage === "starting" || s.stage === "downloading"
             || s.stage === "verifying") {
    const btn = $("#ver-install");
    if (btn) { btn.disabled = true; btn.textContent = "下载中…"; }
  }
}

async function startUpgrade() {
  const v = versionInfo || {};
  if (!v.has_update || !v.latest) return toast("没有可安装的新版本", "fail");
  if (upgradePoll) return;
  const size = fmtBytes((pickFpk(v.assets, v.latest).offline || {}).size) || "数百 MB";
  // 说清楚到底会发生什么：只下载，不动已装的应用
  if (!confirm(
    `下载 v${v.latest} 的官方安装包？\n\n` +
    `将从官方发行页下载并校验安装包（约 ${size}），存到本机应用目录下。\n` +
    `飞牛系统没有自动安装接口，装上这一步仍需你在「应用中心 → 手动安装」里点一下。\n` +
    `当前版本不会受到任何影响，下载完不装也完全没问题。`
  )) return;

  const btn = $("#ver-install");
  if (btn) { btn.disabled = true; btn.textContent = "启动中…"; }
  window.__upgradeState = {};   // 清掉上一轮的 received/speed，避免闪现旧数字
  upgradeUI("downloading", "", 0);
  upgradePoll = setInterval(pollUpgrade, 1500);
  try {
    const r = await api("/api/upgrade/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: v.latest }),
    });
    if (r && r.ok === false) throw new Error(r.error || "发起失败");
  } catch (e) {
    stopUpgradePoll();
    if (btn) { btn.disabled = false; btn.textContent = "下载安装包"; }
    upgradeUI("failed", e.message);
    toast("无法发起下载：" + e.message, "fail");
  }
}

// 页面加载时若上次下载正在跑，接上轮询（刷新页面不丢进度）
(async function resumeUpgrade() {
  try {
    const s = await api("/api/upgrade/state");
    const running = s && ["starting", "downloading", "verifying"].includes(s.stage);
    if (running) {
      upgradePoll = setInterval(pollUpgrade, 1500);
      pollUpgrade();
    } else if (s && (s.stage === "ready" || s.stage === "failed" || s.stage === "interrupted")) {
      // ready 也要重绘：刷新页面后引导卡还得在，别让用户以为白下了
      window.__upgradeState = s;
      upgradeUI(s.stage, s.error || s.message, s.percent);
    }
  } catch (e) { /* 忽略 */ }
})();

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  await loadConfig();
  await loadStatus();
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  // 版本检测不阻塞启动：静默失败无所谓，用户可随时手动点「检查更新」
  loadVersion(true);
  setInterval(loadStatus, 15000);
})();
