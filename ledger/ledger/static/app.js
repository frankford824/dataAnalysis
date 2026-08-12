/* 界面。原生 ES，不上构建链。
 *
 * 这是内部工具，装一套 npm 只是给以后添维护负担；真到这一页装不下的时候再换，
 * 那时也该知道到底需要什么了。
 *
 * 三条贯穿全篇的呈现规则，都是为了不让人拿着半张表去报账：
 *
 *   结论排在数字前面。先说能不能结账、卡在哪，再出损益表。
 *   缺数据出破折号，不出 0。零是结论，破折号是「还不知道」。
 *   每个数都能点回原始行号。只报总数不给证据，对不上账时没人查得动，
 *   这套东西就退化成又一个看不懂的报表。
 */

'use strict';

// --------------------------------------------------------------------------
// 基础
// --------------------------------------------------------------------------

const $ = id => document.getElementById(id);
const main = $('main');

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

/** 金额。null 一律出破折号——那和「算出来是 0」是两件事。 */
function money(v, display) {
  if (v === null || v === undefined) return '<span class="na">—</span>';
  if (display === 'percent') return (v * 100).toFixed(1) + '%';
  if (display === 'count') return v.toLocaleString('zh-CN', {maximumFractionDigits: 0});
  const s = v.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return v < 0 ? '<span class="neg">' + s + '</span>' : s;
}

/** 总览用的紧凑金额。几十家店的矩阵里，两位小数只是噪声。 */
function brief(v) {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
  if (a >= 1e4) return (v / 1e4).toFixed(1) + ' 万';
  return v.toFixed(0);
}

function count(n) { return (n || 0).toLocaleString('zh-CN'); }

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

let toastTimer;
function toast(text, bad) {
  const el = $('toast');
  el.textContent = text;
  el.className = 'on' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, bad ? 6000 : 3000);
}

/** 接口调用。失败一律把后端那句人话原样抛出来，别自己编。 */
async function api(path, opts) {
  opts = opts ? {...opts, headers: {...(opts.headers || {})}} : {headers: {}};
  const token = sessionStorage.getItem('ledger-token');
  if (token) opts.headers.Authorization = 'Bearer ' + token;
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.status + '';
    try { detail = (await res.json()).detail || detail; } catch (e) { /* 非 JSON 就算了 */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function json(method, body) {
  return {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})};
}

const S = {boot: null, overview: null};

function loginGate(message) {
  main.innerHTML = `<div class="card"><header><h1>身份验证</h1></header>
    <p class="muted">${esc(message)}</p>
    <div class="row wrap"><label class="fld">访问 token
      <input id="auth-token" type="password" autocomplete="current-password"></label>
      <button class="primary" id="auth-go">登录</button></div></div>`;
  $('auth-go').onclick = () => {
    const token = $('auth-token').value.trim();
    if (!token) return;
    sessionStorage.setItem('ledger-token', token);
    location.reload();
  };
}

// --------------------------------------------------------------------------
// 路由
// --------------------------------------------------------------------------

const routes = [
  [/^#\/$/, () => viewOverview()],
  [/^#\/deliver$/, () => viewDeliver()],
  [/^#\/commission(?:\?period=(.*))?$/, period =>
    viewCommission(period ? decodeURIComponent(period) : '')],
  [/^#\/stores$/, () => viewStores()],
  [/^#\/store\/([^/?]+)(?:\?period=(.*))?$/, (id, period) =>
    viewStore(decodeURIComponent(id), period ? decodeURIComponent(period) : '')],
  [/^#\/onboard\/([^/?]+)(?:\?(.*))?$/, (sha, query) => {
    const q = new URLSearchParams(query || '');
    return viewOnboard(decodeURIComponent(sha), q.get('sheet') || '',
      q.get('header_row') || '', q.get('source') || '');
  }],
];

async function route() {
  const hash = location.hash || '#/';
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      const view = hash === '#/' ? 'overview'
        : hash.startsWith('#/deliver') ? 'deliver'
        : hash.startsWith('#/commission') ? 'commission'
        : hash.startsWith('#/stores') ? 'stores' : '';
      document.querySelectorAll('#nav a').forEach(a =>
        a.classList.toggle('on', a.dataset.view === view));
      main.innerHTML = '<div class="empty"><span class="spin"></span></div>';
      try { await fn(...m.slice(1)); }
      catch (err) { main.innerHTML = fail(err.message); }
      window.scrollTo(0, 0);
      return;
    }
  }
  location.hash = '#/';
}

function fail(msg) {
  return `<div class="card"><div class="banner bad"><strong>没打开</strong>${esc(msg)}</div></div>`;
}

window.addEventListener('hashchange', route);

// --------------------------------------------------------------------------
// 交表
// --------------------------------------------------------------------------

const picker = $('file');
picker.onchange = () => { send(picker.files); picker.value = ''; };

/* 拖到窗口任何位置都能收。让人先找到那个框再拖，是没必要的一道关。 */
let dragDepth = 0;
window.addEventListener('dragenter', e => {
  if (!(e.dataTransfer && [...e.dataTransfer.types].includes('Files'))) return;
  dragDepth++; $('veil').classList.add('on');
});
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0; $('veil').classList.remove('on'); }
});
window.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; $('veil').classList.remove('on');
  send(e.dataTransfer.files);
});

async function send(files) {
  if (!files || !files.length) return;
  const body = new FormData();
  for (const f of files) body.append('files', f, f.name);

  /* 淘宝那张对账表 22 万行，整套跑下来要半分多钟。一句不动的「正在算账」挂在那里，
     人会以为卡死了然后刷新——刷新等于从头再来。报出秒数就知道它还在动。 */
  const box = $('progress');
  const t0 = Date.now();
  const tick = () => {
    const s = Math.round((Date.now() - t0) / 1000);
    box.innerHTML = `<span class="spin"></span> 正在认表、挂钩、算账……`
      + `${files.length} 个文件，已经 ${s} 秒`
      + (s > 20 ? `<div class="hint">几十万行的对账表要慢一点，别刷新。</div>` : '');
  };
  tick();
  box.classList.add('on');
  const timer = setInterval(tick, 1000);

  try {
    const data = await api('/api/upload', {method: 'POST', body});
    toast(data.summary);
    S.overview = null;
    afterUpload(data);
  } catch (err) {
    toast('没收下：' + err.message, true);
  } finally {
    clearInterval(timer);
    box.classList.remove('on');
  }
}

/** 交完表的落点：算出账就跳到那家店，没算出来就留在原地把原因摆出来。 */
function afterUpload(data) {
  const problems = [...(data.rejected || []), ...(data.failures || [])];
  if (data.periods && data.periods.length) {
    const p = data.periods[data.periods.length - 1];
    sessionStorage.setItem('lastIntake', JSON.stringify({
      rejected: data.rejected || [], failures: data.failures || [],
      unknown: data.unknown_tables || [],
    }));
    location.hash = `#/store/${encodeURIComponent(p.store_id)}?period=${encodeURIComponent(p.period)}`;
    if (location.hash === decodeURI(location.hash)) route();
    return;
  }
  sessionStorage.setItem('lastIntake', JSON.stringify({
    rejected: data.rejected || [], failures: data.failures || [],
    unknown: data.unknown_tables || [],
  }));
  if (!problems.length) toast('收下了，但没算出账期。看下面的原因。', true);
  route();
}

function intakeNotes() {
  const raw = sessionStorage.getItem('lastIntake');
  if (!raw) return '';
  sessionStorage.removeItem('lastIntake');
  let d;
  try { d = JSON.parse(raw); } catch (e) { return ''; }
  let out = '';
  if (d.rejected && d.rejected.length) {
    out += `<div class="card"><div class="banner warn">
      <strong>${d.rejected.length} 份表没进账</strong>
      <ul>${d.rejected.map(r => {
        const s = r.suggest || {};
        const hint = s.store
          ? `——看着像 <b>${esc(s.store)}</b>${s.platform ? `，平台 ${esc(s.platform)}` : '，平台认不出'}`
          : '';
        return `<li>${esc(r.file)}：${esc(r.why)}${hint}</li>`;
      }).join('')}</ul>
      </div><p class="small muted" style="margin-top:12px">
      认不出归属的表不会塞进某家店凑数——那会把一家店的钱记到另一家头上，事后极难发现。
      到<a href="#/stores">店铺</a>里登记，然后重传。</p></div>`;
  }
  if (d.failures && d.failures.length) {
    out += d.failures.map(f => `<div class="card"><div class="banner bad">
      <strong>${esc(f.store)}：${esc(f.why)}</strong>
      <ul>${(f.reasons || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul></div></div>`).join('');
  }
  if (d.unknown && d.unknown.length) {
    out += `<div class="card"><div class="banner warn">
      <strong>${d.unknown.length} 张表没有模板认识</strong>
      <ul>${d.unknown.map(u => {
        const q = u.sheet ? '?sheet=' + encodeURIComponent(u.sheet) : '';
        return `<li>${esc(u.file)}${u.sheet ? ' · ' + esc(u.sheet) : ''}：${esc(u.reason)}
          <a href="#/onboard/${encodeURIComponent(u.sha)}${q}">接进来 →</a></li>`;
      }).join('')}</ul></div>
      <p class="small muted" style="margin-top:12px">
      接一张新表不用改代码：向导会看着表头和值提议一份列到角色的映射，
      你确认之后先试跑一遍，跑得通才写进模型。</p></div>`;
  }
  return out;
}

