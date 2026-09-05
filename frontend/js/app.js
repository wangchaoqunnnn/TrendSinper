/* app.js —— TrendSniper 前端逻辑（数据源固定实时；所有表格支持点击表头排序并带 No 列） */
"use strict";

const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));

/* 所有 API 一律使用相对地址（兼容部署在任意子路径/端口） */
const api = async (path, opts) => {
  const rel = path.startsWith("/") ? path.slice(1) : path;
  const res = await fetch(rel, Object.assign({
    headers: { "Content-Type": "application/json" },
  }, opts || {}));
  return res.json();
};

function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), ms || 3200);
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function pct(x, nd) { return fmtPct(x, nd); }
function num(x, nd) { return fmtNum(x, nd); }
function cls(x) { return x > 0 ? "up" : (x < 0 ? "down" : ""); }
function signedPct(x, nd) {
  if (x === null || x === undefined || isNaN(x)) return "—";
  const s = (x * 100).toFixed(nd === undefined ? 2 : nd);
  return (x > 0 ? "+" : "") + s + "%";
}

const BOARD_CLS = { "沪主板": "c-沪主板", "深主板": "c-深主板", "创业板": "c-创业板", "科创板": "c-科创板", "北证": "c-北证" };
function boardChip(b) {
  const color = { "沪主板": "#4aa3ff", "深主板": "#7c6cff", "创业板": "#ff9f43", "科创板": "#ff6ea9", "北证": "#9adcff" }[b] || "#aaa";
  return `<span class="chip" style="color:${color};border-color:${color}55">${esc(b)}</span>`;
}

/* =====================================================================
 * 通用可排序表格：点击表头排序（再次点击反向），首列自动为 No 编号
 * cols: [{ key, label, num?, sort?(item)=>number|string }]
 * items: 数据对象数组；cell(item) 返回与 cols 等长的 HTML 片段数组
 * ===================================================================== */
const SORT_STATE = new WeakMap();

function makeSortableTable(container, cols, items, cell) {
  const state = SORT_STATE.get(container) || { si: null, asc: true };
  SORT_STATE.set(container, state);

  function raw(item, col) {
    const v = col.sort ? col.sort(item) : (col.key != null ? item[col.key] : null);
    return v;
  }
  function cmp(a, b) {
    const x = raw(a, cols[state.si]);
    const y = raw(b, cols[state.si]);
    const nx = (typeof x === "number") ? x : parseFloat(String(x).replace(/[%+,]/g, ""));
    const ny = (typeof y === "number") ? y : parseFloat(String(y).replace(/[%+,]/g, ""));
    const xn = isFinite(nx), yn = isFinite(ny);
    if (xn && yn) return nx - ny;
    if (xn !== yn) return xn ? 1 : -1;          // 数字排前，非数字排后
    return String(x == null ? "" : x).localeCompare(String(y == null ? "" : y), "zh-Hans-CN");
  }
  function render() {
    const arr = items.slice();
    if (state.si !== null) {
      arr.sort(cmp);
      if (!state.asc) arr.reverse();
    }
    const head = `<tr><th class="col-no">No</th>` + cols.map((c, i) =>
      `<th class="${c.num ? "num" : ""}" data-i="${i}">${esc(c.label)}` +
      (state.si === i ? (state.asc ? " <span class='arrow'>▲</span>" : " <span class='arrow'>▼</span>") : "") +
      `</th>`).join("") + "</tr>";
    const body = arr.map((it, idx) => `<tr><td class="col-no">${idx + 1}</td>` +
      (cell(it, idx).map(h => `<td>${h}</td>`).join("")) + "</tr>").join("");
    container.innerHTML = `<div class="tablewrap"><table>${head}<tbody>${body}</tbody></table></div>`;
  }
  if (!container._bound) {
    container._bound = true;
    container.addEventListener("click", e => {
      const th = e.target.closest("th[data-i]");
      if (!th) return;
      const i = Number(th.dataset.i);
      state.asc = (state.si === i) ? !state.asc : true;
      state.si = i;
      render();
    });
  }
  render();
  return container;
}

function emptyTip(container, text) {
  container.innerHTML = `<div class="empty">${esc(text || "无数据")}</div>`;
}

