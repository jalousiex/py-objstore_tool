/* objstore_tool 前端逻辑
 * 纯原生 JS，无框架、无构建步骤 —— 单机工具的界面不值得再引一层工具链。
 */

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  meta: null,
  connections: [],
  activeId: null,
  path: '',
  parent: null,
  rootPath: '',
  entries: [],
  supportsPresign: false,
  sort: { key: 'name', dir: 1 },
  filter: '',
  loadingCount: 0,
  selected: new Set(), // 勾选的条目路径（复制 / 移动用），换目录即清空
  favorites: { remote: {}, local: [] }, // 收藏夹（服务端配置，启动时拉一次）
};

/* 本地目录栏（右侧） */
const local = {
  path: '',
  parent: null,
  entries: [],
  selected: new Set(), // 勾选的本地条目路径（拖拽上传用），换目录即清空
};

/* 拖拽自定义数据类型：区分「面板间拖拽」与「从资源管理器拖入的 OS 文件」 */
const DRAG_LOCAL = 'application/x-objstore-local';   // 本地栏文件 / 目录 → 远端上传（JSON，含勾选的整批）
const DRAG_REMOTE = 'application/x-objstore-remote'; // 远端文件 / 目录 → 本地栏下载（JSON，含勾选的整批）

/* ------------------------------------------------------------------ */
/* 收藏夹：桶 / 目录快捷跳转                                             */
/* 存服务端配置（不是 localStorage）—— 换浏览器 / 换机器 / 拷备份都能继承；  */
/* 远端收藏按连接 id 分组，切连接只看到自己那组。                          */
/* ------------------------------------------------------------------ */
const FAV_REMOTE_KEY = 'objstore.remoteFavorites'; // 只是旧版遗留键，用来做一次性搬迁
const FAV_LOCAL_KEY = 'objstore.localFavorites';
const FAV_MANAGE = '__manage__';

function readStore(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    const value = raw ? JSON.parse(raw) : fallback;
    return value === null || value === undefined ? fallback : value;
  } catch { return fallback; }
}

async function loadFavorites() {
  const data = await apiGet('/api/favorites');
  state.favorites = data.favorites || { remote: {}, local: [] };
  await migrateLegacyFavorites();
}

/* 老版本收藏夹存在浏览器里：服务端还是空的时候就一次性搬上去，搬完清掉遗留键 */
async function migrateLegacyFavorites() {
  const empty = !Object.keys(state.favorites.remote || {}).length && !(state.favorites.local || []).length;
  if (!empty) return;
  const legacyRemote = readStore(FAV_REMOTE_KEY, {});
  const legacyLocal = readStore(FAV_LOCAL_KEY, []);
  const hasRemote = legacyRemote && typeof legacyRemote === 'object' && Object.keys(legacyRemote).length > 0;
  if (!hasRemote && !(Array.isArray(legacyLocal) && legacyLocal.length)) return;

  state.favorites = {
    remote: hasRemote ? legacyRemote : {},
    local: Array.isArray(legacyLocal) ? legacyLocal : [],
  };
  try {
    await apiPost('/api/favorites', state.favorites);
    localStorage.removeItem(FAV_REMOTE_KEY);
    localStorage.removeItem(FAV_LOCAL_KEY);
    toast('已把浏览器里的收藏夹搬进配置（换浏览器也能看到了）', 'ok');
  } catch { /* 搬不过去就先留着，下次启动再试 */ }
}

/* 改完立刻回传整份（收藏夹很小）；失败只提示，界面照旧可用 */
function saveFavorites() {
  return apiPost('/api/favorites', state.favorites).catch((err) => {
    toast('收藏夹保存失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    throw err;
  });
}

function remoteFavs() {
  const list = state.activeId ? (state.favorites.remote || {})[state.activeId] : null;
  return Array.isArray(list) ? list : [];
}

function setRemoteFavs(list) {
  if (!state.activeId) return;
  if (!state.favorites.remote) state.favorites.remote = {};
  if (list.length) state.favorites.remote[state.activeId] = list;
  else delete state.favorites.remote[state.activeId];
  saveFavorites().catch(() => { /* 已经提示过，界面不回滚（下次改动会带上正确内容） */ });
}

function localFavs() {
  return Array.isArray(state.favorites.local) ? state.favorites.local : [];
}

function setLocalFavs(list) {
  state.favorites.local = list;
  saveFavorites().catch(() => { /* 同上 */ });
}

/* 收藏 / 取消收藏；返回 true 表示这次是新增 */
function toggleFav(list, item, save) {
  const index = list.findIndex((f) => f.path === item.path);
  if (index >= 0) list.splice(index, 1);
  else list.push(item);
  save(list);
  return index < 0;
}

/* 同名收藏（不同桶下的 logs 之类）带上路径，免得下拉里分不清 */
function favLabel(fav, all) {
  const dup = all.filter((f) => f.name === fav.name).length > 1;
  return dup ? `${fav.name}（${fav.path}）` : fav.name;
}

function renderFavSelect(select, favs) {
  select.innerHTML = '';
  const head = document.createElement('option');
  head.value = '';
  head.textContent = favs.length ? `收藏（${favs.length}）` : '收藏夹';
  select.appendChild(head);

  favs.forEach((fav) => {
    const opt = document.createElement('option');
    opt.value = fav.path;
    opt.textContent = favLabel(fav, favs);
    opt.title = fav.path;
    select.appendChild(opt);
  });

  const manage = document.createElement('option');
  manage.value = FAV_MANAGE;
  manage.textContent = '管理收藏…';
  select.appendChild(manage);

  select.value = '';
}

function favStar(on, onToggle, title) {
  const btn = document.createElement('button');
  btn.className = `fav-star${on ? ' on' : ''}`;
  btn.textContent = on ? '★' : '☆';
  btn.title = title || (on ? '取消收藏' : '收藏该目录');
  btn.addEventListener('click', (e) => { e.stopPropagation(); onToggle(); });
  return btn;
}

function basenameOf(path) {
  return String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;
}

/* 收藏夹管理弹窗：跳转 + 删除（列表里只能加，删在这里） */
function openFavManager(title, getFavs, onJump, onRemove) {
  const favs = getFavs();
  const body = document.createElement('div');

  if (!favs.length) {
    const hint = document.createElement('div');
    hint.className = 'field-hint';
    hint.textContent = '还没有收藏。在目录列表里点 ☆（或工具栏的 ☆）即可把常用目录收进来。';
    body.appendChild(hint);
  }

  favs.forEach((fav) => {
    const row = document.createElement('div');
    row.className = 'fav-row';

    const main = document.createElement('div');
    main.className = 'fav-row-main';
    const name = document.createElement('div');
    name.textContent = fav.name;
    const path = document.createElement('div');
    path.className = 'fav-row-path';
    path.textContent = fav.path;
    main.append(name, path);
    row.appendChild(main);

    row.appendChild(makeButton('跳转', 'btn-sm', () => { closeModal(); onJump(fav); }));
    row.appendChild(makeButton('删除', 'btn-sm btn-danger', () => {
      onRemove(fav);
      openFavManager(title, getFavs, onJump, onRemove);
    }));
    body.appendChild(row);
  });

  openModal({
    title,
    body,
    footer: [makeButton('关闭', '', closeModal)],
    width: '520px',
  });
}

class ApiError extends Error {
  constructor(message, detail) {
    super(message);
    this.detail = detail || '';
  }
}

/* ------------------------------------------------------------------ */
/* HTTP                                                                */
/* ------------------------------------------------------------------ */
async function handleResponse(res) {
  const text = await res.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 300) }; }
  }
  if (!res.ok) throw new ApiError(data.error || `请求失败（HTTP ${res.status}）`, data.detail);
  return data;
}

async function apiGet(path, params) {
  const url = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null) url.searchParams.set(k, v);
  });
  return handleResponse(await fetch(url));
}

async function apiPost(path, body) {
  return handleResponse(await fetch(new URL(path, location.origin), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }));
}

/* ------------------------------------------------------------------ */
/* 通用 UI                                                             */
/* ------------------------------------------------------------------ */
function toast(message, kind = '', detail = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  const main = document.createElement('div');
  main.textContent = message;
  el.appendChild(main);
  if (detail) {
    const sub = document.createElement('div');
    sub.className = 'toast-detail';
    sub.textContent = detail;
    el.appendChild(sub);
  }
  $('#toast-root').appendChild(el);
  setTimeout(() => el.remove(), kind === 'fail' ? 9000 : 3800);
}

function setLoading(on) {
  state.loadingCount = Math.max(0, state.loadingCount + (on ? 1 : -1));
  $('#loading').hidden = state.loadingCount === 0;
}

