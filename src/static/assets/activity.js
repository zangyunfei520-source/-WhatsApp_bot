function parseLogLine(line) {
  const raw = String(line || '');
  const m = raw.match(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\[(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]\s*(.*)$/);
  if (m) {
    let level = m[2].toUpperCase();
    if (level === 'WARN') level = 'WARNING';
    if (level === 'CRITICAL') level = 'ERROR';
    return {time: m[1].replace('T',' ').slice(0,19), level, msg: m[3], raw};
  }
  let level = 'INFO';
  if (raw.includes('[ERROR]') || raw.includes('[CRITICAL]')) level = 'ERROR';
  else if (raw.includes('[WARNING]')) level = 'WARNING';
  else if (raw.includes('[DEBUG]')) level = 'DEBUG';
  return {time: raw.slice(0,19), level, msg: raw.slice(20), raw};
}

let activityEntries = [];
let activityLevelFilter = 'all';
let activitySearch = '';

function activityLevelClass(level) {
  return level === 'ERROR' ? 'error' : level === 'WARNING' ? 'warning' : level === 'DEBUG' ? 'debug' : 'info';
}

// Newest first, consecutive identical messages collapsed into one row with a count.
function groupActivityEntries(lines) {
  const parsed = (lines || []).map(parseLogLine).reverse();
  const grouped = [];
  for (const p of parsed) {
    const prev = grouped[grouped.length - 1];
    const key = `${p.level}|${p.msg || p.raw}`;
    if (prev && prev.key === key) { prev.count += 1; prev.firstTime = p.time; continue; }
    grouped.push({...p, key, count: 1, firstTime: p.time});
  }
  return grouped;
}

function renderActivityRows() {
  const body = document.getElementById('activity-rows');
  if (!body) return;
  const q = activitySearch.trim().toLowerCase();
  const rows = activityEntries.filter(p =>
    (activityLevelFilter === 'all' || p.level === activityLevelFilter) &&
    (!q || (p.msg || p.raw).toLowerCase().includes(q)));
  body.innerHTML = rows.map(p => {
    const levelClass = activityLevelClass(p.level);
    const repeat = p.count > 1 ? `<span class="log-repeat" title="${escapeHtml(p.firstTime)} 起连续 ${p.count} 次">×${p.count}</span>` : '';
    return `<tr class="log-row ${levelClass}">
      <td class="log-time">${escapeHtml(p.time)}</td>
      <td><span class="log-level ${levelClass}">${escapeHtml(p.level)}</span></td>
      <td class="log-msg">${escapeHtml(p.msg || p.raw)}${repeat}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="3" class="table-empty"><div class="empty-state"><p>${activityEntries.length ? '没有符合筛选条件的日志' : '暂无运行日志'}</p></div></td></tr>`;
  document.querySelectorAll('.log-filter').forEach(b => {
    const active = b.dataset.level === activityLevelFilter;
    b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active));
  });
}

function setActivityLevel(level) { activityLevelFilter = level; renderActivityRows(); }
function filterActivity(value) { activitySearch = String(value || ''); renderActivityRows(); }

async function loadActivity(el) {
  const d = await get('/activity?lines=500');
  activityEntries = groupActivityEntries(d.lines);
  const counts = {ERROR: 0, WARNING: 0, INFO: 0};
  for (const p of activityEntries) if (p.level in counts) counts[p.level] += p.count;
  const filters = [['all', '全部', (d.lines || []).length], ['ERROR', '错误', counts.ERROR], ['WARNING', '警告', counts.WARNING], ['INFO', '信息', counts.INFO]];

  el.innerHTML = `
    ${pageHeader('运行日志', '最近机器人事件（最新在前）', `<button class="btn btn-outline btn-sm" onclick="loadPage('activity')">${uiIcon('refresh')} 刷新日志</button>`)}
    <div class="panel">
      <div class="panel-header"><h3>事件记录</h3><span class="panel-meta">${d.error ? '日志读取异常' : (d.lines||[]).length + ' 行'}</span></div>
      <div class="log-filters" role="group" aria-label="按级别筛选">
        ${filters.map(([level, label, n]) => `<button type="button" class="log-filter" data-level="${level}" aria-pressed="false" onclick="setActivityLevel('${level}')">${label}<span>${n}</span></button>`).join('')}
        <input type="search" aria-label="搜索日志" placeholder="搜索日志内容…" value="${escapeHtml(activitySearch)}" oninput="filterActivity(this.value)">
      </div>
      <div class="table-wrap">
        <table class="table-wide log-table">
          <thead><tr><th style="width:170px">时间</th><th style="width:110px">级别</th><th>事件</th></tr></thead>
          <tbody id="activity-rows"></tbody>
        </table>
      </div>
      <div class="log-source">日志来源：${escapeHtml(d.source || '未找到日志文件')}${d.error ? ' · ' + escapeHtml(d.error) : ''}</div>
    </div>`;
  renderActivityRows();
}

/* ── Batch Push Unload Guard ────────────────────────────── */
window.addEventListener('beforeunload', function(e) {
  if (batchPushState && batchPushState.running) {
    e.preventDefault();
    e.returnValue = '批量推送正在进行中，离开页面将中断推送。确定要离开吗？';
    return e.returnValue;
  }
});

// Override navigate to warn during batch push
const _originalNavigate = navigate;
navigate = async function(page) {
  if (batchPushState && batchPushState.running) {
    if (!(await uiConfirm('批量推送正在进行中，切换页面将丢失推送进度。\n\n确定要离开吗？'))) return;
    resetBatchState();
  }
  _originalNavigate(page);
};