/* ---------------- 页面切换 ---------------- */
$$(".tab").forEach(t => t.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  $$(".tabpage").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("#tab-" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "backtest") loadBacktest();
  if (t.dataset.tab === "tune") loadTune();
  if (t.dataset.tab === "paper") loadPaper();
  if (t.dataset.tab === "about") loadAbout();
  if (t.dataset.tab === "picks") loadPicks();
}));

/* ---------------- 全局状态 ---------------- */
let G = { status: null };

async function loadStatus() {
  G.status = await api("api/status");
  const st = G.status;
  $("#topActions").innerHTML = `
    <span class="chip hl" title="新浪 / 腾讯 / 东方财富 实时行情">数据源：${esc(st.data_source_label)}</span>
    <span class="chip">策略 ${esc(st.params.version)}${st.params.tuned_at ? " · " + esc(st.params.tuned_at.slice(0, 16)) : ""}</span>
    <button class="btn" id="btnResetCache" title="清空行情缓存并重新拉取最新实时数据">刷新实时数据</button>`;
  const btn = $("#btnResetCache");
  if (btn) btn.addEventListener("click", async () => {
    toast("已请求刷新实时数据（清缓存）…", 3000);
    await api("api/settings", { method: "POST", body: "{}" });
    setTimeout(loadAll, 800);
  });
}

async function loadAll() {
  await loadStatus();
  loadPicks();
}

/* ---------------- 任务轮询 ---------------- */
let pollTimer = null;
function watchJob(jobId, doneCb) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const j = await api("api/jobs/" + jobId);
    if (!j) return;
    const box = $("#jobProbe");
    if (box) box.innerHTML = `<div class="statusbar"><span>⏳ 任务[${esc(j.kind)}] ${(j.progress * 100).toFixed(0)}%</span>
      <span class="src">${esc(j.message || "")}</span></div>`;
    if (j.status === "done" || j.status === "failed") {
      clearInterval(pollTimer);
      if (box) box.innerHTML = "";
      if (j.status === "failed") toast("任务失败：" + (j.message || ""), 6000);
      doneCb && doneCb(j);
    }
  }, 1000);
}

/* ---------------- 今日选股 ---------------- */
async function loadPicks() {
  const st = G.status;
  if (st) {
    $("#picksStatus").innerHTML = `
      <span>数据源：<b class="src">${esc(st.data_source_label)}</b></span>
      <span>策略：<b>${esc(st.params.version)}</b></span>
      <span>最近选股：${esc(st.last_screen || "尚未运行")}</span>
      <span>入选：<b>${st.picks_count}</b> 只</span>`;
  }
  const d = await api("api/picks");
  const meta = d.meta || {};
  const picks = d.picks || [];
  const mt = $("#picksMeta");
  if (meta.empty) {
    mt.innerHTML = "尚未生成选股结果 —— 点击右上“⚡ 立即选股”（首次拉取全市场实时行情，约需 1-2 分钟）。";
  } else if (meta.date) {
    const relaxed = meta.relaxed ? `<span class="chip warn">已自动放宽门槛至 ${meta.threshold} 分</span>` : "";
    mt.innerHTML = `信号日 <b>${esc(meta.date)}</b>（收盘后）· 候选 ${meta.counts.candidate} / 有K线 ${meta.counts.with_kline} 只 · 入选 ${picks.length} 只 ${relaxed}
      <span class="chip">策略版本 ${esc(meta.params_version || "")}</span>`;
  }
  $("#picksList").innerHTML = picks.length
    ? picks.map(pickCard).join("")
    : `<div class="empty">${meta.empty ? meta.msg : "当前无满足条件的趋势票"}</div>`;
}