function setStatus(text) {
  $('#statusbar').textContent = text;
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function iconFor(entry) {
  if (entry.is_dir) return '📁';
  const ext = entry.name.includes('.') ? entry.name.split('.').pop().toLowerCase() : '';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'].includes(ext)) return '🖼';
  if (['parquet', 'orc', 'avro'].includes(ext)) return '📊';
  if (['csv', 'tsv', 'json', 'jsonl', 'ndjson', 'xlsx', 'xls'].includes(ext)) return '📈';
  if (['zip', 'gz', 'tar', '7z', 'rar', 'bz2', 'xz', 'zst'].includes(ext)) return '🗜';
  if (['txt', 'log', 'md', 'sql', 'py', 'js', 'ts', 'sh', 'yaml', 'yml', 'xml', 'conf', 'ini'].includes(ext)) return '📄';
  return '📦';
}

function joinPath(base, name) {
  if (!base) return name;
  return `${base.replace(/\/+$/, '')}/${name}`;
}

/* ------------------------------------------------------------------ */
/* 地址行（两栏共用）：分段可点跳转；点「当前段 / 右侧空白」切成输入框     */
/* Enter 提交、Esc 或失焦取消；提交后由调用方重新渲染回分段形式            */
/* ------------------------------------------------------------------ */
function renderPathBar(host, opts) {
  host.innerHTML = '';
  host.classList.toggle('editable', typeof opts.onSubmit === 'function');

  if (!opts.segments.length) {
    const hint = document.createElement('span');
    hint.className = 'path-hint';
    hint.textContent = opts.emptyHint || '';
    hint.addEventListener('click', () => beginPathEdit(host, opts));
    host.appendChild(hint);
    return;
  }

  opts.segments.forEach((seg, index) => {
    if (index) {
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.textContent = '/';
      host.appendChild(sep);
    }
    const btn = document.createElement('button');
    const isLast = index === opts.segments.length - 1;
    btn.className = 'crumb' + (isLast ? ' current' : '');
    btn.textContent = seg.name;
    btn.title = isLast ? `${seg.path}（点击可编辑路径）` : seg.path;
    if (isLast) btn.addEventListener('click', () => beginPathEdit(host, opts));
    else btn.addEventListener('click', () => opts.onJump(seg.path));
    host.appendChild(btn);
  });

  // 分段之后的空白区也是编辑热区（路径很短时点这里就能改）
  const pad = document.createElement('div');
  pad.className = 'path-pad';
  pad.title = '点击编辑路径';
  pad.addEventListener('click', () => beginPathEdit(host, opts));
  host.appendChild(pad);
}

function beginPathEdit(host, opts) {
  if (typeof opts.onSubmit !== 'function') return;
  host.innerHTML = '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'path-input';
  input.value = opts.current || '';
  input.placeholder = opts.placeholder || '';
  input.spellcheck = false;
  input.autocomplete = 'off';

  let settled = false;
  const cancel = () => {
    if (settled) return;
    settled = true;
    opts.render(); // 退回分段形式
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const value = input.value.trim();
      settled = true;
      if (!value || value === opts.current) return opts.render();
      return opts.onSubmit(value); // 成功后由调用方重新渲染
    }
    if (e.key === 'Escape') {
      e.stopPropagation(); // 别让 Esc 顺带把弹窗关掉
      cancel();
    }
    return undefined;
  });
  input.addEventListener('blur', cancel);
  host.appendChild(input);
  input.focus();
  input.select();
}

/* ------------------------------------------------------------------ */
/* 弹窗                                                                */
/* ------------------------------------------------------------------ */
function closeModal() {
  $('#modal-root').innerHTML = '';
}

function openModal({ title, body, footer, width }) {
  closeModal();
  const mask = document.createElement('div');
  mask.className = 'modal-mask';

  const box = document.createElement('div');
  box.className = 'modal';
  if (width) box.style.width = width;

  const head = document.createElement('div');
  head.className = 'modal-head';
  const h = document.createElement('span');
  h.textContent = title;
  head.appendChild(h);
  const close = document.createElement('button');
  close.className = 'icon-btn';
  close.textContent = '✕';
  close.addEventListener('click', closeModal);
  head.appendChild(close);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'modal-body';
  if (typeof body === 'string') bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);

  const foot = document.createElement('div');
  foot.className = 'modal-foot';
  (footer || []).forEach((btn) => foot.appendChild(btn));

  box.append(head, bodyEl, foot);
  mask.appendChild(box);
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) closeModal(); });
  $('#modal-root').appendChild(mask);
  return { mask, box, bodyEl, foot };
}

function makeButton(label, cls, onClick) {
  const btn = document.createElement('button');
  btn.className = `btn ${cls || ''}`.trim();
  btn.textContent = label;
  btn.addEventListener('click', onClick);
  return btn;
}

async function confirmDialog(title, message, danger = true) {
  return new Promise((resolve) => {
    const body = document.createElement('div');
    body.textContent = message;
    body.style.lineHeight = '1.6';

    const okBtn = makeButton('确认', danger ? 'btn-danger' : 'btn-primary', () => { closeModal(); resolve(true); });
    const cancelBtn = makeButton('取消', '', () => { closeModal(); resolve(false); });
    openModal({ title, body, footer: [cancelBtn, okBtn], width: '440px' });
  });
}

/* ------------------------------------------------------------------ */
/* 连接                                                                */
/* ------------------------------------------------------------------ */
async function loadConnections() {
  const data = await apiGet('/api/connections');
  state.connections = data.connections || [];
  renderConnections();
}

function renderConnections() {
  const list = $('#conn-list');
  list.innerHTML = '';

  if (!state.connections.length) {
    const li = document.createElement('li');
    li.className = 'sidebar-foot-hint';
    li.style.padding = '10px 8px';
    li.textContent = '还没有连接，点右上角 ＋ 新建一个。';
    list.appendChild(li);
    return;
  }

  state.connections.forEach((conn) => {
    const li = document.createElement('li');
    li.className = 'conn-item' + (conn.id === state.activeId ? ' active' : '');

    const dot = document.createElement('span');
    dot.className = `conn-dot ${conn.type}`;

    const body = document.createElement('div');
    body.className = 'conn-body';
    const name = document.createElement('span');
    name.className = 'conn-name';
    name.textContent = conn.name;
    const sub = document.createElement('span');
    sub.className = 'conn-sub';
    sub.textContent = conn.endpoint;
    body.append(name, sub);

    const actions = document.createElement('div');
    actions.className = 'conn-actions';

    const testBtn = document.createElement('button');
    testBtn.className = 'icon-btn';
    testBtn.title = '测试连接';
    testBtn.textContent = '⚡';
    testBtn.addEventListener('click', (e) => { e.stopPropagation(); quickTest(conn); });

    const editBtn = document.createElement('button');
    editBtn.className = 'icon-btn';
    editBtn.title = '编辑';
    editBtn.textContent = '✎';
    editBtn.addEventListener('click', (e) => { e.stopPropagation(); openConnectionModal(conn); });

    const copyBtn = document.createElement('button');
    copyBtn.className = 'icon-btn';
    copyBtn.title = '复制连接（生成新连接，可改名改地址）';
    copyBtn.textContent = '⧉';
    copyBtn.addEventListener('click', (e) => { e.stopPropagation(); duplicateConnection(conn); });

    const delBtn = document.createElement('button');
    delBtn.className = 'icon-btn';
    delBtn.title = '删除';
    delBtn.textContent = '🗑';
    delBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const ok = await confirmDialog('删除连接', `确定删除连接「${conn.name}」吗？该操作只影响本地配置，不会动远端数据。`);
      if (!ok) return;
      try {
        await apiPost('/api/connections/delete', { id: conn.id });
        if (state.activeId === conn.id) {
          state.activeId = null;
          state.entries = [];
          renderFiles();
          renderBreadcrumb();
        }
        await loadConnections();
        toast('已删除连接', 'ok');
      } catch (err) {
        toast('删除失败', 'fail', err.message);
      }
    });

    actions.append(testBtn, editBtn, copyBtn, delBtn);
    li.append(dot, body, actions);
    li.addEventListener('click', () => openConnection(conn.id));
    list.appendChild(li);
  });
}