// --------------------------------------------------------------------------
// 总览
// --------------------------------------------------------------------------

async function viewOverview() {
  if (!S.overview) S.overview = await api('/api/overview');
  const d = S.overview;
  $('c-stores').textContent = d.stores.length || '';
  $('c-open').textContent = d.cells.filter(c => c.state === 'open').length || '';

  if (!d.cells.length) return void (main.innerHTML = `
    <header><h1>总览</h1><div class="sub">还没有任何数据</div></header>
    ${dropzone()}
    ${intakeNotes()}
    <div class="card"><div class="empty">
      <h3>先把一个月的表交上来</h3>
      <p>已经登记了 ${d.stores.length} 家店。文件名别改——认哪家店、认哪张表全靠它。
      交齐一家店一个月的表，损益表和自检结论就出来了。</p>
    </div></div>`);

  const periods = d.periods.slice(0, 6);
  const latest = d.totals[0];
  const byStore = new Map();
  for (const c of d.cells) {
    if (!byStore.has(c.store_id)) byStore.set(c.store_id, {store: c, cells: new Map()});
    byStore.get(c.store_id).cells.set(c.period, c);
  }

  const needWork = d.cells.filter(c => c.state === 'open' && !c.can_close).length;
  main.innerHTML = `
    <header>
      <h1>总览</h1>
      <div class="sub">${byStore.size} 家店 · ${d.periods.length} 个账期${
        latest ? ` · 最近 ${esc(latest.period)}` : ''}</div>
    </header>
    ${latest ? `<div class="kpis">
      <div class="kpi"><div class="label">${esc(latest.period)} 销售收入</div>
        <div class="value">${brief(latest.revenue)}</div>
        <div class="note">${latest.stores} 家店合计${
          latest.incomplete ? `，${latest.incomplete} 家数据不全没计入` : ''}</div></div>
      <div class="kpi"><div class="label">${esc(latest.period)} 利润</div>
        <div class="value">${brief(latest.profit)}</div>
        <div class="note">${latest.revenue ? (latest.profit / latest.revenue * 100).toFixed(1) + '% 利润率' : '—'}</div></div>
      <div class="kpi"><div class="label">已结账</div>
        <div class="value">${latest.closed}/${latest.stores}</div>
        <div class="note">${esc(latest.period)}</div></div>
      <div class="kpi"><div class="label">还结不了</div>
        <div class="value">${needWork}</div>
        <div class="note">${needWork ? '点进去看卡在哪' : '全部通过自检'}</div></div>
    </div>` : ''}
    ${intakeNotes()}
    <div class="card">
      <header><h2>店铺 × 账期</h2>
        <span class="sub">格子里是利润。点开看损益、下钻、结账</span></header>
      <div class="matrix"><table>
        <thead><tr><th>店铺</th>${
          periods.map(p => `<th class="period">${esc(p)}</th>`).join('')}</tr></thead>
        <tbody>${[...byStore.values()].map(r => matrixRow(r, periods)).join('')}</tbody>
      </table></div>
    </div>
    ${dropzone()}`;
  wireDrop();
}

function matrixRow(row, periods) {
  const s = row.store;
  return `<tr>
    <td class="store"><div class="name">${esc(s.store)}</div>
      <div class="meta">${esc(s.platform)}${s.entity ? ' · ' + esc(s.entity) : ' · 主体未配'}</div></td>
    ${periods.map(p => `<td class="cell">${cellBox(row.cells.get(p), s.store_id, p)}</td>`).join('')}
  </tr>`;
}

function cellBox(c, storeId, period) {
  if (!c) return '<span class="cell-none">—</span>';
  const cls = ['cell-box',
    c.state === 'closed' ? 'closed' : '',
    !c.can_close && c.state === 'open' ? 'blocked' : '',
    c.stale ? 'stale' : ''].join(' ');
  const state = c.stale ? '已结账 · 有新数据'
    : c.state === 'closed' ? '已结账'
    : c.can_close ? '可结账'
    : c.missing.length ? `缺 ${c.missing.length} 项`
    : c.blocking.length ? `${c.blocking.length} 项拦住` : '进行中';
  return `<button class="${cls}" data-go="${esc(storeId)}" data-period="${esc(period)}">
    <span class="amount">${brief(c.profit)}</span>
    <span class="state">${esc(state)}</span></button>`;
}

function dropzone() {
  return `<div id="drop" class="card" style="margin-top:16px">
    <p>把文件拖到这里，或者点一下选</p>
    <small>xlsx / xls / xlsb / csv / zip 都行，一次可以多选。文件名别改，认哪家店靠它。</small>
  </div>`;
}

function wireDrop() {
  const el = $('drop');
  if (el) el.onclick = () => picker.click();
  main.querySelectorAll('[data-go]').forEach(b => {
    b.onclick = () => {
      location.hash = `#/store/${encodeURIComponent(b.dataset.go)}`
        + `?period=${encodeURIComponent(b.dataset.period)}`;
    };
  });
}

// --------------------------------------------------------------------------
// 单店
// --------------------------------------------------------------------------

async function viewStore(storeId, period) {
  const d = await api('/api/stores/' + encodeURIComponent(storeId));
  const store = d.store;
  if (!d.periods.length) {
    main.innerHTML = header(store, d) + intakeNotes() + `
      <div class="card"><div class="empty">
        <h3>这家店还没算出账</h3>
        <p>已经收下 ${d.files.length} 份表。要出损益表，至少得有订单明细——
        它是脊柱，别的费用都挂在订单上。</p>
      </div></div>` + dropzone();
    wireDrop();
    return;
  }
  const chosen = d.periods.find(p => p.period === period) || d.periods[0];
  const snap = await api(`/api/stores/${encodeURIComponent(storeId)}`
    + `/periods/${encodeURIComponent(chosen.period)}`);

  main.innerHTML = header(store, d)
    + periodBar(d.periods, chosen.period)
    + intakeNotes()
    + `<div class="card">
        ${verdict(snap)}
        ${closer(store.id, chosen.period, snap)}
        ${statement(snap)}
        ${storeCommission(snap)}
        ${sources(snap)}
        ${unlinked(snap)}
        ${unclassified(snap)}
        ${quality(snap)}
      </div>`
    + dropzone();
  wireDrop();
  wireStore(store.id, chosen.period, snap);
}

function header(store, d) {
  const files = d.files.length;
  return `<header>
    <div class="spread">
      <div>
        <h1>${esc(store.name)}</h1>
        <div class="sub">${esc(store.platform)} · ${
          store.entity ? esc(store.entity)
            : '<a href="#/stores">主体未配置，去配</a>'} · 已收 ${files} 份表</div>
      </div>
      <a href="#/">← 总览</a>
    </div>
  </header>`;
}

function periodBar(periods, chosen) {
  return `<div class="periodbar">${periods.map(p => {
    const mark = p.state === 'closed' ? (p.stale ? ' ·有新数据' : ' ·已结') : (p.can_close ? '' : ' ·结不了');
    return `<button class="${p.period === chosen ? 'on' : ''}" data-period="${esc(p.period)}">
      ${esc(p.period)}<span class="muted xs">${esc(mark)}</span></button>`;
  }).join('')}</div>`;
}

function verdict(s) {
  const blockers = s.findings.filter(f => !f.passed && f.blocking);
  const warns = s.findings.filter(f => !f.passed && !f.blocking);
  if (s.can_close && !warns.length) {
    return `<div class="banner ok"><strong>可以结账</strong>${s.findings.length} 项自检全部通过。</div>`;
  }
  const cls = blockers.length ? 'bad' : 'warn';
  const title = blockers.length
    ? `不能结账：${blockers.length} 项拦住了`
    : `可以结账，但有 ${warns.length} 项要留意`;
  return `<div class="banner ${cls}"><strong>${title}</strong><ul>${
    blockers.map(f => `<li>${esc(f.name)}——${esc(f.message)}</li>`).join('')
    + warns.map(f => `<li>${esc(f.name)}——${esc(f.message)}</li>`).join('')}</ul></div>`;
}