function pickCard(p) {
  const stopStatus = p.status_now ? `<span class="chip ${p.status_now === "跌破止损位" ? "warn" : "hl"}">${esc(p.status_now)}</span>` : "";
  const reasons = (p.reasons || []).slice(0, 7).map(r =>
    `<li class="${r.startsWith("△") ? "miss" : ""}">${esc(r)}</li>`).join("");
  const f = p.fund || {};
  const fundTxt = (f.roe !== undefined && f.roe !== null)
    ? `<span class="chip">ROE ${num(f.roe, 1)}%</span><span class="chip">净利同比 ${f.np_yoy != null ? signedPct(f.np_yoy / 100, 0) : "—"}</span>`
    : "";
  const q = p.quote;
  const curHtml = q
    ? `<b class="${cls(q.pct)}">${num(q.price)}（${signedPct(q.pct)}）</b>`
    : `<b>${num(p.price)}</b>`;
  return `
  <div class="card ${BOARD_CLS[p.board] || ""}">
    <div class="card-head">
      <h2>${esc(p.name)}<span class="code">${esc(p.code)}</span></h2>
      <span class="score"><small>评分</small>${num(p.score, 1)}</span>
    </div>
    <div class="chips">
      ${boardChip(p.board)}
      <span class="chip">${esc(p.industry)}</span>
      ${p.new_high ? `<span class="chip hl">${p.new_high >= 250 ? "历史新高" : "近" + p.new_high + "日新高"}</span>` : ""}
      ${fundTxt}${stopStatus}
    </div>
    <div class="price-row">
      <div class="kv"><span>信号价(买入参考)</span><b>${num(p.entry_price)}</b></div>
      <div class="kv"><span>现价</span>${curHtml}</div>
      <div class="kv"><span>近一月涨幅</span><b class="${p.pct_1m >= 0 ? "up" : "down"}">${p.pct_1m != null ? signedPct(p.pct_1m / 100) : "—"}</b></div>
      <div class="kv"><span>MA20</span><b>${p.ma20 != null ? num(p.ma20) : "—"}</b></div>
      <div class="kv"><span>流通市值</span><b>${num(p.float_mv, 0)}亿</b></div>
    </div>
    <ul class="reasons">${reasons}</ul>
    <div class="stopbox">
      <span>🛑 止损价 <b>${num(p.stop_price)}</b></span>
      <span style="color:var(--muted)">${esc(p.stop_why || "")}</span>
    </div>
  </div>`;
}

$("#btnScreen").addEventListener("click", async () => {
  $("#btnScreen").disabled = true;
  toast("开始实时选股，请稍候（拉取全市场K线）…", 4000);
  const j = await api("api/picks/refresh", { method: "POST", body: "{}" });
  watchJob(j.job_id, () => { $("#btnScreen").disabled = false; loadAll(); });
});
$("#btnRefreshQuotes").addEventListener("click", () => { loadPicks(); toast("已刷新实时现价"); });

/* ---------------- 回测与统计 ---------------- */
async function loadBacktest() {
  const bt = await api("api/backtest/latest");
  const box = $("#btStatus");
  if (!bt || bt.none) {
    box.innerHTML = `<span>尚无回测报告 —— 点击“▶ 运行回测”（首次实时数据需先缓存K线）。系统每周自动回测。</span>`;
    return;
  }
  const s = bt.stats || {};
  box.innerHTML = `回测区间 <b>${esc((bt.period || []).join(" ~ "))}</b>
    · 宇宙 <b>${bt.universe}</b> 只 · 信号日 <b>${bt.screened_days}</b> 个
    · 耗时 ${bt.elapsed_s}s · <span class="src">策略 ${esc(bt.params_version)}</span>`;
  $("#btVersion").textContent = bt.param_summary || "";
  $("#btStats").innerHTML = kpis([
    ["总交易", s.n_trades, "笔"], ["成功率", pct(s.win_rate, 1), "盈利笔数/总笔数"],
    ["盈亏比", s.payoff != null ? num(s.payoff) : "∞", "平均盈利/平均亏损"],
    ["利润因子", s.profit_factor != null ? num(s.profit_factor) : "∞", "总盈利/总亏损"],
    ["每笔期望", signedPct(s.expectancy / 100 || 0, 2), "含双边成本"],
    ["区间收益", signedPct(s.total_return, 1), "等权名义仓位示意"],
    ["最大回撤", pct(s.max_drawdown, 1), ""], ["平均持有", s.avg_hold_days, "交易日"],
  ]);
  drawLineChart($("#equityChart"), bt.equity || []);
  renderBoardTables(bt.by_board, bt.by_reason);
  const trades = bt.trades || [];
  renderTrades(trades);
  $("#btCaveat").innerHTML = "⚠ " + esc(bt.caveat || "");
}

function kpis(items) {
  return items.map(([k, v, s]) => `<div class="kpi"><div class="k">${k}</div>
    <div class="v">${esc(v)}</div>
    <div class="s">${esc(s || "")}</div></div>`).join("");
}

