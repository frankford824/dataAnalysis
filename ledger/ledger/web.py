"""单页界面。

整个页面只有一个动作：把表拖进来。剩下的都是结果。

不上构建链、不引前端框架，原生一个文件——这是内部工具，装 npm 只是给以后添
维护负担。真到了这一页装不下的时候再说，那时也该知道到底需要什么了。

呈现上有几条是刻意的：
「能不能结账」排在数字前面，人才不会拿着一张缺数据的表当结论；
数据不全的项显示破折号而不是 0，那是两件事；
挂不上订单的钱单独列，公司级主表里别家店的部分再分出来——不然本店那点真问题
会被几十万埋掉。
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记账</title>
<style>
  :root {
    --ink: #16181d;
    --muted: #6b7280;
    --line: #e5e7eb;
    --bg: #f7f8fa;
    --card: #fff;
    --good: #0f7b4f;
    --bad: #b42318;
    --warn: #b45309;
    --accent: #1f5eff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
  }
  header {
    padding: 22px 28px; background: var(--card); border-bottom: 1px solid var(--line);
    display: flex; align-items: baseline; gap: 14px;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: .01em; }
  header span { color: var(--muted); font-size: 13px; }
  main { max-width: 940px; margin: 0 auto; padding: 28px; }

  #drop {
    background: var(--card); border: 2px dashed var(--line); border-radius: 14px;
    padding: 46px 28px; text-align: center; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  #drop:hover, #drop.over { border-color: var(--accent); background: #f4f7ff; }
  #drop p { margin: 0; font-size: 16px; }
  #drop small { color: var(--muted); display: block; margin-top: 8px; }
  #file { display: none; }

  .status { margin: 20px 0 0; color: var(--muted); font-size: 14px; }
  .spin { display: inline-block; width: 12px; height: 12px; margin-right: 8px;
    border: 2px solid var(--line); border-top-color: var(--accent);
    border-radius: 50%; animation: r .7s linear infinite; vertical-align: -1px; }
  @keyframes r { to { transform: rotate(360deg); } }

  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 22px 24px; margin-top: 20px;
  }
  .card h2 { margin: 0 0 2px; font-size: 17px; font-weight: 650; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 16px; }

  .verdict { border-radius: 10px; padding: 12px 14px; margin-bottom: 18px; font-size: 14px; }
  .verdict.ok { background: #eaf6f0; color: var(--good); }
  .verdict.no { background: #fdeceb; color: var(--bad); }
  .verdict strong { display: block; margin-bottom: 6px; font-weight: 650; }
  .verdict ul { margin: 6px 0 0; padding-left: 18px; }
  .verdict li { margin: 3px 0; }
  .verdict li.warn { color: var(--warn); }

  /* 报表不占满卡片宽度：940px 里标签贴左、数字贴右，眼睛要横扫一整行才对得上。
     收窄到 480 让名目和金额挨着，一眼能读。 */
  table { width: 100%; max-width: 480px; border-collapse: collapse;
    font-variant-numeric: tabular-nums; }
  td { padding: 5px 0; }
  td.n { text-align: right; white-space: nowrap; }
  tr.lv1 td { font-weight: 600; }
  tr.lv1 td:first-child { padding-left: 0; }
  tr.lv2 td:first-child { padding-left: 18px; color: #3f4652; }
  /* 明细之后的小计要看得出是另一层，否则整张表像一串平铺的数字。 */
  tr.lv1 + tr.lv2 td { padding-top: 10px; }
  tr.lv2 + tr.lv1 td { padding-top: 10px; }
  tr.total td { border-top: 1px solid var(--line); font-weight: 650; padding-top: 8px; }
  td.na { color: var(--muted); }
  .why { color: var(--muted); font-size: 12.5px; padding-left: 18px; }

  .sub { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--line); }
  .sub h3 { margin: 0 0 10px; font-size: 14px; font-weight: 620; }
  .sub p { margin: 10px 0 0; color: #4b5563; font-size: 13px; line-height: 1.65; }
  .sub table { max-width: 560px; }
  /* 别家店的钱压暗一档：它不算本店的账，不能和本店那部分抢注意力。 */
  .other td { color: var(--muted); }

  .note { background: #fffbeb; border: 1px solid #fde68a; border-radius: 12px;
    padding: 16px 18px; margin-top: 20px; font-size: 14px; }
  .note h3 { margin: 0 0 8px; font-size: 14px; }
  .note code { background: #fff; padding: 1px 6px; border-radius: 4px;
    border: 1px solid var(--line); font-size: 13px; }
  .note li { margin: 4px 0; }

  /* 店铺设置。默认收起——店长每月来这里是为了交表，不是为了改配置。 */
  #settings { margin-top: 28px; }
  .link { background: none; border: 0; color: var(--accent); cursor: pointer;
    font-size: 13px; padding: 0 4px; }
  .storerow { display: grid; grid-template-columns: 180px 1fr 200px auto 64px;
    gap: 10px; align-items: center; padding: 8px 0;
    border-top: 1px solid var(--line); }
  .storename { font-size: 14px; }
  .storename small { display: block; color: var(--muted); font-size: 12px; }
  .storerow input { padding: 6px 9px; border: 1px solid var(--line);
    border-radius: 7px; font-size: 13px; font-family: inherit; }
  .storerow input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .storerow button { padding: 6px 14px; border: 1px solid var(--line);
    border-radius: 7px; background: #fff; cursor: pointer; font-size: 13px;
    font-family: inherit; }
  .storerow button:hover { border-color: var(--accent); color: var(--accent); }
  .said { font-size: 12px; color: var(--muted); }
  .other small { color: var(--muted); font-size: 12px; font-weight: 400; }
</style>
</head>
<body>
<header>
  <h1>记账</h1>
  <span>把这个月的表交上来，其余交给引擎</span>
</header>
<main>
  <div id="drop">
    <p>把文件拖到这里，或者点一下选</p>
    <small>xlsx / xls / csv / zip 都行，一次可以多选。文件名别改，认哪家店靠它。</small>
  </div>
  <input type="file" id="file" multiple>
  <div id="status" class="status"></div>
  <div id="out"></div>

  <div class="sub" id="settings">
    <h3>店铺设置 <button id="toggle" class="link">展开</button></h3>
    <div id="stores" hidden></div>
  </div>
</main>
<script>
const drop = document.getElementById('drop');
const picker = document.getElementById('file');
const statusEl = document.getElementById('status');
const out = document.getElementById('out');

drop.onclick = () => picker.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove('over');
  send(e.dataTransfer.files);
};
picker.onchange = () => send(picker.files);

function money(v, display) {
  if (v === null || v === undefined) return '—';
  if (display === 'percent') return (v * 100).toFixed(1) + '%';
  if (display === 'count') return v.toLocaleString('zh-CN', {maximumFractionDigits: 0});
  return v.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

async function send(files) {
  if (!files || !files.length) return;
  const body = new FormData();
  for (const f of files) body.append('files', f, f.name);
  out.innerHTML = '';

  // 淘宝那张对账表 22 万行，整套跑下来要半分多钟。一句不动的「正在算账」
  // 挂在那里，人会以为卡死了然后去刷新——刷新等于从头再来。报出秒数就知道它还在动。
  const t0 = Date.now();
  const tick = () => {
    const s = Math.round((Date.now() - t0) / 1000);
    statusEl.innerHTML = '<span class="spin"></span>正在认表、挂钩、算账……'
      + files.length + ' 个文件，已经 ' + s + ' 秒。'
      + (s > 20 ? '几十万行的对账表要慢一点，别刷新。' : '');
  };
  tick();
  const timer = setInterval(tick, 1000);

  try {
    const res = await fetch('/api/run', {method: 'POST', body});
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    clearInterval(timer);
    render(data, Math.round((Date.now() - t0) / 1000));
  } catch (err) {
    clearInterval(timer);
    statusEl.textContent = '出错了：' + err.message;
  }
}

function render(data, secs) {
  const n = data.slices.length;
  const ok = data.slices.filter(s => s.can_close).length;
  statusEl.textContent = n
    ? n + ' 个店期，' + ok + ' 个可以结账。' + (secs ? '用了 ' + secs + ' 秒。' : '')
    : '没算出结果。';
  out.innerHTML = data.slices.map(card).join('')
    + (data.failures || []).map(failure).join('')
    + orphans(data.orphans || []);
}

function card(s) {
  const blockers = s.findings.filter(f => !f.passed && f.blocking);
  const warns = s.findings.filter(f => !f.passed && !f.blocking);
  const verdict = s.can_close
    ? '<div class="verdict ok"><strong>可以结账</strong>'
      + s.findings.length + ' 项自检全部通过。</div>'
    : '<div class="verdict no"><strong>不能结账：' + blockers.length + ' 项拦住了</strong><ul>'
      + blockers.map(f => '<li>' + esc(f.name) + '——' + esc(f.message) + '</li>').join('')
      + warns.map(f => '<li class="warn">' + esc(f.name) + '——' + esc(f.message) + '</li>').join('')
      + '</ul></div>';

  const rows = s.statement.map(nd => {
    const cls = ['lv' + Math.min(nd.level, 2), nd.is_total ? 'total' : ''].join(' ');
    const cell = nd.available
      ? '<td class="n">' + money(nd.value, nd.display) + '</td>'
      : '<td class="n na">—</td>';
    let line = '<tr class="' + cls + '"><td>' + esc(nd.name) + '</td>' + cell + '</tr>';
    if (!nd.available && nd.missing_sources.length) {
      line += '<tr><td colspan="2" class="why">缺 '
        + nd.missing_sources.map(esc).join('、') + '，这一项不出数</td></tr>';
    }
    return line;
  }).join('');

  return '<div class="card"><h2>' + esc(s.store) + '</h2><div class="meta">'
    + esc(s.platform) + ' · ' + esc(s.period || '账期未定') + ' · '
    + (s.entity ? esc(s.entity) : '主体未配置')
    + '</div>' + verdict + '<table>' + rows + '</table>'
    + missing(s) + unlinked(s) + '</div>';
}

function missing(s) {
  if (!s.missing_sources.length) return '';
  return '<div class="sub"><h3>还缺 ' + s.missing_sources.length + ' 项数据</h3><p>'
    + s.missing_sources.map(esc).join('、') + '</p></div>';
}

// 每类钱为什么不用管。要人查的那类不在这里——它本来就该占注意力。
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
    return '<tr' + (why ? ' class="other"' : '') + '><td>' + esc(b.label)
      + (why ? '<br><small>' + esc(why) + '</small>' : '')
      + '</td><td class="n">' + money(b.amount) + '</td><td class="n">'
      + b.count.toLocaleString('zh-CN') + ' 笔</td></tr>';
  }).join('');
  const head = s.unlinked_total
    ? '要查归属的钱 ' + money(s.unlinked_total)
    : '没有要查归属的钱';
  return '<div class="sub"><h3>没进利润的钱：' + head + '</h3><table>' + rows + '</table>'
    + (s.unlinked_total
        ? '<p>只有「' + NEEDS_WORK + '」要人查，查清了才会进利润。'
          + '其余几类本来就不该算本店这一期。</p>'
        : '<p>列出来的几类都不该算本店这一期，不用管。</p>')
    + '</div>';
}

function failure(f) {
  return '<div class="note"><h3>' + esc(f.store) + '：交了 ' + f.files.length
    + ' 个文件，但没算出结果</h3><ul>'
    + f.reasons.map(r => '<li>' + esc(r) + '</li>').join('')
    + '</ul></div>';
}

function orphans(list) {
  if (!list.length) return '';
  return '<div class="note"><h3>有 ' + list.length
    + ' 个文件认不出是哪家店的，这些数据没进账</h3><ul>'
    + list.map(o => {
        const s = o.suggest || {};
        const hint = s.store
          ? '——看着像 <code>' + esc(s.store) + '</code>'
            + (s.platform ? '，平台 <code>' + esc(s.platform) + '</code>' : '，平台认不出')
          : '';
        return '<li>' + esc(o.file) + hint + '</li>';
      }).join('')
    + '</ul><p>认不出的文件不会塞进某家店凑数，那会把一家店的钱记到另一家头上。'
    + '在下面的「店铺设置」里登记这家店，然后重传即可。</p></div>';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// 店铺设置。法人主体这类东西数据里读不出来——支付宝和微信账单不带主体信息，
// 只能由人告诉引擎。要人去改 YAML 才能配，那是脚手架不是产品，所以放在界面上。
const toggle = document.getElementById('toggle');
const storesEl = document.getElementById('stores');

toggle.onclick = async () => {
  const showing = !storesEl.hidden;
  storesEl.hidden = showing;
  toggle.textContent = showing ? '展开' : '收起';
  if (!showing) await loadStores();
};

async function loadStores() {
  storesEl.innerHTML = '<p class="why">正在读……</p>';
  const res = await fetch('/api/stores');
  const data = await res.json();
  storesEl.innerHTML = data.stores.map(storeRow).join('')
    + '<p class="why">主体是多对一的：几家店可以同属一个主体，这层关系推不出来只能配。'
    + '改完立刻生效，下次算账就按新配置走。</p>';
}

function storeRow(s) {
  return '<div class="storerow" data-id="' + esc(s.id) + '">'
    + '<div class="storename">' + esc(s.name)
    + '<small>' + esc(s.platform) + (s.archived ? ' · 已归档' : '') + '</small></div>'
    + '<input class="entity" value="' + esc(s.entity) + '" placeholder="法人主体全名（未配置）">'
    + '<input class="taxid" value="' + esc(s.entity_tax_id) + '" placeholder="税号（可空）">'
    + '<button class="save">保存</button>'
    + '<span class="said"></span></div>';
}

storesEl.onclick = async e => {
  if (!e.target.classList.contains('save')) return;
  const row = e.target.closest('.storerow');
  const said = row.querySelector('.said');
  said.textContent = '正在存……';
  const res = await fetch('/api/stores/' + encodeURIComponent(row.dataset.id), {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      entity: row.querySelector('.entity').value.trim(),
      entity_tax_id: row.querySelector('.taxid').value.trim(),
    }),
  });
  if (res.ok) {
    said.textContent = '已保存';
    setTimeout(() => { said.textContent = ''; }, 2500);
  } else {
    const body = await res.json().catch(() => ({}));
    said.textContent = '没存上：' + (body.detail || res.status);
  }
};
</script>
</body>
</html>
"""