async function quickTest(conn) {
  setStatus(`正在测试 ${conn.name} …`);
  try {
    const data = await apiPost('/api/connections/test', { id: conn.id });
    toast(`${conn.name}：${data.message}`, 'ok');
  } catch (err) {
    toast(`${conn.name} 连接失败`, 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  } finally {
    setStatus('就绪');
  }
}

/* ---- 复制连接：取明文凭据另存为新 id 的连接，成功后直接打开编辑弹窗 ---- */
async function duplicateConnection(conn) {
  try {
    const data = await apiGet('/api/connections', { reveal: conn.id });
    const full = (data.connections || []).find((c) => c.id === conn.id);
    if (!full) throw new Error('取不到连接明文');
    const payload = { ...full };
    delete payload.id;
    delete payload.root_path;
    delete payload.supports_presign;
    payload.name = `${conn.name} 副本`;
    const saved = await apiPost('/api/connections', payload);
    await loadConnections();
    toast(saved.message || '已复制连接', 'ok');
    const created = state.connections.find((c) => c.name === payload.name);
    if (created) openConnectionModal(created);
  } catch (err) {
    toast('复制连接失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  }
}

/* ---- 新建 / 编辑连接 ---- */
function buildConnectionForm(typeName, values) {
  const spec = state.meta.connection_types[typeName];
  const wrap = document.createElement('div');

  spec.fields.forEach((field) => {
    const div = document.createElement('div');
    div.className = 'field';

    if (field.type === 'bool') {
      div.className = 'field field-check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.id = `f-${field.key}`;
      input.checked = values[field.key] !== undefined ? Boolean(values[field.key]) : Boolean(field.default);
      const label = document.createElement('label');
      label.htmlFor = input.id;
      label.textContent = field.label;
      div.append(input, label);
      wrap.appendChild(div);
      return;
    }

    const label = document.createElement('label');
    label.textContent = field.label;
    if (field.required) {
      const star = document.createElement('span');
      star.className = 'req';
      star.textContent = '*';
      label.appendChild(star);
    }
    label.htmlFor = `f-${field.key}`;
    div.appendChild(label);

    let input;
    if (field.type === 'select') {
      input = document.createElement('select');
      (field.options || []).forEach((opt) => {
        const o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.label;
        input.appendChild(o);
      });
      input.value = values[field.key] ?? field.default ?? '';
    } else {
      input = document.createElement('input');
      input.type = field.type === 'password' ? 'password' : 'text';
      input.value = values[field.key] ?? (field.default ?? '');
      if (field.placeholder) input.placeholder = field.placeholder;
      input.autocomplete = 'off';
    }
    input.id = `f-${field.key}`;
    input.dataset.key = field.key;
    input.dataset.kind = field.type;
    div.appendChild(input);

    if (field.hint || field.placeholder) {
      const hint = document.createElement('div');
      hint.className = 'field-hint';
      hint.textContent = field.hint || field.placeholder;
      div.appendChild(hint);
    }
    wrap.appendChild(div);
  });

  return wrap;
}

function collectForm(container) {
  const payload = {};
  $$('[data-key]', container).forEach((el) => {
    const key = el.dataset.key;
    if (el.dataset.kind === 'bool') payload[key] = el.checked;
    else payload[key] = el.value;
  });
  return payload;
}

async function openConnectionModal(conn) {
  const types = state.meta.connection_types;
  let currentType = conn ? conn.type : Object.keys(types)[0];
  let values = conn ? { ...conn } : {};

  if (conn) {
    try {
      const data = await apiGet('/api/connections', { reveal: conn.id });
      const full = (data.connections || []).find((c) => c.id === conn.id);
      if (full) values = full;
    } catch { /* 取不到明文就沿用掩码值 */ }
  }

  const body = document.createElement('div');
  const tabs = document.createElement('div');
  tabs.className = 'type-tabs';
  const formHost = document.createElement('div');
  const result = document.createElement('div');

  const renderForm = () => {
    formHost.innerHTML = '';
    formHost.appendChild(buildConnectionForm(currentType, values));
  };

  Object.entries(types).forEach(([key, spec]) => {
    const tab = document.createElement('div');
    tab.className = 'type-tab' + (key === currentType ? ' active' : '');
    const strong = document.createElement('div');
    strong.textContent = spec.label;
    const small = document.createElement('small');
    small.textContent = spec.hint || '';
    tab.append(strong, small);
    tab.addEventListener('click', () => {
      currentType = key;
      values = {};
      $$('.type-tab', tabs).forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      renderForm();
    });
    tabs.appendChild(tab);
  });

  renderForm();
  body.append(tabs, formHost, result);

  const testBtn = makeButton('测试连接', '', async () => {
    const payload = { ...collectForm(formHost), type: currentType };
    if (conn) payload.id = conn.id;
    result.className = 'test-result';
    result.textContent = '正在测试…';
    try {
      const data = await apiPost('/api/connections/test', payload);
      result.className = 'test-result ok';
      result.textContent = data.message || '连接成功';
    } catch (err) {
      result.className = 'test-result fail';
      result.textContent = err.message + (err.detail ? `\n${err.detail}` : '');
    }
  });

  const saveBtn = makeButton(conn ? '保存' : '创建', 'btn-primary', async () => {
    const payload = { ...collectForm(formHost), type: currentType };
    if (conn) payload.id = conn.id;
    try {
      const data = await apiPost('/api/connections', payload);
      closeModal();
      await loadConnections();
      toast(data.message || '已保存', 'ok');
    } catch (err) {
      toast('保存失败', 'fail', err.message);
    }
  });

  openModal({
    title: conn ? `编辑连接：${conn.name}` : '新建连接',
    body,
    footer: [makeButton('取消', '', closeModal), testBtn, saveBtn],
    width: '560px',
  });
}

/* ------------------------------------------------------------------ */
/* 浏览                                                                */
/* ------------------------------------------------------------------ */
async function openConnection(id) {
  const conn = state.connections.find((c) => c.id === id);
  if (!conn) return;
  state.activeId = id;
  state.rootPath = conn.root_path ?? '';
  state.filter = '';
  $('#filter-input').value = '';
  renderConnections();
  await browse(state.rootPath);
}

async function browse(path) {
  if (!state.activeId) return;
  setLoading(true);
  setStatus('加载中…');
  try {
    const data = await apiGet('/api/list', { conn: state.activeId, path });
    state.path = data.path;
    state.parent = data.parent;
    state.entries = data.entries || [];
    state.supportsPresign = Boolean(data.supports_presign);
    clearSelection(); // 换了目录，之前的勾选不再有意义
    renderBreadcrumb();
    renderFiles();
    updateToolbarState();
    setStatus(`${state.path || '根'} · ${state.entries.length} 项`);
  } catch (err) {
    state.entries = [];
    renderBreadcrumb(); // 失败时退回当前目录的分段形式
    renderFiles();
    toast('读取目录失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    setStatus('读取失败');
  } finally {
    setLoading(false);
  }
}

function updateToolbarState() {
  const ready = Boolean(state.activeId);
  const atRoot = ready && state.path === state.rootPath;
  $('#btn-refresh').disabled = !ready;
  $('#btn-upload').disabled = !ready || atRoot;
  $('#btn-mkdir').disabled = !ready || atRoot;
  $('#btn-upload').title = atRoot ? '请先进入某个桶 / 目录再上传' : '上传文件到当前目录';
  $('#btn-mkdir').title = atRoot ? '请先进入某个桶 / 目录再新建目录' : '在当前目录下新建目录';

  // 复制 / 移动 / 删除：有勾选才可用
  const picked = state.selected.size;
  $('#btn-copy').disabled = !ready || picked === 0;
  $('#btn-move').disabled = !ready || picked === 0;
  $('#btn-delete').disabled = !ready || picked === 0;
  $('#btn-delete').title = picked ? `删除勾选的 ${picked} 项（目录会递归删除）` : '勾选条目后可批量删除';
  const selLabel = $('#sel-count');
  selLabel.hidden = picked === 0;
  selLabel.textContent = `已选 ${picked} 项`;

  // 收藏：下拉随连接切换，☆ 反映当前目录是否已收藏
  renderFavSelect($('#fav-select'), remoteFavs());
  const favBtn = $('#btn-fav-toggle');
  const faved = ready && !atRoot && remoteFavs().some((f) => f.path === state.path);
  favBtn.disabled = !ready || atRoot;
  favBtn.textContent = faved ? '★' : '☆';
  favBtn.title = faved ? '取消收藏当前目录'
    : (atRoot ? '请先进入某个桶 / 目录再收藏' : `收藏当前目录 ${state.path}`);
}

/* 收藏状态变了：工具栏与列表都要重画 */
function refreshRemoteFavUI() {
  updateToolbarState();
  renderFiles();
}

function toggleRemoteFav(item) {
  const added = toggleFav(remoteFavs(), { name: item.name, path: item.path }, setRemoteFavs);
  toast(added ? `已收藏 ${item.name}` : `已取消收藏 ${item.name}`, 'ok');
  refreshRemoteFavUI();
}

function toggleCurrentRemoteFav() {
  if (!state.activeId || state.path === state.rootPath) return;
  const name = basenameOf(state.path);
  toggleRemoteFav({ name, path: state.path });
}

function crumbs() {
  const out = [];
  if (state.rootPath === '/') {
    const clean = String(state.path || '/').replace(/^\/+/, '');
    const parts = clean ? clean.split('/') : [];
    parts.forEach((name, i) => out.push({ name, path: `/${parts.slice(0, i + 1).join('/')}` }));
    return out;
  }
  const parts = state.path ? state.path.split('/') : [];
  parts.forEach((name, i) => out.push({ name, path: parts.slice(0, i + 1).join('/') }));
  return out;
}

function renderBreadcrumb() {
  const el = $('#breadcrumb');
  if (!state.activeId) {
    el.innerHTML = '';
    const hint = document.createElement('span');
    hint.className = 'path-hint';
    hint.textContent = '先从左侧选一个连接';
    el.appendChild(hint);
    return;
  }

  const segments = [{
    name: state.rootPath === '/' ? 'HDFS 根目录' : '桶列表',
    path: state.rootPath,
  }].concat(crumbs().map((seg) => ({ name: seg.name, path: seg.path })));

  renderPathBar(el, {
    segments,
    current: state.path,
    placeholder: '输入远端路径，如 桶名/目录',
    onJump: (path) => browse(path),
    onSubmit: (value) => browse(value),
    render: renderBreadcrumb,
  });
}

/* 桶列表这一层（S3 的 rootPath 为空串）不能当复制 / 移动的目标，也不给勾选 */
function atBucketList() {
  return state.rootPath === '' && state.path === '';
}

function clearSelection() {
  state.selected.clear();
}

/* 拖拽载荷：拖的是勾选中的行就整批带上，否则只带当前这一行（与本地栏同一套规则） */
function dragRemoteItems(entry) {
  const picked = state.selected.has(entry.path)
    ? state.entries.filter((e) => state.selected.has(e.path))
    : [];
  const items = picked.length ? picked : [entry];
  return items.map((e) => ({ path: e.path, name: e.name, is_dir: e.is_dir }));
}

/* 表头全选框：跟随当前可见条目的勾选情况 */
function syncCheckAll() {
  const box = $('#check-all');
  const items = visibleEntries();
  const picked = items.filter((e) => state.selected.has(e.path)).length;
  box.disabled = items.length === 0 || atBucketList();
  box.checked = !box.disabled && picked === items.length;
  box.indeterminate = !box.disabled && picked > 0 && picked < items.length;
}

function visibleEntries() {
  const kw = state.filter.trim().toLowerCase();
  const list = state.entries.filter((e) => !kw || e.name.toLowerCase().includes(kw));
  const { key, dir } = state.sort;

  return list.slice().sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let r = 0;
    if (key === 'size') r = (a.size ?? -1) - (b.size ?? -1);
    else if (key === 'mtime') r = String(a.mtime || '').localeCompare(String(b.mtime || ''));
    else r = a.name.localeCompare(b.name, 'zh-CN', { numeric: true });
    return r * dir;
  });
}

function renderFiles() {
  const tbody = $('#file-tbody');
  tbody.innerHTML = '';
  const items = visibleEntries();
  $('#empty-state').classList.toggle('hidden', items.length > 0 || !state.activeId);

  items.forEach((entry) => {
    const tr = document.createElement('tr');
    if (entry.is_dir) tr.classList.add('dir');

    // 勾选（复制 / 移动用）
    const tdCheck = document.createElement('td');
    tdCheck.className = 'col-check';
    if (!atBucketList()) {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = state.selected.has(entry.path);
      box.title = `选择 ${entry.name}`;
      box.addEventListener('change', () => {
        if (box.checked) state.selected.add(entry.path);
        else state.selected.delete(entry.path);
        updateToolbarState();
        syncCheckAll();
      });
      tdCheck.appendChild(box);
    }

    // 远端文件与目录都能拖到本地栏（目录由服务端递归下载）
    tr.draggable = true;
    tr.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData(DRAG_REMOTE, JSON.stringify({ items: dragRemoteItems(entry) }));
      e.dataTransfer.effectAllowed = 'copy';
    });

    // 名称
    const tdName = document.createElement('td');
    const cell = document.createElement('div');
    cell.className = 'name-cell';
    const icon = document.createElement('span');
    icon.className = 'name-icon';
    icon.textContent = iconFor(entry);
    const text = document.createElement('span');
    text.className = 'name-text';
    text.textContent = entry.name;
    text.title = entry.path;
    text.addEventListener('click', () => {
      if (entry.is_dir) browse(entry.path);
      else showPreview(entry);
    });
    cell.append(icon, text);
    if (entry.is_dir) {
      cell.appendChild(favStar(remoteFavs().some((f) => f.path === entry.path),
        () => toggleRemoteFav(entry)));
    }
    tdName.appendChild(cell);

    // 大小 / 时间
    const tdSize = document.createElement('td');
    tdSize.className = 'mono';
    tdSize.textContent = entry.is_dir ? '—' : fmtSize(entry.size);

    const tdTime = document.createElement('td');
    tdTime.className = 'mono';
    tdTime.textContent = fmtTime(entry.mtime);

    // 操作
    const tdActions = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';

    if (!entry.is_dir) {
      actions.appendChild(rowButton('预览', () => showPreview(entry)));
      actions.appendChild(rowButton('下载', () => downloadEntry(entry)));
      if (state.supportsPresign) {
        actions.appendChild(rowButton('链接', () => showPresign(entry)));
      }
    }
    actions.appendChild(rowButton('删除', () => deleteEntries([entry]), 'btn-danger'));

    tdActions.appendChild(actions);
    tr.append(tdCheck, tdName, tdSize, tdTime, tdActions);
    tbody.appendChild(tr);
  });

  syncCheckAll();
}