function renderBoardTables(byBoard, byReason) {
  const boardItems = Object.entries(byBoard || {}).map(([k, v]) => ({ k, ...v }));
  if (boardItems.length) {
    makeSortableTable($("#btByBoard"), [
      { key: "k", label: "板块" }, { key: "n", label: "笔数", num: true },
      { key: "win_rate", label: "胜率", num: true }, { key: "payoff", label: "盈亏比", num: true },
    ], boardItems, it => [
      esc(it.k),
      `<span class="num">${it.n}</span>`,
      `<span class="num">${pct(it.win_rate, 1)}</span>`,
      `<span class="num">${it.payoff != null ? num(it.payoff) : "∞"}</span>`,
    ]);
  } else emptyTip($("#btByBoard"), "无数据");

  const reasonItems = Object.entries(byReason || {}).map(([k, v]) => ({ k, ...v }));
  if (reasonItems.length) {
    makeSortableTable($("#btByReason"), [
      { key: "k", label: "离场原因" }, { key: "n", label: "笔数", num: true },
      { key: "win_rate", label: "胜率", num: true }, { key: "payoff", label: "盈亏比", num: true },
    ], reasonItems, it => [
      esc(it.k),
      `<span class="num">${it.n}</span>`,
      `<span class="num">${pct(it.win_rate, 1)}</span>`,
      `<span class="num">${it.payoff != null ? num(it.payoff) : "∞"}</span>`,
    ]);
  } else emptyTip($("#btByReason"), "无数据");
}

function renderTrades(trades) {
  if (!trades || !trades.length) return emptyTip($("#btTrades"), "暂无交易明细");
  makeSortableTable($("#btTrades"), [
    { key: "code", label: "代码" }, { key: "name", label: "名称" },
    { key: "board", label: "板块" }, { key: "industry", label: "行业" },
    { key: "entry_date", label: "买入日" }, { key: "entry_price", label: "买入价", num: true },
    { key: "stop_price", label: "止损价", num: true }, { key: "exit_date", label: "卖出日" },
    { key: "exit_price", label: "卖出价", num: true }, { key: "exit_reason", label: "离场原因" },
    { key: "ret", label: "收益率", num: true }, { key: "hold_days", label: "持有(日)", num: true },
  ], trades.slice().reverse(), t => [
    esc(t.code), esc(t.name), esc(t.board), esc(t.industry || ""),
    esc(t.entry_date), `<span class="num">${num(t.entry_price)}</span>`,
    `<span class="num">${num(t.stop_price)}</span>`, esc(t.exit_date),
    `<span class="num">${num(t.exit_price)}</span>`, esc((t.exit_reason || "").slice(0, 14)),
    `<span class="num ${cls(t.ret)}">${signedPct(t.ret, 2)}</span>`,
    `<span class="num">${t.hold_days != null ? t.hold_days : "—"}</span>`,
  ]);
}

$("#btnBacktest").addEventListener("click", async () => {
  $("#btnBacktest").disabled = true;
  toast("回测进行中（首次实时数据需缓存约 240 只K线）…", 5000);
  const j = await api("api/backtest/run", { method: "POST", body: "{}" });
  watchJob(j.job_id, () => { $("#btnBacktest").disabled = false; loadBacktest(); });
});