function closer(storeId, period, s) {
  const pill = s.state === 'closed'
    ? `<span class="pill ${s.stale ? 'warn' : 'ok'}"><span class="dot"></span>${
        s.stale ? '已结账，之后有新数据交上来' : '已结账'}${s.by ? ' · ' + esc(s.by) : ''}</span>`
    : `<span class="pill"><span class="dot"></span>进行中</span>`;
  const btn = s.state === 'closed'
    ? `<button class="danger" id="reopen">反结账</button>`
    : `<button class="primary" id="close" ${s.can_close ? '' : 'disabled'}>结账</button>`;
  return `<div class="spread" style="margin:16px 0 20px">
    <div class="row">${pill}
      <span class="muted xs">${s.at ? '算于 ' + esc(when(s.at)) : ''}${
        /* 哪一版引擎算的。改坏了要回滚，得先说得清回到哪一版；-dirty 是拿没进
           版本库的代码算的，回不去，所以单独标出来。 */
        s.engine ? ' · 引擎 ' + esc(s.engine) : ''}${
        s.note ? ' · ' + esc(s.note) : ''}</span></div>
    <div class="row">
      <button class="link" id="recompute">重算</button>
      ${btn}
    </div></div>`;
}

function statement(s) {
  const rows = s.statement.map(nd => {
    const cls = ['lv' + Math.min(nd.level, 2), nd.is_total ? 'total' : '',
      nd.drillable ? 'drillable' : ''].join(' ');
    let line = `<tr class="${cls}"${nd.drillable ? ` data-node="${esc(nd.id)}"` : ''}>
      <td class="name">${esc(nd.name)}</td>
      <td class="amt">${nd.available ? money(nd.value, nd.display) : '<span class="na">—</span>'}</td></tr>`;
    if (!nd.available && nd.missing_sources.length) {
      line += `<tr class="why"><td colspan="2">缺 ${
        nd.missing_sources.map(esc).join('、')}，这一项不出数</td></tr>`;
    }
    return line;
  }).join('');
  return `<div class="statement"><table><tbody>${rows}</tbody></table>
    <p class="xs muted" style="margin-top:10px">带 › 的行可以点开，看它由哪些原始行组成。</p></div>`;
}

/* 提成跟着损益表一起显示，而不是单开一页。它是从上面那张表的毛利派生出来的，
   两个数放在一屏里，对不上一眼就看得出来；分开放，就得靠人记住上一页是多少。 */
function storeCommission(s) {
  const c = s.commission;
  if (!c) return '';
  if (!c.configured) {
    return `<div class="panel"><h3>提成</h3>
      <p class="muted small">${c.notes && c.notes.length ? esc(c.notes[0])
        : '这家店还没配提成。'} <a href="#/commission">去配</a></p></div>`;
  }
  const rows = c.people.map(p => `<tr>
    <td class="name">${esc(p.person)}</td>
    <td class="amt">${money(p.amount)}</td>
    <td class="amt muted">${money(p.base)}</td>
    <td class="muted small">${count(p.products)} 个商品</td></tr>`).join('');
  const flags = [];
  if (c.unassigned_base) flags.push(
    `${money(c.unassigned_base)} 的${esc(c.base_name)}没有分配对象`);
  if (c.fallback_base) flags.push(`${money(c.fallback_base)} 走店铺兜底`);
  if (c.negative_orders) flags.push(
    `${count(c.negative_orders)} 单${esc(c.base_name)}为负，合计 ${money(c.negative_base)}，
     按现行口径冲减了提成`);
  return `<div class="panel"><h3>提成 ${money(c.total)}</h3>
    <table class="grid" style="max-width:620px"><thead><tr>
      <th>人员</th><th class="amt">提成</th><th class="amt">计提基数</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>
    ${flags.length ? `<p class="small muted" style="margin-top:10px">${
      flags.map(esc).join('；')}。</p>` : ''}
    <p class="xs muted" style="margin-top:6px">
    按${esc(c.base_name)} ${money(c.base_total)} 计提，取每单下单时生效的那一版比例。
    <a href="#/commission">看配置</a></p></div>`;
}

function sources(s) {
  const missing = s.sources.filter(x => !x.arrived);
  if (!s.sources.length) return '';
  return `<div class="panel"><h3>该交的表 ${s.sources.length - missing.length}/${s.sources.length}</h3>
    <table style="max-width:620px"><tbody>${s.sources.map(x => `<tr class="${x.arrived ? '' : 'quiet'}">
      <td style="width:1%;white-space:nowrap">${esc(x.name)}</td>
      <td style="width:1%">${x.arrived ? '<span class="pill ok"><span class="dot"></span>已到</span>'
        : '<span class="pill warn"><span class="dot"></span>没到</span>'}</td>
      <td class="muted small">${esc(x.reason)}</td></tr>`).join('')}</tbody></table></div>`;
}

/* 每类钱为什么不用管。要人查的那类不在这里——它本来就该占注意力。 */
const BUCKET_WHY = {
  '其他店的数据（公司级主表）': '交上来就是全公司的，绝大多数属于别家店',
  '非经营流水（规则已排除）': '理财、调拨、保证金、广告预充值，不是损益',
  '其他账期的订单': '订单号是对的，但订单不在本期。跨期结算，或者导出时日期选宽了',
};
const NEEDS_WORK = '取不出订单号，要查归属';

function unlinked(s) {
  if (!s.unlinked_buckets.length) return '';
  const rows = s.unlinked_buckets.map(b => {
    const why = BUCKET_WHY[b.label] || '';
    return `<tr class="${why ? 'quiet' : ''}">
      <td>${esc(b.label)}${why ? `<div class="xs muted">${esc(why)}</div>` : ''}</td>
      <td class="right num">${money(b.amount)}</td>
      <td class="right num muted small">${count(b.count)} 笔</td></tr>`;
  }).join('');
  return `<div class="panel"><h3>没进利润的钱：${
    s.unlinked_total ? '要查归属的 ' + money(s.unlinked_total) : '没有要查归属的'}</h3>
    <table style="max-width:620px"><tbody>${rows}</tbody></table>
    <p class="small muted" style="margin-top:10px">${
      s.unlinked_total
        ? `只有「${NEEDS_WORK}」要人查，查清了才会进利润。其余几类本来就不该算本店这一期。`
        : '列出来的几类都不该算本店这一期，不用管。'}</p></div>`;
}

function unclassified(s) {
  if (!s.unclassified || !s.unclassified.length) return '';
  return `<div class="panel"><h3>没认出来的科目 ${s.unclassified.length} 类</h3>
    <table style="max-width:620px"><tbody>${s.unclassified.slice(0, 12).map(u => `<tr>
      <td>${esc(u.label)}</td>
      <td class="right num">${money(u.amount)}</td>
      <td class="right num muted small">${count(u.count)} 行</td></tr>`).join('')}</tbody></table>
    <p class="small muted" style="margin-top:10px">
    金额大的先处理：往科目字典里补一条，或者在模板的归类规则里排除掉。
    补完重算，这一行就消失。</p></div>`;
}

function quality(s) {
  if (!s.quality || !s.quality.length) return '';
  const rate = v => {
    if (v === null || v === undefined) return '<span class="na">不适用</span>';
    const pct = Math.max(0, Math.min(1, v)) * 100;
    const cls = v >= .98 ? '' : v >= .9 ? 'mid' : 'low';
    return `<span class="qbar ${cls}"><i style="width:${pct.toFixed(0)}%"></i></span>`
      + (v * 100).toFixed(1) + '%';
  };
  const why = q => q.coverage === null
    ? '偶发项，不按订单数算'
    : `${count(q.covered)}/${count(q.expected)}${q.expect_label ? ' · ' + esc(q.expect_label) : ''}`;
  return `<div class="panel"><h3>挂得准不准，盖得全不全</h3>
    <table><thead><tr><th>科目</th><th class="right">行数</th>
      <th class="right">命中率</th><th class="right">覆盖率</th><th>分母</th></tr></thead>
    <tbody>${s.quality.map(q => `<tr class="${q.coverage === null ? 'quiet' : ''}">
      <td>${esc(q.name)}${q.company_wide
        ? '<div class="xs muted">公司级主表，本店只认领一部分</div>' : ''}</td>
      <td class="right num muted">${count(q.rows)}</td>
      <td class="right num">${rate(q.hit_rate)}</td>
      <td class="right num">${rate(q.coverage)}</td>
      <td class="xs muted">${why(q)}</td></tr>`).join('')}</tbody></table>
    <p class="small muted" style="margin-top:10px">
    命中率高而覆盖率低是最危险的组合：钱少算了一半，但所有关联指标都是绿的。
    只有「每个订单都该有」的科目报覆盖率，分母也只算预期该有这项数据的订单——
    没发货的订单本来就没有出库成本。偶发科目和公司级主表报覆盖率只会一片红，
    真正的缺数据信号反而没人看得见。</p></div>`;
}