function rowButton(label, onClick, extraClass = '') {
  const btn = document.createElement('button');
  btn.className = `btn btn-sm ${extraClass}`.trim();
  btn.textContent = label;
  btn.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return btn;
}

function downloadEntry(entry) {
  const url = new URL('/api/download', location.origin);
  url.searchParams.set('conn', state.activeId);
  url.searchParams.set('path', entry.path);
  const a = document.createElement('a');
  a.href = url.toString();
  a.download = entry.name;
  a.click();
}

/* ---- 预览 ---- */
async function showPreview(entry) {
  setLoading(true);
  try {
    const data = await apiGet('/api/preview', { conn: state.activeId, path: entry.path });
    const body = document.createElement('div');

    if (data.kind === 'text') {
      const pre = document.createElement('pre');
      pre.className = 'preview-body';
      pre.textContent = data.text || '（空文件）';
      body.appendChild(pre);
      if (data.truncated) {
        const note = document.createElement('div');
        note.className = 'preview-note';
        note.textContent = `文件较大，仅显示前 256 KB（总大小 ${fmtSize(data.total)}）`;
        body.appendChild(note);
      }
    } else if (data.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'preview-image';
      const url = new URL('/api/raw', location.origin);
      url.searchParams.set('conn', state.activeId);
      url.searchParams.set('path', entry.path);
      img.src = url.toString();
      body.appendChild(img);
    } else {
      const note = document.createElement('div');
      note.textContent = '该文件类型不支持在线预览，请下载后查看。';
      note.style.lineHeight = '1.6';
      body.appendChild(note);
    }

    openModal({
      title: entry.name,
      body,
      footer: [makeButton('关闭', '', closeModal), makeButton('下载', 'btn-primary', () => downloadEntry(entry))],
      width: '720px',
    });
  } catch (err) {
    toast('预览失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  } finally {
    setLoading(false);
  }
}

/* ---- 预签名 ---- */
async function showPresign(entry) {
  try {
    const data = await apiGet('/api/presign', { conn: state.activeId, path: entry.path, expires: 3600 });
    const body = document.createElement('div');

    const box = document.createElement('div');
    box.className = 'link-box';
    const input = document.createElement('input');
    input.type = 'text';
    input.readOnly = true;
    input.value = data.url;
    const copyBtn = makeButton('复制', '', async () => {
      try {
        await navigator.clipboard.writeText(data.url);
        toast('链接已复制', 'ok');
      } catch {
        input.select();
        document.execCommand('copy');
        toast('链接已复制', 'ok');
      }
    });
    box.append(input, copyBtn);
    body.appendChild(box);

    const note = document.createElement('div');
    note.className = 'preview-note';
    note.textContent = `有效期 ${Math.round(data.expires / 60)} 分钟。任何拿到该链接的人都能直接下载，请按需分发。`;
    body.appendChild(note);

    openModal({
      title: `预签名链接：${entry.name}`,
      body,
      footer: [makeButton('关闭', '', closeModal)],
      width: '640px',
    });
  } catch (err) {
    toast('生成链接失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  }
}

/* ---- 上传 ---- */
function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (!state.activeId || state.path === state.rootPath) {
    toast('请先进入某个桶 / 目录再上传', 'fail');
    return;
  }
  runUploads(files);
}

async function runUploads(files) {
  let ok = 0;
  const failed = [];

  for (let i = 0; i < files.length; i += 1) {
    const file = files[i];
    const target = joinPath(state.path, file.name);
    setStatus(`上传中 ${i + 1}/${files.length}：${file.name}`);
    try {
      await uploadOne(file, target);
      ok += 1;
    } catch (err) {
      failed.push(`${file.name}（${err.message}）`);
    }
  }

  await browse(state.path);
  if (ok) toast(`已上传 ${ok} 个文件`, 'ok');
  if (failed.length) toast(`有 ${failed.length} 个文件上传失败`, 'fail', failed.join('\n'));
  setStatus('就绪');
}

function uploadOne(file, target, displayName) {
  const label = displayName || file.name || target;
  return new Promise((resolve, reject) => {
    const url = new URL('/api/upload', location.origin);
    url.searchParams.set('conn', state.activeId);
    url.searchParams.set('path', target);

    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url.toString());
    xhr.upload.addEventListener('progress', (ev) => {
      if (ev.lengthComputable) {
        setStatus(`上传 ${label}：${Math.round((ev.loaded / ev.total) * 100)}%`);
      }
    });
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let message = `HTTP ${xhr.status}`;
      try { message = JSON.parse(xhr.responseText).error || message; } catch { /* 忽略 */ }
      reject(new Error(message));
    });
    xhr.addEventListener('error', () => reject(new Error('网络错误')));
    xhr.send(file);
  });
}