/* ---------------- 策略自调优 ---------------- */
async function loadTune() {
  const params = await api("api/params");
  const hist = await api("api/tune/history");
  $("#paramsVer").textContent = " v" + (params.version || "") +
    (params.tuned_at ? "（调优于 " + params.tuned_at.slice(0, 16) + "）" : "（初始参数）");
  const r = params.rules || {};
  const w = params.weights || {};
  const rk = params.risk || {};
  const sc = params.screen || {};
  const fund = params.fundamental || {};
  const weightItems = Object.entries(w).map(([k, v]) => ({ k, v }));
  $("#paramsView").innerHTML = `<div class="params"><table>
    <tr><td>创新高窗口</td><td>${(r.new_high || {}).windows.join(" / ")} 日（≥250 视为历史新高）</td></tr>
    <tr><td>均线多头</td><td>价 &gt; MA20 &gt; MA60 &gt; MA120，MA60 上行确认 ${(r.ma_bull || {}).mid_rising_days} 日</td></tr>
    <tr><td>近一月涨幅区间</td><td>${((r.month_ret || {}).min * 100).toFixed(0)}% ~ ${((r.month_ret || {}).max * 100).toFixed(0)}%</td></tr>
    <tr><td>换手率适中</td><td>${((r.turnover || {}).min * 100).toFixed(1)}% ~ ${((r.turnover || {}).max * 100).toFixed(0)}%（10日均）</td></tr>
    <tr><td>回调低点抬高</td><td>近10日低点 &gt; 前20日低点</td></tr>
    <tr><td>量价配合</td><td>上涨温和放量 / 回调明显缩量</td></tr>
    <tr><td>行业景气</td><td>板块≥${(r.sector || {}).min_members}只，≥${((r.sector || {}).min_frac_above_ma60 * 100).toFixed(0)}%站上MA60，20日板块上行</td></tr>
    <tr><td>基本面</td><td>ROE≥${fund.min_roe}% · 净利同比≥${fund.min_np_yoy}% · 营收同比≥${fund.min_rev_yoy}%（数据缺失时不硬卡）</td></tr>
    <tr><td>止损价</td><td>max(MA20×98%、近10日低点×98%、买入价×(1-${(rk.stop_pct * 100).toFixed(0)}%))</td></tr>
    <tr><td>破位离场</td><td>收盘跌破MA20 或 止损价 或 持有${rk.timeout_days}日未再上攻</td></tr>
    <tr><td>入选门槛/只数</td><td>${sc.min_score}分 · 最多${sc.top_n}只（单板块≤50%）</td></tr>
    </table></div>
    <h3 style="margin-top:12px">维度权重（点击表头可排序）</h3>
    <div id="weightsView"></div>`;
  makeSortableTable($("#weightsView"), [
    { key: "k", label: "维度" }, { key: "v", label: "权重", num: true },
  ], weightItems, it => [esc(dimName(it.k)), `<span class="num">${num(it.v * 100, 0)}%</span>`]);

  const items = (hist.items || []).slice().reverse();
  if (!items.length) {
    $("#tuneHistory").innerHTML = `<div class="empty">尚无调优记录 —— 系统每周自动执行，也可手动触发</div>`;
    return;
  }
  makeSortableTable($("#tuneHistory"), [
    { key: "at", label: "时间" }, { key: "method", label: "方式" },
    { key: "trades", label: "样本(笔)", num: true, sort: h => (h.stats || {}).n_trades },
    { key: "wr", label: "胜率", num: true, sort: h => (h.stats || {}).win_rate },
    { key: "payoff", label: "盈亏比", num: true, sort: h => (h.stats || {}).payoff },
    { key: "note", label: "决策说明" },
  ], items, h => {
    const s = h.stats || {};
    return [
      esc(h.at || ""), esc(h.method || ""),
      `<span class="num">${s.n_trades != null ? s.n_trades : "—"}</span>`,
      `<span class="num">${pct(s.win_rate, 1)}</span>`,
      `<span class="num">${s.payoff != null ? num(s.payoff) : "∞"}</span>`,
      esc((h.note || "").slice(0, 200)),
    ];
  });
}

function dimName(k) {
  return { sector: "行业景气(自上而下)", fundamental: "基本面", ma_bull: "均线多头排列",
    new_high: "创阶段新高", month_ret: "月涨幅适中", turnover: "换手适中",
    volume: "量价配合", relative_strength: "相对强度", anti_weak: "大盘弱势抗跌",
    pullback_rising: "回调低点抬高" }[k] || k;
}

$("#btnTune").addEventListener("click", async () => {
  $("#btnTune").disabled = true;
  toast("自调优开始：训练窗随机搜索→验证窗确认（实时模式约 5-15 分钟）…", 8000);
  const j = await api("api/tune/run", { method: "POST", body: JSON.stringify({ combos: 16 }) });
  watchJob(j.job_id, () => { $("#btnTune").disabled = false; loadStatus(); loadTune(); });
});

