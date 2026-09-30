/* ================= 智慧实验室数据汇总助手 · 前端 ================= */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const S = {
  datasets: [], fields: [], config: {}, baseDir: '', outDir: '', appVersion: '',
  picked: new Set(),          // "dsId||sheetName"
  outFields: [],              // 输出字段（有序）
  outSet: new Set(),
  mapOverride: {},            // key -> {srcCol: outCol}
  filters: [],
  lastResult: null,
  page: 0, pageSize: 500,
  mode: 'union',
  joinList: [],
  fdView: 'v',            // 字段字典视图：v=纵向列表 h=横向矩阵
  fold: new Set(),        // ① 选数据表：被折叠的文件 id
  mapFold: new Set(),     // 字段映射：被折叠的 "文件||工作表"
  pickSameBase: '',       // 「仅选中表头一致的表」：基准（兼容保留）
  sameSel: new Set(),     // 基准组：被显式选中的表 "文件id||工作表"
  sameScope: 'all',
  pkKeep: null,      // 「查看已勾选」弹窗本次显示的清单（快照，取消勾选后行仍保留）
  pkDrop: new Set(), // 本次在该弹窗里被取消勾选的表
  homeGroups: [],    // 概览「表头结构分组」的当前分组（一键合并用）
  exportsTotal: 0,   // 「导出结果」目录里的文件总数（用于提示"还有 N 份没列出来"）
  reportDir: '',     // v2.1.0：「分析报告」目录（后端给，用于"打开文件夹"）
  rp: null,          // v2.1.0：分析报告模块状态
};

/* ---------------- 基础 ---------------- */
function toast(msg, type) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (type ? ' ' + type : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = 'toast'; }, 3200);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(path, body) {
  let r;
  try {
    r = await fetch(path, body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : {});
  } catch (e) {
    throw new Error('连不上本地程序了。请确认那个黑色命令行窗口还开着（关了它程序就停了），然后刷新本页。');
  }
  const raw = await r.text();
  if (r.status === 413 || r.status === 431) {
    throw new Error('一次导入的文件太多了，请分几批导入。');
  }
  let j;
  try { j = JSON.parse(raw); }
  catch (e) { throw new Error('服务返回了异常内容（HTTP ' + r.status + '）：' + raw.slice(0, 160)); }
  if (!j.ok) throw new Error(j.error || ('请求失败（HTTP ' + r.status + '）'));
  return j;
}
function fmtSize(n) {
  if (n === null || n === undefined || n === '') return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}