/* ---- 从资源管理器拖进来的内容 ---- */
/* 目录在 dataTransfer.files 里是 size=0 的伪文件（浏览器读不出它的字节，直接 PUT 会在
   客户端就失败，前端只能报「网络错误」），所以必须用 webkitGetAsEntry 自己递归展开：
   目录先建目录，文件按相对路径逐个上传。 */
async function collectDropped(dt) {
  const entries = Array.from(dt.items || [])
    .filter((item) => item.kind === 'file' && typeof item.webkitGetAsEntry === 'function')
    .map((item) => item.webkitGetAsEntry())
    .filter(Boolean);

  const files = [];
  const dirs = [];
  if (!entries.length) {
    // 拿不到 entry（非 Chromium 等）：退回 files，能传多少传多少
    Array.from(dt.files || []).forEach((file) => files.push({ file, rel: file.name }));
    return { files, dirs };
  }
  for (const entry of entries) await walkEntry(entry, '', files, dirs);
  return { files, dirs };
}

/* readEntries 一次最多返回 100 条，必须循环到返回空数组为止，否则会静默丢文件 */
async function walkEntry(entry, prefix, files, dirs) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    files.push({ file, rel: prefix + entry.name });
    return;
  }
  if (!entry.isDirectory) return;

  const rel = prefix + entry.name;
  dirs.push(rel);
  const reader = entry.createReader();
  for (;;) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, `${rel}/`, files, dirs);
  }
}

/* 资源管理器的文件 / 文件夹 → 远端当前目录 */
async function uploadDropped(dt) {
  const { files, dirs } = await collectDropped(dt);
  if (!files.length && !dirs.length) return;
  if (!state.activeId || state.path === state.rootPath) {
    toast('请先进入某个桶 / 目录再上传', 'fail');
    return;
  }

  const failed = [];
  let ok = 0;
  setLoading(true);
  try {
    for (let i = 0; i < dirs.length; i += 1) {
      setStatus(`新建目录 ${i + 1}/${dirs.length}：${dirs[i]}`);
      try {
        await apiPost('/api/mkdir', { conn: state.activeId, path: joinPath(state.path, dirs[i]) });
      } catch (err) {
        failed.push(`${dirs[i]}/（${err.message}）`);
      }
    }
    for (let i = 0; i < files.length; i += 1) {
      setStatus(`上传中 ${i + 1}/${files.length}：${files[i].rel}`);
      try {
        await uploadOne(files[i].file, joinPath(state.path, files[i].rel), files[i].rel);
        ok += 1;
      } catch (err) {
        failed.push(`${files[i].rel}（${err.message}）`);
      }
    }
    await browse(state.path);
    if (ok) toast(`已上传 ${ok} 个文件${dirs.length ? ` + ${dirs.length} 个目录` : ''}`, 'ok');
    if (failed.length) toast(`有 ${failed.length} 项失败`, 'fail', failed.join('\n'));
  } finally {
    setLoading(false);
    setStatus('就绪');
  }
}

/* ------------------------------------------------------------------ */
/* 本地目录栏 + 双向拖拽                                                 */
/* ------------------------------------------------------------------ */
async function browseLocal(path) {
  setLoading(true);
  try {
    const data = await apiGet('/api/local/list', { path });
    local.path = data.path;
    local.parent = data.parent;
    local.entries = data.entries || [];
    local.selected.clear(); // 换了目录，之前的勾选不再有意义
    renderLocalPath();
    renderLocal();
    setStatus(`本地 ${local.path} · ${local.entries.length} 项`);
  } catch (err) {
    renderLocalPath(); // 失败时退回当前目录的分段形式
    renderLocal();
    toast('读取本地目录失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  } finally {
    setLoading(false);
  }
}

/* 本地地址行的分段：逐级取真实前缀，点哪段就跳哪段（与远端同一套交互） */
function localCrumbs() {
  const path = String(local.path || '');
  if (!path) return [];
  const out = [];
  let cursor = 0;
  path.split(/([\\/]+)/).forEach((chunk) => {
    if (!chunk) return;
    if (/^[\\/]+$/.test(chunk)) {
      if (out.length) out[out.length - 1].path += chunk; // 分隔符归到上一段，拼出来就是可用的目录
      cursor += chunk.length;
      return;
    }
    cursor += chunk.length;
    out.push({ name: chunk, path: path.slice(0, cursor) });
  });
  return out;
}

function renderLocalPath() {
  renderPathBar($('#local-path'), {
    segments: localCrumbs(),
    current: local.path,
    emptyHint: '点击输入本地目录，如 D:\\data',
    placeholder: '如 D:\\data',
    onJump: (path) => browseLocal(path),
    onSubmit: (value) => browseLocal(value),
    render: renderLocalPath,
  });
  const up = $('#btn-local-up');
  up.disabled = !local.parent;
}

/* 本地收藏：下拉 + ☆ 按钮状态 */
function updateLocalFavState() {
  renderFavSelect($('#local-fav-select'), localFavs());
  const btn = $('#btn-local-fav');
  const faved = Boolean(local.path) && localFavs().some((f) => f.path === local.path);
  btn.disabled = !local.path;
  btn.textContent = faved ? '★' : '☆';
  btn.title = faved ? '取消收藏当前目录' : `收藏当前目录 ${local.path}`;
}

function toggleLocalFav(item) {
  const added = toggleFav(localFavs(), { name: item.name, path: item.path }, setLocalFavs);
  toast(added ? `已收藏 ${item.name}` : `已取消收藏 ${item.name}`, 'ok');
  renderLocal();
}

function toggleCurrentLocalFav() {
  if (!local.path) return;
  toggleLocalFav({ name: basenameOf(local.path), path: local.path });
}

function localEntries() {
  return local.entries.slice().sort((a, b) => (
    a.is_dir !== b.is_dir ? (a.is_dir ? -1 : 1)
      : a.name.localeCompare(b.name, 'zh-CN', { numeric: true })
  ));
}

/* 本地栏勾选：与远端栏同一套做法，表头全选只作用于当前显示的条目 */
function syncLocalCheckAll() {
  const box = $('#local-check-all');
  const items = localEntries();
  const picked = items.filter((e) => local.selected.has(e.path)).length;
  box.disabled = items.length === 0;
  box.checked = !box.disabled && picked === items.length;
  box.indeterminate = !box.disabled && picked > 0 && picked < items.length;
}

function updateLocalSelCount() {
  const picked = local.selected.size;
  const label = $('#local-sel-count');
  label.hidden = picked === 0;
  label.textContent = `已选 ${picked} 项`;
}

/* 拖拽载荷：拖的是勾选中的行就整批带上，否则只带当前这一行 */
function dragLocalItems(entry) {
  const picked = local.selected.has(entry.path)
    ? localEntries().filter((e) => local.selected.has(e.path))
    : [];
  const items = picked.length ? picked : [entry];
  return items.map((e) => ({ path: e.path, name: e.name, is_dir: e.is_dir }));
}

function renderLocal() {
  updateLocalFavState();
  const tbody = $('#local-tbody');
  tbody.innerHTML = '';
  const items = localEntries();
  $('#local-empty').classList.toggle('hidden', items.length > 0 || Boolean(local.path));

  items.forEach((entry) => {
    const tr = document.createElement('tr');
    if (entry.is_dir) tr.classList.add('dir');

    // 勾选（配合拖拽做多选上传）
    const tdCheck = document.createElement('td');
    tdCheck.className = 'col-check';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = local.selected.has(entry.path);
    box.title = `选择 ${entry.name}`;
    box.addEventListener('change', () => {
      if (box.checked) local.selected.add(entry.path);
      else local.selected.delete(entry.path);
      updateLocalSelCount();
      syncLocalCheckAll();
    });
    tdCheck.appendChild(box);

    const tdName = document.createElement('td');
    const cell = document.createElement('div');
    cell.className = 'name-cell';
    const icon = document.createElement('span');
    icon.className = 'name-icon';
    icon.textContent = entry.is_dir ? '📁' : '📄';
    const text = document.createElement('span');
    text.className = 'name-text';
    text.textContent = entry.name;
    text.title = entry.path;
    if (entry.is_dir) text.addEventListener('click', () => browseLocal(entry.path));
    cell.append(icon, text);
    if (entry.is_dir) {
      cell.appendChild(favStar(localFavs().some((f) => f.path === entry.path),
        () => toggleLocalFav(entry)));
    }
    tdName.appendChild(cell);

    const tdSize = document.createElement('td');
    tdSize.className = 'mono';
    tdSize.textContent = entry.is_dir ? '—' : fmtSize(entry.size);

    const tdTime = document.createElement('td');
    tdTime.className = 'mono';
    tdTime.textContent = fmtTime(entry.mtime);

    // 文件与目录都能拖到远端栏上传（目录由服务端递归读取后上传）
    tr.draggable = true;
    tr.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData(DRAG_LOCAL, JSON.stringify({ items: dragLocalItems(entry) }));
      e.dataTransfer.effectAllowed = 'copy';
    });

    tr.append(tdCheck, tdName, tdSize, tdTime);
    tbody.appendChild(tr);
  });

  syncLocalCheckAll();
  updateLocalSelCount();
}

