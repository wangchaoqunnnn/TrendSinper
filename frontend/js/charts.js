/* charts.js —— 零依赖 Canvas 图表 */
function fmtPct(x, nd) {
  if (x === null || x === undefined || isNaN(x)) return "—";
  return (x * 100).toFixed(nd === undefined ? 1 : nd) + "%";
}
function fmtNum(x, nd) {
  if (x === null || x === undefined || isNaN(x)) return "—";
  return Number(x).toFixed(nd === undefined ? 2 : nd);
}

function drawLineChart(canvas, points, opts) {
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  const W = Math.max(rect.width - 20, 320);
  const H = 260;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const padL = 64, padR = 18, padT = 18, padB = 36;
  const iw = W - padL - padR, ih = H - padT - padB;
  const data = (points || []).filter(p => p && isFinite(p.value));
  if (data.length < 2) {
    ctx.fillStyle = "#8b97bd"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("暂无数据", W / 2, H / 2);
    return;
  }
  let min = Math.min(...data.map(p => p.value));
  let max = Math.max(...data.map(p => p.value));
  const span = (max - min) || max * 0.1 || 1;
  min -= span * 0.12; max += span * 0.12;
  const X = i => padL + (i / (data.length - 1)) * iw;
  const Y = v => padT + ih - ((v - min) / (max - min)) * ih;

  // 网格 + 纵轴标签（右对齐到轴线，避免与网格线重叠）
  ctx.font = "11px sans-serif"; ctx.fillStyle = "#8b97bd";
  ctx.strokeStyle = "rgba(38,51,92,.6)"; ctx.lineWidth = 1;
  ctx.textAlign = "right";
  const nGrid = 4;
  for (let g = 0; g <= nGrid; g++) {
    const v = min + (max - min) * g / nGrid;
    const y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(v >= 100 ? v.toFixed(0) : v.toFixed(2), padL - 8, y + 4);
  }
  // 基准线 1.0
  if (min < 1 && max > 1) {
    ctx.strokeStyle = "rgba(245,196,83,.5)"; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, Y(1)); ctx.lineTo(W - padR, Y(1)); ctx.stroke();
    ctx.setLineDash([]);
  }
  // X 轴标签（居中、彼此留白）
  ctx.textAlign = "center";
  const tickN = Math.min(6, data.length);
  for (let i = 0; i < tickN; i++) {
    const idx = Math.round(i * (data.length - 1) / (tickN - 1));
    ctx.fillText((data[idx].date || "").slice(5), X(idx), H - 14);
  }
  // 折线
  const grad = ctx.createLinearGradient(0, padT, 0, H - padB);
  grad.addColorStop(0, "rgba(91,140,255,.35)");
  grad.addColorStop(1, "rgba(91,140,255,.02)");
  ctx.beginPath();
  data.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p.value)) : ctx.moveTo(X(0), Y(p.value))));
  ctx.strokeStyle = opts.color || "#5b8cff"; ctx.lineWidth = 2;
  ctx.stroke();
  ctx.lineTo(X(data.length - 1), H - padB); ctx.lineTo(X(0), H - padB); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
}

function drawDonut(canvas, items, opts) {
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  const W = Math.max(rect.width - 20, 320);
  canvas.width = W * dpr; canvas.height = 120 * dpr;
  canvas.style.height = "120px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const cx = W / 2, cy = 60, R = 46;
  const total = items.reduce((s, it) => s + it.value, 0);
  if (!total) { ctx.fillStyle = "#8b97bd"; ctx.textAlign = "center"; ctx.font = "13px sans-serif"; ctx.fillText("无数据", cx, cy + 4); return; }
  let a0 = -Math.PI / 2;
  const palette = ["#5b8cff", "#36d399", "#f5c453", "#ff6ea9", "#9adcff", "#ff9f43", "#7c6cff", "#5ad1d1"];
  const legend = [];
  items.forEach((it, i) => {
    const a1 = a0 + it.value / total * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, R, a0, a1);
    ctx.closePath();
    ctx.fillStyle = palette[i % palette.length];
    ctx.fill();
    legend.push('<span style="color:' + palette[i % palette.length] + '">■</span> ' + it.label + " " + it.value + "笔");
    a0 = a1;
  });
  ctx.fillStyle = "#e6ecff"; ctx.textAlign = "center"; ctx.font = "bold 14px sans-serif";
  ctx.fillText(total + " 笔", cx, cy + 5);
  return legend;
}