function wireStore(storeId, period, snap) {
  main.querySelectorAll('.periodbar button').forEach(b => {
    b.onclick = () => {
      location.hash = `#/store/${encodeURIComponent(storeId)}`
        + `?period=${encodeURIComponent(b.dataset.period)}`;
    };
  });
  main.querySelectorAll('tr.drillable').forEach(tr => {
    tr.onclick = () => openDrill(snap.run_id, tr.dataset.node,
      tr.querySelector('.name').textContent);
  });

  const close = $('close');
  if (close) close.onclick = async () => {
    close.disabled = true;
    try {
      await api(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/close`,
        json('POST'));
      toast(`${period} 已结账，数字冻住了`);
      S.overview = null;
      route();
    } catch (err) { toast('结不了：' + err.message, true); close.disabled = false; }
  };

  const reopen = $('reopen');
  if (reopen) reopen.onclick = async () => {
    const note = prompt('为什么要反结账？（必填，会记进留痕）');
    if (!note) return;
    try {
      await api(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/reopen`,
        json('POST', {note}));
      toast(`${period} 已反结账`);
      S.overview = null;
      route();
    } catch (err) { toast(err.message, true); }
  };

  const again = $('recompute');
  if (again) again.onclick = async () => {
    again.disabled = true;
    again.textContent = '正在算……';
    try {
      await api(`/api/stores/${encodeURIComponent(storeId)}/recompute`, json('POST'));
      toast('算完了');
      S.overview = null;
      route();
    } catch (err) { toast(err.message, true); again.disabled = false; again.textContent = '重算'; }
  };
}

// --------------------------------------------------------------------------
// 下钻
// --------------------------------------------------------------------------

$('drawer-close').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;
window.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

function closeDrawer() {
  $('drawer').classList.remove('on');
  $('scrim').classList.remove('on');
}