/* ---------------- 虚拟持仓跟踪 ---------------- */
async function loadPaper() {
  const d = await api("api/paper");
  const s = d.stats || {};
  $("#paperStatus").innerHTML = `<span>虚拟盘：持仓 <b>${s.n_open || 0}</b> · 已平仓 <b>${s.n_trades || 0}</b> 笔（样本随每日盯盘增长）</span>`;
  $("#paperStats").innerHTML = kpis([
    ["已平仓", s.n_trades || 0, "笔"], ["成功率", pct(s.win_rate, 1), ""],
    ["盈亏比", s.payoff != null ? num(s.payoff) : "—", "样本尚少时参考意义有限"],
  ]);
  const open = d.positions || [];
  if (open.length) {
    makeSortableTable($("#paperOpen"), [
      { key: "code", label: "代码" }, { key: "name", label: "名称" },
      { key: "board", label: "板块" }, { key: "entry_date", label: "买入日" },
      { key: "entry_price", label: "买入价", num: true }, { key: "stop_price", label: "止损价", num: true },
      { key: "current_price", label: "最新", num: true }, { key: "pct", label: "浮盈", num: true },
    ], open, p => [
      esc(p.code), esc(p.name), esc(p.board), esc(p.entry_date),
      `<span class="num">${num(p.entry_price)}</span>`,
      `<span class="num" style="color:var(--up)">${num(p.stop_price)}</span>`,
      `<span class="num">${p.current_price != null ? num(p.current_price) : "—"}</span>`,
      `<span class="num ${cls(p.pct)}">${p.pct != null ? signedPct(p.pct, 2) : "—"}</span>`,
    ]);
  } else emptyTip($("#paperOpen"), "暂无持仓（执行选股后自动建立）");
  const closed = (d.closed || []).slice().reverse();
  if (closed.length) {
    makeSortableTable($("#paperClosed"), [
      { key: "code", label: "代码" }, { key: "name", label: "名称" }, { key: "board", label: "板块" },
      { key: "entry_date", label: "买入日" }, { key: "entry_price", label: "买入价", num: true },
      { key: "exit_price", label: "卖出价", num: true }, { key: "exit_reason", label: "离场原因" },
      { key: "ret", label: "收益", num: true },
    ], closed, p => [
      esc(p.code), esc(p.name), esc(p.board), esc(p.entry_date),
      `<span class="num">${num(p.entry_price)}</span>`,
      `<span class="num">${num(p.exit_price)}</span>`,
      esc((p.exit_reason || "").slice(0, 14)),
      `<span class="num ${cls(p.ret)}">${signedPct(p.ret, 2)}</span>`,
    ]);
  } else emptyTip($("#paperClosed"), "暂无平仓记录");
}

/* ---------------- 策略说明 ---------------- */
async function loadAbout() {
  const d = await api("api/strategy-text");
  $("#strategyText").textContent = d.text || "（未找到 趋势策略.md）";
  const map = [
    ["第一步 · 自上而下找赛道", "行业景气度被量化为 sector 规则：行业内≥4只样本、≥30%站上MA60、20日板块均涨≥2% 视为“赛道景气”（行业归属来自最新报告期财报接口）。"],
    ["第二步 · 基本面筛选", "fundamental：ROE≥8%、归母净利同比≥8%、营收同比≥0%（东方财富最新报告期全市场自动抓取；缺数据时不硬拦，仅少得该维度分）。"],
    ["第三步 · 技术面确认", "ma_bull 均线多头排列（价>MA20>MA60>MA120 且 MA60 上行）；new_high 创60/120/250日新高；pullback_rising 回调低点抬高；volume 上涨温和放量/回调缩量；month_ret 月涨幅区间过滤；relative_strength 相对沪深300强度；anti_weak 大盘下跌日抗跌。"],
    ["第四步 · 实战选出", "加权打分排序 + 入选门槛 + 单板块50%上限，覆盖主板/创业板/科创板/北证；分数不足时自动放宽并明确标注。"],
    ["风险纪律", "止损价 = max(MA20×98%、近10日低点×98%、买入价×(1-止损比例))；破位坚决跑：跌破止损价/MA20、持有超时即离场。"],
    ["成功率与盈亏比", "历史回测 + 虚拟持仓簿双口径：胜率=盈利笔数/总笔数，盈亏比=平均盈利/平均亏损，另附利润因子、每笔期望、最大回撤与资金曲线。"],
    ["自动回测与自调优", "调度器每周自动执行：训练窗随机搜索参数→验证窗确认→只有验证窗更优才采纳并升级版本号（防过拟合）；每次决策（含“维持不变”）都会写入调优历史。"],
    ["数据说明", "本系统只使用实时行情数据（新浪/腾讯/东方财富），不包含任何模拟数据。"],
  ];
  $("#mappingDoc").innerHTML = map.map(([t, c]) =>
    `<div class="mapping-item"><b>${esc(t)}</b><div style="color:var(--muted)">${esc(c)}</div></div>`).join("");
}

/* ---------------- 启动 ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  loadAll();
  setInterval(() => loadStatus(), 60000);
});