/* 本地文件 / 目录 → 远端当前目录：服务端直接读本地磁盘写入存储，不经过浏览器中转 */
async function pushLocalToRemote(items) {
  const list = (Array.isArray(items) ? items : []).filter((it) => it && it.path);
  if (!list.length) return;
  if (!state.activeId || state.path === state.rootPath) {
    toast('请先在左侧进入某个桶 / 目录再上传', 'fail');
    return;
  }

  const label = list.length === 1 ? list[0].name : `${list.length} 项`;
  setLoading(true);
  setStatus(`上传 ${label} …`);
  try {
    const data = await apiPost('/api/local/push', {
      conn: state.activeId,
      sources: list.map((it) => ({ path: it.path, is_dir: Boolean(it.is_dir) })),
      dest_dir: state.path,
    });
    await browse(state.path); // browse 里会清掉远端勾选
    toast(data.message || `已上传 ${label}`, 'ok');
  } catch (err) {
    toast('上传失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    setStatus('就绪');
  } finally {
    setLoading(false);
  }
}

/* 远端文件 / 目录 → 本地栏当前目录：服务端直接读存储写本地磁盘，不经过浏览器 */
async function pullRemoteToLocal(items) {
  const list = (Array.isArray(items) ? items : []).filter((it) => it && it.path);
  if (!list.length) return;
  if (!local.path) {
    toast('请先在右侧打开一个本地目录', 'fail');
    return;
  }

  const label = list.length === 1 ? list[0].name : `${list.length} 项`;
  setLoading(true);
  setStatus(`下载 ${label} …`);
  try {
    const data = await apiPost('/api/local/pull', {
      conn: state.activeId,
      sources: list.map((it) => ({ path: it.path, is_dir: Boolean(it.is_dir) })),
      dest_dir: local.path,
    });
    await browseLocal(local.path);
    toast(data.message || `已下载 ${label}`, 'ok');
  } catch (err) {
    toast('下载失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    setStatus('就绪');
  } finally {
    setLoading(false);
  }
}

/* OS 拖入或面板下载的文件 → 写到本地当前目录 */
async function saveLocalBlob(blob, name) {
  const url = new URL('/api/local/upload', location.origin);
  url.searchParams.set('dir', local.path);
  url.searchParams.set('name', name);
  const res = await fetch(url, { method: 'PUT', body: blob });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try { message = (await res.json()).error || message; } catch { /* 忽略 */ }
    throw new Error(message);
  }
}

async function saveOsFilesToLocal(dt) {
  const { files, dirs } = await collectDropped(dt);
  if (dirs.length) {
    // 目录伪文件直接写盘只会得到一个 0 字节垃圾文件，明确拒绝而不是静默出错
    toast('文件夹不能直接存到本地目录', 'fail', `已跳过 ${dirs.length} 个文件夹（子目录不会跟着进去）。需要的话先拖到左侧远端，再从远端拖回来。`);
  }
  if (!files.length) return;
  if (!local.path) {
    toast('请先在右侧打开一个本地目录', 'fail');
    return;
  }
  let ok = 0;
  const failed = [];
  for (let i = 0; i < files.length; i += 1) {
    try {
      await saveLocalBlob(files[i].file, files[i].file.name);
      ok += 1;
    } catch (err) {
      failed.push(`${files[i].file.name}（${err.message}）`);
    }
  }
  await browseLocal(local.path);
  if (ok) toast(`已保存 ${ok} 个文件到本地目录`, 'ok');
  if (failed.length) toast(`有 ${failed.length} 个文件保存失败`, 'fail', failed.join('\n'));
}

/* ---- 本地栏宽度拖拽（记住到 localStorage） ---- */
const LOCAL_PANE_KEY = 'objstore.localPaneWidth';
const LOCAL_PANE_MIN = 200;
const LOCAL_PANE_MAX_RATIO = 0.5; // 本地栏最多占内容区一半，换屏后不至于把远端栏挤没

// 把宽度夹到 [LOCAL_PANE_MIN, 内容区一半]，同时给远端栏留 LOCAL_PANE_MIN
function clampPaneWidth(width) {
  const box = $('.panes');
  const total = box ? box.clientWidth : window.innerWidth;
  const max = Math.max(
    Math.min(Math.round(total * LOCAL_PANE_MAX_RATIO), total - LOCAL_PANE_MIN),
    LOCAL_PANE_MIN,
  );
  return Math.max(LOCAL_PANE_MIN, Math.min(Math.round(width), max));
}

function initPaneSplitter() {
  const splitter = $('#pane-splitter');
  const pane = $('.pane-local');
  let userSet = false; // 是否被手动拖过（没拖过时允许退回 CSS 默认宽）

  try {
    const saved = parseInt(localStorage.getItem(LOCAL_PANE_KEY), 10);
    if (saved >= LOCAL_PANE_MIN) {
      pane.style.width = `${clampPaneWidth(saved)}px`;
      userSet = true;
    }
  } catch { /* localStorage 不可用就用默认宽 */ }

  // 屏幕切换 / 窗口缩放后，旧宽度可能已经把远端栏挤没，夹回上限（各一半）
  window.addEventListener('resize', () => {
    const w = Math.round(pane.getBoundingClientRect().width);
    const clamped = clampPaneWidth(w);
    if (!userSet && clamped >= w) { pane.style.width = ''; return; }
    if (clamped !== w) pane.style.width = `${clamped}px`;
  });

  let startX = 0;
  let startWidth = 0;

  splitter.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    startWidth = pane.getBoundingClientRect().width;
    splitter.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!splitter.classList.contains('dragging')) return;
    // 本地栏在右缘，往左拖 = 变宽
    pane.style.width = `${clampPaneWidth(startWidth + (startX - e.clientX))}px`;
  });

  document.addEventListener('mouseup', () => {
    if (!splitter.classList.contains('dragging')) return;
    splitter.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    userSet = true;
    try {
      localStorage.setItem(LOCAL_PANE_KEY, String(Math.round(pane.getBoundingClientRect().width)));
    } catch { /* 忽略 */ }
  });
}

/* ---- 左侧连接栏：拖动调宽 / 收起（宽度与收起状态都记住） ---- */
const SIDEBAR_WIDTH_KEY = 'objstore.sidebarWidth';
const SIDEBAR_COLLAPSED_KEY = 'objstore.sidebarCollapsed';
const SIDEBAR_MIN = 160;
const SIDEBAR_DEFAULT = 268;
const CONTENT_MIN = 320; // 内容区至少留这么多，别把双栏挤没