async function openDrill(runId, nodeId, name) {
  if (!runId) return toast('这个账期没留明细，先重算一次', true);
  $('drawer-title').textContent = name;
  $('drawer-sub').textContent = '正在取明细……';
  $('drawer-body').innerHTML = '<div class="empty"><span class="spin"></span></div>';
  $('drawer').classList.add('on');
  $('scrim').classList.add('on');
  try {
    const d = await api(`/api/runs/${runId}/drill/${encodeURIComponent(nodeId)}`);
    $('drawer-sub').textContent =
      `${count(d.rows)} 行 · 合计 ${d.total.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
      + ` · ${d.metrics.map(m => m.name).join('、')}`;
    $('drawer-body').innerHTML = drillBody(d);
  } catch (err) {
    $('drawer-sub').textContent = '';
    $('drawer-body').innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
  }
}

function drillBody(d) {
  if (!d.rows) return '<div class="empty"><h3>没有明细</h3><p>这一项本期没有数据。</p></div>';
  return `
    ${d.by_subject.length ? `<h3>按科目</h3>
    <table><tbody>${d.by_subject.map(x => `<tr>
      <td>${esc(x.subject)}</td>
      <td class="right num">${money(x.amount)}</td>
      <td class="right num muted small">${count(x.count)} 行</td></tr>`).join('')}</tbody></table>
    <h3 style="margin-top:24px">按来源文件</h3>` : `<h3>按来源文件</h3>`}
    <table><tbody>${d.by_file.map(x => `<tr>
      <td class="truncate">${esc(x.file)}${x.sheet ? ` <span class="muted xs">${esc(x.sheet)}</span>` : ''}</td>
      <td class="right num">${money(x.amount)}</td>
      <td class="right num muted small">${count(x.count)} 行</td></tr>`).join('')}</tbody></table>

    <h3 style="margin-top:24px">原始行（金额最大的 ${d.sample.length} 行）</h3>
    ${(() => {
      // 这项没有科目列时就别摆一列空白。空列比没有列更让人怀疑数据丢了。
      const named = d.sample.some(r => r.minor || r.subject);
      return `<table><thead><tr><th>订单</th>${named ? '<th>科目</th>' : ''}
        <th class="right">金额</th><th>出处</th></tr></thead>
      <tbody>${d.sample.map(r => `<tr>
        <td class="num xs">${esc(r.link_key || '—')}</td>
        ${named ? `<td class="small">${esc(r.minor || r.subject || '')}</td>` : ''}
        <td class="right num">${money(r.amount)}</td>
        <td class="evidence truncate">${esc(r.file_name)}${
          r.sheet ? ' · ' + esc(r.sheet) : ''} · 第 ${r.row_no} 行</td></tr>`).join('')}</tbody></table>`;
    })()}
    ${d.truncated ? `<p class="small muted" style="margin-top:10px">
      一共 ${count(d.rows)} 行，这里只列了金额最大的 ${d.sample.length} 行——异常基本都在两端。</p>` : ''}`;
}

// --------------------------------------------------------------------------
// 数据交付
// --------------------------------------------------------------------------

async function viewDeliver() {
  if (!S.overview) S.overview = await api('/api/overview');
  const stores = S.overview.stores.filter(s => !s.archived);
  const details = await Promise.all(
    stores.map(s => api('/api/stores/' + encodeURIComponent(s.id)).catch(() => null)));
  const rows = details.filter(Boolean);
  const total = rows.reduce((n, d) => n + d.files.length, 0);
  $('c-files').textContent = total || '';

  main.innerHTML = `
    <header><h1>数据交付</h1>
      <div class="sub">谁交了什么、哪一版在算、还缺什么。共 ${total} 份表</div></header>
    ${dropzone()}
    ${rows.map(deliverCard).join('')}`;
  wireDrop();
  main.querySelectorAll('[data-drop-file]').forEach(b => {
    b.onclick = async () => {
      if (!confirm(`把「${b.dataset.dropFile}」撤下来？内容会留档，只是不再参与计算。`)) return;
      b.disabled = true;
      try {
        await api(`/api/stores/${encodeURIComponent(b.dataset.store)}/files`
          + `?name=${encodeURIComponent(b.dataset.dropFile)}`, {method: 'DELETE'});
        toast('已撤下并重算');
        S.overview = null;
        route();
      } catch (err) { toast(err.message, true); b.disabled = false; }
    };
  });
}

function deliverCard(d) {
  const s = d.store;
  return `<div class="card">
    <header><h2>${esc(s.name)}</h2>
      <span class="sub">${esc(s.platform)} · ${d.periods.length} 个账期 · ${d.files.length} 份表</span>
    </header>
    ${d.files.length ? `<table class="deliver">
      <thead><tr><th>文件</th><th>交表人</th><th class="right">版本</th>
        <th>最近更新</th><th></th></tr></thead>
      <tbody>${d.files.map(f => `<tr>
        <td class="file">${esc(f.name)}<small>${(f.size / 1024).toFixed(0)} KB</small></td>
        <td class="small muted">${esc(f.by || '—')}</td>
        <td class="right num">${f.versions > 1
          ? `<span class="pill accent">${f.versions} 版</span>` : '1'}</td>
        <td class="small muted">${esc(when(f.updated_at))}</td>
        <td class="right"><button class="link tiny" data-store="${esc(s.id)}"
          data-drop-file="${esc(f.name)}">撤下</button></td></tr>`).join('')}</tbody></table>
      <p class="xs muted" style="margin-top:10px">
      同名文件重新上传算新版本，旧版本自动退出计算——重导出的表名字不会变，
      两版都算就是双份成本。</p>`
      : '<div class="empty"><p>这家店还没交过表。</p></div>'}
  </div>`;
}

// --------------------------------------------------------------------------
// 提成
// --------------------------------------------------------------------------

/* 「按人」那张表排在最前面，因为它是这一页唯一拿去做事的东西——按它发钱。
   底下的按店、按配置都是用来解释它为什么是这个数的。 */

async function viewCommission(period) {
  const d = await api('/api/commission' + (period ? '?period=' + encodeURIComponent(period) : ''));
  const cfg = await api('/api/commission/config');
  $('c-comm').textContent = d.people.length || '';

  main.innerHTML = `
    <header><h1>提成</h1>
      <div class="sub">按${esc(d.base_name || '毛利')}算。下单那一刻生效的比例是多少，
      这一单就按多少算——之后再改配置，也不会动到已经下过的单。</div></header>
    ${d.periods.length ? `<div class="periodbar">${d.periods.map(p =>
      `<button class="${p === d.period ? 'on' : ''}" data-period="${esc(p)}">${esc(p)}</button>`
    ).join('')}</div>` : ''}
    ${commissionPeople(d)}
    ${commissionStores(d)}
    ${commissionConfig(cfg, d)}`;
  wireCommission(d, cfg);
}

function commissionPeople(d) {
  if (!d.people.length) {
    /* 一家还没配提成的公司，打开这一页最需要的不是一句「还没配」，而是那份
       待配清单：商品和毛利系统已经知道了，人只要填谁拿多少。 */
    return `<div class="card"><div class="empty">
      <h3>${d.period ? d.period + ' 还没有算出提成' : '还没有账期'}</h3>
      <p>${d.rules ? '已经有 ' + d.rules + ' 条配置，但这个账期的账里没有匹配上的商品。'
        : '系统已经知道每个商品这个月赚了多少，只差谁拿多少。'}</p>
      ${d.period ? `<p style="margin-top:14px">
        <a class="button primary" href="/api/commission/products.csv?period=${
          encodeURIComponent(d.period)}">下载待配商品表</a></p>
      <p class="small muted">商品和本期${esc(d.base_name || '毛利')}都填好了，按${
        esc(d.base_name || '毛利')}从大到小排。填上人员和比例传回来即可，
      长尾商品不用一个个配，配一条商品留空的店铺兜底就行。</p>` : ''}
    </div></div>`;
  }
  const rows = d.people.map(p => `<tr>
    <td class="name">${esc(p.person)}</td>
    <td class="amt strong">${money(p.amount)}</td>
    <td class="amt muted">${money(p.base)}</td>
    <td class="muted small">${p.stores.map(s =>
      esc(s.store) + ' ' + money(s.amount)).join('　')}</td></tr>`).join('');
  return `<div class="card">
    <header><h2>${esc(d.period)} 要发 ${money(d.total)}</h2>
      <span class="sub">${d.people.length} 个人</span></header>
    <table class="grid"><thead><tr>
      <th>人员</th><th class="amt">提成</th><th class="amt">计提基数</th><th>分布</th>
    </tr></thead><tbody>${rows}
    <tr class="total"><td>合计</td><td class="amt strong">${money(d.total)}</td>
      <td colspan="2"></td></tr></tbody></table>
    <p class="xs muted" style="margin-top:10px">
    这一栏的每个数都是各店金额相加，合计等于上面那些数逐个相加，不差分。</p>
  </div>`;
}

function commissionStores(d) {
  if (!d.stores.length) return '';
  const rows = d.stores.map(s => {
    const name = `<td class="name"><a href="#/store/${encodeURIComponent(s.store_id)}?period=${
      encodeURIComponent(d.period)}">${esc(s.store)}</a>
      <small class="muted"> ${esc(s.platform)}</small></td>`;
    /* 没算过的店不摆 0.00。摆了就分不清「算过、这个月没提成」和「压根没算」，
       而这两件事该做的下一步完全不同。 */
    if (!s.computed) {
      return `<tr class="quiet">${name}<td colspan="3" class="muted small">
        这个账期是加提成功能之前算的，还没有提成数</td>
        <td><button class="link tiny recomp" data-store="${esc(s.store_id)}">重算</button></td></tr>`;
    }
    const flags = [];
    if (!s.configured) flags.push('<span class="pill warn"><span class="dot"></span>没配提成</span>');
    if (s.unassigned_base) flags.push(
      `<span class="pill warn"><span class="dot"></span>${money(s.unassigned_base)} 没人管</span>`);
    if (s.negative_orders) flags.push(
      `<span class="muted xs">${count(s.negative_orders)} 单亏损 ${money(s.negative_base)}</span>`);
    if (s.state === 'closed') flags.push('<span class="pill ok"><span class="dot"></span>已结账</span>');
    return `<tr>${name}
      <td class="amt">${money(s.total)}</td>
      <td class="amt muted">${money(s.base_total)}</td>
      <td class="amt muted">${s.fallback_base ? money(s.fallback_base) : '<span class="na">—</span>'}</td>
      <td>${flags.join(' ')}</td></tr>`;
  }).join('');
  return `<div class="card">
    <header><h2>按店</h2><span class="sub">点店名去看这家店的账</span></header>
    <table class="grid"><thead><tr>
      <th>店铺</th><th class="amt">提成</th><th class="amt">${esc(d.base_name || '毛利')}</th>
      <th class="amt">走店铺兜底</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>
    ${d.unassigned_base ? `<div class="banner warn" style="margin-top:14px">
      <strong>${money(d.unassigned_base)} 没有分配对象</strong>
      这部分${esc(d.base_name || '毛利')}既没配商品负责人，也没有店铺兜底规则。
      不是算错，是没人配——补一条商品为空的店铺兜底规则就能兜住。</div>` : ''}
  </div>`;
}

function commissionConfig(cfg, d) {
  const rows = cfg.rules.slice(0, 200).map(r => `<tr>
    <td>${esc(r.effective_from)}</td>
    <td class="muted">${esc(r.store)}</td>
    <td>${r.product_id ? esc(r.product_id) : '<span class="pill">店铺兜底</span>'}
      ${r.product_name ? '<small class="muted"> ' + esc(r.product_name) + '</small>' : ''}</td>
    <td class="name">${esc(r.person)}</td>
    <td class="amt">${(r.share * 100).toFixed(2)}%</td>
    <td class="amt muted">${(r.total_rate * 100).toFixed(2)}%</td>
    <td class="muted small">${esc(r.note || '')}</td></tr>`).join('');
  return `<div class="card">
    <header><h2>配置 ${cfg.rules.length} 条</h2>
      <div class="row">
        ${d && d.period ? `<a class="link" href="/api/commission/products.csv?period=${
          encodeURIComponent(d.period)}">待配商品表</a>` : ''}
        <a class="link" href="/api/commission/config.csv">导出当前配置</a>
        <button class="primary" id="cfg-pick">传一份新的</button>
      </div></header>
    ${cfg.rules.length ? `<table class="grid"><thead><tr>
      <th>生效日期</th><th>店铺</th><th>商品</th><th>人员</th>
      <th class="amt">子提成率</th><th class="amt">总提成率</th><th>备注</th>
    </tr></thead><tbody>${rows}</tbody></table>
    ${cfg.rules.length > 200 ? `<p class="xs muted">只列了前 200 条，全部请导出看。</p>` : ''}`
    : `<div class="empty"><h3>还没有配置</h3>
       <p>导出会给你一份带表头的空表，照着填再传回来。</p></div>`}
    <input type="file" id="cfg-file" accept=".csv,.xlsx,.xls,.xlsm" style="display:none">
    <div class="panel" style="margin-top:20px"><h3>怎么填</h3>
      <table style="max-width:760px"><tbody>
        <tr><td style="width:1%;white-space:nowrap"><code>生效日期</code></td>
          <td class="muted small">从这天起按这一版算。<strong>当天下的单算新的。</strong>
          改比例、换人、离职、继承，一律是加一版新日期，旧的原样留着——
          这样以前算过的单重算一百遍都不变。</td></tr>
        <tr><td><code>店铺</code></td>
          <td class="muted small">填店铺 id（${cfg.stores.slice(0, 4).map(s =>
            '<code>' + esc(s.id) + '</code>').join('、')}${
            cfg.stores.length > 4 ? ' 等 ' + cfg.stores.length + ' 家' : ''}）。</td></tr>
        <tr><td><code>商品ID</code></td>
          <td class="muted small">留空表示这家店的兜底：没单独配人的商品都归这一版。</td></tr>
        <tr><td><code>子提成率</code></td>
          <td class="muted small">这个人分到多少。写 <code>3%</code> 或 <code>0.03</code> 都行。</td></tr>
        <tr><td><code>总提成率</code></td>
          <td class="muted small">这个商品这一版一共给出去多少。
          <strong>同一版里几个人的子提成率相加必须等于它</strong>，
          差一点就整份退回不落盘——少配一个人算出来的数完全合法，只是那个人一分钱没有，
          而他看不到这个界面。</td></tr>
      </tbody></table>
      <p class="small muted" style="margin-top:12px">
      传上来是整份替换，先整体校验，有一条不对就全退回。校验过了会把涉及的店重算一遍，
      免得配置已经变了、账期里的数字还是旧的。</p>
    </div>
  </div>`;
}

function wireCommission(d, cfg) {
  document.querySelectorAll('.periodbar button').forEach(b => {
    b.onclick = () => { location.hash = '#/commission?period=' + encodeURIComponent(b.dataset.period); };
  });
  document.querySelectorAll('.recomp').forEach(b => {
    b.onclick = async () => {
      b.disabled = true; b.textContent = '算着……';
      $('progress').className = 'on';
      try {
        await api('/api/stores/' + encodeURIComponent(b.dataset.store) + '/recompute',
          {method: 'POST'});
        route();
      } catch (err) { toast(err.message, true); b.disabled = false; b.textContent = '重算'; }
      finally { $('progress').className = ''; }
    };
  });
  const file = $('cfg-file');
  $('cfg-pick').onclick = () => file.click();
  file.onchange = async () => {
    if (!file.files.length) return;
    const body = new FormData();
    body.append('file', file.files[0], file.files[0].name);
    file.value = '';
    $('progress').className = 'on';
    try {
      const r = await api('/api/commission/config', {method: 'POST', body});
      toast(`存下 ${r.count} 条，重算了 ${r.stores.length} 家店`);
      route();
    } catch (err) {
      toast(err.message, true);
    } finally { $('progress').className = ''; }
  };
}

// --------------------------------------------------------------------------
// 店铺设置
// --------------------------------------------------------------------------

async function viewStores() {
  const d = await api('/api/stores');
  $('c-stores').textContent = d.stores.length || '';
  main.innerHTML = `
    <header><h1>店铺</h1>
      <div class="sub">法人主体这类东西数据里读不出来——支付宝和微信账单都不带主体信息，
      只能由人告诉引擎</div></header>
    <div class="card">
      <header><h2>已登记 ${d.stores.length} 家</h2></header>
      <div id="rows">${d.stores.map(storeRow).join('')}</div>
      <p class="small muted" style="margin-top:16px">
      主体是多对一的：几家店可以同属一个主体，这层关系推不出来只能配。改完立刻生效。
      店名和平台不能改——认文件靠店名，改了以前交过的表立刻认不出。</p>
    </div>
    <div class="card">
      <header><h2>登记一家新店</h2>
        <span class="sub">开新店、接新平台都走这里，不用改代码也不用改文件</span></header>
      <div class="row wrap">
        <input id="n-id" placeholder="英文 id，如 taobao_abc" style="width:200px">
        <input id="n-name" placeholder="店铺名（要和文件名里的一致）" style="width:240px">
        <select id="n-platform">${d.platforms.map(p =>
          `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select>
        <input id="n-entity" placeholder="法人主体（可空）" style="width:220px">
        <button class="primary" id="add">登记</button>
      </div>
      <p class="small muted" style="margin-top:12px">
      店铺名必须和文件名里那一截对得上。文件叫「聚水潭成本-淘宝喜必顺.xlsx」，
      店名就得是「淘宝喜必顺」。</p>
    </div>`;
  wireStores();
}

function storeRow(s) {
  return `<div class="storerow" data-id="${esc(s.id)}">
    <div class="who"><div class="name">${esc(s.name)}</div>
      <small>${esc(s.platform)}${s.archived ? ' · 已归档' : ''}</small></div>
    <input class="entity" value="${esc(s.entity)}" placeholder="法人主体全名（未配置）">
    <input class="taxid" value="${esc(s.entity_tax_id)}" placeholder="税号（可空）">
    <div class="row">
      <button class="save">保存</button>
      <button class="link tiny arch">${s.archived ? '取消归档' : '归档'}</button>
    </div>
    <span class="said"></span></div>`;
}

function wireStores() {
  $('rows').onclick = async e => {
    const row = e.target.closest('.storerow');
    if (!row) return;
    const said = row.querySelector('.said');
    const id = row.dataset.id;
    if (e.target.classList.contains('save')) {
      said.textContent = '正在存……';
      try {
        await api('/api/stores/' + encodeURIComponent(id), json('PATCH', {
          entity: row.querySelector('.entity').value.trim(),
          entity_tax_id: row.querySelector('.taxid').value.trim(),
        }));
        said.textContent = '已保存';
        setTimeout(() => { said.textContent = ''; }, 2500);
      } catch (err) { said.textContent = '没存上：' + err.message; }
    }
    if (e.target.classList.contains('arch')) {
      const on = e.target.textContent === '归档';
      try {
        await api('/api/stores/' + encodeURIComponent(id), json('PATCH', {archived: on}));
        toast(on ? '已归档，不再催它交表' : '已恢复在营');
        route();
      } catch (err) { toast(err.message, true); }
    }
  };

  $('add').onclick = async () => {
    const body = {
      id: $('n-id').value.trim(),
      name: $('n-name').value.trim(),
      platform: $('n-platform').value,
      entity: $('n-entity').value.trim(),
    };
    if (!body.id || !body.name) return toast('id 和店铺名都要填', true);
    try {
      await api('/api/stores', json('POST', body));
      toast('已登记 ' + body.name);
      route();
    } catch (err) { toast('登记不了：' + err.message, true); }
  };
}

// --------------------------------------------------------------------------
// 接新表
// --------------------------------------------------------------------------

/* 这一屏是「以后所有店铺自助接入」真正落地的地方。没有它，接一张新表要有人去改
   templates.yaml——那意味着接第四家店得等排期，引擎再通用也白搭。

   两条贯穿的规矩：
     提议只是默认值，落库的是人确认的那一份。字面像不等于同一个东西。
     不试跑不许落库。只看列名点确认，人确认的是一份纸面映射：表头行差一行、
     金额列混着「-」、表底合计没丢掉，全都在纸面上看不见，却会让金额错掉。 */

const W = {draft: null, roles: [], sources: [], tried: null};

/* 标签写的是「你要做什么」，不是「系统有多确定」。这四档对应四个不同的动作：
   放过、在几个里定一个、逐字核对已填的、自己挑一个。用同一个词概括它们，
   人就只能每行都去读右边那段依据，78 列读不完就会一路点下去。 */
const CONF = {
  exact: ['有把握', 'ok'],
  likely: ['要你定', 'warn'],
  guess: ['按字面猜', 'bad'],
  unknown: ['没见过', ''],
};

function conf(c) {
  /* 没填上但有候选，跟没填上也没候选，是两件事：前者是「在候选里挑一个」，
     后者是「这个数据源没有能装它的角色，可能得先加角色」。都写「没见过」的话，
     真正要挑的那几列就混在几十列噪音里了。 */
  if (c.confidence === 'unknown' && c.alternatives.length && !c.derived) {
    return ['要你挑', 'warn'];
  }
  return CONF[c.confidence] || CONF.unknown;
}

async function viewOnboard(sha, sheet, headerRow, source) {
  const q = new URLSearchParams();
  if (sheet) q.set('sheet', sheet);
  if (headerRow !== undefined && headerRow !== '') q.set('header_row', headerRow);
  if (source) q.set('source', source);
  const d = await api(`/api/onboard/${encodeURIComponent(sha)}?${q}`);
  W.draft = d;
  W.tried = null;
  const r = await api('/api/roles?source=' + encodeURIComponent(d.source || ''));
  W.roles = r.roles;
  W.sources = r.sources;

  const src = W.sources.find(s => s.id === d.source);
  main.innerHTML = `
    <header><h1>接一张新表</h1>
      <div class="sub">${esc(d.file)}${d.sheet ? ' · ' + esc(d.sheet) : ''}
        · ${count(d.rows)} 行 · ${esc(d.summary)}</div></header>

    <div id="w-warn">${warnCard(d)}</div>

    <div class="card">
      <header><h2>这张表是什么</h2>
        <span class="sub">${d.kind === 'revision'
          ? `看着是「${esc(d.base)}」改版——平台加减了几列`
          : '没见过的表'}</span></header>
      <div class="row wrap">
        <label class="fld">表头在第几行
          <input id="w-header" type="number" min="0" max="20" value="${d.header_row}" style="width:80px">
        </label>
        <label class="fld">挂到哪个数据源
          <select id="w-source">${W.sources.map(s =>
            `<option value="${esc(s.id)}"${s.id === d.source ? ' selected' : ''}>${esc(s.name)}${
              s.is_spine ? '（脊柱）' : ''}</option>`).join('')}</select>
        </label>
        <label class="fld">模板 id
          <input id="w-id" value="${esc(d.suggest_id)}" style="width:190px">
        </label>
        <label class="fld">模板名
          <input id="w-name" placeholder="给人看的中文名" style="width:220px">
        </label>
      </div>
      <p class="small muted" style="margin-top:12px">
      表头行是所有参数里最容易错的一个：差一行，第一行数据会被当成表头，于是每列都认不出来，
      而报出来的现象只是「没见过这种表头」。改完会重新提议。
      ${src ? (src.metrics.length
        ? `数据源「${esc(src.name)}」有 ${src.metrics.length} 个指标从它取数：${esc(src.metrics.slice(0, 4).join('、'))}。`
        : `数据源「${esc(src.name)}」目前没有指标从它取数——接上之后能解析能查，但不会进损益表。`) : ''}</p>
    </div>

    <div id="w-assist"></div>
    <div id="w-cols">${columnCards(d)}</div>

    <div class="card">
      <header><h2>试跑</h2>
        <span class="sub">用这份映射真解析一遍，不写任何东西</span></header>
      <div class="row">
        <button class="primary" id="w-try">试跑</button>
        <button id="w-land" disabled>落库并重算</button>
        <span class="said" id="w-said"></span>
      </div>
      <div id="w-result"></div>
      <p class="small muted" style="margin-top:12px">
      不试跑不能落库。列名对上不代表值取得对——表底那行合计不丢掉，每一列金额刚好翻倍，
      而这在纸面上完全看不出来：列名全对、填充率 100%、行数也只多一行。</p>
    </div>`;
  wireOnboard();
  askModel(sha, sheet, headerRow, source);
}

/* 警告说的全是「照现在这份映射落库会出什么事」。映射一改就得重画,不然屏幕上会
   同时挂着「spend 没映上」和一行映着 spend 的表格——自相矛盾的警告比没有警告坏,
   它会让人开始忽略所有警告。 */
function warnCard(d) {
  return (d.warnings || []).length ? `<div class="card"><div class="banner warn">
    <strong>接之前先看这几件事</strong>
    <ul>${d.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div></div>` : '';
}

/* 规则草案已经在屏幕上了,这一步再去问模型,回来把它的意见叠上去。

   分两次请求不是为了快,是为了人能对照:先看到的是确定性那份,模型动了哪几列
   一目了然。合成一次的话,屏幕上是一份分不清谁提的混合结果,而这套东西的全部
   安全性都建立在「模型错了有人看得出来」上面。

   模型没配、超时、答得不成样子,这里安静地什么都不做——向导本来就不靠它。 */
async function askModel(sha, sheet, headerRow, source) {
  const box = $('w-assist');
  if (!box) return;
  const asked = W.draft && W.draft.sha;
  box.innerHTML = `<div class="card"><span class="small muted">正在问模型…（不影响上面这份规则提议）</span></div>`;
  let d;
  try {
    const q = new URLSearchParams();
    if (sheet) q.set('sheet', sheet);
    if (headerRow !== undefined && headerRow !== '') q.set('header_row', headerRow);
    if (source) q.set('source', source);
    d = await api(`/api/onboard/${encodeURIComponent(sha)}/assist?${q}`);
  } catch (e) {
    box.innerHTML = '';
    return;
  }
  /* 期间人改了表头行或数据源,这份回来的已经不是他正在看的那张表了。 */
  if (!W.draft || W.draft.sha !== asked || !$('w-assist')) return;

  const a = d.assist || {};
  if (!a.ok) {
    box.innerHTML = a.summary
      ? `<div class="card"><span class="small muted">${esc(a.summary)}</span></div>` : '';
    return;
  }
  /* 人可能已经动过下拉框了。把他改过的保留下来,模型不许覆盖人的手。

     判据是「现在框里的值 ≠ 规则当初提的值」。拿模型那份当基准是不行的:规则本来
     没填、模型刚补上的那一列,框里还是空的,一比就成了「人清空过」,于是模型补的
     那一列被当作人的决定丢掉——屏幕上是下拉框写着「不映射」、旁边挂着「模型说
     spend」的标签,自相矛盾。 */
  const ruleSaid = {};
  (W.draft.columns || []).forEach(c => ruleSaid[c.index] = c.role);
  const mineNow = {};
  main.querySelectorAll('tr[data-index]').forEach(tr => {
    mineNow[tr.dataset.index] = tr.querySelector('.w-role').value;
  });
  const mine = c => c.index in mineNow && mineNow[c.index] !== (ruleSaid[c.index] ?? '');
  const touched = d.columns.filter(mine);
  W.draft = {...d, columns: d.columns.map(c => mine(c) ? {...c, role: mineNow[c.index]} : c)};

  box.innerHTML = `<div class="card">
    <header><h2>模型看了一遍</h2>
      <span class="sub">${esc(a.model)} · ${(a.elapsed_ms / 1000).toFixed(1)} 秒</span></header>
    <p class="small">${esc(a.summary)}</p>
    ${a.adopted.length ? `<p class="small">规则没认出来、采纳了模型的：
      <span class="mono">${a.adopted.map(esc).join('、')}</span>
      —— 这几列在下面标着「模型提的」，理由摆在依据那一列。</p>` : ''}
    ${a.disputed.length ? `<div class="banner warn"><strong>这几列两边说法不一样，保留的是规则那份</strong>
      <ul>${a.disputed.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
      <p class="small">下拉框里两个都在，你挑。悄悄采纳模型的说法是不行的——
      那样「规则提议」里就混着模型的猜测，再没有一处能对照。</p></div>` : ''}
    ${a.refused.length ? `<p class="small muted">被挡掉 ${a.refused.length} 条：
      ${a.refused.map(esc).join('；')}</p>` : ''}
    ${touched.length ? `<p class="small muted">你已经改过的
      ${touched.length} 列保持你的选择，模型不覆盖。</p>` : ''}
    <p class="small muted">模型只在这一步说话。往下的试跑、合计行、掉行率、控制合计、
    脊柱缺列全是确定性检查，它一道都绕不过去；这里错了的后果是你多点几下，不是钱算错。</p>
  </div>`;
  $('w-warn').innerHTML = warnCard(W.draft);
  $('w-cols').innerHTML = columnCards(W.draft);
  wireRoles(a.adopted.length > 0);
}

/* 78 列平铺是不可读的。万相台那张表里 71 列是平台自带的展现量、转化率、投产比,
   它们跟账没关系,但每一行都占着和「消耗金额」一样的视觉分量,于是真正要拍板的
   那 3 列被埋掉。所以分两段:要你拍板的摆在前面且给足依据,其余收拢成一组共用一句话。

   收拢不是隐藏。藏起来的话人会以为系统没读到这一列,而这里恰恰可能藏着该进账的
   原始数据——那种漏掉了不报错、只是少算钱的东西。所以照样一列一行、照样能改。 */
function columnCards(d) {
  const decide = d.columns.filter(c => !c.no_name_match);
  const bulk = d.columns.filter(c => c.no_name_match);
  const head = `<thead><tr>
    <th>表里的列</th><th>值长什么样</th><th>映到哪个角色</th><th>依据</th></tr></thead>`;
  return `
    <div class="card">
      <header><h2>要你拍板的 ${decide.length} 列</h2>
        <span class="sub">提议只是默认值，落库的是你确认的这一份</span></header>
      <table class="cols">${head}<tbody>${decide.map(colRow).join('')}</tbody></table>
    </div>
    ${bulk.length ? `<div class="card">
      <header><h2>另外 ${bulk.length} 列，默认不映射</h2>
        <span class="sub">列名跟这个数据源的角色都对不上，只是数据类型相同</span></header>
      <p class="small muted">
        平台报表自带的指标列大多落在这里——展现量、点击率、投产比这些不进账，不映是对的。
        但要是里面有该进账的原始数据，漏掉了不会报错，只是少算钱，而且以后不会有人再看这张表。
        所以还是一列一行摆在这里，扫一遍确认没有认识的东西。</p>
      <table class="cols">${head}<tbody>${bulk.map(colRow).join('')}</tbody></table>
    </div>` : ''}`;
}

function colRow(c) {
  const [label, cls] = conf(c);
  const opts = roleOptions(c);
  /* 用序号寻址而不是列名：列名会重复。淘宝万相台那张表两列都叫「推广主体ID」，
     按列名回传的话两列只能一起设同一个角色，而引擎会取到几乎全空的那一列——
     8226 行被当成合计行丢掉，推广费从 8.85 万变成 3354 元，全程不报错。 */
  /* 模型的意见单独一个标记,不混进可信度里。三种情况在屏幕上得长得不一样:

       模型提的    规则没认出来,只有模型一家之言
       模型同意    规则和模型各自走到了同一个结论,这是最强的一档
       模型说 X    两边打架,框里留的是规则那份,要人拍板

     写成同一句话的话,唯一需要人费神的那种(打架)就混在另外两种里了;而把「模型
     提的」显示成「模型同意」,等于让人以为一家之言有两重依据。 */
  const m = !c.model_role ? ''
    : c.model_filled ? '<span class="pill warn">模型提的</span>'
    : c.model_role === c.role ? '<span class="pill ok">模型同意</span>'
    : `<span class="pill warn">模型说 ${esc(c.model_role)}</span>`;
  return `<tr class="${c.derived ? 'quiet' : ''}" data-index="${c.index}">
    <td><div class="mono">${esc(c.column)}</div>
      ${c.occurrence ? `<div class="xs warn-text">第 ${c.occurrence + 1} 个同名列</div>` : ''}</td>
    <td class="xs muted">${SHAPE[c.shape] || c.shape}
      ${c.samples.length ? `<div class="mono xs">${esc(c.samples.slice(0, 2).join(' · ')).slice(0, 40)}</div>` : ''}</td>
    <td><select class="w-role">${opts}</select>
      <div class="xs"><span class="pill ${cls}">${label}</span> ${m}</div></td>
    <td class="xs muted">${esc(c.why)}
      ${c.model_role && c.model_role !== c.role && c.model_why
        ? `<div class="xs">模型：${esc(c.model_why)}</div>` : ''}</td></tr>`;
}

const SHAPE = {number: '数字', time: '日期', id: '编号', text: '文本', empty: '整列空'};

function roleOptions(c) {
  /* 候选排在最前面：认不出的列如果有几个像的角色，那几个就是人要在里面挑的。
     全表角色也留着，因为提议不可能永远对。 */
  /* 模型给的那个必须在候选里,而且排在最前:它是这一行唯一要人拍板的东西,
     让人去「这个数据源的全部角色」那一组里翻着找,等于没提。 */
  const near = c.alternatives.map(a => a.role);
  if (c.model_role && c.model_role !== c.role && !near.includes(c.model_role)) {
    near.unshift(c.model_role);
  }
  const rest = W.roles.map(r => r.role).filter(r => !near.includes(r) && r !== c.role);
  const one = (role, group) => {
    const f = W.roles.find(x => x.role === role);
    const hint = f && f.hint ? ` — ${f.hint}` : '';
    return `<option value="${esc(role)}"${role === c.role ? ' selected' : ''}>${
      esc(role)}${esc(hint)}</option>`;
  };
  return `<option value=""${c.role ? '' : ' selected'}>— 不映射 —</option>`
    + (c.role ? one(c.role) : '')
    + (near.length ? `<optgroup label="像这几个">${near.map(r => one(r)).join('')}</optgroup>` : '')
    + `<optgroup label="这个数据源的全部角色">${rest.map(r => one(r)).join('')}</optgroup>`;
}

function onboardBody() {
  const roles = {};
  main.querySelectorAll('tr[data-index]').forEach(tr => {
    roles[tr.dataset.index] = tr.querySelector('.w-role').value;
  });
  return {
    sha: W.draft.sha,
    sheet: W.draft.sheet,
    header_row: Number($('w-header').value),
    template_id: $('w-id').value.trim(),
    name: $('w-name').value.trim(),
    source: $('w-source').value,
    roles,
    /* 不回传签名：签名要按人最终确认的映射重算。回传草案那份的话，
       人把「消耗金额」映成 spend 之后，签名里仍然没有这个新列名，
       于是新模板的签名是老模板列集的子集，老版的表以后会被新模板抢走。 */
    time_slots: W.draft.time_slots,
    total_row_marker: W.draft.total_row_marker,
    model_revision: W.draft.model_revision,
  };
}

function wireOnboard() {
  const reload = () => {
    const q = new URLSearchParams(location.hash.split('?')[1] || '');
    q.set('header_row', $('w-header').value);
    q.set('source', $('w-source').value);
    location.hash = `#/onboard/${encodeURIComponent(W.draft.sha)}?${q}`;
    route();
  };
  $('w-header').onchange = reload;
  $('w-source').onchange = reload;

  wireRoles();

  $('w-try').onclick = async () => {
    const body = onboardBody();
    if (!body.template_id) return toast('模板 id 要填', true);
    $('w-said').innerHTML = '<span class="spin"></span> 正在解析……';
    try {
      const r = await api('/api/onboard/try', json('POST', body));
      W.tried = r;
      $('w-said').textContent = '';
      $('w-result').innerHTML = tryResult(r);
      $('w-land').disabled = !r.ok;
      $('w-land').className = r.ok ? 'primary' : '';
    } catch (err) {
      $('w-said').textContent = '';
      $('w-result').innerHTML = `<div class="banner bad"><strong>试跑没跑起来</strong>${esc(err.message)}</div>`;
      $('w-land').disabled = true;
    }
  };

  $('w-land').onclick = async () => {
    if (!W.tried || !W.tried.ok) return toast('先试跑，过了才能落库', true);
    $('w-said').innerHTML = '<span class="spin"></span> 正在写模型并重算……';
    try {
      const r = await api('/api/onboard', json('POST', onboardBody()));
      toast(`已接入 ${r.template_id}，重算了 ${r.stores.length} 家店`);
      location.hash = '#/';
      route();
    } catch (err) {
      $('w-said').textContent = '';
      toast('没落库：' + err.message, true);
    }
  };
}

/* 改了映射，之前那次试跑就不算了。不清掉的话人会拿着旧结果去落库——列名全对、
   填充率 100% 的那份旧结果，看不出跟新映射有什么关系。

   模型的意见叠上来之后整张表会重画，所以这里单独一个函数：重画完要再绑一次，
   忘了绑的表现是「改了下拉框但落库按钮还亮着」。 */
function wireRoles(invalidate) {
  main.querySelectorAll('.w-role').forEach(sel => sel.onchange = () => {
    W.tried = null;
    $('w-land').disabled = true;
    $('w-result').innerHTML = '';
    $('w-said').textContent = '映射改了，要重新试跑';
  });
  if (invalidate && W.tried) {
    W.tried = null;
    $('w-land').disabled = true;
    $('w-result').innerHTML = '';
    $('w-said').textContent = '模型补了几列，要重新试跑';
  }
}

function tryResult(r) {
  const bar = v => {
    const pct = Math.max(0, Math.min(1, v)) * 100;
    const cls = v >= .98 ? '' : v > 0 ? 'mid' : 'low';
    return `<span class="qbar ${cls}"><i style="width:${pct.toFixed(0)}%"></i></span>${(v * 100).toFixed(1)}%`;
  };
  return `
    <div class="banner ${r.ok ? 'ok' : 'bad'}" style="margin-top:14px">
      <strong>${r.ok ? '试跑通过' : '试跑没过，不能落库'}</strong>${esc(r.summary)}
      ${r.errors.length ? `<ul>${r.errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
    </div>
    ${r.warnings.length ? `<div class="banner warn"><strong>顺带说一下</strong>
      <ul>${r.warnings.slice(0, 6).map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${/* 签名和合计行标记这两项决定的是「以后」：签名决定下个月的表认不认得出来，
         合计行标记决定丢掉哪些行。它们不摆出来，人就只能在下个月发现问题。 */''}
    <div class="row wrap xs muted" style="margin-top:12px;gap:20px">
      <span>以后靠这几列认出这张表：${(r.match_columns || []).length
        ? r.match_columns.map(c => `<b class="mono">${esc(c)}</b>`).join('、')
        : '<b class="neg">没有——一列都没映上的话认不出来</b>'}</span>
      <span>合计行标记：${r.total_row_marker
        ? `<b class="mono">${esc(r.total_row_marker)}</b>（这一列为空的行会被丢掉）`
        : '没有（这张表没有表底合计行）'}</span>
    </div>
    ${r.controls.length ? `<table style="margin-top:12px"><thead><tr>
      <th>文件自己声明的合计</th><th>核对</th></tr></thead><tbody>${r.controls.map(c =>
      `<tr><td class="xs">${esc(c.label)}</td><td class="xs ${c.ok ? '' : 'neg'}">${
        c.ok ? '对上了' : esc(c.why)}</td></tr>`).join('')}</tbody></table>` : ''}
    <table style="margin-top:12px"><thead><tr>
      <th>角色</th><th>取自哪列</th><th class="right">有值的行</th>
      <th class="right">合计</th><th>取出来长什么样</th></tr></thead>
    <tbody>${r.roles.map(x => `<tr>
      <td class="mono xs">${esc(x.role)}</td>
      <td class="xs muted">${esc(x.column)}</td>
      <td class="right num">${bar(x.filled)}</td>
      <td class="right num">${x.total === null ? '' : money(x.total)}</td>
      <td class="mono xs muted">${esc((x.samples || []).slice(0, 2).join(' · ')).slice(0, 36)}</td>
    </tr>`).join('')}</tbody></table>`;
}

// --------------------------------------------------------------------------
// 启动
// --------------------------------------------------------------------------

(async () => {
  try {
    S.boot = await api('/api/bootstrap');
    $('c-stores').textContent = S.boot.stores.length || '';
  } catch (err) {
    if (err.status === 401) {
      loginGate(err.message);
      return;
    }
    main.innerHTML = fail(err.message);
    return;
  }
  route();
})();