function norm(s) {
  if (s == null) return '';
  return String(s).normalize('NFKC').trim().toLowerCase()
    .replace(/\s+/g, '')
    .replace(/[、，,。.；;：:（）()\[\]【】{}<>《》\/\\\-_—~!！?？“”"'’‘*#·|＋+=]+/g, '');
}
function sim(a, b) {
  a = norm(a); b = norm(b);
  if (!a || !b) return 0;
  if (a === b) return 1;
  let m = 0;
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 0; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++)
    dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  m = 1 - dp[a.length][b.length] / Math.max(a.length, b.length);
  if (a.includes(b) || b.includes(a)) m = Math.max(m, 0.82 + 0.18 * (Math.min(a.length, b.length) / Math.max(a.length, b.length)));
  return m;
}

/* ---------------- 载入 ---------------- */
async function loadAll() {
  const st = await api('/api/state');
  S.datasets = st.datasets; S.config = st.config;
  S.baseDir = st.base_dir; S.outDir = st.out_dir;
  S.appVersion = st.app_version || '';
  S.health = st.health || null;
  S.reportDir = st.report_dir || '';   // v2.1.0：「分析报告」目录
  // 恢复上次记住的字段映射
  S.mapOverride = (st.config && st.config.mapping) || {};
  console.log('智慧实验室运营管理-数据汇总助手 前端已加载（程序版本 v' + (st.app_version || '?') + '）');
  const fd = await api('/api/fields');
  S.fields = fd.fields;
  $('#sf-ds').textContent = S.datasets.length;
  $('#sf-fields').textContent = S.fields.length;
  // 侧栏版本号从后端取，别写死在 index.html 里（否则升版本总会漏改一处）
  const sfv = $('#sf-ver'); if (sfv) sfv.textContent = 'v' + (S.appVersion || '?');
  renderDataWarn();
  renderHome(); renderSources(); renderFields(); renderBuild(); fillConfigForm();
  loadExports().catch(() => {});
  dtRefresh().catch(() => {});
}

/* ---------------- 数据自检告警 ----------------
   索引里写着"有 N 张表"，但原始文件（library/sources）已经不在了。
   典型场景：拷贝程序时只拷了 index.json；或者手工清理过 sources 目录。
   这时界面会显示成"6 张表 / 0 个字段"这种自相矛盾的样子，点什么都动不了 ——
   必须直接说清楚是什么问题、怎么修，不能等用户自己发现。 */
function renderDataWarn() {
  const box = $('#data-warn');
  if (!box) return;
  const h = S.health;
  const miss = (h && h.missing) || [];
  if (!miss.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = '';
  box.innerHTML = '<div class="wc-h">⚠ 有 ' + miss.length + ' 张数据表的原始文件不在了</div>'
    + '<div class="wc-b">索引里还记着这些表（共 ' + h.total + ' 张），但 <code>library/sources</code> 里找不到对应文件，'
    + '所以这些表点不开、也汇总不了。<br>'
    + '<b>常见原因：</b>① 拷贝程序时只拷了 <code>index.json</code>，没拷 <code>library/sources</code> 文件夹；'
    + '② 手工清理过 <code>library/sources</code>；③ 换了台电脑。<br>'
    + '<b>怎么修：</b>到「数据源管理」把这些表的旧记录删掉，再重新导入原始 Excel 即可（导入后字段会自动识别）。</div>'
    + '<div class="wc-l">' + miss.slice(0, 12).map(m => '<span class="wc-t">' + esc(m) + '</span>').join('')
    + (miss.length > 12 ? '<span class="wc-t">…还有 ' + (miss.length - 12) + ' 张</span>' : '') + '</div>';
}

/* ---------------- 最近导出 ---------------- */
function fmtStamp(sec) {
  const t = new Date(sec * 1000);
  return t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-'
    + String(t.getDate()).padStart(2, '0') + ' '
    + String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
}

/* 把 /api/exports（或删除接口）返回的 {files,total} 画进列表。
   删除后后端会把最新的 files/total 一起返回，所以这里同一个函数既首次渲染也刷新。 */
function paintExports(r) {
  const box = $('#exports-list');
  if (!box) return;
  const files = (r && r.files) || [];
  const total = (r && typeof r.total === 'number') ? r.total : files.length;
  S.exportsTotal = total;

  const cb = $('#btn-clear-exports');
  if (cb) {
    cb.disabled = !files.length;
    cb.title = files.length
      ? '删除「导出结果」目录里的全部 ' + total + ' 份文件'
      : '当前没有可删除的导出文件';
  }

  if (!files.length) {
    box.innerHTML = '<div class="empty">还没有导出过文件。到「汇总提取」生成结果后点「导出 Excel」即可。</div>';
    return;
  }
  box.innerHTML = files.map(f => {
    return '<div class="expitem">'
      + '<div class="ei-main"><div class="ei-name">' + esc(f.name) + '</div>'
      + '<div class="ei-meta">' + fmtStamp(f.mtime) + ' · ' + fmtSize(f.size) + '</div></div>'
      + '<div class="ei-act">'
      + '<a class="btn small" href="/api/download?name=' + encodeURIComponent(f.name)
      + '" download="' + esc(f.name) + '">下载</a>'
      + '<button class="btn small danger act-expdel" data-name="' + esc(f.name) + '"'
      + ' title="删除这份导出备份（磁盘上的文件也会一起删掉，不可恢复）">删除</button>'
      + '</div></div>';
  }).join('')
    + (total > files.length
      ? '<div class="exp-more">「导出结果」里共 ' + total + ' 份，这里只列出最近 '
        + files.length + ' 份。较早的可以点「打开导出目录」去文件夹里看。</div>'
      : '');
}

async function loadExports() {
  const box = $('#exports-list');
  if (!box) return;
  let r;
  try { r = await api('/api/exports'); } catch (e) { box.innerHTML = '<div class="empty">读不到导出目录</div>'; return; }
  paintExports(r);
}

/* 删除单条导出文件。
   ★ 用事件委托而不是给每个按钮单独 onclick —— 列表是 innerHTML 重画的，
     逐个绑定会在每次刷新后失效（这是本项目里反复踩过的坑）。 */
const expBox = $('#exports-list');
if (expBox) expBox.addEventListener('click', async e => {
  const b = e.target.closest('.act-expdel');
  if (!b || b.disabled) return;
  const name = b.dataset.name || '';
  if (!confirm('确定删除这份导出文件吗？\n\n' + name
    + '\n\n程序目录「导出结果」里的这份备份会一起删掉，删了不能恢复。\n'
    + '（你自己另存到别处的文件不受影响。）')) return;
  const old = b.textContent;
  b.disabled = true; b.textContent = '删除中…';
  try {
    const r = await api('/api/export/delete', { names: [name] });
    paintExports(r);
    if (r.errors && r.errors.length) toast('删除失败：' + r.errors[0].error, 'err');
    else toast('已删除导出文件：' + name, 'ok');
  } catch (err) {
    toast('删除失败：' + err.message, 'err');
    b.disabled = false; b.textContent = old;
  }
});

/* 清空全部导出文件 */
const clrExp = $('#btn-clear-exports');
if (clrExp) clrExp.onclick = async () => {
  const n = S.exportsTotal || 0;
  if (!confirm('确定清空全部导出文件吗？\n\n程序目录「导出结果」里的 '
    + (n ? n + ' 份' : '全部') + '备份都会被删掉，删了不能恢复。\n'
    + '（你自己另存到别处的文件不受影响。）')) return;
  const old = clrExp.textContent;
  clrExp.disabled = true; clrExp.textContent = '清空中…';
  try {
    const r = await api('/api/export/clear', {});
    paintExports(r);
    if (r.errors && r.errors.length) {
      toast('部分文件没删掉：' + r.errors[0].error, 'err');
    } else {
      toast('已清空 ' + ((r.removed || []).length) + ' 份导出文件', 'ok');
    }
  } catch (err) {
    toast('清空失败：' + err.message, 'err');
  } finally {
    clrExp.textContent = old;
  }
};

/* ---------------- 概览 ---------------- */
function renderHome() {
  const sheets = S.datasets.reduce((a, d) => a + d.sheet_count, 0);
  const rows = S.datasets.reduce((a, d) => a + d.total_rows, 0);
  $('#kpi-ds').textContent = S.datasets.length;
  $('#kpi-sheet').textContent = sheets;
  $('#kpi-row').textContent = rows.toLocaleString();
  $('#kpi-field').textContent = S.fields.length;

  const byLab = {}, byType = {};
  S.datasets.forEach(d => {
    (byLab[d.lab || '未标注'] = byLab[d.lab || '未标注'] || []).push(d);
    (byType[d.table_type || '未标注'] = byType[d.table_type || '未标注'] || []).push(d);
  });
  $('#home-labs').innerHTML = bars(byLab, '张表');
  $('#home-types').innerHTML = bars(byType, '张表');

  // 字段结构分组
  const groups = {};
  S.datasets.forEach(d => (d.sheets || []).forEach(sh => {
    const sig = sh.signature || '';
    if (!sig) return;
    (groups[sig] = groups[sig] || []).push({ ds: d, sh });
  }));
  const list = Object.values(groups).filter(g => g.length > 1).sort((a, b) => b.length - a.length);
  // 给每个分组编号，按钮只需带上编号即可（明细留在 S.homeGroups 里，避免把长列名塞进 DOM 属性）
  S.homeGroups = list;
  $('#home-groups').innerHTML = list.length
    ? list.map((g, gi) => {
      const cols = (g[0].sh.columns || []).length;
      const nOn = g.filter(x => S.picked.has(x.ds.id + '||' + x.sh.name)).length;
      return `<div class="grp">
        <div class="grphead">
          <div class="gh">${g.length} 张表表头完全一致（同样 ${cols} 个列名）—— 可直接一键合并${nOn ? ` <span class="tag on">已勾选 ${nOn}/${g.length}</span>` : ''}</div>
          <button class="btn small merge-peek" data-g="${gi}" title="先看看这一组合并后是什么样（真跑一遍合并，但不会占用结果；看完再决定要不要合并）">预览</button>
          <button class="btn small merge-one" data-g="${gi}" title="把这 ${g.length} 张表全部勾选，输出字段也自动填好，直接跳到「汇总提取」">一键合并</button>
        </div>
        <div class="gl">${g.map(x => esc(x.ds.lab ? x.ds.lab + '/' : '') + esc(x.sh.name)).join('　·　')}</div>
      </div>`;
    }).join('')
    : '<div class="empty sm">暂无「表头完全一致」的表。表头不同的表也可以汇总，在「汇总提取」里用字段映射对齐即可。</div>';
}

/* 【一键合并】概览里的分组 -> 把这组表全部勾选 + 输出字段自动填好 + 跳到汇总提取
   分组已保证「表头完全一致」，所以字段映射一定是 1:1，不需要人工对齐。 */
function homeMergeGroup(gi) {
  const g = (S.homeGroups || [])[gi];
  if (!g || !g.length) return toast('这个分组已失效，请刷新重试', 'err');
  if (!S.datasets.length) return toast('还没有数据表，先去「数据源管理」导入', 'err');

  // 1) 这组表全部勾选（其他表的勾选状态不动）
  let nAdd = 0;
  g.forEach(x => {
    const k = x.ds.id + '||' + x.sh.name;
    if (!S.picked.has(k)) { S.picked.add(k); nAdd++; }
  });

  // 2) 输出字段 = 这组的列名（表头一致，取第一张表的列名即可）
  const cols = (g[0].sh.columns || []).filter(Boolean);
  if (cols.length) {
    S.outFields = cols.slice();
    S.outSet = new Set(S.outFields);
  } else {
    S.outFields = []; S.outSet = new Set();
  }

  // 3) 刷新各处视图 + 跳到「汇总提取」
  renderHome(); renderPickList(); renderFieldList(); renderMapping(); renderFilters();
  nav('build');
  if (S.mode !== 'union') switchMode('union');

  const extra = nAdd ? `新增勾选 ${nAdd} 张` : '这组表本来就都是勾选状态';
  toast(`已选中 ${g.length} 张表（${extra}），输出字段已自动填好 ${cols.length} 个，直接点「生成汇总表」就行`, 'ok');
}
$('#home-groups').addEventListener('click', e => {
  const k = e.target.closest('.merge-peek');
  if (k) { homePeekGroup(parseInt(k.dataset.g, 10)); return; }
  const b = e.target.closest('.merge-one');
  if (b) { homeMergeGroup(parseInt(b.dataset.g, 10)); return; }
});

/* ================= 合并预览（概览「一键合并」旁边的「预览」） =================
   目的：让人在真正跳去「汇总提取」动手之前，先看清这一组会合并成什么 ——
   一共多少行、哪几列、每张表各贡献多少行、数据长什么样。

   做法：调 /api/aggregate_peek —— 与 /api/aggregate 走完全相同的合并逻辑，
   区别只有"不落盘"（不写 library/results、不占用「汇总结果」）。
   所以这里看到的就是点「就这样合并」之后会得到的东西。 */

/* 预览用的 sources：按「这组表的列名」直接同名映射。
   分组的前提就是表头完全一致，所以一定是 1:1 映射，不需要模糊匹配；
   但用户手改过的字段映射（S.mapOverride）优先，保证和正式合并的口径一致。 */
function peekSources(g, cols) {
  const outSet = new Set(cols);
  return g.map(x => {
    const key = x.ds.id + '||' + x.sh.name;
    const ov = S.mapOverride[key] || {};
    const mapping = {};
    const srcCols = x.sh.kind === 'info' ? ['项目', '内容'] : (x.sh.columns || []);
    srcCols.forEach(sc => {
      const tgt = ov[sc];
      if (tgt !== undefined) { if (tgt && outSet.has(tgt)) mapping[sc] = tgt; return; }
      if (outSet.has(sc)) mapping[sc] = sc;
    });
    return { dataset_id: x.ds.id, sheet: x.sh.name,
             header_rows: x.sh.header_rows || '1',
             kind: x.sh.kind === 'info' ? 'info' : 'table', mapping };
  });
}

function peekLoading(nTab, nCol) {
  return '<div class="peek-load"><span class="peek-spin"></span>'
    + '<div>正在把 ' + nTab + ' 张表合并一遍（' + nCol + ' 个列名），马上就好…</div></div>';
}

function peekBody(r, g, cols, gi) {
  const detail = r.detail || [];
  const byKey = {};
  detail.forEach(d => { byKey[d.dataset_id + '||' + d.sheet] = d; });

  const mis = (r.skipped || []).filter(x => x && x.reason);
  const rowInfo = '合并后 <b>' + (r.total || 0).toLocaleString() + '</b> 行 / <b>'
    + (r.columns || []).length + '</b> 列';
  const shown = (r.rows || []).length;

  // 参与的表：优先用后端返回的 detail（带各自的真实行数），拿不到就退回分组本身
  const tabs = g.map((x, i) => {
    const k = x.ds.id + '||' + x.sh.name;
    const d = byKey[k];
    const used = d ? d.used_rows : null;
    const tot = d ? d.total_rows : null;
    const rowTxt = used == null ? '—'
      : (used === tot ? used + ' 行' : used + ' / ' + tot + ' 行');
    return '<div class="peek-tab">'
      + '<span class="pt-n">' + (i + 1) + '</span>'
      + '<span class="pt-lab">' + esc(x.ds.lab || x.ds.name) + '</span>'
      + '<span class="pt-sh">' + esc(x.sh.name) + '</span>'
      + '<span class="pt-r" title="这张表实际并入的行数 / 总行数">' + rowTxt + '</span>'
      + '</div>';
  }).join('');

  return '<div class="peek-head">'
      + '<div class="peek-kpi">' + g.length + ' 张表 → ' + rowInfo
      + '<span class="dot">·</span>用时 ' + (r.elapsed != null ? r.elapsed + 's' : '—') + '</div>'
      + '<div class="peek-note">下面是合并后的<b>前 ' + shown + ' 行</b>（真实跑出来的，不是示意）。'
      + '确认没问题就点下面的「就这样合并」。</div>'
    + '</div>'

    + (mis.length
      ? '<div class="peek-warn">有 ' + mis.length + ' 张表没能并进来：<br>'
        + mis.slice(0, 5).map(m => '· ' + esc(m.dataset || '') + ' / ' + esc(m.sheet || '')
          + '：' + esc(m.reason || '')).join('<br>') + '</div>'
      : '')

    + '<h2 class="peek-h">参与合并的数据表（' + g.length + ' 张）</h2>'
    + '<div class="peek-tabs">' + tabs + '</div>'

    + '<h2 class="peek-h">合并后的列名（' + (r.columns || []).length + ' 个）</h2>'
    + '<div class="peek-cols">'
    + (r.columns || []).map(c => '<span class="tag">' + esc(c) + '</span>').join('')
    + '</div>'

    + '<h2 class="peek-h">前 ' + shown + ' 行预览'
    + (r.total > shown ? ' <span class="peek-more">（共 ' + r.total.toLocaleString() + ' 行）</span>' : '')
    + '</h2>'
    + '<div class="tablewrap">' + tableHTML(r.columns, r.rows) + '</div>'

    + '<div class="peek-bar">'
    + '<span class="peek-bar-tip">这一步只是预览，还没有生成结果。</span>'
    + '<span style="flex:1"></span>'
    + '<button class="btn small" id="peek-close">关闭</button>'
    + '<button class="btn primary peek-do" data-g="' + gi + '">就这样合并</button>'
    + '</div>';
}

async function homePeekGroup(gi) {
  const g = (S.homeGroups || [])[gi];
  if (!g || !g.length) return toast('这个分组已失效，请刷新重试', 'err');
  if (!S.datasets.length) return toast('还没有数据表，先去「数据源管理」导入', 'err');

  const cols = (g[0].sh.columns || []).filter(Boolean);
  if (!cols.length) {
    return toast('这组表没有识别出列名，没法预览。先到「数据源管理」确认表头识别是否正确', 'err');
  }

  // 先弹出来 + 显示加载态，别让用户觉得点了没反应
  $('#modal-title').textContent = '合并预览 · ' + g.length + ' 张表';
  $('#modal-body').classList.remove('rawmode');
  $('#modal-body').classList.add('peekmode');
  $('#modal-body').innerHTML = peekLoading(g.length, cols.length);
  $('#modal').classList.add('show');

  let r;
  try {
    r = await api('/api/aggregate_peek', {
      mode: 'union',
      sources: peekSources(g, cols),
      fields: cols,
      filters: [],
      sort: { field: '' },
      add_source_col: true,     // 预览带上「来源实验室/文件/工作表」，看得出每行是哪来的
      dedup: false,
      title: '合并预览',
    });
  } catch (e) {
    $('#modal-body').innerHTML = '<div class="peek-err"><b>预览失败：</b>' + esc(e.message) + '</div>';
    return;
  }

  // 弹窗可能已经被用户关掉了，这时就别再往里写（也避免按钮绑到已移除的节点上）
  if (!$('#modal').classList.contains('show')) return;

  $('#modal-body').innerHTML = peekBody(r, g, cols, gi);

  const cb = $('#peek-close');
  if (cb) cb.onclick = () => closeModal();
  const db = $('.peek-do');
  if (db) db.onclick = () => { closeModal(); homeMergeGroup(gi); };
}
function bars(map, unit) {
  const arr = Object.entries(map).sort((a, b) => b[1].length - a[1].length).slice(0, 10);
  if (!arr.length) return '<div class="empty sm">暂无数据</div>';
  const max = arr[0][1].length;
  return arr.map(([k, v]) => `<div class="bar"><div class="bn" title="${esc(k)}">${esc(k)}</div>
    <div class="bt"><div class="bf" style="width:${Math.max(6, v.length / max * 100)}%"></div></div>
    <div class="bv">${v.length}</div></div>`).join('');
}

/* ---------------- 数据源 ---------------- */
function renderSources() {
  const tb = $('#src-table tbody');
  $('#src-count').textContent = S.datasets.length;
  $('#src-empty').style.display = S.datasets.length ? 'none' : 'block';
  $('#src-table').style.display = S.datasets.length ? '' : 'none';
  tb.innerHTML = S.datasets.map(d => `
    <tr data-id="${d.id}">
      <td><input type="checkbox" class="src-check" data-id="${d.id}"></td>
      <td class="ell" title="${esc(d.name)}">
        <b>${esc(d.name)}</b>
        <div class="tip">${d.sheet_count} 个工作表 · ${fmtSize(d.size)}</div>
      </td>
      <td><input class="cell-in" data-id="${d.id}" data-k="lab" value="${esc(d.lab)}" placeholder="未标注"></td>
      <td><input class="cell-in" data-id="${d.id}" data-k="table_type" value="${esc(d.table_type)}" placeholder="未标注"></td>
      <td>${d.total_rows.toLocaleString()}</td>
      <td>${d.field_count}</td>
      <td class="tip">${esc(d.imported_at)}</td>
      <td>
        <button class="btn small act-raw" data-id="${d.id}" title="按 Excel 原样查看这个文件里的原始数据（不做任何清洗）。">预览</button>
        <button class="btn small act-detail" data-id="${d.id}">详情</button>
        <button class="btn small danger act-del" data-id="${d.id}">删除</button>
      </td>
    </tr>`).join('');
}
$('#src-table').addEventListener('change', async e => {
  const el = e.target;
  if (el.classList.contains('cell-in')) {
    const ds = S.datasets.find(d => d.id === el.dataset.id);
    if (ds) ds[el.dataset.k] = el.value;
    await api('/api/dataset/update', { id: el.dataset.id, [el.dataset.k]: el.value });
    updateLabOptions();
  }
});
$('#src-table').addEventListener('click', async e => {
  const dbtn = e.target.closest('.act-del');
  if (dbtn) {
    const ds = S.datasets.find(d => d.id === dbtn.dataset.id);
    if (!confirm(`确定删除「${ds.name}」吗？\n程序内保存的那份文件也会一起删掉（不会动你原来的文件）。`)) return;
    await api('/api/dataset/delete', { ids: [dbtn.dataset.id] });
    toast('已删除', 'ok'); await loadAll(); return;
  }
  const vbtn = e.target.closest('.act-detail');
  if (vbtn) return showDetail(vbtn.dataset.id);
  const rbtn = e.target.closest('.act-raw');
  if (rbtn) return showRawPreview(rbtn.dataset.id);
});
$('#src-all').addEventListener('change', e => {
  $$('.src-check').forEach(c => c.checked = e.target.checked);
});
async function bulkDelete() {
  const ids = $$('.src-check').filter(c => c.checked).map(c => c.dataset.id);
  if (!ids.length) return toast('先勾选要删除的表', 'err');
  if (!confirm(`确定删除选中的 ${ids.length} 个数据表吗？`)) return;
  await api('/api/dataset/delete', { ids });
  toast('已删除 ' + ids.length + ' 个', 'ok'); $('#src-all').checked = false; await loadAll();
}
async function bulkSet(key) {
  const ids = $$('.src-check').filter(c => c.checked).map(c => c.dataset.id);
  if (!ids.length) return toast('先勾选数据表', 'err');
  const label = key === 'lab' ? '实验室名称' : '表类型';
  const v = prompt(`把选中的 ${ids.length} 张表的「${label}」统一设为：`, '');
  if (v === null) return;
  for (const id of ids) await api('/api/dataset/update', { id, [key]: v });
  toast('已更新', 'ok'); await loadAll();
}
// 【必须保留】三个批量按钮的接线（同样曾经丢失）
$('#btn-bulk-del').onclick = bulkDelete;
$('#btn-bulk-lab').onclick = () => bulkSet('lab');
$('#btn-bulk-type').onclick = () => bulkSet('table_type');

/* 一键恢复：删除记录只删程序里的副本，原始文件还在，可以重新捞回来 */
$('#btn-restore-recent').onclick = async () => {
  const dirs = (S.config && S.config.recent_source_dirs) || [];
  if (!dirs.length) return toast('还没有记录过导入来源。先正常导入一次文件，之后就能一键恢复了', 'err');
  if (!confirm('将从下面这些位置重新扫描并导入表格：\n\n' + dirs.join('\n')
    + '\n\n已经导入过的会自动跳过。继续吗？')) return;
  const b = $('#btn-restore-recent');
  b.disabled = true; const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>恢复中…';
  try {
    const r = await api('/api/import_recent', {});
    await loadAll();
    const n = (r.added || []).length, sk = (r.skipped || []).length;
    toast(`恢复完成：新导入 ${n} 个，跳过已存在的 ${sk} 个`, n ? 'ok' : 'err');
    if (!n && sk) showError('没有新文件可恢复 —— 这些位置里的表格都还在程序里。\n\n' + dirs.join('\n'));
  } catch (e) {
    showError('恢复失败：' + e.message);
  } finally { b.innerHTML = old; b.disabled = false; }
};

async function showDetail(id) {
  const ds = S.datasets.find(d => d.id === id);
  const body = [];
  body.push(`<div class="kv-row"><span>文件名</span><code>${esc(ds.name)}</code></div>
    <div class="kv-row"><span>实验室</span><code>${esc(ds.lab || '未标注')}</code></div>
    <div class="kv-row"><span>表类型</span><code>${esc(ds.table_type || '未标注')}</code></div>
    <div class="kv-row"><span>文件信息</span><code>${fmtSize(ds.size)} · 导入于 ${esc(ds.imported_at)}</code></div>`);
  body.push('<div class="tip" style="margin:12px 0">表头行：程序自动判断的，如果字段名不对（比如多行表头没拼对），改成正确行号即可，例如单行填 <code>1</code>，两行表头填 <code>1-2</code>。</div>');
  for (const sh of ds.sheets || []) {
    const isInfo = sh.kind === 'info';
    body.push(`<div class="mapgrp" style="margin-bottom:12px">
      <div class="mh">${esc(sh.name)} · ${sh.rows} 行 · ${(sh.columns || []).length} 字段
        ${isInfo ? '<span class="tag o" style="margin-left:6px">信息表（标签/内容）</span>' : ''}</div>
      <div style="padding:8px 10px">
        <label class="chk" style="margin-right:16px">读取方式
          <select class="kd-sel" data-id="${id}" data-sheet="${esc(sh.name)}" style="width:auto;margin-left:6px">
            <option value="table"${isInfo ? '' : ' selected'}>列式表格（第一行是表头）</option>
            <option value="info"${isInfo ? ' selected' : ''}>信息表（一行一个"标签｜内容"）</option>
          </select>
        </label>
        <label class="chk">表头行 <input class="hr-in" data-id="${id}" data-sheet="${esc(sh.name)}"
          value="${esc(isInfo ? '—' : (sh.header_rows || '1'))}" ${isInfo ? 'disabled' : ''} style="width:70px;margin:0 8px">
          <button class="btn small hr-save" data-id="${id}" data-sheet="${esc(sh.name)}">重新识别</button></label>
        <div class="tip" style="margin-top:8px">字段：${(sh.columns || []).map(c => `<span class="tag">${esc(c)}</span>`).join('') || '（无）'}</div>
        ${sh.error ? `<div class="tip" style="color:#b93a3a;margin-top:6px">解析出错：${esc(sh.error)}</div>` : ''}
      </div></div>`);
  }
  $('#modal-title').textContent = '数据表详情';
  $('#modal-body').innerHTML = body.join('');
  $('#modal-body').classList.remove('rawmode');   // 详情用窄弹窗，别受原始预览的宽模式影响
  $('#modal-body').classList.remove('peekmode');  // 合并预览也是宽模式，同样要还原
  $('#modal').classList.add('show');

  try {
    const pv = await api(`/api/preview?dataset_id=${id}&sheet=${encodeURIComponent(ds.sheets[0].name)}`);
    $('#modal-body').insertAdjacentHTML('beforeend',
      `<h2 style="margin-top:6px">数据预览（${esc(ds.sheets[0].name)}，前 ${pv.rows.length} 行）</h2>
       <div class="tablewrap">${tableHTML(pv.columns, pv.rows)}</div>`);
  } catch (e) { }
}
$('#modal-body').addEventListener('click', async e => {
  const b = e.target.closest('.hr-save');
  if (!b) return;
  const inp = $(`.hr-in[data-sheet="${CSS.escape(b.dataset.sheet)}"][data-id="${b.dataset.id}"]`);
  await api('/api/dataset/update', { id: b.dataset.id, sheets: [{ name: b.dataset.sheet, header_rows: inp.value }] });
  toast('已按新表头行重新识别', 'ok');
  S.datasets = (await api('/api/state')).datasets;
  showDetail(b.dataset.id);
});
$('#modal-body').addEventListener('change', async e => {
  const s = e.target.closest('.kd-sel');
  if (!s) return;
  await api('/api/dataset/update', {
    id: s.dataset.id,
    sheets: [{ name: s.dataset.sheet, kind: s.value, header_rows: s.value === 'info' ? 'info' : '' }],
  });
  toast(s.value === 'info' ? '已按信息表读取' : '已按列式表格读取', 'ok');
  S.datasets = (await api('/api/state')).datasets;
  showDetail(s.dataset.id);
});
function closeModal() {
  $('#modal').classList.remove('show');
  // 宽弹窗模式（原始数据预览 / 合并预览）都会放宽弹窗宽度，
  // 关掉时必须把两个模式类都还原，否则会连带把「详情」这类窄弹窗撑宽。
  $('#modal-body').classList.remove('rawmode');
  $('#modal-body').classList.remove('peekmode');
}
$('#modal-close').onclick = closeModal;
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };

/* ================= 原始数据预览（读磁盘 Excel 原样显示 + 可编辑） =================
   需求：在「数据源管理 → 已导入数据表」能直接看原始数据表，并**直接改/删**。

   与「详情」里的数据预览的区别（这是本功能存在的理由）：
   · 详情里的预览走 /api/preview —— 是程序*清洗后*的识别结果：
     多层表头已合并成一列、标题行已跳过、图表说明行被去掉。
     适合确认"字段识别对不对"，但看不到文件本来的样子。
   · 这里的预览走 /api/raw_preview —— 从磁盘 xlsx 原样读回来：
     第 1 行就是第 1 行，空行空列照留，合并单元格照旧。
     适合确认"我这份表到底长什么样""第几行有问题"。
   —— 两者的关系是"识别结果"与"原始底稿"，不是重复功能。

   ------------------------------------------------------------------
   ★ v2.2.0 编辑能力：**只改内存，绝不写回磁盘上的 Excel**。
     改完点「导出为新文件」落一份新表到「导出结果」目录；
     关掉程序就还原成原样，library/sources 里的原始底稿永远不动。
   ------------------------------------------------------------------

   另外支持：切换该文件里的任意工作表、100 行/页翻页、行列号（Excel 的 A/B/C 与 1/2/3）。
*/
const RAW_PAGE_SIZE = 100;
// 前端渲染保险：即便后端因故返回了超宽表，也只画这么多列。
// 背景：Excel 里给整行拖过格式，行长度会被撑到 16384（Excel 列上限），
// 直接照画就是几十万个 <td>，浏览器会卡死。
const RAW_MAX_COLS = 120;

let rawState = {
  id: null,       // 当前数据集 id
  sheet: '',      // 当前工作表名
  offset: 0,      // 当前页起始行（0-based，指的是"数据行序号"不含表头行）
  size: RAW_PAGE_SIZE,
  total: 0,
  cols: 0,
  sheets: [],
  loading: false,
  // ---- v2.2.0 编辑态 ----
  editing: false,     // 是否处于「编辑模式」（默认关闭，防误触）
  rowIdx: [],         // 本页每行对应的**原始行号**（改/删都要按它提交）
  colIdx: [],         // 本页每列对应的**原始列号**
  marks: {},          // "本页行,本页列" -> true，被改过的格（用来标黄）
  dirty: false,       // 有没有未导出的改动
  removedRows: 0,
  removedCols: 0,
  cleared: false,
  canUndo: false,
  canRedo: false,
  changed: 0,         // 改过多少个格
  allSheets: [],      // 文件里原本的全部子表（含被删的）
  delSheets: [],      // 已被删掉的子表名
};

/* 0 -> A, 25 -> Z, 26 -> AA, 701 -> ZZ, 702 -> AAA */
function colLetter(i) {
  let s = '';
  i = i + 1;
  while (i > 0) {
    const r = (i - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    i = Math.floor((i - 1) / 26);
  }
  return s;
}
/* 反解：A -> 0, Z -> 25, AA -> 26（用于把用户输入的列号转成索引） */
function colIndex(s) {
  let n = 0;
  for (const ch of String(s).toUpperCase()) {
    const c = ch.charCodeAt(0);
    if (c < 65 || c > 90) return -1;
    n = n * 26 + (c - 64);
  }
  return n - 1;
}

/* 原样预览的表格：左侧行号（Excel 真实行号）+ 表头写列号 A/B/C
   v2.2.0：编辑模式下每个格可双击编辑；行号/列号上有删除按钮。 */
function rawTableHTML(rows, cols, offset) {
  let n = cols || 1;
  if (n > RAW_MAX_COLS) n = RAW_MAX_COLS;      // 渲染保险，见 RAW_MAX_COLS 注释
  const on = rawState.editing;
  const rowIdx = rawState.rowIdx || [];
  const colIdx = rawState.colIdx || [];

  const head = '<thead><tr><th class="raw-gut">#</th>'
    + Array.from({ length: n }, (_, i) => {
      const rawc = colIdx.length ? colIdx[i] : i;
      return `<th class="raw-colh">`
        + `<span class="raw-cl">${colLetter(rawc)}</span>`
        + (on ? `<button class="raw-delcol" data-c="${rawc}" title="删掉整列 ${colLetter(rawc)}（只影响当前会话的内存数据）">✕</button>` : '')
        + `</th>`;
    }).join('')
    + '</tr></thead>';

  if (!rows || !rows.length) {
    return `<table class="grid rawgrid${on ? ' editon' : ''}">${head}<tbody></tbody></table>`
      + '<div class="empty">这个工作表没有可显示的内容（是空表）。</div>';
  }
  const body = rows.map((r, ri) => {
    const lineNo = offset + ri + 1;          // Excel 里的真实行号
    const rawr = rowIdx.length ? rowIdx[ri] : (offset + ri);
    const tds = Array.from({ length: n }, (_, ci) => {
      const rawc = colIdx.length ? colIdx[ci] : ci;
      const v = r[ci] == null ? '' : r[ci];
      const t = String(v);
      const mk = rawState.marks[ri + ',' + ci] ? ' changed' : '';
      return `<td class="ell${mk}" data-r="${rawr}" data-c="${rawc}"`
        + ` title="${esc(t).slice(0, 300)}">`
        + `${esc(t.length > 200 ? t.slice(0, 200) + '…' : t)}</td>`;
    }).join('');
    return `<tr data-r="${rawr}"><td class="raw-gut">`
      + `<span class="raw-ln">${lineNo}</span>`
      + (on ? `<button class="raw-delrow" data-r="${rawr}" title="删掉整行（Excel 第 ${lineNo} 行，只影响当前会话的内存数据）">✕</button>` : '')
      + `</td>${tds}</tr>`;
  }).join('');
  return `<table class="grid rawgrid${on ? ' editon' : ''}">${head}<tbody>${body}</tbody></table>`;
}

/* 画一个「子表」标签。v2.2.0：编辑模式下悬停出现 ✕ 可整表删掉。
   canDel 为 false 时（只剩最后一张 / 只读模式）不渲染按钮，避免误操作。 */
function rawTabHTML(name) {
  const on = name === rawState.sheet;
  const editable = rawState.editing;
  // 至少留一张：剩 1 张时不给删
  const canDel = editable && (rawState.sheets || []).length > 1;
  return `<button class="raw-tab${on ? ' on' : ''}" data-sheet="${esc(name)}">`
    + `<span class="raw-tab-t">${esc(name)}</span>`
    + (canDel
        ? `<span class="raw-deltab" data-sheet="${esc(name)}"`
          + ` title="删掉整个子表「${esc(name)}」（不写成文件，只影响界面上看到的数据；想落盘再点「保存并替换原文件」或「导出为新文件」）">✕</span>`
        : '')
    + `</button>`;
}

/* 画预览弹窗的骨架（工作表标签条 + 表格区 + 分页条），只画一次 */
function rawSkeleton() {
  const tabs = rawState.sheets.map(rawTabHTML).join('');
  return `
    <div class="rawbar">
      <div class="raw-tabs" id="raw-tabs">${tabs}</div>
      <div class="raw-editbar" id="raw-editbar"></div>
    </div>
    <div class="rawmeta" id="raw-meta"></div>
    <div class="tablewrap rawwrap" id="raw-wrap"></div>
    <div class="rawpager" id="raw-pager"></div>
    <div class="raw-editfoot" id="raw-editfoot"></div>`;
}

/* 编辑工具条：模式开关 + 撤销/重做 + 丢弃 + 导出 */
function rawEditBar() {
  const e = rawState;
  const on = e.editing;
  const cnt = e.changed || 0;
  const extra = [];
  if (e.removedRows) extra.push(`删 ${e.removedRows} 行`);
  if (e.removedCols) extra.push(`删 ${e.removedCols} 列`);
  if ((e.delSheets || []).length) extra.push(`删 ${e.delSheets.length} 个子表`);
  if (e.cleared) extra.push('已清空整表');
  const badge = e.dirty
    ? `<span class="raw-dirty" title="这些改动默认只在内存里。点「保存并替换原文件」写回 Excel，或点「导出为新文件」另存一份。">`
      + `● 已改 ${cnt} 处${extra.length ? '（' + extra.join('、') + '）' : ''}</span>`
    : `<span class="raw-clean">未改动</span>`;
  return `
    <label class="raw-modsw${on ? ' on' : ''}" title="打开后才能改单元格。默认关闭，防止手滑改坏数据。">
      <input type="checkbox" id="raw-edit-on" ${on ? 'checked' : ''}> 编辑模式
    </label>
    ${on ? `
      <button class="btn small" id="raw-undo" ${e.canUndo ? '' : 'disabled'} title="撤销上一步（Ctrl+Z）">↶ 撤销</button>
      <button class="btn small" id="raw-redo" ${e.canRedo ? '' : 'disabled'} title="重做（Ctrl+Y）">↷ 重做</button>
      <button class="btn small danger" id="raw-reset" ${e.dirty ? '' : 'disabled'} title="丢弃本次的全部编辑，恢复成原始数据">丢弃编辑</button>
      <button class="btn small primary" id="raw-export" title="把编辑后的表另存成一份新文件（不动原表）">导出为新文件</button>
      <button class="btn small save-orig" id="raw-save" ${e.dirty ? '' : 'disabled'} title="把编辑结果写回这份数据表本身（覆盖前会自动备份原文件）">保存并替换原文件</button>
    ` : ''}
    <span style="flex:1"></span>
    ${badge}`;
}

/* 底部提示条：说清"改了没、存到哪、原文件动没动" */
function rawEditFoot() {
  if (!rawState.editing) return '';
  if (!rawState.dirty) {
    return `<div class="raw-foot-tip">双击单元格改内容 · 选中后按 Delete 清空 · 点行号/列号上的 ✕ 删整行整列。
      改动<b>只在这个会话里生效</b>，随时可以「丢弃编辑」还原。
      想留下改动：「保存并替换原文件」写回这份数据表，或「导出为新文件」另存一份。</div>`;
  }
  return `<div class="raw-foot-tip warn">当前有未保存的改动，它们<b>只在内存里</b>——
    点「保存并替换原文件」会<b>覆盖这份数据表</b>（覆盖前自动备份原文件到「导出结果/_原始文件备份」）；
    点「导出为新文件」则另存一份、不碰原表。关掉程序后未保存的改动就没了。</div>`;
}

/* 只重画子表标签条。
   为什么单独抽出来：标签上的 ✕ 是"编辑模式才显示"的，而切 sheet / 删子表 /
   开关编辑模式这三处都需要刷新它 —— 各自抄一遍 innerHTML 容易漏（曾漏过）。 */
function rawPaintTabs() {
  const bar = $('#raw-tabs');
  if (!bar) return;
  if (!(rawState.sheets || []).length) return;
  bar.innerHTML = rawState.sheets.map(rawTabHTML).join('');
}

/* 只重画表格区、分页条与编辑条（切页/切 sheet 走这里，不重建标签条，避免闪烁） */
function rawPaint(pv) {
  rawState.total = pv.total;
  rawState.cols = pv.cols;
  rawState.offset = pv.offset;
  rawState.size = pv.size;
  if (pv.row_idx) rawState.rowIdx = pv.row_idx;
  if (pv.col_idx) rawState.colIdx = pv.col_idx;
  rawState.dirty = !!pv.dirty;
  rawState.removedRows = pv.removed_rows || 0;
  rawState.removedCols = pv.removed_cols || 0;
  rawState.cleared = !!pv.cleared;
  if (pv.sheets) rawState.sheets = pv.sheets;
  if (pv.all_sheets) rawState.allSheets = pv.all_sheets;
  rawState.delSheets = pv.del_sheets || [];
  // 后端回传的改动标记 → 前端用 "本页行,本页列" 索引，方便重画时复原标黄
  const marks = {};
  (pv.edits || []).forEach(([r, c]) => { marks[r + ',' + c] = true; });
  rawState.marks = marks;

  // 子表标签条：数量/内容可能因"删子表"而变化，每次重画都同步一次
  if (pv.sheets) {
    rawState.sheets = pv.sheets;
    rawPaintTabs();
  }
  $('#raw-wrap').innerHTML = rawTableHTML(pv.rows, pv.cols, pv.offset);
  $('#raw-editbar').innerHTML = rawEditBar();
  $('#raw-editfoot').innerHTML = rawEditFoot();

  const from = pv.total ? pv.offset + 1 : 0;
  const to = pv.offset + pv.rows.length;
  // 列被截断时要说清楚（Excel 里拖过格式的表，原始列数可能是 16384）
  const clipNote = pv.cols_clipped
    ? `<span class="dot">·</span><span title="这份表在 Excel 里被拖过格式，原始列数被撑到 ${pv.cols_raw} 列。为了不卡死浏览器，这里只画有内容的前 ${pv.cols} 列。">`
      + `已裁掉末尾空列（原 ${pv.cols_raw} 列 → 显示 ${pv.cols} 列）</span>`
    : '';
  const editNote = rawState.dirty
    ? `<span class="dot">·</span><span class="raw-flag-edit" title="这是叠加了本次编辑之后的样子，还没导出。">已编辑（未导出）</span>`
    : '';
  const delNote = (rawState.delSheets || []).length
    ? `<span class="dot">·</span><span class="raw-flag-del" title="这些子表在本会话里被删掉了：`
      + `${esc((rawState.delSheets || []).join('、'))}">已删 ${rawState.delSheets.length} 个子表</span>`
    : '';
  // ★ 「刚保存回原文件」这个角标必须由 rawPaint 自己渲染。
  //   我第一次是用 insertAdjacentHTML 往 #raw-meta 里塞的 —— 但保存完紧接着
  //   就 rawLoad() 重画，而 rawPaint 是 `#raw-meta.innerHTML = ...` 整块覆盖，
  //   塞进去的角标当场被抹掉（CDP 断言meta里找不到 .raw-saveok 就是这么来的）。
  const savedNote = rawState.savedAt
    ? `<span class="dot">·</span><span class="raw-saveok" title="已写回这份数据表本身`
      + `${rawState.savedBackup ? '，覆盖前已备份原文件' : ''}">已保存回原文件</span>`
    : '';
  $('#raw-meta').innerHTML =
    `<span>工作表 <b>${esc(rawState.sheet)}</b></span>`
    + `<span class="dot">·</span><span>共 <b>${pv.total}</b> 行 / <b>${pv.cols}</b> 列</span>`
    + `<span class="dot">·</span><span>当前 ${from}–${to} 行</span>`
    + clipNote + editNote + delNote + savedNote
    + `<span style="flex:1"></span>`
    + `<span class="rawflag" title="这里是文件本来的样子：第 1 行就是第 1 行，空行空列都保留，不做任何清洗。表头列号对应 Excel 的列。">原样显示 · 未清洗</span>`;

  const pages = Math.max(1, Math.ceil(pv.total / rawState.size));
  const cur = Math.floor(pv.offset / rawState.size) + 1;
  $('#raw-pager').innerHTML =
    `<button class="btn small" id="raw-first" ${cur <= 1 ? 'disabled' : ''}>« 首页</button>`
    + `<button class="btn small" id="raw-prev" ${cur <= 1 ? 'disabled' : ''}>‹ 上一页</button>`
    + `<span class="pg">第 <b>${cur}</b> / ${pages} 页</span>`
    + `<button class="btn small" id="raw-next" ${cur >= pages ? 'disabled' : ''}>下一页 ›</button>`
    + `<button class="btn small" id="raw-last" ${cur >= pages ? 'disabled' : ''}>末页 »</button>`
    + `<span style="flex:1"></span>`
    + `<span class="pg">每页</span>`
    + `<select class="raw-size" id="raw-size">`
    + [50, 100, 200, 500].map(s => `<option value="${s}"${s === rawState.size ? ' selected' : ''}>${s}</option>`).join('')
    + `</select><span class="pg">行</span>`;
}

/* 拉一页数据并重画。edit=true 时叠加内存编辑层。 */
async function rawLoad(offset) {
  if (rawState.loading) return;
  rawState.loading = true;
  $('#raw-wrap').innerHTML = '<div class="empty">正在读取原始数据…</div>';
  try {
    const pv = await api(`/api/raw_preview?dataset_id=${encodeURIComponent(rawState.id)}`
      + `&sheet=${encodeURIComponent(rawState.sheet)}`
      + `&offset=${offset}&size=${rawState.size}`
      + (rawState.editing ? '&edit=1' : ''));
    rawPaint(pv);
  } catch (e) {
    $('#raw-wrap').innerHTML = `<div class="empty">读不到原始数据：${esc(e.message)}</div>`;
    $('#raw-meta').innerHTML = '';
    $('#raw-pager').innerHTML = '';
  } finally {
    rawState.loading = false;
  }
}

/* 提交一批编辑操作 → 后端只改内存 → 拿最新这一页重画 */
async function rawApply(ops) {
  if (!ops || !ops.length) return;
  try {
    const r = await api('/api/raw_edit/apply', {
      dataset_id: rawState.id, sheet: rawState.sheet, ops: ops });
    rawState.canUndo = !!r.can_undo;
    rawState.canRedo = !!r.can_redo;
    rawState.changed = r.changed || 0;
    // 又改了东西 → "已保存回原文件"那个角标不再成立，撤掉
    if (rawState.savedAt) { rawState.savedAt = 0; rawState.savedBackup = false; }
    // 提交后重新拉当前页（行号映射可能因删行而变化）
    await rawLoad(rawState.offset);
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* 撤销 / 重做：走同一套 op 通道 */
async function rawUndo() { await rawApply([{ t: 'undo' }]); }
async function rawRedo() { await rawApply([{ t: 'redo' }]); }

/* 双击单元格 → 就地编辑（用 input 覆盖，回车/失焦提交，Esc 取消） */
function rawBeginCellEdit(td) {
  if (!rawState.editing || td.querySelector('input')) return;
  const rawr = parseInt(td.dataset.r, 10);
  const rawc = parseInt(td.dataset.c, 10);
  if (isNaN(rawr) || isNaN(rawc)) return;
  const old = td.textContent;
  const inp = document.createElement('input');
  inp.className = 'raw-cellin';
  inp.value = old;
  td.innerHTML = '';
  td.appendChild(inp);
  inp.focus();
  inp.select();
  let done = false;
  const finish = async commit => {
    if (done) return;
    done = true;
    const nv = commit ? inp.value : old;
    td.textContent = old;                 // 先还原，等重画
    if (commit && nv !== old) {
      // 记下"本页哪一格变黄"，重画时按 row/col 索引复原
      const ri = rawState.rowIdx.indexOf(rawr);
      const ci = rawState.colIdx.indexOf(rawc);
      if (ri >= 0 && ci >= 0) rawState.marks[ri + ',' + ci] = true;
      await rawApply([{ t: 'set', r: rawr, c: rawc, v: nv }]);
    }
  };
  inp.onkeydown = ev => {
    if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
    else if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
  };
  inp.onblur = () => finish(true);
}

/* 删整行 / 删整列 / 清空整表 —— 都带二次确认（预勾选"不再问"的记忆放在会话里） */
async function rawDeleteRows(rawRows) {
  if (!rawRows.length) return;
  if (!confirm(`确定删掉这 ${rawRows.length} 行？\n\n这个改动不写成文件，只影响界面上看到的数据；想落盘再点「保存并替换原文件」或「导出为新文件」。`)) return;
  await rawApply(rawRows.map(r => ({ t: 'row', r: r })));
}
async function rawDeleteCols(rawCols) {
  if (!rawCols.length) return;
  if (!confirm(`确定删掉这 ${rawCols.length} 列？\n\n这个改动不写成文件，只影响界面上看到的数据；想落盘再点「保存并替换原文件」或「导出为新文件」。`)) return;
  await rawApply(rawCols.map(c => ({ t: 'col', c: c })));
}
async function rawClearSheet() {
  if (!confirm('确定清空整个工作表的内容？\n\n这个改动不写成文件，只影响界面上看到的数据；想落盘再点「保存并替换原文件」或「导出为新文件」，也可随时「撤销」。')) return;
  await rawApply([{ t: 'sheet' }]);
}

/* 删掉一整个子表（工作表）—— 只改内存，不写成文件。
   删的是当前正在看的这张时，自动切到相邻的一张，免得看到空白。 */
async function rawDeleteSheet(name) {
  const live = rawState.sheets || [];
  if (live.length <= 1) {
    toast('至少要留一个子表，不能全删光', 'err');
    return;
  }
  if (!confirm(`确定删掉整个子表「${name}」？\n\n`
    + '这个改动不写成文件，只影响界面上看到的数据；想落盘再点「保存并替换原文件」或「导出为新文件」，也可随时「撤销」。\n'
    + '导出为新文件时将不再包含这个子表。')) return;

  // 删的是当前这张 → 先选好下一个落脚点（优先右边，没有就左边）
  const wasCurrent = (name === rawState.sheet);
  let next = rawState.sheet;
  if (wasCurrent) {
    const i = live.indexOf(name);
    next = live[i + 1] || live[i - 1] || live[0];
  }
  try {
    const r = await api('/api/raw_edit/apply', {
      dataset_id: rawState.id, sheet: rawState.sheet,
      ops: [{ t: 'sheettab', name: name }] });
    rawState.canUndo = !!r.can_undo;
    rawState.canRedo = !!r.can_redo;
    rawState.sheets = r.sheets || [];
    rawState.delSheets = r.del_sheets || [];
    rawState.marks = {};
    if (wasCurrent && next && next !== name) {
      rawState.sheet = next;
      rawState.offset = 0;
    }
    await rawLoad(rawState.offset);
    toast(`已删掉子表「${name}」（只在本会话生效，原始文件未动）`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* 导出编辑后的表为新文件 */
async function rawExportEdit() {
  const live = rawState.sheets || [];
  const del = rawState.delSheets || [];
  const multi = live.length > 1;
  const fmt = (prompt(
    multi
      ? `导出格式：xlsx 还是 csv？\n\n（xlsx 会把剩下的 ${live.length} 个子表都写进同一个工作簿）`
      : '导出格式：xlsx 还是 csv？', 'xlsx') || '').trim().toLowerCase();
  if (fmt !== 'xlsx' && fmt !== 'csv') return;
  const name = prompt('文件名（可留空，默认「原文件名_已编辑」）：', '') || '';
  try {
    const r = await api('/api/raw_edit/export', {
      dataset_id: rawState.id, sheet: rawState.sheet, fmt: fmt, name: name });
    let msg = `已导出：${r.file}（${r.rows} 行 / ${r.cols} 列`;
    const ns = (r.exported_sheets || []).length;
    if (ns > 1) msg += ` · ${ns} 个子表`;
    msg += '）';
    toast(msg, 'ok');
    if (r.note) toast(r.note, 'info');
    $('#raw-meta').insertAdjacentHTML('beforeend',
      `<span class="dot">·</span><span class="raw-expok" title="${esc(r.path)}">`
      + `已导出 ${esc(r.file)}${ns > 1 ? `（${ns} 个子表）` : ''}`
      + `${(r.skipped_sheets || []).length ? `，已跳过 ${r.skipped_sheets.length} 个删掉的子表` : ''}`
      + `</span>`);
  } catch (e) {
    toast('导出失败：' + e.message, 'err');
  }
}

/* ★ v2.3.0 把编辑结果写回原始文件（覆盖）。
   覆盖不可逆，所以这里刻意做了"看得见后果"的三段确认：
     ① 列出目标文件名 + 会写几个子表/多少行；
     ② 点名哪些子表会被删掉（删了就从文件里消失了）；
     ③ 告诉用户备份会落在哪。
   后端在动手前还会自动备份一份，但**不能指望用户知道**，所以这里写清楚。 */
async function rawSaveOriginal() {
  if (!rawState.dirty) return;
  const live = rawState.sheets || [];
  const del = rawState.delSheets || [];
  const ds = S.datasets.find(d => d.id === rawState.id) || {};
  const lines = [
    `要把编辑结果写回这份数据表本身吗？`,
    ``,
    `文件：${ds.name || rawState.id}`,
    `会写入：${live.length} 个子表${live.length ? '（' + live.join('、') + '）' : ''}`,
  ];
  if (del.length) lines.push(`会被删掉：${del.join('、')}（从文件里真正移除）`);
  lines.push(
    ``,
    `覆盖前会自动把原文件备份一份到：`,
    `「导出结果/_原始文件备份」`,
    ``,
    `写完之后，这份数据表就是编辑后的样子了。`,
  );
  if (!confirm(lines.join('\n'))) return;

  const btn = $('#raw-save');
  if (btn) { btn.disabled = true; btn.textContent = '正在保存…'; }
  try {
    const r = await api('/api/raw_edit/save', {
      dataset_id: rawState.id, sheet: rawState.sheet, backup: true });
    // 改动已落盘 → 编辑态清空，并让当前子表回到"还活着"的一栏
    rawState.marks = {}; rawState.changed = 0;
    rawState.canUndo = false; rawState.canRedo = false;
    rawState.dirty = false;
    rawState.delSheets = [];
    // ★ 角标靠 rawState 驱动、由 rawPaint 渲染 —— 别往 #raw-meta 里塞 HTML，
    //   rawLoad 会整块覆盖它（踩过这个坑）。
    rawState.savedAt = Date.now();
    rawState.savedBackup = !!r.backup;
    if (r && r.saved_sheets) rawState.sheets = r.saved_sheets;
    rawState.allSheets = (r && r.saved_sheets ? r.saved_sheets.slice() : rawState.allSheets);
    if (rawState.sheets.length && rawState.sheets.indexOf(rawState.sheet) < 0) {
      rawState.sheet = rawState.sheets[0];
      rawState.offset = 0;
    }
    await rawLoad(rawState.offset);
    // 侧栏/字段字典都要刷新：表头识别结果可能已经变了
    if (typeof loadAll === 'function') { try { await loadAll(true); } catch (e) {} }
    toast(`已保存并替换原文件（${r.rows} 行`
      + `${(r.saved_sheets || []).length > 1 ? ` · ${r.saved_sheets.length} 个子表` : ''}）`, 'ok');
    if (r.backup) toast('原文件已备份：' + r.backup, 'info');
    if (r.reparse_error) {
      toast('数据已保存，但表头重新识别失败（索引可能不准）：'
        + r.reparse_error + '　建议重新导入这个文件。', 'err');
    }
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '保存并替换原文件'; }
  }
}

async function showRawPreview(id) {
  const ds = S.datasets.find(d => d.id === id);
  if (!ds) return;
  rawState.id = id;
  rawState.offset = 0;
  rawState.size = RAW_PAGE_SIZE;
  rawState.sheet = ((ds.sheets || [])[0] || {}).name || '';
  rawState.editing = false;         // 每次打开都从只读开始，防误触
  rawState.marks = {};
  rawState.dirty = false;
  rawState.canUndo = false;
  rawState.canRedo = false;
  rawState.changed = 0;
  rawState.delSheets = [];
  rawState.savedAt = 0;             // 重开预览：清掉"已保存"角标
  rawState.savedBackup = false;
  // 先用索引里的子表列表占位；等下 rawLoadFirst 会拿后端"还活着的"列表覆盖它
  rawState.sheets = (ds.sheets || []).map(s => s.name).filter(Boolean);
  if (!rawState.sheets.length && rawState.sheet) rawState.sheets = [rawState.sheet];
  rawState.allSheets = rawState.sheets.slice();

  $('#modal-title').textContent = '原始数据预览 · ' + ds.name;
  $('#modal-body').innerHTML = rawSkeleton();
  $('#modal').classList.add('show');
  $('#modal-body').classList.add('rawmode');

  // 工作表标签条只在第一次拿数据时才知道完整列表（这里用后端的 sheets）
  await rawLoadFirst();
  rawWire();
}

/* 绑定这一轮的事件（骨架重画后要重新绑） */
function rawWire() {
  // 点工作表标签：切表；点到标签里的 ✕ 则是"删整个子表"（★ 必须先判断 ✕）
  $('#raw-tabs').onclick = async e => {
    const del = e.target.closest('.raw-deltab');
    if (del) {
      e.stopPropagation();
      return rawDeleteSheet(del.dataset.sheet);
    }
    const b = e.target.closest('.raw-tab');
    if (!b || b.classList.contains('on')) return;
    rawState.sheet = b.dataset.sheet;
    rawState.offset = 0;
    rawState.marks = {};
    $$('#raw-tabs .raw-tab').forEach(x => x.classList.toggle('on', x === b));
    await rawLoad(0);
  };

  // 编辑条（重画后仍用委托，绑在容器上）
  $('#raw-editbar').onclick = async e => {
    if (e.target.id === 'raw-undo') return rawUndo();
    if (e.target.id === 'raw-redo') return rawRedo();
    if (e.target.id === 'raw-reset') {
      if (!confirm('丢弃本次的全部编辑，恢复成原始数据？')) return;
      try {
        // all:true → 被删掉的子表也一并放回来（按钮承诺的是"全部编辑"）
        const r = await api('/api/raw_edit/reset',
          { dataset_id: rawState.id, sheet: rawState.sheet, all: true });
        rawState.marks = {}; rawState.changed = 0;
        rawState.canUndo = false; rawState.canRedo = false;
        if (r && r.sheets) rawState.sheets = r.sheets;
        rawState.delSheets = (r && r.del_sheets) || [];
        // 当前停在的子表可能刚被放回来 / 或被删了，校准一下
        if (rawState.sheets.length && rawState.sheets.indexOf(rawState.sheet) < 0) {
          rawState.sheet = rawState.sheets[0];
          rawState.offset = 0;
        }
        await rawLoad(rawState.offset);
        toast('已丢弃编辑，恢复原始数据', 'ok');
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
    if (e.target.id === 'raw-export') return rawExportEdit();
    if (e.target.id === 'raw-save') return rawSaveOriginal();
  };
  $('#raw-editbar').onchange = e => {
    if (e.target.id !== 'raw-edit-on') return;
    rawState.editing = e.target.checked;
    rawState.marks = {};
    // ★ 标签条上的 ✕ 只在编辑模式出现 —— rawLoad 只重画表格区，
    //   所以这里必须**显式重画一次标签条**，否则开关打开后看不到删除入口。
    rawPaintTabs();
    rawLoad(rawState.offset);
  };

  // 表格区：双击编辑 + 行/列删除按钮 + 选格后 Delete 清空
  const wrap = $('#raw-wrap');
  wrap.ondblclick = e => {
    const td = e.target.closest('td.ell');
    if (td) rawBeginCellEdit(td);
  };
  wrap.onclick = e => {
    const dr = e.target.closest('.raw-delrow');
    if (dr) return rawDeleteRows([parseInt(dr.dataset.r, 10)]);
    const dc = e.target.closest('.raw-delcol');
    if (dc) return rawDeleteCols([parseInt(dc.dataset.c, 10)]);
    const td = e.target.closest('td.ell');
    if (td) rawSelectCell(td);
  };
  wrap.onkeydown = e => {
    // 选中的单元格按 Delete / Backspace → 清空
    if (e.key !== 'Delete' && e.key !== 'Backspace') return;
    const td = wrap.querySelector('td.ell.sel');
    if (!td || td.querySelector('input')) return;
    e.preventDefault();
    rawApply([{ t: 'set', r: parseInt(td.dataset.r, 10), c: parseInt(td.dataset.c, 10), v: '' }]);
  };

  // 翻页 / 换每页行数（事件委托，因为分页条是重画的）
  $('#raw-pager').onclick = async e => {
    const pages = Math.max(1, Math.ceil(rawState.total / rawState.size));
    const cur = Math.floor(rawState.offset / rawState.size) + 1;
    if (e.target.id === 'raw-first') return rawLoad(0);
    if (e.target.id === 'raw-prev') return rawLoad((cur - 2) * rawState.size);
    if (e.target.id === 'raw-next') return rawLoad(cur * rawState.size);
    if (e.target.id === 'raw-last') return rawLoad((pages - 1) * rawState.size);
  };
  $('#raw-pager').onchange = async e => {
    if (e.target.id !== 'raw-size') return;
    rawState.size = parseInt(e.target.value, 10) || RAW_PAGE_SIZE;
    await rawLoad(0);
  };
}

/* 选中一个单元格（给 Delete 清空用）—— 单选，够用且不引入区域选择的复杂度 */
function rawSelectCell(td) {
  $$('#raw-wrap td.ell.sel').forEach(x => x.classList.remove('sel'));
  td.classList.add('sel');
  const wrap = $('#raw-wrap');
  if (wrap && !wrap.getAttribute('tabindex')) {
    wrap.setAttribute('tabindex', '0');    // 让容器能接住键盘事件
  }
  if (wrap) wrap.focus();
}

async function rawLoadFirst() {
  rawState.loading = true;
  $('#raw-wrap').innerHTML = '<div class="empty">正在读取原始数据…</div>';
  try {
    const pv = await api(`/api/raw_preview?dataset_id=${encodeURIComponent(rawState.id)}`
      + `&sheet=${encodeURIComponent(rawState.sheet)}`
      + `&offset=0&size=${rawState.size}`
      + (rawState.editing ? '&edit=1' : ''));
    // 后端返回的"还活着的"子表列表最可靠（索引里的可能已过期）
    if (pv.sheet) rawState.sheet = pv.sheet;
    if (pv.sheets && pv.sheets.length) rawState.sheets = pv.sheets;
    if (pv.all_sheets && pv.all_sheets.length) rawState.allSheets = pv.all_sheets;
    rawState.delSheets = pv.del_sheets || [];
    rawPaint(pv);       // 标签条由 rawPaint 统一重画，这里不再重复渲染
  } catch (e) {
    $('#raw-wrap').innerHTML = `<div class="empty">读不到原始数据：${esc(e.message)}</div>`;
  } finally {
    rawState.loading = false;
  }
}

/* ---------------- 导入 ---------------- */
function setupImport() {
  const drop = $('#drop');
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => {
    const files = Array.from(e.dataTransfer.files || []).filter(f => /\.(xlsx|xlsm|csv)$/i.test(f.name));
    if (!files.length) return toast('请拖入 .xlsx / .xlsm / .csv 文件', 'err');
    uploadFiles(files);
  });
  $('#btn-choose').onclick = () => $('#file-input').click();
  $('#file-input').onchange = e => { uploadFiles(Array.from(e.target.files)); e.target.value = ''; };
  $('#btn-scan').onclick = () => scanFolder();
}
async function uploadFiles(files) {
  if (!files.length) return;
  // 预检：超大文件允许导入（走流式大表模式），但提前说明耗时
  const big = files.filter(f => f.size > 100 * 1024 * 1024);
  if (big.length) {
    toast(`大文件（${big.map(f => f.name).join('、')}）将走流式导入，可能需要几分钟，请勿关闭页面`, 'ok');
  }
  const btn = $('#btn-choose');
  btn.innerHTML = '<span class="spin"></span>导入中…';
  btn.disabled = true;
  const okList = [], badList = [];
  // ★ 逐个文件用 FormData 原样直传（不走 base64）：
  //   大文件 base64 + 字符串拼接会卡死页面；逐个传还让每个文件都有独立进度。
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    btn.innerHTML = `<span class="spin"></span>导入中（${i + 1}/${files.length}）…`;
    try {
      const fd = new FormData();
      fd.append('files', f, f.name);
      const resp = await fetch('/api/import_upload', { method: 'POST', body: fd });
      let r;
      try { r = await resp.json(); }
      catch (e) { throw new Error('服务返回异常（HTTP ' + resp.status + '）'); }
      if (!r.ok) throw new Error(r.error || '导入失败');
      (r.added || []).forEach(x => okList.push(x));
      (r.errors || []).forEach(x => badList.push(x));
    } catch (e) {
      badList.push({ file: f.name, error: e.message });
      console.error('导入失败', f.name, e);
    }
  }
  toast(`导入完成：成功 ${okList.length} 个${badList.length ? '，失败 ' + badList.length + ' 个' : ''}`, badList.length ? 'err' : 'ok');
  if (badList.length) console.warn('导入失败明细', badList);
  btn.innerHTML = '选择文件导入';
  btn.disabled = false;
  if (okList.length) await loadAll();
}
async function scanFolder() {
  const p = $('#folder-path').value.trim();
  if (!p) return toast('请先粘贴文件夹路径', 'err');
  $('#btn-scan').innerHTML = '<span class="spin"></span>扫描中…';
  try {
    const r = await api('/api/import_paths', { paths: [p] });
    toast(`扫描完成：导入 ${r.added.length} 个文件${r.errors.length ? '，' + r.errors.length + ' 个失败' : ''}`, r.errors.length ? 'err' : 'ok');
    if (r.errors.length) console.warn(r.errors);
    await loadAll();
  } catch (e) { toast('失败：' + e.message, 'err'); }
  $('#btn-scan').innerHTML = '扫描文件夹';
}

/* ---------------- 钉钉在线文档 ---------------- */
function dtLog(msg, append) {
  const el = $('#dt-log');
  el.style.display = 'block';
  el.textContent = append ? (el.textContent + '\n' + msg) : msg;
  el.scrollTop = el.scrollHeight;
}

async function dtRefresh() {
  const dot = $('#dt-dot'), txt = $('#dt-text');
  try {
    const r = await api('/api/dingtalk/status');
    const dt = (S.config && S.config.dingtalk) || {};
    if ($('#dt-url') && !$('#dt-url').value) $('#dt-url').value = r.doc_url || '';
    if ($('#dt-cli') && !$('#dt-cli').value) $('#dt-cli').value = r.cli_template || '';
    if ($('#dt-token') && dt.has_token) $('#dt-token').placeholder = '已保存（' + (dt.token_masked || '****') + '），留空则不改动';
    dot.className = 'dt-dot ' + (r.client_found ? 'ok' : 'bad');
    let s;
    if (!r.client_found) {
      s = '没有检测到钉钉本机服务。请先打开并登录钉钉桌面客户端，然后点「重新检测」。';
    } else if (!r.has_token) {
      s = '已检测到钉钉（本机端口 ' + r.detected_port + '），但还没填访问令牌 —— 装好「钉钉」连接器后把令牌填到 ② 就能直连。';
    } else {
      s = '已检测到钉钉（本机端口 ' + r.detected_port + '），令牌已保存，可以「测试连接并列出工具」。';
    }
    s += ' 已在钉钉相关目录里找到 ' + r.files + ' 个表格文件，可直接「扫描钉钉目录」。';
    txt.textContent = s;
  } catch (e) {
    dot.className = 'dt-dot bad';
    txt.textContent = '检测失败：' + e.message;
  }
}

async function dtSave() {
  const b = {};
  const u = $('#dt-url').value.trim();
  const t = $('#dt-token').value.trim();
  const c = $('#dt-cli').value.trim();
  b.doc_url = u; b.cli_template = c;
  if (t) b.token = t;
  await api('/api/config', { dingtalk: b });
  if (t) $('#dt-token').value = '';
  await loadAll();
  return true;
}

$('#btn-dt-save').onclick = async () => {
  try { await dtSave(); toast('钉钉设置已保存', 'ok'); dtRefresh(); }
  catch (e) { toast('保存失败：' + e.message, 'err'); }
};

$('#btn-dt-refresh').onclick = () => dtRefresh();

$('#btn-dt-test').onclick = async () => {
  const b = $('#btn-dt-test');
  b.disabled = true; const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>连接中…';
  try {
    await dtSave();
    dtLog('正在连接钉钉本机服务…', false);
    const r = await api('/api/dingtalk/test');
    const si = r.server || {};
    dtLog('✓ 连接成功\n  服务：' + (si.name || '钉钉') + ' ' + (si.version || '') +
      '\n  可用工具 ' + (r.tools || []).length + ' 个：\n' +
      (r.tools || []).map(t => '   · ' + t.name + (t.desc ? '  —— ' + t.desc : '')).join('\n'), true);
    toast('钉钉连接成功，共 ' + (r.tools || []).length + ' 个工具', 'ok');
  } catch (e) {
    dtLog('✗ ' + e.message, true);
    showError('连不上钉钉：' + e.message);
  } finally { b.innerHTML = old; b.disabled = false; }
};

$('#btn-dt-pull').onclick = async () => {
  const b = $('#btn-dt-pull');
  b.disabled = true; const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>拉取中…';
  try {
    await dtSave();
    dtLog('正在用命令行模板拉取在线文档…', true);
    const r = await api('/api/dingtalk/cli_pull', {});
    dtLog('✓ 已导入：' + r.file, true);
    await loadAll();
    toast('已从钉钉拉取并导入：' + r.file, 'ok');
  } catch (e) {
    dtLog('✗ ' + e.message, true);
    showError('从钉钉拉取失败：' + e.message);
  } finally { b.innerHTML = old; b.disabled = false; }
};

$('#btn-dt-scan').onclick = async () => {
  const b = $('#btn-dt-scan');
  b.disabled = true; const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>扫描中…';
  const box = $('#dt-files');
  box.innerHTML = '';
  try {
    const r = await api('/api/dingtalk/scan');
    if (!r.files.length) {
      box.innerHTML = '<div class="empty">钉钉相关目录里没找到表格文件。<br>'
        + '（钉盘挂载目录、钉钉下载目录、桌面、文档都找过了）</div>';
      dtLog('扫描完成：0 个表格文件', true);
      return;
    }
    dtLog('扫描完成：找到 ' + r.files.length + ' 个表格文件', true);
    box.innerHTML = '<div class="tl" style="margin-bottom:8px"><b>找到 ' + r.files.length
      + ' 个文件</b> <span class="tip">勾选后点「导入所选」</span></div>'
      + r.files.map((f, i) => `<label class="chk" style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px solid #f0f4f9">
          <input type="checkbox" class="dt-file" value="${esc(f.path)}" style="margin-top:3px">
          <span style="min-width:0">
            <b style="font-size:12.5px">${esc(f.name)}</b>
            <span class="tip"> · ${fmtSize(f.size)} · ${new Date(f.mtime * 1000).toLocaleDateString()}</span>
            <div class="tip" style="word-break:break-all">${esc(f.path)}</div>
          </span></label>`).join('')
      + '<div class="actions"><button class="btn primary" id="btn-dt-import">导入所选</button>'
      + '<button class="btn small" id="btn-dt-import-all">全部导入</button></div>';
    $('#btn-dt-import').onclick = () => dtImport(false);
    $('#btn-dt-import-all').onclick = () => dtImport(true);
  } catch (e) {
    box.innerHTML = '<div class="empty">扫描失败：' + esc(e.message) + '</div>';
  } finally { b.innerHTML = old; b.disabled = false; }
};

async function dtImport(all) {
  let paths = all
    ? $$('.dt-file').map(c => c.value)
    : $$('.dt-file').filter(c => c.checked).map(c => c.value);
  if (!paths.length) return toast('先勾选要导入的文件', 'err');
  if (paths.length > 20 && !confirm(`要导入 ${paths.length} 个文件吗？会有点慢。`)) return;
  toast('正在导入 ' + paths.length + ' 个文件…');
  try {
    const r = await api('/api/import_paths', { paths });
    dtLog('导入完成：成功 ' + (r.added || []).length + ' 个，失败 ' + (r.errors || []).length + ' 个'
      + ((r.errors || []).length ? '\n' + r.errors.map(e => '   × ' + e.file + '：' + e.error).join('\n') : ''), true);
    await loadAll();
    toast('已导入 ' + (r.added || []).length + ' 个文件', 'ok');
  } catch (e) {
    showError('导入失败：' + e.message);
  }
}

/* ---------------- 「① 选择要汇总的数据表」刷新 ----------------
   为什么需要这个按钮：
   导入表格后本列表没变化的根因是「数据不同步」——
   nav('build') 只调用 renderBuild() 重绘**内存里已有的 S.datasets**，
   它不会重新去后端拉数据。所以只要导入动作发生在别处
   （另一个标签页、钉钉扫描、后台任务……），这里就会一直显示旧列表，
   而且切页也救不回来（切页同样只重绘）。
   点这个按钮 = 强制 loadAll() 重新拉一次，任何不同步都能一键修好。 */
$('#src-refresh').onclick = async () => {
  const b = $('#src-refresh');
  if (b.disabled) return;
  b.disabled = true;
  const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>刷新中…';
  const before = S.datasets.length;
  const beforePicked = S.picked.size;
  const kw = $('#src-filter').value;          // 刷新不该把用户的筛选条件清掉
  try {
    await loadAll();                          // 内部会重绘 picklist / 字段 / 映射 / 筛选
    $('#src-filter').value = kw;              // 还原筛选框内容
    renderPickList();
    const after = S.datasets.length;
    // 勾选的项目里，若某个数据表已被删除，它的键会变成"孤儿"，顺手清掉
    const alive = new Set();
    S.datasets.forEach(d => (d.sheets || []).forEach(s => alive.add(d.id + '||' + s.name)));
    let dropped = 0;
    Array.from(S.picked).forEach(k => { if (!alive.has(k)) { S.picked.delete(k); dropped++; } });
    if (dropped) { renderPickList(); renderFieldList(); renderMapping(); }
    const d = after - before;
    let msg = '已刷新：共 ' + after + ' 个数据表';
    if (d > 0) msg += '（新增 ' + d + ' 个）';
    else if (d < 0) msg += '（减少 ' + (-d) + ' 个）';
    if (dropped) msg += '，清理了 ' + dropped + ' 个已失效的勾选';
    toast(msg, 'ok');
  } catch (e) {
    toast('刷新失败：' + e.message, 'err');
  } finally {
    b.innerHTML = old;
    b.disabled = false;
  }
};

/* ---------------- 字段字典 ---------------- */
/* 表头里常有换行（Excel 里自动折行产生的）。只改"显示"，不碰字段名本身，
   否则拿去汇总时和真实列名对不上。 */
function dispName(s) {
  return esc(String(s == null ? '' : s).replace(/[\r\n]+/g, ' ').trim());
}

function fdFiltered() {
  const kw = $('#fd-search').value.trim().toLowerCase();
  const onlyMulti = $('#fd-onlymulti').dataset.on === '1';
  let list = S.fields;
  if (kw) list = list.filter(f => (f.name + (f.aliases || []).join('') + (f.full_names || []).join('')).toLowerCase().includes(kw));
  if (onlyMulti) list = list.filter(f => f.dataset_count > 1);
  return list;
}

function renderFields() {
  const list = fdFiltered();
  const empty = $('#fd-empty');
  if (empty) {
    empty.style.display = list.length ? 'none' : 'block';
    empty.textContent = S.fields.length ? '没有匹配的字段' : '还没有字段。先导入数据表。';
  }
  if (S.fdView === 'h') fdRenderMatrix(list); else fdRenderList(list);
}

/* —— 纵向列表：一行一个字段 —— */
function fdRenderList(list) {
  $('#fd-table').style.display = list.length ? '' : 'none';
  $('#fd-table tbody').innerHTML = list.map(f => {
    const ids = f.dataset_ids || [];
    const labs = [...new Set(ids.map(id => (S.datasets.find(d => d.id === id) || {}).lab || '未标注'))];
    const extra = (f.aliases || []).concat(f.full_names || []);
    return `<tr>
      <td><input type="checkbox" class="fd-check" data-name="${esc(f.name)}" data-ids="${esc(ids.join(','))}"></td>
      <td><b>${dispName(f.name)}</b></td>
      <td class="tip">${extra.length ? extra.slice(0, 4).map(a => dispName(a)).join(' / ') : '—'}</td>
      <td>${f.dataset_count} 个文件</td>
      <td>${f.sheet_count || f.dataset_count} 个工作表</td>
      <td class="tip" title="${esc(labs.join('、'))}">${esc(labs.slice(0, 6).join('、'))}${labs.length > 6 ? ' …' : ''}</td>
    </tr>`;
  }).join('');
}

/* —— 横向矩阵：行=字段，列=数据文件，✓ 表示该文件里有这个字段 —— */
function fdRenderMatrix(list) {
  const box = $('#fd-matrix');
  if (!list.length) { box.innerHTML = ''; return; }
  const ds = S.datasets;
  const head = '<thead><tr><th style="width:38px"></th><th style="min-width:140px">字段名（表头）</th>'
    + ds.map(d => `<th class="mtxcol" title="${esc(d.name + '（' + (d.lab || '未标注') + '）')}">`
      + `<div class="mtxlab">${esc(d.lab || '未标注')}</div>`
      + `<div class="mtxfile">${esc(d.name.replace(/\.xlsx?$/i, '').slice(0, 12))}</div></th>`).join('')
    + '<th style="width:64px">文件数</th></tr></thead>';
  const body = list.map(f => {
    const keys = f.sheet_keys || [];
    const cells = ds.map(d => {
      const n = keys.filter(k => k.indexOf(d.id + '||') === 0).length;
      if (!n) return '<td class="mtx no">·</td>';
      return `<td class="mtx yes" title="${n} 个工作表里有">✓${n > 1 ? '<sub>' + n + '</sub>' : ''}</td>`;
    }).join('');
    return `<tr>
      <td><input type="checkbox" class="fd-check" data-name="${esc(f.name)}" data-ids="${esc((f.dataset_ids || []).join(','))}"></td>
      <td><b>${dispName(f.name)}</b></td>${cells}
      <td class="mtx"><b>${f.dataset_count}</b></td>
    </tr>`;
  }).join('');
  box.innerHTML = head + '<tbody>' + body + '</tbody>';
}

$('#fd-search').oninput = renderFields;
$('#fd-onlymulti').onclick = e => {
  const on = $('#fd-onlymulti').dataset.on === '1';
  $('#fd-onlymulti').dataset.on = on ? '0' : '1';
  $('#fd-onlymulti').classList.toggle('primary', !on);
  renderFields();
};
function setFdView(v) {
  S.fdView = v;
  $('#fd-view-v').classList.toggle('on', v === 'v');
  $('#fd-view-h').classList.toggle('on', v === 'h');
  $('#fd-wrap-list').style.display = v === 'v' ? '' : 'none';
  $('#fd-wrap-matrix').style.display = v === 'h' ? '' : 'none';
  renderFields();
}
$('#fd-view-v').onclick = () => setFdView('v');
$('#fd-view-h').onclick = () => setFdView('h');

/* 纵向：用勾选的字段去多表堆叠汇总 */
$('#fd-use').onclick = () => {
  const picked = $$('.fd-check').filter(c => c.checked);
  if (!picked.length) return toast('先勾选字段', 'err');
  const names = picked.map(c => c.dataset.name);
  let ids = new Set();
  picked.forEach(c => c.dataset.ids.split(',').forEach(i => i && ids.add(i)));
  S.picked = new Set();
  S.datasets.filter(d => ids.has(d.id)).forEach(d => (d.sheets || []).forEach(s => S.picked.add(d.id + '||' + s.name)));
  S.outSet = new Set(names); S.outFields = names.slice();
  // 注意：这里不要清空 S.mapOverride —— 之前手动调好的字段映射要保留（会自动套用）
  S.mode = 'union'; switchMode('union');
  nav('build');
  renderBuild();
  toast(`已预选 ${ids.size} 个文件、${names.length} 个字段（纵向合并），点「生成汇总表」即可`, 'ok');
};

/* 横向：用勾选的第一个字段当关联键，跳到横向关联 */
/* ★ 删除所选字段：同步删除原始表里对应的列（信息表为行），原文件自动备份 */
$('#fd-del').onclick = async () => {
  const picked = $$('.fd-check').filter(c => c.checked);
  if (!picked.length) return toast('先勾选要删除的字段', 'err');
  const names = picked.map(c => c.dataset.name);
  if (!confirm('确定删除所选 ' + names.length + ' 个字段吗？\n\n会同步删除原始表里对应的列（信息表为对应行），原文件会自动备份。')) return;
  try {
    const r = await api('/api/fields/delete', { keys: names });
    toast('已删除 ' + (r.deleted || []).length + ' 个字段，涉及 ' + (r.touched || []).length + ' 个工作表', 'ok');
    if (r.errors && r.errors.length) toast('部分工作表未处理：' + r.errors.join('；'), 'err');
    await loadAll();
    renderFields();
    if (typeof renderAnDatasets === 'function') renderAnDatasets();
    loadResultPicker();
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
};

$('#fd-join').onclick = () => {
  const picked = $$('.fd-check').filter(c => c.checked);
  if (!picked.length) return toast('先勾选一个要当「关联键」的字段', 'err');
  const key = picked[0].dataset.name;
  const ids = (picked[0].dataset.ids || '').split(',').filter(Boolean);

  nav('build');
  S.mode = 'join'; switchMode('join');

  // 把主表设成"包含这个字段的第一个文件"
  const ds = S.datasets.find(d => d.id === ids[0]) || S.datasets[0];
  if (ds) {
    const dSel = $('#j-base-ds');
    if (![...dSel.options].some(o => o.value === ds.id)) {
      dSel.insertAdjacentHTML('beforeend', `<option value="${esc(ds.id)}">${esc(ds.name)}</option>`);
    }
    dSel.value = ds.id;
    const sh = (ds.sheets || []).find(s => (s.columns || []).includes(key)) || (ds.sheets || [])[0];
    if (sh) {
      const sSel = $('#j-base-sheet');
      if (![...sSel.options].some(o => o.value === sh.name)) {
        sSel.insertAdjacentHTML('beforeend', `<option value="${esc(sh.name)}">${esc(sh.name)}</option>`);
      }
      sSel.value = sh.name;
    }
  }

  // 至少要有一张关联表，否则横向关联没意义
  if (!S.joinList.length) {
    const other = S.datasets.find(x => x.id !== (ds && ds.id)) || S.datasets[1] || S.datasets[0];
    if (other) {
      S.joinList.push({
        dataset_id: other.id,
        sheet: (other.sheets[0] || {}).name || '',
        on: key,
        key: key,
      });
    }
  }

  renderJoin();     // 注意：它会重建下拉框，所以关联键必须在它之后再设

  const on = $('#j-base-on');
  if (on) {
    if (![...on.options].some(o => o.value === key)) {
      on.insertAdjacentHTML('beforeend', `<option value="${esc(key)}">${esc(key)}</option>`);
    }
    on.value = key;
  }
  toast(`关联键已设为「${key}」。请在下方关联表里选对应字段，再点「生成关联表」`, 'ok');
};

/* ---------------- 汇总提取 · 选择列表 ---------------- */
function pickedSheets() {
  const out = [];
  S.datasets.forEach(d => (d.sheets || []).forEach(s => {
    if (S.picked.has(d.id + '||' + s.name)) out.push({ ds: d, sh: s, key: d.id + '||' + s.name });
  }));
  return out;
}
function renderPickList() {
  const kw = $('#src-filter').value.trim().toLowerCase();
  let dsList = S.datasets;
  if (kw) dsList = dsList.filter(d => ((d.name || '') + (d.lab || '') + (d.table_type || '')).toLowerCase().includes(kw));
  if (!S.datasets.length) { $('#picklist').innerHTML = '<div class="empty sm">还没有数据表，先去「数据源管理」导入</div>'; return; }
  if (!dsList.length) { $('#picklist').innerHTML = '<div class="empty sm">没有匹配的数据表</div>'; return; }
  $('#picklist').innerHTML = dsList.map(d => {
    const keys = (d.sheets || []).map(s => d.id + '||' + s.name);
    const allOn = keys.length && keys.every(k => S.picked.has(k));
    const someOn = !allOn && keys.some(k => S.picked.has(k));
    const fold = S.fold.has(d.id);          // 该文件是否折叠
    return `<div class="pickgroup${fold ? ' folded' : ''}" data-ds="${d.id}">
      <div class="pick ghead">
        <span class="caret" data-fold="${d.id}" title="${fold ? '展开这个文件的全部工作表' : '折叠这个文件'}">${fold ? '▸' : '▾'}</span>
        <input type="checkbox" class="dg-check" data-id="${d.id}" ${allOn ? 'checked' : ''} style="margin-top:3px">
        <div class="pi">
          <div class="pn">${esc(d.name)}${someOn ? ' <span class="tag o">部分选中</span>' : ''}</div>
          <div class="ps">
            <span class="tag">${esc(d.lab || '未标注')}</span>
            <span class="tag o">${esc(d.table_type || '未标注')}</span>
            ${(d.sheets || []).length} 个工作表 · ${d.total_rows} 行
          </div>
        </div>
      </div>
      <div class="gsheets"${fold ? ' style="display:none"' : ''}>
      ${(d.sheets || []).map(s => {
      const k = d.id + '||' + s.name;
      const on = S.picked.has(k);
      return `<div class="pick ${on ? 'on' : ''}" data-key="${esc(k)}">
          <input type="checkbox" class="sh-check" data-key="${esc(k)}" ${on ? 'checked' : ''} style="margin-top:3px">
          <div class="pi">
            <div class="pn" style="font-weight:400">工作表：${esc(s.name)} <span class="tip">· ${s.rows} 行 · ${(s.columns || []).length} 字段</span>${s.kind === 'info' ? ' <span class="tag o">信息表</span>' : ''}${pickSameTag(s)}</div>
            <div class="pc">${(s.columns || []).map(c => dispName(c)).join(' / ')}</div>
          </div>
        </div>`;
    }).join('')}
      </div>
    </div>`;
  }).join('');
}
$('#src-filter').oninput = renderPickList;
$('#picklist').addEventListener('click', e => {
  // 折叠箭头
  const fd = e.target.closest('.caret');
  if (fd) {
    const id = fd.dataset.fold;
    S.fold.has(id) ? S.fold.delete(id) : S.fold.add(id);
    renderPickList(); return;
  }
  const c = e.target.closest('.sh-check');
  if (c) { togglePick(c.dataset.key); return; }
  const g = e.target.closest('.dg-check');
  if (g) {
    const d = S.datasets.find(x => x.id === g.dataset.id);
    const keys = (d.sheets || []).map(s => d.id + '||' + s.name);
    const all = keys.every(k => S.picked.has(k));
    keys.forEach(k => all ? S.picked.delete(k) : S.picked.add(k));
    afterPickChange(); return;
  }
  const row = e.target.closest('.pick[data-key]');
  if (row) { togglePick(row.dataset.key); return; }
});
function togglePick(k) { S.picked.has(k) ? S.picked.delete(k) : S.picked.add(k); afterPickChange(); }
function afterPickChange() { renderPickList(); renderFieldList(); renderMapping(); }

/* 当前筛选条件下"看得见"的数据表 —— 全选/全不选/折叠只作用于这些，
   免得把被搜索过滤掉、用户看不见的表也一起改了 */
function visibleDatasets() {
  const kw = $('#src-filter').value.trim().toLowerCase();
  if (!kw) return S.datasets;
  return S.datasets.filter(d => ((d.name || '') + (d.lab || '') + (d.table_type || '')).toLowerCase().includes(kw));
}
$('#src-pick-all').onclick = () => {
  const ds = visibleDatasets();
  if (!ds.length) return toast('没有可勾选的数据表', 'err');
  let n = 0;
  ds.forEach(d => (d.sheets || []).forEach(s => { const k = d.id + '||' + s.name; if (!S.picked.has(k)) { S.picked.add(k); n++; } }));
  afterPickChange();
  toast(n ? `已全选 ${ds.length} 个文件的全部工作表` : '已经是全选状态了', n ? 'ok' : 'err');
};
$('#src-pick-none').onclick = () => {
  const ds = visibleDatasets();
  let n = 0;
  ds.forEach(d => (d.sheets || []).forEach(s => { const k = d.id + '||' + s.name; if (S.picked.has(k)) { S.picked.delete(k); n++; } }));
  afterPickChange();
  toast(n ? `已取消 ${n} 个工作表的勾选` : '本来就没勾选', n ? 'ok' : 'err');
};
$('#src-fold-all').onclick = () => {
  const ds = visibleDatasets();
  if (!ds.length) return toast('没有数据表', 'err');
  ds.forEach(d => S.fold.add(d.id));
  renderPickList();
};
$('#src-unfold-all').onclick = () => {
  const ds = visibleDatasets();
  if (!ds.length) return toast('没有数据表', 'err');
  ds.forEach(d => S.fold.delete(d.id));
  renderPickList();
};

/* 「仅选中表头一致的表」+ 基准组选择器
   ─────────────────────────────────────────────────────────
   判定口径：**只看表头（列名），完全不看单元格里的内容**。
   表头比较（与「字段映射」的匹配一致）：
     · 列名归一化：全角转半角、去空格、去标点、转小写
     · 列顺序不影响判定（A,B,C 与 C,A,B 一致）
     · **允许子集**：这张表的列名只要能被基准组全覆盖就算一致，缺的列汇总时留空

   基准组由**用户显式指定**（点按钮后弹窗里挑），不再自动投票 ——
   因为"同模板派生的兄弟表"（室间质评填报 8 列 / 正确度验证 7 列）互为超集，
   自动选会让结果随勾选顺序漂移、无法预期。

   作用范围两种：
     · 'all'  全库 —— 扫全库把符合基准的挑出来（适合"要找齐所有同类表"）
     · 'pick' 只在已勾选里收缩，不新增表（适合"已勾了一堆，帮我清掉不匹配的"）

   结果分三档：完全一致 / 缺列子集 / 表头不符。 */
function headerKeyOf(sh) {
  if (!sh || sh.kind === 'info') return '';
  const cols = (sh.columns || []).map(norm).filter(Boolean);
  if (!cols.length) return '';
  return cols.slice().sort().join('|');
}
function headerColsOf(sh) {
  return (sh.columns || []).filter(c => norm(c));
}
function headerSetOf(sh) {
  return new Set((sh.columns || []).map(norm).filter(Boolean));
}
/* 这张表的表头是否被基准"包容"（子集或完全相同） */
function headerFits(sh, baseSet) {
  const mine = headerSetOf(sh);
  if (!mine.size) return false;
  for (const c of mine) if (!baseSet.has(c)) return false;
  return true;
}
/* 用户在弹窗里显式选中的基准表 → 列名并集 */
function sameSelSet() {
  if (!S.sameSel || !S.sameSel.size) return null;
  const set = new Set();
  S.datasets.forEach(d => (d.sheets || []).forEach(s => {
    if (S.sameSel.has(d.id + '||' + s.name)) headerSetOf(s).forEach(c => set.add(c));
  }));
  return set.size ? set : null;
}

/* ---------------- 「查看已勾选」弹窗 ----------------
   用途：汇总前把**所有已勾选的工作表**汇总到一处复核，
   并可直接取消勾选（立即生效、同步主界面），不必在长列表里翻找。
   与 #psdlg（选基准）职责不同：这里只改 S.picked，不碰 S.sameSel。 */
function showPickedDialog() {
  const list = pickedSheets();
  if (!S.datasets.length) return toast('还没有数据表，先去「数据源管理」导入', 'err');
  if (!list.length) return toast('当前还没有勾选任何工作表', 'err');
  // 快照：本次弹窗里要显示的清单。取消勾选后**不从清单里移除**，
  // 只把勾选框变成未选中，方便对照着重新勾回来。
  S.pkKeep = list.map(x => ({ ds: x.ds, sh: x.sh, key: x.key }));
  S.pkDrop = new Set();          // 本次被取消的（仅用于"撤销取消"）
  $('#pk-filter').value = '';
  renderPickedDialog();
  $('#pkdlg').classList.add('show');
}

function renderPickedDialog() {
  const kw = ($('#pk-filter').value || '').trim().toLowerCase();
  const baseSet = sameSelSet();
  // ★ 以快照为数据源：即使某张表已被取消勾选，它这一行也保留在列表里
  const base = (S.pkKeep && S.pkKeep.length) ? S.pkKeep : pickedSheets();
  let list = base;
  if (kw) list = list.filter(x =>
    ((x.sh.name || '') + (x.ds.name || '') + (x.ds.lab || '') + (x.ds.table_type || ''))
      .toLowerCase().includes(kw));

  const total = S.picked.size;                       // 当前真实已勾选数
  const shownOn = list.filter(x => S.picked.has(x.key)).length;  // 本窗口里还勾着的
  const shownOff = list.length - shownOn;            // 本窗口里被取消的
  const dsCount = new Set(list.filter(x => S.picked.has(x.key)).map(x => x.ds.id)).size;
  const infoOnly = list.filter(x => x.sh.kind === 'info' || !headerKeyOf(x.sh)).length;
  $('#pk-stats').innerHTML =
    `<span class="tag tagok">已勾选 ${total} 张</span>` +
    (shownOff ? `<span class="tag tagno">本次已取消 ${shownOff} 张</span>` : '') +
    `<span class="tip">· 勾选的分布在 ${dsCount} 个文件里` +
    (kw ? ` · 当前筛选出 ${list.length} 张` : '') +
    (infoOnly ? ` · 其中 ${infoOnly} 张是信息表/无表头` : '') + '</span>' +
    (baseSet ? '' : '<span class="tip">· 还没设过基准表，所以这里不显示表头标记 —— 想核对表头是否一致，可用上方「仅选中表头一致的表」先设一次基准</span>');

  if (!list.length) {
    $('#pk-list').innerHTML = `<div class="empty sm">${kw ? '没有匹配的工作表' : '当前没有勾选任何工作表'}</div>`;
    return;
  }

  // 按文件分组
  const byDs = {};
  list.forEach(x => (byDs[x.ds.id] = byDs[x.ds.id] || { ds: x.ds, arr: [] }).arr.push(x));
  $('#pk-list').innerHTML = Object.values(byDs).map(g => {
    const onN = g.arr.filter(x => S.picked.has(x.key)).length;
    const rows = g.arr.map(x => {
      const cols = (x.sh.columns || []).filter(c => norm(c));
      const on = S.picked.has(x.key);
      let badge = '';
      if (x.sh.kind === 'info') badge = '<span class="sigtag none">信息表</span>';
      else if (!baseSet) badge = '';
      else if (headerSetOf(x.sh).size === baseSet.size && headerFits(x.sh, baseSet))
        badge = '<span class="sigtag ok">表头一致</span>';
      else if (headerFits(x.sh, baseSet)) {
        const lack = [...baseSet].filter(c => !headerSetOf(x.sh).has(c));
        badge = `<span class="sigtag warn" title="缺少：${esc(lack.join('、'))}">缺 ${lack.length} 列</span>`;
      } else badge = '<span class="sigtag no" title="列名超出了基准组的范围">超出基准</span>';
      return `<div class="pkrow${on ? '' : ' isoff'}" data-key="${esc(x.key)}">
        <input type="checkbox" class="pkck" data-key="${esc(x.key)}" ${on ? 'checked' : ''}>
        <div class="pi">
          <div class="pn" style="font-weight:400">工作表：${esc(x.sh.name)} <span class="tip">· ${x.sh.rows} 行 · ${cols.length} 列</span>${badge}${on ? '' : '<span class="sigtag no">已取消</span>'}</div>
          <div class="pc">${cols.map(c => dispName(c)).join(' / ')}</div>
        </div>
      </div>`;
    }).join('');
    return `<div class="psgrp">
      <div class="psgh">${esc(g.ds.name)} <span class="tip">· ${esc(g.ds.lab || '未标注')} · 已勾选 ${onN}/${g.arr.length} 张</span></div>
      ${rows}
    </div>`;
  }).join('');
}

$('#src-picked-view').onclick = () => showPickedDialog();
$('#pk-filter').oninput = renderPickedDialog;
$('#pk-x').onclick = () => $('#pkdlg').classList.remove('show');
$('#pk-ok').onclick = () => $('#pkdlg').classList.remove('show');
$('#pk-check-all').onclick = () => {
  // 把清单里的（或筛选出来的）重新勾上
  const kw = ($('#pk-filter').value || '').trim().toLowerCase();
  let n = 0;
  const pool = (S.pkKeep && S.pkKeep.length) ? S.pkKeep : pickedSheets();
  pool.forEach(x => {
    if (kw && !((x.sh.name || '') + (x.ds.name || '')).toLowerCase().includes(kw)) return;
    if (!S.picked.has(x.key)) { S.picked.add(x.key); n++; }
  });
  afterPickChange(); renderPickedDialog();
  toast(n ? `已恢复勾选 ${n} 张` : '这些都已经勾选了', n ? 'ok' : 'err');
};

// 弹窗内点击：勾选框取消/恢复勾选；点行也能切换。★ 改完重渲染，行保留不消失
document.addEventListener('click', e => {
  const dlg = $('#pkdlg');
  if (!dlg || !dlg.classList.contains('show')) return;
  if (e.target.closest('#pk-ok') || e.target.closest('#pk-x') || e.target.closest('#pk-check-all')) return;
  if (e.target.closest('#pk-filter')) return;
  const row = e.target.closest('.pkrow');
  if (!row) return;
  const k = row.dataset.key;
  S.picked.has(k) ? S.picked.delete(k) : S.picked.add(k);
  afterPickChange();
  renderPickedDialog();
});

$('#src-pick-same').onclick = () => showPickSameDialog();

function showPickSameDialog() {
  const all = [];
  S.datasets.forEach(d => (d.sheets || []).forEach(s => {
    const k = d.id + '||' + s.name;
    if (!headerKeyOf(s)) return;
    all.push({ ds: d, sh: s, key: k, picked: S.picked.has(k) });
  }));
  if (!all.length) return toast('还没有可用的数据表，先去「数据源管理」导入', 'err');
  window._psAll = all;
  S.sameSel = S.sameSel || new Set();
  // 首次打开：以当前勾选里"列名最全的那张"作为起点（没勾选就用全库最全的）
  if (!S.sameSel.size) {
    const inPick = all.filter(x => x.picked);
    const pool = (inPick.length ? inPick : all).slice();
    pool.sort((a, b) => headerColsOf(b.sh).length - headerColsOf(a.sh).length);
    if (pool[0]) S.sameSel.add(pool[0].key);
  }
  if (!S.sameScope) S.sameScope = 'all';
  // 打开时同步「作用范围」按钮的选中态（否则首次打开两个按钮都是灰的）
  [...$('#psdlg').querySelectorAll('.ps-scope')].forEach(b =>
    b.classList.toggle('on', b.dataset.scope === S.sameScope));
  renderPickSameDialog();
  $('#psdlg').classList.add('show');
}

function renderPickSameDialog() {
  const all = window._psAll || [];
  const sel = S.sameSel || new Set();
  const baseSet = sameSelSet();
  const scope = S.sameScope || 'all';
  const inScope = scope === 'pick' ? all.filter(x => x.picked) : all;

  const selList = all.filter(x => sel.has(x.key));
  $('#ps-base').innerHTML = selList.length
    ? selList.map(x => `<span class="pschip" data-unsel="${esc(x.key)}" title="点一下把它移出基准组">${esc(x.sh.name)} <b>${headerColsOf(x.sh).length} 列</b><i>×</i></span>`).join('')
    : '<span class="tip">还没选基准表 —— 在下面点任意一张表，它就成为基准</span>';

  const ok = [], sub = [], diff = [];
  inScope.forEach(x => {
    if (!baseSet || !headerFits(x.sh, baseSet)) { diff.push(x); return; }
    if (headerSetOf(x.sh).size === baseSet.size) ok.push(x); else sub.push(x);
  });

  $('#ps-stats').innerHTML = baseSet
    ? `<span class="tag tagok">完全一致 ${ok.length}</span><span class="tag tagwarn">缺列子集 ${sub.length}</span><span class="tag tagno">表头不符 ${diff.length}</span><span class="tip">· 范围：${scope === 'all' ? '全库' : '仅已勾选'}，共 ${inScope.length} 张</span>`
    : `<span class="tip">选定基准后这里显示匹配情况（当前范围共 ${inScope.length} 张）</span>`;

  const byDs = {};
  [...ok, ...sub, ...diff].forEach(x => (byDs[x.ds.name] = byDs[x.ds.name] || []).push(x));
  $('#ps-list').innerHTML = Object.keys(byDs).length ? Object.entries(byDs).map(([nm, arr]) => {
    const rows = arr.map(x => {
      const cols = headerColsOf(x.sh);
      const isSel = sel.has(x.key);
      let cls = 'psrow', badge = '';
      if (!baseSet) badge = '<span class="sigtag none">待定</span>';
      else if (ok.includes(x)) { cls += ' isok'; badge = '<span class="sigtag ok">表头一致</span>'; }
      else if (sub.includes(x)) {
        cls += ' issub';
        const lack = [...baseSet].filter(c => !headerSetOf(x.sh).has(c));
        badge = `<span class="sigtag warn" title="缺少：${esc(lack.join('、'))}">缺 ${lack.length} 列</span>`;
      } else badge = '<span class="sigtag no" title="列名超出了基准组的范围">表头不符</span>';
      return `<div class="${cls}${isSel ? ' basesel' : ''}" data-key="${esc(x.key)}">
        <input type="checkbox" class="psck" data-key="${esc(x.key)}" ${isSel ? 'checked' : ''}>
        <div class="pi">
          <div class="pn" style="font-weight:400">${esc(x.sh.name)} <span class="tip">· ${x.sh.rows} 行 · ${cols.length} 列</span>${badge}${x.picked ? '<span class="tag o">已勾选</span>' : ''}</div>
          <div class="pc">${cols.map(c => dispName(c)).join(' / ')}</div>
        </div></div>`;
    }).join('');
    return `<div class="psgrp"><div class="psgh">${esc(nm)}</div>${rows}</div>`;
  }).join('') : '<div class="empty sm">这个范围里没有表，换个作用范围试试</div>';

}


document.addEventListener('click', e => {
  const dlg = $('#psdlg');
  if (!dlg || !dlg.classList.contains('show')) return;
  if (e.target.closest('#ps-cancel') || e.target.closest('#ps-apply') || e.target.closest('#ps-clear')) return;
  const ck = e.target.closest('.psck');
  if (ck) {
    ck.checked ? S.sameSel.add(ck.dataset.key) : S.sameSel.delete(ck.dataset.key);
    renderPickSameDialog(); return;
  }
  const chip = e.target.closest('.pschip');
  if (chip) { S.sameSel.delete(chip.dataset.unsel); renderPickSameDialog(); return; }
  const sc = e.target.closest('.ps-scope');
  if (sc) {
    S.sameScope = sc.dataset.scope;
    [...dlg.querySelectorAll('.ps-scope')].forEach(b => b.classList.toggle('on', b.dataset.scope === S.sameScope));
    renderPickSameDialog(); return;
  }
  const row = e.target.closest('.psrow');
  if (row) { S.sameSel = new Set([row.dataset.key]); renderPickSameDialog(); }
});
$('#ps-clear').onclick = () => { S.sameSel = new Set(); renderPickSameDialog(); };
$('#ps-cancel').onclick = () => $('#psdlg').classList.remove('show');
$('#ps-x').onclick = () => $('#psdlg').classList.remove('show');
$('#ps-apply').onclick = () => {
  const baseSet = sameSelSet();
  if (!baseSet) return toast('请先在列表里点一张表作为基准', 'err');
  const scope = S.sameScope || 'all';
  const all = window._psAll || [];
  const pool = scope === 'pick' ? all.filter(x => x.picked) : all;

  let added = 0, sub = 0;
  pool.forEach(x => {
    if (headerFits(x.sh, baseSet)) {
      if (!S.picked.has(x.key)) { S.picked.add(x.key); added++; }
      if (headerSetOf(x.sh).size < baseSet.size) sub++;
    }
  });
  let removed = 0;
  if (scope === 'all') {
    S.datasets.forEach(d => (d.sheets || []).forEach(s => {
      const k = d.id + '||' + s.name;
      if (!headerKeyOf(s)) return;
      if (!headerFits(s, baseSet) && S.picked.has(k)) { S.picked.delete(k); removed++; }
    }));
  }
  $('#psdlg').classList.remove('show');
  afterPickChange();
  toast(`已按 ${baseSet.size} 列的基准对齐：新增 ${added} 张，取消 ${removed} 张不符${sub ? `，含 ${sub} 张缺列子集` : ''}`, 'ok');
};
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#psdlg')) $('#psdlg').classList.remove('show');
  if (e.key === 'Escape' && $('#pkdlg')) $('#pkdlg').classList.remove('show');
});

/* 列表里的标记：以用户当前选定的基准为准 */
function pickSameTag(sh) {
  const baseSet = sameSelSet();
  if (!baseSet) return '';
  const g = headerKeyOf(sh);
  if (!g || !headerFits(sh, baseSet)) {
    return '<span class="sigtag no" title="列名超出基准组的范围（只比对列名，不看单元格内容）">超出基准</span>';
  }
  const mine = headerSetOf(sh);
  const lack = [...baseSet].filter(c => !mine.has(c));
  if (!lack.length) {
    return '<span class="sigtag ok" title="表头列名与基准组完全相同（只比对列名，不看单元格内容）">表头一致</span>';
  }
  return `<span class="sigtag warn" title="是基准组的子集，缺少 ${lack.length} 个列名：${esc(lack.join('、'))}（汇总时留空）">缺 ${lack.length} 列</span>`;
}

/* ---------------- 输出字段 ---------------- */
function candidateFields() {
  const map = {};   // normKey -> {name, count, cols:Set}
  pickedSheets().forEach(p => {
    const cols = p.sh.kind === 'info' ? ['项目', '内容'] : (p.sh.columns || []);
    cols.forEach(c => {
      const k = norm(c); if (!k) return;
      const e = map[k] = map[k] || { key: k, names: {}, count: 0 };
      e.names[c] = (e.names[c] || 0) + 1; e.count++;
    });
  });
  return Object.values(map).map(e => ({
    key: e.key,
    name: Object.entries(e.names).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0],
    count: e.count,
  })).sort((a, b) => b.count - a.count);
}
function renderFieldList() {
  const kw = $('#field-filter').value.trim().toLowerCase();
  let cand = candidateFields();
  const total = pickedSheets().length;
  if (kw) cand = cand.filter(c => c.name.toLowerCase().includes(kw));
  if (!total) { $('#fieldlist').innerHTML = '<div class="empty sm">先在上一步勾选数据表</div>'; fillSortOptions(); return; }
  if (!cand.length) { $('#fieldlist').innerHTML = '<div class="empty sm">选中的表没有字段</div>'; fillSortOptions(); return; }
  $('#fieldlist').innerHTML = cand.map(c => {
    const on = S.outSet.has(c.name);
    return `<div class="pick ${on ? 'on' : ''}" data-field="${esc(c.name)}">
      <input type="checkbox" class="of-check" data-name="${esc(c.name)}" ${on ? 'checked' : ''} style="margin-top:3px">
      <div class="pi"><div class="pn">${esc(c.name)}</div>
      <div class="ps">出现在 ${c.count}/${total} 张选中的表</div></div></div>`;
  }).join('');
  fillSortOptions();
}
$('#field-filter').oninput = renderFieldList;
function toggleField(nm, on) {
  if (on) { if (!S.outSet.has(nm)) { S.outSet.add(nm); S.outFields.push(nm); } }
  else { S.outSet.delete(nm); S.outFields = S.outFields.filter(x => x !== nm); }
  renderFieldList(); renderMapping(); renderFilters();
}
$('#fieldlist').addEventListener('change', e => {
  const ck = e.target.closest('.of-check');
  if (ck) toggleField(ck.dataset.name, ck.checked);
});
$('#fieldlist').addEventListener('click', e => {
  if (e.target.classList.contains('of-check')) return;
  const row = e.target.closest('.pick[data-field]');
  if (!row) return;
  toggleField(row.dataset.field, !S.outSet.has(row.dataset.field));
});
$('#field-all').onclick = () => {
  candidateFields().forEach(c => { if (!S.outSet.has(c.name)) { S.outSet.add(c.name); S.outFields.push(c.name); } });
  renderFieldList(); renderMapping(); renderFilters();
};
$('#field-none').onclick = () => { S.outSet = new Set(); S.outFields = []; renderFieldList(); renderMapping(); renderFilters(); };
function fillSortOptions() {
  const sel = $('#opt-sort-field');
  const cur = sel.value;
  sel.innerHTML = '<option value="">不排序</option>' + S.outFields.map(f => `<option>${esc(f)}</option>`).join('');
  if (S.outFields.includes(cur)) sel.value = cur;
}

/* ---------------- 字段映射 ---------------- */
function renderMapping() {
  const ps = pickedSheets();
  if (!ps.length || !S.outFields.length) {
    $('#mapping').innerHTML = '<div class="tip">勾选数据表和输出字段后，这里会显示自动映射结果。</div>';
    return;
  }
  $('#mapping').innerHTML = '<div class="mapgrid">' + ps.map(p => {
    const ov = S.mapOverride[p.key] || {};
    const ovCount = Object.keys(ov).length;
    const used = new Set();
    const srcCols = p.sh.kind === 'info' ? ['项目', '内容'] : (p.sh.columns || []);
    const rows = srcCols.map(sc => {
      let tgt = ov[sc];
      if (tgt === undefined) {
        let best = '', bs = 0;
        S.outFields.forEach(tc => { if (used.has(tc)) return; const s = sim(sc, tc); if (s > bs) { bs = s; best = tc; } });
        tgt = bs >= 0.86 ? best : '';
        if (tgt) used.add(tgt);
      } else if (tgt) used.add(tgt);
      const selOpts = ['<option value=""' + (tgt ? '' : ' selected') + '>（不要）</option>']
        .concat(S.outFields.map(f => `<option value="${esc(f)}"${f === tgt ? ' selected' : ''}>${esc(f)}</option>`)).join('');
      return `<tr><td>${dispName(sc)}</td><td><select class="map-sel" data-key="${esc(p.key)}" data-src="${esc(sc)}">${selOpts}</select></td></tr>`;
    }).join('');
    const fold = S.mapFold.has(p.key);      // 该表是否折叠
    return `<div class="mapgrp${fold ? ' folded' : ''}">
      <div class="mh">
        <span class="caret" data-fold="${esc(p.key)}" title="${fold ? '展开这张表' : '折叠这张表'}">${fold ? '▸' : '▾'}</span>
        <span class="mhtext">${esc(p.ds.lab ? p.ds.lab + ' / ' : '')}${esc(p.ds.name)} · ${esc(p.sh.name)}</span>
        <span class="mhmeta">${srcCols.length} 列${ovCount ? ` · <b>手动调过 ${ovCount} 列</b>` : ''}</span>
      </div>
      <table${fold ? ' style="display:none"' : ''}>${rows}</table></div>`;
  }).join('') + '</div>';
  $('#mapping').querySelectorAll('.map-sel').forEach(sel => {
    sel.addEventListener('change', () => {
      const k = sel.dataset.key;
      S.mapOverride[k] = S.mapOverride[k] || {};
      S.mapOverride[k][sel.dataset.src] = sel.value;
      saveMapping();
    });
  });
}

/* 字段映射区：单个表折叠 / 全部折叠 / 全部展开（点击箭头即可） */
$('#mapping').addEventListener('click', e => {
  const c = e.target.closest('.caret');
  if (!c) return;
  const k = c.dataset.fold;
  S.mapFold.has(k) ? S.mapFold.delete(k) : S.mapFold.add(k);
  renderMapping();
});
$('#btn-map-fold').onclick = () => {
  pickedSheets().forEach(p => S.mapFold.add(p.key));
  renderMapping();
};
$('#btn-map-unfold').onclick = () => {
  pickedSheets().forEach(p => S.mapFold.delete(p.key));
  renderMapping();
};

/* 查看已记住的映射记录 */
$('#btn-map-view').onclick = () => showMappingRecords();
function showMappingRecords() {
  const mem = S.mapOverride || {};
  const keys = Object.keys(mem).filter(k => mem[k] && Object.keys(mem[k]).length);
  const body = [];
  if (!keys.length) {
    body.push('<div class="empty">还没有记住任何映射。<br>'
      + '在「高级：字段映射」里把某一列的对应关系改一下，就会自动记到这里，下次遇到同样的表自动套用。</div>');
  } else {
    // 按"哪个文件"分组展示，方便核对
    const byDs = {};
    keys.forEach(k => {
      const idx = k.indexOf('||');
      const dsId = idx >= 0 ? k.slice(0, idx) : k;
      const sheet = idx >= 0 ? k.slice(idx + 2) : '';
      (byDs[dsId] = byDs[dsId] || []).push({ sheet: sheet, pairs: Object.entries(mem[k]) });
    });
    body.push(`<div class="tip">共记住 <b>${keys.length}</b> 张表的映射。这些都是「源列 → 输出字段」，
      下次导入结构相同的表会自动套用，不用再调一遍。</div>`);
    Object.entries(byDs).forEach(([dsId, items]) => {
      const d = S.datasets.find(x => x.id === dsId);
      const title = d ? ((d.lab ? d.lab + ' / ' : '') + d.name) : ('（已删除的数据表 ' + dsId + '）');
      body.push(`<div class="mapgrp" style="margin-top:12px">
        <div class="mh"><span class="mhtext">${esc(title)}</span></div>
        <table>${items.map(it => `
          <tr><td colspan="3" style="background:#f6f9fd"><b>${esc(it.sheet || '（工作表已不存在）')}</b></td></tr>
          ${it.pairs.map(([s, t]) => `<tr><td style="width:38%">${dispName(s)}</td>
            <td style="width:24px;text-align:center">→</td>
            <td><b style="color:${t ? 'var(--blue)' : '#b93a3a'}">${t ? dispName(t) : '（不要这一列）'}</b></td></tr>`).join('')}
        `).join('')}</table></div>`);
    });
  }
  $('#modal-title').textContent = '已记住的字段映射记录';
  $('#modal-body').innerHTML = body.join('');
  $('#modal').classList.add('show');
}

/* 字段映射记忆：用户调过就存到本机配置里，下次打开自动恢复 */
$('#btn-map-clear').onclick = () => clearMapping();
let _mapSaveTimer = null;
function saveMapping() {
  clearTimeout(_mapSaveTimer);
  const hint = $('#map-saved');
  if (hint) hint.textContent = '正在记住…';
  _mapSaveTimer = setTimeout(() => {
    api('/api/config', { mapping: S.mapOverride })
      .then(() => { if (hint) hint.textContent = '✓ 映射已记住，下次自动套用'; })
      .catch(e => { if (hint) hint.textContent = '映射保存失败：' + e.message; });
  }, 450);
}
async function clearMapping() {
  S.mapOverride = {};
  try {
    await api('/api/config', { mapping: {} });
    renderMapping(); renderBuild();
    toast('已清除记住的字段映射', 'ok');
    const hint = $('#map-saved'); if (hint) hint.textContent = '';
  } catch (e) {
    toast('清除失败：' + e.message, 'err');
  }
}

/* ---------------- 过滤器 ---------------- */
/* 操作符一律用符号显示；鼠标悬停会给出中文说明（title） */
const FILTER_OPS = [
  ['contains', '⊃', '包含'],
  ['notcontains', '⊅', '不包含'],
  ['eq', '=', '等于'],
  ['ne', '≠', '不等于'],
  ['gt', '>', '大于'],
  ['lt', '<', '小于'],
  ['ge', '≥', '大于等于'],
  ['le', '≤', '小于等于'],
  ['empty', '∅', '为空'],
  ['notempty', '≠∅', '不为空'],
];
const NO_VALUE_OPS = ['empty', 'notempty'];

function renderFilters() {
  const opts = ['<option value="">（选字段）</option>'].concat(S.outFields.map(f => `<option>${esc(f)}</option>`)).join('');
  const opOpts = FILTER_OPS.map(([v, sym, cn]) => `<option value="${v}" title="${cn}">${sym}</option>`).join('');
  $('#filterlist').innerHTML = S.filters.map((f, i) => `
    <div class="frow" data-i="${i}">
      <select class="f-field">${opts}</select>
      <select class="f-op" title="比较方式">${opOpts}</select>
      <input class="f-val" placeholder="值" value="${esc(f.value || '')}" style="min-width:200px">
      <span class="x" title="删除">✕</span>
    </div>`).join('');
  $$('#filterlist .frow').forEach((row, i) => {
    const f = S.filters[i];
    const opEl = row.querySelector('.f-op');
    const valEl = row.querySelector('.f-val');
    row.querySelector('.f-field').value = f.field || '';
    opEl.value = f.op || 'contains';
    // 「为空 / 不为空」不需要填值，把输入框灰掉，避免用户以为漏填
    const syncVal = () => {
      const noVal = NO_VALUE_OPS.includes(f.op);
      valEl.disabled = noVal;
      valEl.placeholder = noVal ? '（不需要填值）' : '值';
      if (noVal) valEl.value = '';
    };
    syncVal();
    row.querySelector('.f-field').onchange = e => { f.field = e.target.value; };
    opEl.onchange = e => { f.op = e.target.value; if (NO_VALUE_OPS.includes(f.op)) f.value = ''; syncVal(); };
    valEl.oninput = e => { f.value = e.target.value; };
    row.querySelector('.x').onclick = () => { S.filters.splice(i, 1); renderFilters(); };
  });
}
$('#add-filter').onclick = () => { S.filters.push({ field: '', op: 'contains', value: '' }); renderFilters(); };

/* ---------------- 生成 ---------------- */
function buildSources() {
  return pickedSheets().map(p => {
    const ov = S.mapOverride[p.key] || {};
    const used = new Set(), mapping = {};
    const srcCols = p.sh.kind === 'info' ? ['项目', '内容'] : (p.sh.columns || []);
    srcCols.forEach(sc => {
      let tgt = ov[sc];
      if (tgt === undefined) {
        let best = '', bs = 0;
        S.outFields.forEach(tc => { if (used.has(tc)) return; const s = sim(sc, tc); if (s > bs) { bs = s; best = tc; } });
        tgt = bs >= 0.86 ? best : '';
      }
      if (tgt && S.outSet.has(tgt)) { mapping[sc] = tgt; used.add(tgt); }
    });
    return { dataset_id: p.ds.id, sheet: p.sh.name, header_rows: p.sh.header_rows || '1',
             kind: p.sh.kind === 'info' ? 'info' : 'table', mapping };
  });
}
async function runUnion() {
  if (!S.picked.size) { toast('请先在第 ① 步勾选要汇总的数据表', 'err'); return; }
  if (!S.outFields.length) { toast('请先在第 ② 步勾选要输出的字段', 'err'); return; }
  const btn = $('#btn-run');
  const tip = $('#run-tip');
  const t0 = Date.now();
  btn.innerHTML = '<span class="spin"></span>正在汇总…'; btn.disabled = true;
  tip.textContent = `正在处理 ${S.picked.size} 张表 × ${S.outFields.length} 个字段…`;
  S.timer = setInterval(() => {
    tip.textContent = '已用时 ' + Math.round((Date.now() - t0) / 1000) + ' 秒，数据量大时请稍候…';
  }, 1000);
  try {
    const r = await api('/api/aggregate', {
      mode: 'union', sources: buildSources(), fields: S.outFields,
      filters: S.filters.filter(f => f.field && f.op), sort: { field: $('#opt-sort-field').value, desc: false },
      add_source_col: $('#opt-src').checked, dedup: $('#opt-dedup').checked,
      limit: parseInt($('#opt-limit').value) || 0,
      title: S.resultTitle || '汇总结果',
    });
    showResult(r, S.outFields);
    tip.textContent = '';
    toast(`汇总完成：${r.total.toLocaleString()} 行，用时 ${r.elapsed}s`, 'ok');
    nav('analysis');   // 生成后直接进入「数据分析」查看结果分析与分析报告
  } catch (e) {
    tip.textContent = '';
    showError(e.message);
    toast('汇总失败，请看页面上的红色提示', 'err');
    console.error('汇总失败', e);
  } finally {
    clearInterval(S.timer);
    btn.innerHTML = '生成汇总表'; btn.disabled = false;
  }
}
// 【必须保留】把「生成汇总表」按钮接到 runUnion 上。
// 这一行曾经丢失，导致点按钮完全没反应（函数定义了但没人调用）。
$('#btn-run').onclick = runUnion;
// 【清除结果】两个入口：生成按钮旁边 + 结果卡片工具栏
$('#btn-run-clear').onclick = () => {
  if (S.lastResult && S.lastResult.id && !confirm('确定清除当前已生成的汇总结果吗？\n\n只清界面上的结果：数据表、勾选、已导出的文件都不受影响。')) return;
  clearResult();
};
$('#btn-res-clear').onclick = () => {
  if (S.lastResult && S.lastResult.id && !confirm('确定清除当前这份汇总结果吗？\n\n只清界面上的结果：数据表、勾选、已导出的文件都不受影响。')) return;
  clearResult();
};

function showError(msg) {
  const box = $('#error-card');
  if (!box) return;
  box.style.display = 'block';
  box.innerHTML = '<b>操作没有完成</b>'
    + '<div class="err-body">' + esc(msg).replace(/\n/g, '<br>') + '</div>'
    + '<button class="btn ghost sm" onclick="document.getElementById(\'error-card\').style.display=\'none\'">知道了，关闭</button>';
  try { box.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
}

/* 【清除结果】清掉已生成的汇总结果（只清界面上的结果，不动数据表、不动勾选、不删导出的文件） */
function clearResult(silent) {
  const has = !!(S.lastResult && S.lastResult.id);
  S.lastResult = null;
  S.page = 0;
  const rc = $('#result-card'); if (rc) rc.style.display = 'none';
  const t = $('#res-table');
  if (t) { const tt = t.querySelector('thead'), tb = t.querySelector('tbody'); if (tt) tt.innerHTML = ''; if (tb) tb.innerHTML = ''; }
  const rd = $('#res-detail'); if (rd) rd.textContent = '';
  const rpn = $('#res-pageinfo'); if (rpn) rpn.textContent = '';
  const rp = $('#res-prev'); if (rp) rp.disabled = true;
  const rn = $('#res-next'); if (rn) rn.disabled = true;
  const ebc = $('#export-card'); if (ebc) ebc.style.display = 'none';
  const eb = $('#error-card'); if (eb) eb.style.display = 'none';
  const rt = $('#run-tip'); if (rt) rt.textContent = '';
  anReset();                   // v2.0.0：结果没了，下面的「结果分析」一起收起来
  rpReset();                   // v2.1.0：报告卡片也一起收起来
  if (!silent) toast(has ? '已清除汇总结果' : '目前没有可清除的结果', has ? 'ok' : 'err');
}
function showResult(r, cols) {
  const eb = $('#error-card'); if (eb) eb.style.display = 'none';
  const xb = $('#export-card'); if (xb) xb.style.display = 'none';
  S.lastResult = { id: r.result_id, cols: r.columns, total: r.total };
  S.page = 0;
  $('#result-card').style.display = 'block';
  $('#res-count').textContent = r.total.toLocaleString();
  let d = '';
  if (r.detail && r.detail.length) {
    d += '<div><b>数据来源（' + r.detail.length + ' 张表）</b></div>';
    d += r.detail.map(x => `　· ${esc(x.lab || '')} ${esc(x.dataset)} / ${esc(x.sheet)} → 取用 ${x.used_rows} 行（原表 ${x.total_rows} 行）`).join('<br>');
  }
  if (r.skipped && r.skipped.length) {
    d += `<div style="color:#b93a3a;margin-top:6px"><b>被跳过的表：</b>${r.skipped.map(s => esc(s.dataset) + '（' + esc(s.reason) + '）').join('；')}</div>`;
  }
  if (r.total === 0) {
    d += '<div style="color:#b93a3a;margin-top:6px"><b>结果是 0 行</b>，常见原因：'
      + '① 筛选条件把数据全过滤掉了；'
      + '② 勾选的字段和这些表对不上（可展开下面的「字段映射」看看每一列有没有配上）；'
      + '③ 选错了工作表（比如选到了说明页而不是填报表）。</div>';
  }
  $('#res-detail').innerHTML = d;
  renderResultPage();
  anStart(r.columns);          // v2.0.0：同时准备「结果分析」（数据概览 + 数据透视）
  rpStart(r.result_id, r.columns);   // v2.1.0：同时准备「分析报告」（模板清单 + 可用性）
  tatFill(r.columns);          // TAT 分析：预选时间列
  loadResultPicker(r.result_id);     // 同步历史结果下拉，保证分析/报告可独立切换
  $('#result-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
async function renderResultPage() {
  if (!S.lastResult) return;
  const r = await api(`/api/result?id=${S.lastResult.id}&offset=${S.page * S.pageSize}&size=${S.pageSize}`);
  $('#res-table').outerHTML = tableHTML(r.columns, r.rows, 'res-table');
  const pages = Math.max(1, Math.ceil(r.total / S.pageSize));
  $('#res-pageinfo').textContent = `第 ${S.page + 1} / ${pages} 页 · 共 ${r.total.toLocaleString()} 行`;
  $('#res-prev').disabled = S.page === 0;
  $('#res-next').disabled = S.page >= pages - 1;
}
// 每页行数：可以调大一点，直接在预览里看更多数据
$('#res-size').onchange = () => {
  S.pageSize = parseInt($('#res-size').value) || 500;
  S.page = 0;
  renderResultPage().catch(e => showError('翻页失败：' + e.message));
};
$('#res-prev').onclick = () => { if (S.page > 0) { S.page--; renderResultPage(); } };
$('#res-next').onclick = () => { S.page++; renderResultPage(); };
function tableHTML(cols, rows, id, emptyText) {
  const tag = `<table class="grid"${id ? ' id="' + id + '"' : ''}>`;
  const head = '<thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
  if (!rows || !rows.length) return `${tag}${head}<tbody></tbody></table><div class="empty">${emptyText || '没有数据'}</div>`;
  const body = rows.map(r => '<tr>' + cols.map((c, i) => {
    const v = r[i] == null ? '' : r[i];
    const t = String(v);
    return `<td class="ell" title="${esc(t).slice(0, 300)}">${esc(t.length > 200 ? t.slice(0, 200) + '…' : t)}</td>`;
  }).join('') + '</tr>').join('');
  return `${tag}${head}<tbody>${body}</tbody></table>`;
}
$('#btn-export-xlsx').onclick = () => doExport('xlsx');
$('#btn-export-csv').onclick = () => doExport('csv');
async function doExport(fmt) {
  if (!S.lastResult) {
    toast('请先点上面的「生成汇总表」，有了结果才能导出', 'err');
    showError('还没有可导出的汇总结果。\n\n请先回到第 ① 步勾选数据表、第 ② 步勾选字段，点「生成汇总表」；出结果之后，结果卡片右上角就有「导出 Excel」了。');
    return;
  }
  const b = fmt === 'csv' ? $('#btn-export-csv') : $('#btn-export-xlsx');
  const old = b.innerHTML;
  b.innerHTML = '<span class="spin"></span>导出中…';
  b.disabled = true;
  try {
    const r = await api('/api/export', {
      result_id: S.lastResult.id, format: fmt, name: '智慧实验室数据汇总',
    });
    // 让浏览器真正“下载”一份，用户不用去翻目录
    try {
      const a = document.createElement('a');
      a.href = '/api/download?name=' + encodeURIComponent(r.file);
      a.download = r.file;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 1500);
    } catch (e) { console.error('触发下载失败', e); }
    showExportDone(r, fmt);
    toast('导出成功：' + r.file, 'ok');
  } catch (e) {
    showError('导出失败：' + e.message);
    console.error('导出失败', e);
  } finally {
    b.innerHTML = old; b.disabled = false;
  }
}


function showExportDone(r, fmt) {
  const box = $('#export-card');
  if (!box) { return; }
  box.style.display = 'block';
  box.innerHTML =
    '<div class="exp-head"><b>✓ 导出成功</b>'
    + '<span class="tip">' + (fmt === 'csv' ? 'CSV' : 'Excel') + ' · ' + fmtSize(r.size) + '</span></div>'
    + '<div class="exp-name">' + esc(r.file) + '</div>'
    + '<div class="exp-tip">浏览器已经开始下载这一份。同时程序在下面这个目录里也存了一份备份：</div>'
    + '<div class="exp-path">' + esc(r.path) + '</div>'
    + '<div class="exp-btns">'
    + '<a class="btn small primary" href="/api/download?name=' + encodeURIComponent(r.file) + '" download="' + esc(r.file) + '">再下载一次</a>'
    + '<button class="btn small" id="btn-exp-folder">打开所在文件夹</button>'
    + '<button class="btn ghost sm" id="btn-exp-close">关闭</button>'
    + '</div>';
  const fb = $('#btn-exp-folder');
  if (fb) fb.onclick = async () => {
    try { await api('/api/open_folder', { path: r.path }); toast('已打开文件夹', 'ok'); }
    catch (e) { toast('打不开文件夹，请手动复制上面的路径：' + e.message, 'err'); }
  };
  const cb = $('#btn-exp-close');
  if (cb) cb.onclick = () => { box.style.display = 'none'; };
  try { box.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
}
$('#btn-open-out').onclick = () => api('/api/open_folder', { path: S.outDir });
$('#btn-open-out2').onclick = () => api('/api/open_folder', { path: S.outDir });
$('#btn-open-lib').onclick = () => api('/api/open_folder', { path: S.baseDir });

/* ==========================================================================
   v2.0.0 「结果分析」—— 数据概览 + 数据透视
   --------------------------------------------------------------------------
   汇总/关联生成结果后，出现在结果卡片下方。
   ★ 计算全在后端：结果上限 30 万行，整表丢给浏览器做透视会直接卡死页面。
     这里只把算好的前若干行画出来，所以 1000 行和 30 万行用起来一样快。
   ========================================================================== */

const PV_AGGS = [
  ['count', '计数（有几行）'],
  ['nunique', '去重计数（几个不同值）'],
  ['sum', '求和'],
  ['avg', '平均'],
  ['max', '最大值'],
  ['min', '最小值'],
];

function anBlank() {
  return { id: null, cols: [], dims: [], pvKey: false, pvBody: null };
}

/* 清空并收起整个分析区（「清除结果」时调用） */
/* ★ 独立使用：结果分析 / 分析报告 不必先跑汇总，可直接选历史结果（含重启前生成的） */
async function loadResultPicker(preferId) {
  const sel = $('#an-pick'); if (!sel) return;
  try {
    const r = await api('/api/results');
    const list = r.results || [];
    const cur = preferId || (S.lastResult && S.lastResult.id) || (list[0] && list[0].id) || '';
    sel.innerHTML = list.length
      ? list.map(x => `<option value="${esc(x.id)}">${esc(x.title)} · ${Number(x.rows || 0).toLocaleString()} 行 · ${new Date((x.at || 0) * 1000).toLocaleString('zh-CN')}</option>`).join('')
      : '<option value="">（暂无历史结果）</option>';
    sel.value = cur;
    sel.disabled = !list.length;
    if (!S.lastResult && list.length) await applyPickedResult(cur);
  } catch (e) { /* 静默：拿不到清单就保持现状 */ }
}
async function applyPickedResult(rid) {
  if (!rid) return;
  try {
    const r = await api(`/api/result?id=${encodeURIComponent(rid)}&offset=0&size=1`);
    showResult({ result_id: rid, columns: r.columns, total: r.total, detail: [] }, r.columns);
    const sel = $('#an-pick'); if (sel) sel.value = rid;
  } catch (e) { toast('读取该结果失败：' + (e && e.message ? e.message : e), 'err'); }
}

/* ★ 独立「数据分析」页：勾选数据表供汇总提取使用；TAT / 设备负载常驻 */
if (!S.anDs) S.anDs = new Set();      // 勾选状态跨渲染 / 切页保留；默认不勾选
function anDsVisible() {
  const kw = ($('#an-ds-kw') ? $('#an-ds-kw').value : '').trim().toLowerCase();
  let list = S.datasets || [];
  if (kw) list = list.filter(d => ((d.name || '') + (d.lab || '') + (d.table_type || '')).toLowerCase().includes(kw));
  return list;
}
function renderAnDatasets() {
  const box = $('#an-ds-list'); if (!box) return;
  if (!S.datasets.length) { box.innerHTML = '<div class="empty sm">还没有数据表，可先在上方导入</div>'; return; }
  const list = anDsVisible();
  if (!list.length) { box.innerHTML = '<div class="empty sm">没有匹配的数据表</div>'; return; }
  box.innerHTML = list.map(d => `<label class="pick" style="cursor:pointer">
      <input type="checkbox" class="an-ds" data-id="${esc(d.id)}" ${S.anDs.has(d.id) ? 'checked' : ''} style="margin-top:3px">
      <div class="pi"><div class="pn">${esc(d.name)}</div>
      <div class="ps"><span class="tag">${esc(d.lab || '未标注')}</span><span class="tag o">${esc(d.table_type || '未标注')}</span>
      ${(d.sheets || []).length} 个工作表 · ${Number(d.total_rows || 0).toLocaleString()} 行</div></div>
    </label>`).join('');
}
function setupAnDsControls() {
  const kw = $('#an-ds-kw'); if (kw) kw.oninput = () => renderAnDatasets();
  const all = $('#an-ds-all'); if (all) all.onclick = () => { anDsVisible().forEach(d => S.anDs.add(d.id)); renderAnDatasets(); };
  const none = $('#an-ds-none'); if (none) none.onclick = () => { S.anDs.clear(); renderAnDatasets(); };
  const box = $('#an-ds-list');
  if (box) box.onchange = e => {
    const cb = e.target.closest('.an-ds'); if (!cb) return;
    if (cb.checked) S.anDs.add(cb.dataset.id); else S.anDs.delete(cb.dataset.id);
  };
}
/* ★ 勾选数据表 → 直接驱动下方分析：把勾选映射到汇总状态；无结果时点分析自动先生成 */
async function ensureResultFromSelection(force) {
  if (!force && S.lastResult && S.lastResult.id) return true;
  const ids = S.anDs || new Set();
  if (!ids.size) return false;
  S.picked = new Set();
  S.datasets.forEach(d => { if (ids.has(d.id)) (d.sheets || []).forEach(s => S.picked.add(d.id + '||' + s.name)); });
  S.outFields = (S.fields || []).map(f => f.key);
  S.outSet = new Set(S.outFields);
  renderMapping();
  const tip = $('#an-run-tip');
  if (tip) tip.textContent = '正在按勾选的数据表生成分析结果…';
  await runUnion();
  if (tip) tip.textContent = '勾选数据表后，点下方任一分析即自动生成结果';
  return !!(S.lastResult && S.lastResult.id);
}

/* 快捷场景：键 → [开始列关键词(按序匹配), 结束列关键词]。点按钮自动选列并计算 */
const TAT_PRESETS = {
  cj:   { label: '采集-签收', start: ['采集时间', '采样时间', '采集'], end: ['签收时间', '接收时间', '签收'] },
  qs:   { label: '签收-核收', start: ['签收时间', '接收时间', '签收'], end: ['核收时间', '核收'] },
  hbg:  { label: '核收-报告', start: ['核收时间', '核收'], end: ['检测完成时间', '报告时间', '报告'] },
  bgsh: { label: '报告-审核', start: ['检测完成时间', '报告时间', '报告'], end: ['审核时间', '审核'] },
};
function tatPresetRun(key) {
  const p = TAT_PRESETS[key]; if (!p) return;
  const s = $('#tat-start'), e = $('#tat-end');
  const tl = Array.from(s.options).map(o => o.value);
  const find = (kws) => { for (const k of kws) { const hit = tl.find(c => c.includes(k)); if (hit) return hit; } return ''; };
  const sc = find(p.start), ec = find(p.end);
  if (!sc || !ec || sc === ec) {
    return toast(`「${p.label}」需要的时间列不全（${p.start[0]} / ${p.end[0]}），请检查数据或手动选择`, 'err');
  }
  s.value = sc; e.value = ec;
  $$('#tat-presets .btn').forEach(b => b.classList.toggle('on', b.dataset.pre === key));
  tatRun();
}

/* ★ TAT 分析：对当前结果计算 结束时间−开始时间 的时长统计（只读） */
function tatFill(cols) {
  const s = $('#tat-start'), e = $('#tat-end'), g = $('#tat-group');
  if (!s || !e) return;
  const list = cols || [];
  const opt = list.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  s.innerHTML = opt; e.innerHTML = opt;
  g.innerHTML = '<option value="">不分组</option>' + opt;
  // 只在"时间/日期"类列里猜，避免把"××申请模式"之类的文本列选进来
  const tl = list.filter(c => /时间|日期/.test(c));
  const pick = (kws, not) => {
    for (const k of kws) {
      const hit = tl.find(c => c.includes(k) && !(not || []).some(n => c.includes(n)));
      if (hit) return hit;
    }
    return '';
  };
  const endC = pick(['检测完成', '报告', '审核', '复查完成', '完成'], ['失效']);
  const startC = pick(['采样', '接收', '采集', '签收', '上机', '申请'], ['完成', '失效', '审核', '报告']);
  s.value = startC || (tl[0] || ''); e.value = endC || (tl[tl.length - 1] || '');
  if (list.includes('模块')) g.value = '模块';
  const tip = $('#tat-tip');
  if (tip) tip.textContent = list.length
    ? '周转时间 = 结束时间 − 开始时间；已按列名预选，可自行调整。'
    : '暂无分析结果：可先在上方生成，或从「结果分析」下拉选择历史结果。';
}
function tatFmt(h) {
  if (h === null || h === undefined) return '—';
  return h < 48 ? h.toFixed(1) + ' 小时' : (h / 24).toFixed(1) + ' 天';
}
/* ★ TAT 结果导出：KPI / 时长分布 / 分组统计 三段 CSV（浏览器本地生成，不动数据） */
function tatExport() {
  const r = S.tatLast;
  if (!r || !r.stats) return toast('还没有可导出的 TAT 结果，请先计算', 'err');
  const st = r.stats;
  const rows = [];
  rows.push(['TAT 分析', `${r.end} − ${r.start}${r.group ? '（按 ' + r.group + ' 分组）' : ''}`]);
  rows.push([]);
  rows.push(['指标', '数值']);
  rows.push(['有效样本', st.valid]);
  rows.push(['无法解析/负值', st.invalid]);
  rows.push(['平均 TAT（小时）', st.avg_h]);
  rows.push(['中位数（小时）', st.median_h]);
  rows.push(['P90（小时）', st.p90_h]);
  rows.push(['最短（小时）', st.min_h]);
  rows.push(['最长（小时）', st.max_h]);
  rows.push([]);
  rows.push(['时长分布', '样本数', '占比(%)']);
  (r.dist || []).forEach(d => rows.push([d.label, d.count, d.pct]));
  if (r.groups && r.groups.length) {
    rows.push([]);
    rows.push([`按 ${r.group} 分组`, '样本数', '平均(小时)', '中位数(小时)', 'P90(小时)', '最长(小时)']);
    r.groups.forEach(g => rows.push([g.name, g.n, g.avg_h, g.median_h, g.p90_h, g.max_h]));
  }
  const csv = '\ufeff' + rows.map(row => row.map(c => {
    const s = String(c === null || c === undefined ? '' : c);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(',')).join('\r\n');
  const a = document.createElement('a');
  const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  a.download = `TAT分析_${r.start}_${r.end}_${stamp}.csv`.replace(/[\\/:*?"<>|]/g, '_');
  document.body.appendChild(a); a.click(); a.remove();
  toast('TAT 分析已导出为 CSV', 'ok');
}
/* ★ TAT 清除：收起结果、恢复默认提示（不动数据与结果池） */
function tatClear() {
  S.tatLast = null;
  $('#tat-body').innerHTML = '';
  $('#tat-badge').textContent = '—';
  $$('#tat-presets .btn').forEach(b => b.classList.remove('on'));
  const tip = $('#tat-tip');
  if (tip) tip.textContent = '周转时间 = 结束时间 − 开始时间；用于查看检测流程各环节耗时分布。';
  toast('已清除 TAT 分析结果', 'ok');
}
async function tatRun() {
  if (!(S.lastResult && S.lastResult.id)) {
    const ok = await ensureResultFromSelection();
    if (!ok) return toast('请先勾选要分析的数据表，或从「结果分析」下拉选择历史结果', 'err');
  }
  const rid = S.lastResult && S.lastResult.id;
  const start = $('#tat-start').value, end = $('#tat-end').value, group = $('#tat-group').value;
  const btn = $('#tat-run'); btn.disabled = true; btn.textContent = '计算中…';
  try {
    const r = await api('/api/tat', { result_id: rid, start, end, group });
    S.tatLast = r;            // 存起来供「导出」使用
    const st = r.stats || {};
    $('#tat-badge').textContent = st.valid ? `${Number(st.valid).toLocaleString()} 例` : '无有效数据';
    const kpi = (l, v) => `<div class="kpi"><div class="kv">${esc(v)}</div><div class="kl">${esc(l)}</div></div>`;
    let html = '<div class="cards">'
      + kpi('有效样本', Number(st.valid || 0).toLocaleString())
      + kpi('平均 TAT', tatFmt(st.avg_h))
      + kpi('中位数', tatFmt(st.median_h))
      + kpi('P90', tatFmt(st.p90_h))
      + kpi('最长', tatFmt(st.max_h))
      + '</div>';
    if (r.dist && r.dist.length) {
      const mx = Math.max(...r.dist.map(d => d.count), 1);
      html += '<h2>时长分布</h2><div class="bars">' + r.dist.map(d =>
        `<div class="bar"><span class="bn">${esc(d.label)}</span><span class="bt"><i class="bf" style="display:block;height:100%;width:${Math.round(d.count * 100 / mx)}%"></i></span><span class="bv">${d.count}（${d.pct}%）</span></div>`).join('') + '</div>';
    }
    if (r.groups && r.groups.length > 1) {
      html += '<h2>按 ' + esc(r.group) + ' 分组</h2><div class="tablewrap"><table class="grid"><thead><tr>'
        + '<th>分组</th><th>样本数</th><th>平均</th><th>中位数</th><th>P90</th><th>最长</th></tr></thead><tbody>'
        + r.groups.map(g2 => `<tr><td><b>${esc(g2.name)}</b></td><td>${Number(g2.n).toLocaleString()}</td><td>${tatFmt(g2.avg_h)}</td><td>${tatFmt(g2.median_h)}</td><td>${tatFmt(g2.p90_h)}</td><td>${tatFmt(g2.max_h)}</td></tr>`).join('')
        + '</tbody></table></div>';
    }
    if (st.invalid) html += `<div class="tip" style="margin-top:8px">另有 ${Number(st.invalid).toLocaleString()} 行时间无法解析或为负值，已跳过。</div>`;
    $('#tat-body').innerHTML = html;
  } catch (err) {
    toast('TAT 计算失败：' + err.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = '计算';
  }
}

function anReset() {
  S.an = anBlank();
  const hide = ['#analysis-card', '#an-body', '#an-loading', '#an-fail'];
  hide.forEach(s => { const e = $(s); if (e) e.style.display = 'none'; });
  ['#an-kpis', '#an-table', '#an-sum', '#pv-out', '#pv-dims'].forEach(s => {
    const e = $(s); if (e) e.innerHTML = '';
  });
  const f = $('#an-fail'); if (f) f.textContent = '';
  anSwitchTab('profile');
}

function anSwitchTab(t) {
  // ★ 先切面板、再触发首次计算。
  //   之前把 anRunPivot() 写在前面并 return，导致第一次点「数据透视」时
  //   明明算出来了、面板却还停在「数据概览」，要再点一次才显示。
  $('#an-pane-profile').style.display = t === 'profile' ? '' : 'none';
  $('#an-pane-pivot').style.display = t === 'pivot' ? '' : 'none';
  $('#an-tab-p').classList.toggle('on', t === 'profile');
  $('#an-tab-x').classList.toggle('on', t === 'pivot');
  if (t === 'pivot' && !S.an.pvKey) anRunPivot();
}
$('#an-tab-p').onclick = () => anSwitchTab('profile');
$('#an-tab-x').onclick = () => anSwitchTab('pivot');

/* 生成结果后由 showResult() 调用 */
async function anStart(cols) {
  const list = (cols || []).slice();
  S.an = anBlank();
  S.an.id = (S.lastResult && S.lastResult.id) || null;
  S.an.cols = list;
  S.an.dims = list.length ? [list[0]] : [];   // 默认拿第一列当行维度，切过去就有东西看
  anInitPivot(list);
  anPaintDims();
  anSyncAgg();
  $('#pv-out').innerHTML = '';
  $('#analysis-card').style.display = 'block';
  $('#an-badge').textContent = '—';
  $('#an-fail').style.display = 'none';
  $('#an-body').style.display = 'none';
  $('#an-loading').style.display = '';
  try {
    const r = await api('/api/result/profile', { result_id: S.an.id });
    // 等结果的这段时间里用户可能已经清了结果 / 换了一份，别把旧数据画上去
    if (!S.an.id || S.an.id !== ((S.lastResult && S.lastResult.id) || null)) return;
    anPaintProfile(r.profile);
  } catch (e) {
    $('#an-fail').style.display = '';
    $('#an-fail').innerHTML = '数据概览没算出来：' + esc(e.message);
  } finally {
    $('#an-loading').style.display = 'none';
  }
}

function anPaintProfile(p) {
  $('#an-body').style.display = '';
  $('#an-badge').textContent = p.rows.toLocaleString() + ' 行 × ' + p.cols + ' 列';
  const kpis = [
    ['总行数', p.rows.toLocaleString(), false],
    ['列数', String(p.cols), false],
    ['有缺失的列', String(p.missing_cols), !!p.missing_cols],
    ['可统计的数值列', String(p.numeric_cols), false],
  ];
  $('#an-kpis').innerHTML = kpis.map(([k, v, warn]) =>
    `<div class="card kpi${warn ? ' warn' : ''}"><div class="kv">${esc(v)}</div><div class="kl">${esc(k)}</div></div>`
  ).join('');

  const head = '<thead><tr><th>字段</th><th style="width:76px">类型</th>'
    + '<th style="width:88px" class="num">非空</th><th style="width:76px" class="num">空值</th>'
    + '<th style="width:76px" class="num">去重</th><th>统计摘要</th></tr></thead>';
  const body = p.columns.map(c => '<tr>'
    + `<td class="k">${esc(c.name)}</td>`
    + `<td><span class="an-kind k-${esc(c.kind)}">${esc(c.kind_label)}</span></td>`
    + `<td class="num">${c.nonempty.toLocaleString()}</td>`
    + `<td class="num${c.empty ? ' an-warn' : ''}">${esc(String(c.empty))}</td>`
    + `<td class="num">${c.distinct.toLocaleString()}</td>`
    + `<td>${esc(c.summary)}${c.note ? ` <span class="an-note">⚠ ${esc(c.note)}</span>` : ''}</td>`
    + '</tr>').join('');
  $('#an-table').innerHTML = head + '<tbody>' + body + '</tbody>';

  $('#an-sum').className = 'an-sum' + (p.missing_cols ? ' warn' : '');
  $('#an-sum').innerHTML = esc(p.summary);
}

function anInitPivot(cols) {
  const opts = (cols || []).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  $('#pv-dim-sel').innerHTML = opts;
  $('#pv-col').innerHTML = '<option value="">（不要列维度）</option>' + opts;
  $('#pv-val').innerHTML = '<option value="">（不指定）</option>' + opts;
  $('#pv-agg').innerHTML = PV_AGGS.map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join('');
}

function anPaintDims() {
  const d = S.an.dims;
  if (!d.length) {
    $('#pv-dims').innerHTML = '<span class="tip">还没加行维度。不加也可以 —— 那就只按「列维度」横向展开。</span>';
    return;
  }
  $('#pv-dims').innerHTML = d.map((n, i) =>
    `<span class="pv-chip"><i class="ix">${i + 1}</i>${esc(n)}<button data-i="${i}" title="移除">✕</button></span>`
  ).join('');
  $$('#pv-dims .pv-chip button').forEach(b => {
    b.onclick = () => { S.an.dims.splice(parseInt(b.dataset.i), 10); anPaintDims(); };
  });
}

$('#pv-dim-add').onclick = () => {
  const v = $('#pv-dim-sel').value;
  if (!v) return;
  if (S.an.dims.includes(v)) { toast('「' + v + '」已经在行维度里了', 'err'); return; }
  if (S.an.dims.length >= 3) { toast('行维度最多 3 个，再多表格就宽得没法看了', 'err'); return; }
  S.an.dims.push(v);
  anPaintDims();
};

/* 「计数」不需要指定字段；其它聚合方式必须选一列，否则不知道该统计什么 */
function anSyncAgg() {
  const a = $('#pv-agg').value;
  const need = a !== 'count';
  $('#pv-val').disabled = !need;
  const label = (PV_AGGS.find(x => x[0] === a) || ['', ''])[1];
  $('#pv-hint').innerHTML = need
    ? `「${esc(label)}」需要指定一个<b>字段</b> —— 说明要统计的是哪一列的值。`
    : '「计数」统计每个分组里有<b>几行</b>，不需要指定字段。空值会单独归成一组「（空）」，不会被当成 0。';
}
$('#pv-agg').onchange = anSyncAgg;

async function anRunPivot() {
  if (!S.lastResult || !S.lastResult.id) return;
  const a = $('#pv-agg').value;
  const val = a === 'count' ? '' : $('#pv-val').value;
  if (a !== 'count' && !val) {
    const label = (PV_AGGS.find(x => x[0] === a) || ['', ''])[1];
    toast('用「' + label + '」要先在上面选一个字段', 'err');
    $('#pv-val').focus();
    return;
  }
  const dims = S.an.dims.slice();
  const cd = $('#pv-col').value || '';
  if (!dims.length && !cd) { toast('至少要选一个行维度或列维度', 'err'); return; }

  const body = {
    result_id: S.lastResult.id, row_dims: dims, col_dim: cd, value: val, agg: a,
    show_total: $('#pv-total').checked, drop_blank_keys: $('#pv-dropblank').checked,
  };
  const btn = $('#pv-run');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="an-spin dark"></span> 计算中…';
  btn.disabled = true;
  try {
    const r = await api('/api/result/pivot', body);
    S.an.pvKey = true;
    S.an.pvBody = body;
    anPaintPivot(r);
  } catch (e) {
    $('#pv-out').innerHTML = '<div class="empty">' + esc(e.message).replace(/\n/g, '<br>') + '</div>';
  } finally {
    btn.innerHTML = old; btn.disabled = false;
  }
}
$('#pv-run').onclick = anRunPivot;

function anPaintPivot(pv) {
  const m = pv.meta || {};
  const nd = (m.row_dims || []).length;
  // 有列维度时，表头就是列取值，看不出用了哪种聚合，所以这里始终写明
  const tags = ['聚合：' + (m.value ? m.value + ' ' : '') + m.agg_label,
    m.used_rows.toLocaleString() + ' 行参与透视'];
  if (m.blank_rows) tags.push('丢掉 ' + m.blank_rows.toLocaleString() + ' 行（分组键全空）');
  if (m.bad_value) tags.push(m.bad_value.toLocaleString() + ' 个单元格不是数字，未计入' + m.agg_label);
  if (pv.truncated) tags.push('⚠ 分组共 ' + pv.groups.toLocaleString() + ' 组，界面只显示前 '
    + pv.shown.toLocaleString() + ' 组（导出仍是全量）');

  const head = '<thead><tr>' + pv.columns.map((c, i) =>
    `<th${i >= nd ? ' class="num"' : ''}>${esc(c)}</th>`).join('') + '</tr></thead>';
  const body = pv.rows.map(r => {
    const isTotal = nd > 0 && r[0] === '总计';
    return `<tr${isTotal ? ' class="an-total"' : ''}>` + r.map((v, i) =>
      i < nd ? `<td class="k">${esc(v)}</td>` : `<td class="num">${esc(v)}</td>`
    ).join('') + '</tr>';
  }).join('');

  $('#pv-out').innerHTML =
    '<div class="toolbar"><div class="tl">透视结果 '
    + '<span class="badge">' + pv.rows.length.toLocaleString() + ' 行</span></div>'
    + '<div class="tr">'
    + '<button class="btn small primary" id="pv-exp-xlsx">导出 Excel</button>'
    + '<button class="btn small" id="pv-exp-csv">导出 CSV</button>'
    + '</div></div>'
    + '<div class="tablewrap tall"><table class="grid" id="pv-table">'
    + head + '<tbody>' + body + '</tbody></table></div>'
    + '<div class="pv-note">' + tags.map(esc).join(' ｜ ') + '</div>';

  $('#pv-exp-xlsx').onclick = () => anExportPivot('xlsx');
  $('#pv-exp-csv').onclick = () => anExportPivot('csv');
}

async function anExportPivot(fmt) {
  if (!S.an.pvBody) { toast('先生成一次透视表再导出', 'err'); return; }
  const by = S.an.pvBody.row_dims.length ? S.an.pvBody.row_dims.join('_') : (S.an.pvBody.col_dim || '汇总');
  try {
    const r = await api('/api/result/pivot_export', Object.assign({}, S.an.pvBody, {
      format: fmt, name: '数据透视_' + by,
    }));
    try {
      const a = document.createElement('a');
      a.href = '/api/download?name=' + encodeURIComponent(r.file);
      a.download = r.file;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 1500);
    } catch (e) { console.error('触发下载失败', e); }
    toast('透视结果已导出：' + r.file + (r.rows ? '（全量 ' + r.rows.toLocaleString() + ' 行）' : ''), 'ok');
  } catch (e) {
    toast('导出失败：' + e.message, 'err');
  }
}

/* ==========================================================================
   v2.1.0 「分析报告」—— 模板驱动，一键产出独立 HTML 报告
   —— 与「结果分析」并列，同样在生成汇总结果后出现。
   —— 后端：/api/report/templates · /generate · /open · /delete
   ========================================================================== */

/* 模板角色 → 中文名（缺列提示用） */
const RP_ROLE = { time: '检测时间列', module: '模块/仪器列', project: '项目名称列' };

function rpBlank() {
  return { id: null, tpls: [], pick: '', opts: {}, key: '', busy: false };
}

/* 结果没了 → 整个报告卡片收起来 */
function rpReset() {
  S.rp = rpBlank();
  const c = $('#report-card'); if (c) c.style.display = 'none';
  ['#rp-tpls', '#rp-opts', '#rp-done', '#rp-hist'].forEach(s => {
    const e = $(s); if (e) e.innerHTML = '';
  });
  const w = $('#rp-work'); if (w) w.style.display = 'none';
  const f = $('#rp-fail'); if (f) { f.style.display = 'none'; f.textContent = ''; }
  const l = $('#rp-loading'); if (l) l.style.display = 'none';
  const t = $('#rp-tip'); if (t) t.textContent = '';
  const n = $('#rp-name'); if (n) n.value = '';
  const hn = $('#rp-hist-n'); if (hn) hn.textContent = '0';
}

/* 生成结果后由 showResult() 调用 */
async function rpStart(rid, cols) {
  rpReset();
  S.rp.id = rid || null;
  $('#report-card').style.display = 'block';
  $('#rp-badge').textContent = '正在读模板…';
  try {
    const r = await api('/api/report/templates', { result_id: S.rp.id });
    // 期间用户可能清了结果
    if (S.rp.id !== ((S.lastResult && S.lastResult.id) || null)) return;
    S.rp.tpls = (r.templates || []).filter(t => t.id === 'equip_load');   // 聚焦设备负载分析
    if (!S.rp.tpls.some(t => t.id === S.rp.pick)) S.rp.pick = 'equip_load';
    rpPaintTpls();
    rpPaintHist(r.reports || []);
    const n = S.rp.tpls.filter(t => !t.check || t.check.ok).length;
    $('#rp-badge').textContent = n + ' / ' + S.rp.tpls.length + ' 个模板可用';
    // 只有一个可用就自动选中，省一步点击
    const usable = S.rp.tpls.filter(t => !t.check || t.check.ok);
    if (usable.length === 1) rpPick(usable[0].id);
    // 一个都不可用：直接说清楚缺什么
    if (!usable.length) {
      const miss = new Set();
      S.rp.tpls.forEach(t => (t.check && t.check.missing || []).forEach(m => miss.add(m)));
      $('#rp-fail').style.display = '';
      $('#rp-fail').innerHTML = '这份数据里找不到报告需要的列，暂时生成不了。缺：'
        + Array.from(miss).map(m => esc(RP_ROLE[m] || m)).join('、')
        + '。请确认勾选的字段里包含「检测完成时间 / 模块 / 项目名称」这三列。';
    }
  } catch (e) {
    $('#rp-badge').textContent = '—';
    $('#rp-fail').style.display = '';
    $('#rp-fail').innerHTML = '模板清单没读出来：' + esc(e.message);
  }
}

/* ---------- 模板卡片 ---------- */
function rpPaintTpls() {
  const box = $('#rp-tpls');
  box.innerHTML = S.rp.tpls.map(t => {
    const ck = t.check || {};
    const ok = ck.ok !== false;
    const on = S.rp.pick === t.id;
    let extra = '';
    if (!ok) {
      const ms = (ck.missing || []).map(m => RP_ROLE[m] || m).join('、');
      extra = '<div class="rpt-miss">缺列：' + esc(ms || '未知') + '</div>';
    } else if (ck.found_label && Object.keys(ck.found_label).length) {
      extra = '<div class="rpt-hit">已识别：'
        + Object.keys(ck.found_label).map(k => esc(RP_ROLE[k] || k) + ' → 「' + esc(ck.found_label[k]) + '」').join('；')
        + '</div>';
    }
    return `<button class="rp-tpl${on ? ' on' : ''}${ok ? '' : ' off'}"
        data-tpl="${esc(t.id)}"${ok ? '' : ' disabled'}>
      <div class="rpt-h"><span class="rpt-ic">${esc(t.icon || '📄')}</span>
        <span class="rpt-nm">${esc(t.name)}</span></div>
      <span class="rpt-tag ${ok ? 'ok' : 'no'}">${ok ? '可用' : '缺列'}</span>
      <div class="rpt-ds">${esc(t.desc || '')}</div>${extra}
    </button>`;
  }).join('');
  Array.from(box.querySelectorAll('.rp-tpl')).forEach(b => {
    b.onclick = () => { if (!b.disabled) rpPick(b.dataset.tpl); };
  });
}

/* ---------- 选中模板 → 出参数 ---------- */
function rpPick(id) {
  S.rp.pick = id;
  rpPaintTpls();
  const t = S.rp.tpls.find(x => x.id === id);
  if (!t) return;
  const opts = t.opts || [];
  const box = $('#rp-opts');
  if (!opts.length) {
    box.innerHTML = '<div class="tip" style="grid-column:1/-1;margin:0">'
      + '这个模板不需要额外参数，直接点「生成报告」就行。</div>';
  } else {
    box.innerHTML = opts.map(o => {
      const id2 = 'ro-' + o.key;
      const dv = o.default === null || o.default === undefined ? '' : String(o.default);
      const ph = o.type === 'number' ? '留空 = 自动' : '';
      return `<div class="rp-opt">
        <label class="ro-lb" for="${id2}">${esc(o.label)}</label>
        <input class="inp ro-inp" id="${id2}" data-key="${esc(o.key)}"
               type="${o.type === 'number' ? 'number' : 'text'}"
               value="${esc(dv)}" placeholder="${esc(ph)}">
        ${o.hint ? '<div class="ro-hint">' + esc(o.hint) + '</div>' : ''}
      </div>`;
    }).join('');
  }
  $('#rp-work').style.display = 'block';
  $('#rp-done').style.display = 'none';
  $('#rp-name').value = '';
  const f = $('#rp-fail'); f.style.display = 'none'; f.textContent = '';
  $('#rp-tip').textContent = '';
}

/* 收集参数：空字符串一律不发，让后端用模板默认值 */
function rpGatherOpts() {
  const o = {};
  Array.from($$('#rp-opts input[data-key]')).forEach(i => {
    const v = (i.value || '').trim();
    if (v) o[i.dataset.key] = v;
  });
  return o;
}

async function rpRun() {
  if (!S.rp.id || !S.rp.pick) {
    // 没结果时：勾选了数据表就自动生成一份，再接着出设备负载报告
    const ok = await ensureResultFromSelection();
    if (!ok || !S.rp.id) return toast('请先勾选要分析的数据表，或从「结果分析」下拉选择历史结果', 'err');
  }
  const btn = $('#rp-run');
  btn.disabled = true;
  $('#rp-loading').style.display = '';
  $('#rp-fail').style.display = 'none';
  $('#rp-done').style.display = 'none';
  $('#rp-tip').textContent = '';
  try {
    const r = await api('/api/report/generate', {
      result_id: S.rp.id, template_id: S.rp.pick,
      opts: rpGatherOpts(), name: ($('#rp-name').value || '').trim(),
    });
    rpPaintDone(r);
    toast('报告已生成：' + r.file, 'ok');
    // 生成完顺手刷新历史列表
    const t = await api('/api/report/templates', { result_id: S.rp.id });
    rpPaintHist(t.reports || []);
  } catch (e) {
    $('#rp-fail').style.display = '';
    // 报错里可能带换行（模板给的提示），转成行
    $('#rp-fail').innerHTML = esc(e.message).replace(/\n/g, '<br>');
    toast('报告没生成出来', 'err');
  } finally {
    $('#rp-loading').style.display = 'none';
    btn.disabled = false;
  }
}

const RP_KPI_LABEL = {
  row_count: '数据行数', 总完成量: '总完成量', 峰值时段: '峰值时段', 峰值: '峰值(测试)',
  模块数: '模块数', 时段数: '时段数', 过载点数: '过载点数',
  标称速度: '标称速度', 饱和窗: '饱和窗', 窗口时长: '窗口(h)',
  满转模块: '满转模块', 实际速度下限: '实际速度下限', 短时峰值: '短时峰值',
};
const RP_KPI_UNIT = { 总完成量: '测试', 峰值: '测试', 标称速度: '测试/h', 实际速度下限: '测试/h', 短时峰值: '测试/h' };

function rpPaintDone(r) {
  const box = $('#rp-done');
  box.style.display = 'block';
  const k = r.kpis || {};
  const items = Object.keys(RP_KPI_LABEL).filter(x => x in k).map(x => {
    const v = k[x];
    if (v === null || v === undefined || v === '') return '';
    const u = RP_KPI_UNIT[x];
    return `<div class="rdk"><b>${esc(String(v))}</b><span>${esc(RP_KPI_LABEL[x])}${u ? '（' + esc(u) + '）' : ''}</span></div>`;
  }).filter(Boolean).join('');
  box.innerHTML = `<div class="rp-done">
    <div class="rdt">✅ 已生成：${esc(r.template_name || r.template)}
      <span class="tip" style="margin:0">${fmtSize(r.size)} · 用时 ${r.elapsed || 0}s</span></div>
    <div class="rd-p">${esc(r.file)}<br>${esc(r.dir || '')}</div>
    ${items ? '<div class="rd-kpis">' + items + '</div>' : ''}
    <div class="rd-act">
      <button class="btn primary small" id="rp-do-open">在浏览器里打开</button>
      <button class="btn small" id="rp-do-folder">打开所在文件夹</button>
      <button class="btn ghost small" id="rp-do-copy">复制文件路径</button>
    </div>
  </div>`;
  $('#rp-do-open').onclick = () => rpOpen(r.file);
  $('#rp-do-folder').onclick = () => api('/api/open_folder', { path: r.dir })
    .then(() => toast('已打开报告文件夹', 'ok'))
    .catch(e => toast('打不开文件夹：' + e.message, 'err'));
  $('#rp-do-copy').onclick = () => {
    const p = r.path || ((r.dir || '') + '\\' + r.file);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(p).then(() => toast('路径已复制', 'ok'),
        () => toast('复制失败，请手动复制：' + p, 'err'));
    } else { toast('请手动复制：' + p, 'err'); }
  };
}

async function rpOpen(file) {
  try {
    await api('/api/report/open', { file });
    toast('已用默认浏览器打开：' + file, 'ok');
  } catch (e) {
    toast('打不开报告：' + e.message, 'err');
  }
}

/* ---------- 生成历史 ---------- */
function rpPaintHist(list) {
  const box = $('#rp-hist');
  $('#rp-hist-n').textContent = String((list || []).length);
  if (!list || !list.length) {
    box.innerHTML = '<div class="rp-empty">还没有生成过报告。选一个模板，点「生成报告」试试。</div>';
    return;
  }
  box.innerHTML = list.map(f => `<div class="rp-h" data-file="${esc(f.file)}">
    <span class="rh-i">📄</span>
    <span class="rh-n" title="${esc(f.file)}">${esc(f.file)}</span>
    <span class="rh-m">${esc(f.time)} · ${fmtSize(f.size)}</span>
    <span class="rh-a">
      <button class="btn small" data-act="open">打开</button>
      <button class="btn ghost small" data-act="del">删除</button>
    </span>
  </div>`).join('');
  Array.from(box.querySelectorAll('.rp-h')).forEach(row => {
    const fn = row.dataset.file;
    row.querySelector('[data-act="open"]').onclick = () => rpOpen(fn);
    row.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm('删除这份报告？\n\n' + fn + '\n\n（删了就找不回来了）')) return;
      try {
        await api('/api/report/delete', { file: fn });
        toast('已删除：' + fn, 'ok');
        const t = await api('/api/report/templates', { result_id: S.rp.id });
        rpPaintHist(t.reports || []);
      } catch (e) {
        toast('删除失败：' + e.message, 'err');
      }
    };
  });
}

$('#rp-run').onclick = () => rpRun();
$('#rp-open-dir').onclick = () => api('/api/open_folder', { path: S.reportDir || '' })
  .then(() => toast('已打开报告文件夹', 'ok'))
  .catch(e => toast('打不开文件夹：' + e.message, 'err'));
$('#rp-name').addEventListener('keydown', e => { if (e.key === 'Enter') rpRun(); });

/* ---------------- 横向关联 ---------------- */
function switchMode(m) {
  S.mode = m;
  $('#pane-union').style.display = m === 'union' ? '' : 'none';
  $('#pane-join').style.display = m === 'join' ? '' : 'none';
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
}
$$('.tab').forEach(t => t.onclick = () => switchMode(t.dataset.mode));
function dsOptions(sel, val) {
  sel.innerHTML = S.datasets.map(d => `<option value="${d.id}">${esc(d.lab ? d.lab + ' / ' : '')}${esc(d.name)}</option>`).join('');
  if (val) sel.value = val;
}
function sheetOptions(dsId, sel, val) {
  const d = S.datasets.find(x => x.id === dsId);
  sel.innerHTML = !d ? '' : (d.sheets || []).map(s => `<option value="${esc(s.name)}">${esc(s.name)}（${s.rows}行）</option>`).join('');
  if (val) sel.value = val;
}
function colOptions(dsId, sheetName, sel, val) {
  const d = S.datasets.find(x => x.id === dsId);
  const s = d && (d.sheets || []).find(x => x.name === sheetName);
  sel.innerHTML = !s ? '' : (s.columns || []).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  if (val) sel.value = val;
}
function renderJoin() {
  dsOptions($('#j-base-ds'), $('#j-base-ds').value);
  const bds = $('#j-base-ds').value || (S.datasets[0] || {}).id;
  sheetOptions(bds, $('#j-base-sheet'), $('#j-base-sheet').value);
  colOptions(bds, $('#j-base-sheet').value, $('#j-base-on'), $('#j-base-on').value);

  $('#joins').innerHTML = S.joinList.map((j, i) => `
    <div class="optrow" style="border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:10px">
      <select class="j-ds sel wide"></select>
      <select class="j-sheet sel"></select>
      <label class="chk">关联字段 <select class="j-on sel"></select></label>
      <span class="x" style="cursor:pointer;color:#7b8fa5" data-i="${i}">✕</span>
    </div>`).join('');
  $$('#joins .optrow').forEach((row, i) => {
    const j = S.joinList[i];
    dsOptions(row.querySelector('.j-ds'), j.dataset_id);
    sheetOptions(row.querySelector('.j-ds').value, row.querySelector('.j-sheet'), j.sheet);
    colOptions(row.querySelector('.j-ds').value, row.querySelector('.j-sheet').value, row.querySelector('.j-on'), j.on);
    row.querySelector('.j-ds').onchange = e => {
      j.dataset_id = e.target.value; j.sheet = ''; j.on = ''; renderJoin();
    };
    row.querySelector('.j-sheet').onchange = e => { j.sheet = e.target.value; j.on = ''; renderJoin(); };
    row.querySelector('.j-on').onchange = e => { j.on = e.target.value; };
    row.querySelector('.x').onclick = () => { S.joinList.splice(i, 1); renderJoin(); };
  });
}
$('#j-base-ds').onchange = e => { renderJoin(); };
$('#j-base-sheet').onchange = e => { colOptions(e.target.value ? $('#j-base-ds').value : '', e.target.value, $('#j-base-on'), ''); };
$('#add-join').onclick = () => {
  const d = S.datasets.find(x => x.id !== $('#j-base-ds').value) || S.datasets[0];
  if (!d) return toast('没有可关联的表', 'err');
  S.joinList.push({ dataset_id: d.id, sheet: (d.sheets[0] || {}).name, on: (d.sheets[0] || {}).columns ? d.sheets[0].columns[0] : '' });
  renderJoin();
};
$('#btn-run-join').onclick = async () => {
  const bds = $('#j-base-ds').value;
  if (!bds) return toast('还没有数据表', 'err');
  const btn = $('#btn-run-join');
  btn.innerHTML = '<span class="spin"></span>正在关联…'; btn.disabled = true;
  try {
    const r = await api('/api/aggregate', {
      mode: 'join',
      base: { dataset_id: bds, sheet: $('#j-base-sheet').value, header_rows: '', on: $('#j-base-on').value },
      joins: S.joinList.map(j => ({ dataset_id: j.dataset_id, sheet: j.sheet, header_rows: '', on: j.on })),
      dedup: $('#j-dedup').checked, limit: parseInt($('#j-limit').value) || 0, title: '关联结果',
    });    showResult(r, r.columns);
    toast(`关联完成：${r.total} 行`, 'ok');
  } catch (e) { toast('失败：' + e.message, 'err'); }
  btn.innerHTML = '生成关联表'; btn.disabled = false;
};

/* ---------------- AI ---------------- */
function fillConfigForm() {
  const ai = S.config.ai || {};
  $('#ai-url').value = ai.base_url || '';
  $('#ai-model').value = ai.model || '';
  $('#ai-temp').value = ai.temperature != null ? ai.temperature : 0.2;
  $('#ai-key').placeholder = ai.has_key ? '已保存（' + (ai.api_key_masked || '****') + '），留空则不改动' : 'sk-...';
  $('#ai-warn').style.display = ai.has_key ? 'none' : 'block';
  $('#set-labs').value = (S.config.labs || []).join('\n');
  $('#set-types').value = (S.config.table_types || []).join('\n');
  $('#set-basedir').textContent = S.baseDir;
  $('#set-libdir').textContent = S.baseDir + '\\library';
  $('#set-outdir').textContent = S.outDir;
  const sv = $('#set-version');
  if (sv) sv.textContent = 'v' + (S.appVersion || '?');
}
$('#btn-ai-save').onclick = async () => {
  const ai = {
    base_url: $('#ai-url').value.trim(), model: $('#ai-model').value.trim(),
    temperature: parseFloat($('#ai-temp').value) || 0.2, enabled: true,
  };
  if ($('#ai-key').value.trim()) ai.api_key = $('#ai-key').value.trim();
  if ($('#ai-clear').checked) ai.clear_key = true;
  await api('/api/config', { ai });
  $('#ai-key').value = ''; $('#ai-clear').checked = false;
  await loadAll();
  toast('AI 配置已保存', 'ok');
};
$('#btn-ai-test').onclick = async () => {
  const b = $('#btn-ai-test'); b.innerHTML = '<span class="spin"></span>测试中…';
  try { const r = await api('/api/ai/test', {}); toast('连接正常，AI 回复：' + r.reply, 'ok'); }
  catch (e) { toast(e.message, 'err'); }
  b.innerHTML = '测试连接';
};
$('#btn-ai-plan').onclick = async () => {
  const q = $('#ai-q').value.trim();
  if (!q) return toast('先用一句话描述你要什么数据', 'err');
  if (!S.datasets.length) return toast('还没有导入数据表', 'err');
  const b = $('#btn-ai-plan'); b.innerHTML = '<span class="spin"></span>AI 正在理解…'; b.disabled = true;
  try {
    const r = await api('/api/ai/plan', { question: q });
    $('#ai-plan').textContent = JSON.stringify(r.plan, null, 2);
    applyPlan(r.plan, q);
  } catch (e) { toast(e.message, 'err'); $('#ai-plan').textContent = '失败：' + e.message; }
  b.innerHTML = '让 AI 帮我配好并生成'; b.disabled = false;
};
async function applyPlan(plan, q) {
  const ids = new Set(plan.source_ids || []);
  if (!ids.size) return toast('AI 没识别出要用的数据表，请把问题说得更具体些', 'err');
  S.picked = new Set();
  S.datasets.filter(d => ids.has(d.id)).forEach(d => (d.sheets || []).forEach(s => S.picked.add(d.id + '||' + s.name)));
  const all = candidateFields();
  const want = (plan.fields || []).map(f => {
    let hit = all.find(c => c.name === f) || all.find(c => norm(c.name) === norm(f));
    if (!hit) { let bs = 0; all.forEach(c => { const s = sim(c.name, f); if (s > bs) { bs = s; hit = c; } }); if (bs < 0.7) hit = null; }
    return hit ? hit.name : null;
  }).filter(Boolean);
  S.outSet = new Set(want); S.outFields = [];
  want.forEach(w => { if (!S.outFields.includes(w)) S.outFields.push(w); });
  if (!S.outFields.length) {
    const multi = all.filter(c => c.count > 1).slice(0, 12);
    (multi.length ? multi : all.slice(0, 12)).forEach(c => { S.outSet.add(c.name); S.outFields.push(c.name); });
  }
  S.mapOverride = {};
  S.filters = (plan.filters || []).filter(f => f && f.field).map(f => ({ field: f.field, op: f.op || 'contains', value: f.value == null ? '' : String(f.value) }));
  if (plan.add_source_col != null) $('#opt-src').checked = !!plan.add_source_col;
  if (plan.dedup != null) $('#opt-dedup').checked = !!plan.dedup;
  if (plan.limit) $('#opt-limit').value = plan.limit;
  S.resultTitle = plan.title || 'AI 汇总结果';
  switchMode('union');
  nav('build'); renderBuild();
  toast(`AI 已配好：${ids.size} 张表 / ${S.outFields.length} 个字段，正在生成…`);
  setTimeout(runUnion, 260);
}
$('#btn-ai-analyze').onclick = async () => {
  if (!S.lastResult) return toast('请先在「汇总提取」里生成一份结果', 'err');
  const q = $('#ai-a').value.trim() || '请分析这份汇总数据';
  const b = $('#btn-ai-analyze'); b.innerHTML = '<span class="spin"></span>分析中…'; b.disabled = true;
  try {
    const r = await api('/api/ai/analyze', { result_id: S.lastResult.id, question: q });
    const el = $('#ai-answer'); el.style.display = 'block'; el.textContent = r.answer;
  } catch (e) { toast(e.message, 'err'); }
  b.innerHTML = '分析当前汇总结果'; b.disabled = false;
};

/* ---------------- 设置 ---------------- */
$('#btn-save-config').onclick = async () => {
  await api('/api/config', {
    labs: $('#set-labs').value.split('\n').map(s => s.trim()).filter(Boolean),
    table_types: $('#set-types').value.split('\n').map(s => s.trim()).filter(Boolean),
  });
  await loadAll();
  toast('设置已保存', 'ok');
};
$('#btn-clear-all').onclick = () => {
  if (!confirm('确定清空全部已导入的数据表吗？此操作不可恢复（library/sources 里的文件也会删除）。')) return;
  api('/api/dataset/clear', {}).then(async () => {
    S.picked = new Set(); S.outFields = []; S.outSet = new Set(); S.lastResult = null;
    $('#result-card').style.display = 'none';
    await loadAll(); toast('已清空', 'ok');
  });
};

/* ---------------- 导航 ---------------- */
/* ★ 切到「汇总提取」时先静默同步一次后端数据，再渲染。
   原因：renderBuild() 只重绘**内存里已有的 S.datasets**，不会重新拉取。
   若导入动作发生在别处（另一个标签页 / 钉钉扫描 / 后台任务），
   本页就会一直显示旧列表，而且反复切页也没用 —— 以前只有 F5 整页刷新才行。
   现在每次进入本页都自动对齐一次；失败也不阻断导航（按旧数据渲染）。 */
let __buildSyncSeq = 0;
function nav(p) {
  $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.page === p));
  $$('.page').forEach(s => s.classList.toggle('active', s.id === 'page-' + p));
  if (p === 'build') {
    renderBuild();                       // 先按当前数据立刻渲染，界面不空
    loadResultPicker();                  // 结果分析/报告可独立选历史结果
    const seq = ++__buildSyncSeq;
    loadAll().then(() => {
      if (seq !== __buildSyncSeq) return;   // 期间又切走了，丢弃这次结果
      renderBuild();
    }).catch(() => { /* 静默：拿不到就保持旧渲染 */ });
  }
  if (p === 'analysis') {
    // 面板常驻本页（从汇总提取页迁移过来，DOM 移动不丢状态）：
    // 顺序固定为 结果分析 → TAT → 设备负载；设备负载无论有无结果都常显示
    const host = $('#page-analysis');
    const an = $('#analysis-card'), tc = $('#tat-card'), rp = $('#report-card');
    if (host && an) host.appendChild(an);
    if (host && tc) host.appendChild(tc);
    if (host && rp) host.appendChild(rp);
    renderAnDatasets();
    loadResultPicker();
    if (tc) tc.style.display = 'block';
    if (rp) {
      rp.style.display = 'block';
      const rb = $('#rp-badge');
      if (!S.lastResult && rb) rb.textContent = '暂无结果';
    }
    if (S.lastResult) tatFill(S.lastResult.cols);
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
// ★ 只给带 data-page 的导航项绑定切页；「使用说明」是外链 <a href>，不能拦（否则会调 nav(undefined)）
$$('.nav a').forEach(a => { if (a.dataset.page) a.onclick = () => nav(a.dataset.page); });
const __anPick = $('#an-pick'); if (__anPick) __anPick.onchange = () => applyPickedResult(__anPick.value);

function renderBuild() {
  renderPickList(); renderFieldList(); renderMapping(); renderFilters();
  if ($('#pane-join').style.display !== 'none' || S.mode === 'join') renderJoin();
  else if (S.datasets.length && !$('#j-base-ds').options.length) renderJoin();
}
function updateLabOptions() { /* 预留：实验室下拉 */ }

/* ---------------- 启动 ---------------- */
setupImport();
setupAnDsControls();
const __anRun = $('#btn-an-run'); if (__anRun) __anRun.onclick = async () => { const ok = await ensureResultFromSelection(true); if (!ok) toast('请先勾选至少一个数据表', 'err'); };
const __anClear = $('#an-clear'); if (__anClear) __anClear.onclick = () => { clearResult(); const rp = $('#report-card'); if (rp && $('#page-analysis').classList.contains('active')) { rp.style.display = 'block'; const rb = $('#rp-badge'); if (rb) rb.textContent = '暂无结果'; } $$('#tat-presets .btn').forEach(b => b.classList.remove('on')); };
['#tat-start', '#tat-end'].forEach(sel => { const el = $(sel); if (el) el.onchange = () => $$('#tat-presets .btn').forEach(b => b.classList.remove('on')); });
const __tatRun = $('#tat-run'); if (__tatRun) __tatRun.onclick = () => tatRun();
const __tatExport = $('#tat-export'); if (__tatExport) __tatExport.onclick = () => tatExport();
const __tatClear = $('#tat-clear'); if (__tatClear) __tatClear.onclick = () => tatClear();
$$('#tat-presets .btn').forEach(b => b.onclick = async () => {
  if (!(S.lastResult && S.lastResult.id)) {
    const ok = await ensureResultFromSelection();
    if (!ok) return toast('请先勾选要分析的数据表，或从「结果分析」下拉选择历史结果', 'err');
  }
  tatPresetRun(b.dataset.pre);
});
S.rp = rpBlank();          // v2.1.0：分析报告模块状态先建好，防止任何早期点击报 null
rpReset();                 // 初始收起「分析报告」卡片
// 自检：页面里每个 btn-* 按钮都必须绑好点击事件。
// 以前出现过"函数写好了但忘了接线"，用户点了完全没反应、也没有任何报错，
// 极难排查。这里主动检查一次，缺了就明确报出来。
// 注意：必须放在 setupImport() 之后，否则 btn-choose / btn-scan 还没接上会误报。
(function checkBindings() {
  const loose = $$('button[id^="btn-"]').filter(b => !b.onclick);
  window.__unboundButtons = loose.map(b => b.id);
  if (loose.length) {
    const names = loose.map(b => b.id + '（' + (b.textContent || '').trim() + '）').join('、');
    console.error('【自检】以下按钮没有绑定点击事件，点了不会有任何反应：' + names);
    const box = document.getElementById('error-card');
    if (box) {
      box.style.display = 'block';
      box.innerHTML = '<b>程序自检发现问题</b>'
        + '<div class="err-body">以下按钮没有生效，请把这句话截图反馈：<br>'
        + esc(names) + '</div>';
    }
  }
})();

loadAll().then(() => { renderJoin(); }).catch(e => toast('加载失败：' + e.message, 'err'));