function clampSidebarWidth(width) {
  const layout = $('.layout');
  const total = layout ? layout.clientWidth : window.innerWidth;
  const max = Math.max(Math.min(Math.round(total * 0.5), total - CONTENT_MIN - 5), SIDEBAR_MIN);
  return Math.max(SIDEBAR_MIN, Math.min(Math.round(width), max));
}

function initSidebar() {
  const layout = $('.layout');
  const sidebar = $('.sidebar');
  const splitter = $('#sidebar-splitter');
  let width = SIDEBAR_DEFAULT;
  let userSet = false;

  const apply = (value) => {
    width = clampSidebarWidth(value);
    sidebar.style.width = `${width}px`;
  };
  const syncButton = () => {
    const collapsed = layout.classList.contains('sidebar-collapsed');
    const btn = $('#btn-sidebar');
    btn.textContent = collapsed ? '»' : '«';
    btn.title = collapsed ? '展开连接栏' : '收起连接栏';
  };

  try {
    const saved = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY), 10);
    if (saved >= SIDEBAR_MIN) {
      apply(saved);
      userSet = true;
    }
    if (localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1') layout.classList.add('sidebar-collapsed');
  } catch { /* localStorage 不可用就用默认宽 */ }
  syncButton();

  $('#btn-sidebar').addEventListener('click', () => {
    const collapsed = !layout.classList.contains('sidebar-collapsed');
    layout.classList.toggle('sidebar-collapsed', collapsed);
    if (!collapsed) apply(width); // 收起时量不到宽度，用记住的值
    syncButton();
    try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch { /* 忽略 */ }
  });

  let startX = 0;
  let startWidth = 0;

  splitter.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    startWidth = sidebar.getBoundingClientRect().width;
    splitter.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!splitter.classList.contains('dragging')) return;
    // 连接栏在左缘，往右拖 = 变宽
    apply(startWidth + (e.clientX - startX));
  });

  document.addEventListener('mouseup', () => {
    if (!splitter.classList.contains('dragging')) return;
    splitter.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    userSet = true;
    try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(Math.round(width))); } catch { /* 忽略 */ }
  });

  // 换屏 / 缩窗后夹回上限，别让连接栏把内容区挤没
  window.addEventListener('resize', () => {
    if (userSet) apply(width);
  });
}

/* ------------------------------------------------------------------ */
/* 复制 / 移动：目标目录树（懒加载，可跨桶）                              */
/* ------------------------------------------------------------------ */
async function openTransferDialog(mode) {
  const sources = state.entries.filter((e) => state.selected.has(e.path));
  if (!sources.length) return;

  const verb = mode === 'move' ? '移动' : '复制';
  const body = document.createElement('div');

  const hint = document.createElement('div');
  hint.className = 'field-hint';
  hint.textContent = sources.length === 1
    ? `把「${sources[0].name}」${verb}到下面选中的目录里`
    : `把勾选的 ${sources.length} 项${verb}到下面选中的目录里`;
  body.appendChild(hint);

  const treeHost = document.createElement('div');
  treeHost.className = 'tree';
  body.appendChild(treeHost);

  const targetLine = document.createElement('div');
  targetLine.className = 'tree-target';
  body.appendChild(targetLine);

  const warn = document.createElement('div');
  warn.className = 'field-hint tree-warn';
  body.appendChild(warn);

  const nodes = new Map();
  let chosen = null;

  const okBtn = makeButton(verb, 'btn-primary', () => {
    if (!chosen || problem()) return;
    closeModal();
    runTransfer(mode, sources, chosen.path);
  });

  function makeNode(entry, depth) {
    const node = { path: entry.path, depth, children: null, expanded: false };
    const wrap = document.createElement('div');

    const row = document.createElement('div');
    row.className = 'tree-row';
    row.style.paddingLeft = `${6 + depth * 14}px`;

    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    toggle.textContent = '▸';
    toggle.addEventListener('click', (e) => { e.stopPropagation(); toggleNode(node); });

    const label = document.createElement('span');
    label.className = 'tree-label';
    label.textContent = entry.name;
    label.title = entry.path;

    row.append(toggle, label);
    row.addEventListener('click', () => { chosen = node; refresh(); });

    const childHost = document.createElement('div');
    childHost.hidden = true;
    wrap.append(row, childHost);

    Object.assign(node, { wrap, row, toggle, childHost });
    nodes.set(node.path, node);
    return node;
  }

  async function toggleNode(node) {
    if (node.expanded) {
      node.expanded = false;
      node.toggle.textContent = '▸';
      node.childHost.hidden = true;
      return;
    }
    if (!node.children) {
      node.toggle.textContent = '·';
      try {
        const data = await apiGet('/api/list', { conn: state.activeId, path: node.path });
        node.children = (data.entries || []).filter((e) => e.is_dir);
      } catch (err) {
        node.toggle.textContent = '▸';
        toast('读取目录失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
        return;
      }
      node.children.forEach((child) => node.childHost.appendChild(makeNode(child, node.depth + 1).wrap));
    }
    node.expanded = true;
    node.toggle.textContent = '▾';
    node.childHost.hidden = false;
  }

  function problem() {
    const dest = chosen ? chosen.path : '';
    if (!dest) return '请选到某个桶里面，桶列表这一层不能作为目标';
    if (dest === state.path) return '目标就是这些条目现在所在的目录，请另选一个';
    for (const src of sources) {
      if (dest === src.path) return `目标不能是「${src.name}」本身`;
      if (dest.startsWith(`${src.path}/`)) return `目标不能在「${src.name}」里面`;
    }
    return '';
  }

  function refresh() {
    $$('.tree-row', treeHost).forEach((row) => row.classList.remove('selected'));
    if (chosen) chosen.row.classList.add('selected');
    const issue = problem();
    targetLine.textContent = chosen ? `目标：${chosen.path}` : '目标：（未选择）';
    warn.textContent = issue || (mode === 'move' ? '移动后源位置不再保留。' : '目标目录下同名的内容会被覆盖。');
    warn.classList.toggle('is-warn', Boolean(issue));
    okBtn.disabled = Boolean(issue);
  }

  openModal({
    title: `${verb} ${sources.length} 项到…`,
    body,
    footer: [makeButton('取消', '', closeModal), okBtn],
    width: '520px',
  });

  const rootNode = makeNode({
    name: state.rootPath === '/' ? 'HDFS 根目录' : '桶列表',
    path: state.rootPath,
  }, 0);
  treeHost.appendChild(rootNode.wrap);

  // 默认展开到当前目录并选中它：多数时候目标是它的兄弟目录，改选一步到位
  let node = rootNode;
  for (const seg of crumbs()) {
    if (!node.expanded) await toggleNode(node);
    const child = nodes.get(seg.path);
    if (!child) break;
    node = child;
  }
  chosen = node;
  refresh();
}

async function runTransfer(mode, sources, dest) {
  const verb = mode === 'move' ? '移动' : '复制';
  setLoading(true);
  setStatus(`${verb}中…`);
  try {
    const data = await apiPost(`/api/${mode}`, {
      conn: state.activeId,
      sources: sources.map((e) => ({ path: e.path, is_dir: e.is_dir })),
      dest,
    });
    await browse(state.path); // browse 里会清掉勾选
    toast(data.message || `已${verb}`, 'ok');
  } catch (err) {
    toast(`${verb}失败`, 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    setStatus('就绪');
  } finally {
    setLoading(false);
  }
}

/* ---- 删除：单条与批量走同一条路径（目录由适配器递归删） ---- */
function deleteSummary(sources) {
  if (sources.length === 1) {
    const only = sources[0];
    return only.is_dir ? `目录「${only.name}」及其全部内容` : `对象「${only.name}」`;
  }
  const dirs = sources.filter((e) => e.is_dir).length;
  return `勾选的 ${sources.length} 项` + (dirs ? `（其中 ${dirs} 个目录会连同内容一起删）` : '');
}

async function deleteEntries(sources) {
  if (!sources.length) return;
  const ok = await confirmDialog('确认删除', `将删除 ${deleteSummary(sources)}。删除后不可恢复。`);
  if (!ok) return;

  let removed = 0;
  const failed = [];
  setLoading(true);
  try {
    for (let i = 0; i < sources.length; i += 1) {
      const entry = sources[i];
      setStatus(`删除中 ${i + 1}/${sources.length}：${entry.name}`);
      try {
        const data = await apiPost('/api/delete', { conn: state.activeId, path: entry.path, is_dir: entry.is_dir });
        removed += Number(data.removed || 1);
      } catch (err) {
        failed.push(`${entry.name}（${err.message}）`); // 单条失败不中断，剩下的继续删
      }
    }
    await browse(state.path); // browse 里会清掉勾选
  } finally {
    setLoading(false);
  }
  if (removed) toast(removed > 1 ? `已删除 ${removed} 个对象` : '已删除', 'ok');
  if (failed.length) toast(`有 ${failed.length} 项删除失败`, 'fail', failed.join('\n'));
}

function deleteSelected() {
  deleteEntries(state.entries.filter((e) => state.selected.has(e.path)));
}

/* ---- 新建目录 ---- */
async function createFolder() {
  const body = document.createElement('div');
  const field = document.createElement('div');
  field.className = 'field';
  const label = document.createElement('label');
  label.textContent = '目录名';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = '例如 raw/2026-09-16';
  field.append(label, input);
  const hint = document.createElement('div');
  hint.className = 'field-hint';
  hint.textContent = `将在 ${state.path} 下创建`;
  field.appendChild(hint);
  body.appendChild(field);

  const submit = async () => {
    const name = input.value.trim().replace(/^\/+|\/+$/g, '');
    if (!name) return toast('请填写目录名', 'fail');
    try {
      const data = await apiPost('/api/mkdir', { conn: state.activeId, path: joinPath(state.path, name) });
      closeModal();
      toast(data.message || '已创建目录', 'ok');
      await browse(state.path);
    } catch (err) {
      toast('创建目录失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
    }
  };

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  openModal({
    title: '新建目录',
    body,
    footer: [makeButton('取消', '', closeModal), makeButton('创建', 'btn-primary', submit)],
    width: '440px',
  });
  setTimeout(() => input.focus(), 30);
}

/* ------------------------------------------------------------------ */
/* 备份 / 退出                                                          */
/* ------------------------------------------------------------------ */
function exportBackup() {
  const url = new URL('/api/config/export', location.origin);
  const a = document.createElement('a');
  a.href = url.toString();
  a.click();
  toast('已导出配置备份', 'ok');
}

async function importBackup(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const data = await apiPost('/api/config/import', { content: text, merge: true });
    toast(data.message || '已导入', 'ok');
    await loadConnections();
    if (state.activeId) await browse(state.path);
  } catch (err) {
    toast('导入失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  }
}

async function shutdown() {
  const ok = await confirmDialog('退出服务', '将停止本地服务，浏览器页面随即失效。之后重新运行 start.bat（或 objstore_tool 命令）即可。');
  if (!ok) return;
  try {
    await apiPost('/api/shutdown', {});
  } catch { /* 服务停掉后请求可能中断，属正常 */ }
  document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:#9da0a8;font-family:sans-serif">服务已停止，可以关闭此页面。</div>';
}

/* ------------------------------------------------------------------ */
/* 事件绑定与初始化                                                     */
/* ------------------------------------------------------------------ */
function bindEvents() {
  $('#btn-new-conn').addEventListener('click', () => openConnectionModal(null));
  $('#btn-refresh').addEventListener('click', () => browse(state.path));
  $('#btn-mkdir').addEventListener('click', createFolder);
  $('#btn-copy').addEventListener('click', () => openTransferDialog('copy'));
  $('#btn-move').addEventListener('click', () => openTransferDialog('move'));
  $('#btn-delete').addEventListener('click', deleteSelected);
  $('#check-all').addEventListener('change', (e) => {
    // 全选只作用于当前显示的条目（过滤后的）
    visibleEntries().forEach((entry) => {
      if (e.target.checked) state.selected.add(entry.path);
      else state.selected.delete(entry.path);
    });
    renderFiles();
    updateToolbarState();
  });
  $('#btn-upload').addEventListener('click', () => $('#file-input').click());
  $('#btn-export').addEventListener('click', exportBackup);
  $('#btn-import').addEventListener('click', () => $('#import-input').click());
  $('#btn-shutdown').addEventListener('click', shutdown);

  $('#file-input').addEventListener('change', (e) => {
    uploadFiles(e.target.files);
    e.target.value = '';
  });

  $('#import-input').addEventListener('change', (e) => {
    importBackup(e.target.files[0]);
    e.target.value = '';
  });

  $('#filter-input').addEventListener('input', (e) => {
    state.filter = e.target.value;
    renderFiles();
  });

  $$('.file-table th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) state.sort.dir *= -1;
      else { state.sort.key = key; state.sort.dir = 1; }
      $$('.file-table th.sortable').forEach((other) => {
        other.classList.toggle('sorted', other === th);
        other.classList.toggle('asc', other === th && state.sort.dir === 1);
      });
      renderFiles();
    });
  });

  const wrap = $('#table-wrap');
  const overlay = $('#drop-overlay');
  let dragDepth = 0;

  wrap.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth += 1;
    overlay.classList.add('active');
  });
  wrap.addEventListener('dragover', (e) => e.preventDefault());
  wrap.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.classList.remove('active');
  });
  wrap.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    overlay.classList.remove('active');
    const rawLocal = e.dataTransfer.getData(DRAG_LOCAL);
    if (rawLocal) {
      try {
        const { items } = JSON.parse(rawLocal);
        pushLocalToRemote(items);
      } catch { /* 数据损坏时忽略 */ }
      return;
    }
    uploadDropped(e.dataTransfer);
  });

  // 本地栏：接收远端文件（下载）或 OS 文件（另存）
  const localWrap = $('#local-wrap');
  const localOverlay = $('#local-drop-overlay');
  let localDepth = 0;

  localWrap.addEventListener('dragenter', (e) => {
    e.preventDefault();
    localDepth += 1;
    localOverlay.classList.add('active');
  });
  localWrap.addEventListener('dragover', (e) => e.preventDefault());
  localWrap.addEventListener('dragleave', () => {
    localDepth = Math.max(0, localDepth - 1);
    if (localDepth === 0) localOverlay.classList.remove('active');
  });
  localWrap.addEventListener('drop', (e) => {
    e.preventDefault();
    localDepth = 0;
    localOverlay.classList.remove('active');
    const rawRemote = e.dataTransfer.getData(DRAG_REMOTE);
    if (rawRemote) {
      try {
        const { items } = JSON.parse(rawRemote);
        pullRemoteToLocal(items);
      } catch { /* 数据损坏时忽略 */ }
      return;
    }
    saveOsFilesToLocal(e.dataTransfer);
  });

  // 收藏夹下拉：选中即跳转，末项「管理收藏…」可删除
  $('#fav-select').addEventListener('change', (e) => {
    const value = e.target.value;
    e.target.value = '';
    if (!value) return;
    if (value === FAV_MANAGE) {
      openFavManager('收藏夹（当前连接）', remoteFavs, (fav) => browse(fav.path), (fav) => {
        setRemoteFavs(remoteFavs().filter((f) => f.path !== fav.path));
        refreshRemoteFavUI();
      });
      return;
    }
    const fav = remoteFavs().find((f) => f.path === value);
    if (fav) browse(fav.path);
  });
  $('#btn-fav-toggle').addEventListener('click', toggleCurrentRemoteFav);

  $('#local-fav-select').addEventListener('change', (e) => {
    const value = e.target.value;
    e.target.value = '';
    if (!value) return;
    if (value === FAV_MANAGE) {
      openFavManager('本地收藏夹', localFavs, (fav) => browseLocal(fav.path), (fav) => {
        setLocalFavs(localFavs().filter((f) => f.path !== fav.path));
        renderLocal();
      });
      return;
    }
    const fav = localFavs().find((f) => f.path === value);
    if (fav) browseLocal(fav.path);
  });
  $('#btn-local-fav').addEventListener('click', toggleCurrentLocalFav);

  $('#local-check-all').addEventListener('change', (e) => {
    localEntries().forEach((entry) => {
      if (e.target.checked) local.selected.add(entry.path);
      else local.selected.delete(entry.path);
    });
    renderLocal();
  });
  $('#btn-local-up').addEventListener('click', () => {
    if (local.parent) browseLocal(local.parent);
  });

  initPaneSplitter();
  initSidebar();

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
    if (e.key === 'F5') { e.preventDefault(); browse(state.path); }
  });
}

async function init() {
  bindEvents();
  try {
    state.meta = await apiGet('/api/meta');
    $('#app-version').textContent = `v${state.meta.version}`;
    $('#config-path').textContent = state.meta.config_file;
    await loadFavorites(); // 收藏夹来自配置，渲染下拉前必须先拿到
    await loadConnections();
    updateToolbarState(); // 渲染远端收藏下拉
    renderBreadcrumb();   // 未选连接时的地址行占位
    renderLocalPath();    // 本地地址行占位（点击即可输入路径）
    renderLocal();        // 渲染本地收藏下拉（未打开目录时也能直接跳到收藏）
    setStatus('就绪');
  } catch (err) {
    toast('初始化失败', 'fail', err.message + (err.detail ? ` — ${err.detail}` : ''));
  }
}

init();
