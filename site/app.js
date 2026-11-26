async function fetchJson(path, fallback) {
  try {
    const resp = await fetch(`${path}?t=${Date.now()}`);
    if (!resp.ok) return fallback;
    return await resp.json();
  } catch {
    return fallback;
  }
}

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return Number(v).toLocaleString("zh-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pnlClass(v) {
  const n = Number(v);
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "";
}

function dirText(side) {
  if (side === "long") return '<span class="pos">做多</span>';
  if (side === "short") return '<span class="neg">做空</span>';
  return side || "-";
}

function drawEquityChart(points) {
  const canvas = document.getElementById("equity-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.parentElement.clientWidth || 800;
  const height = 260;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, width, height);

  if (!points || points.length === 0) {
    ctx.fillStyle = "#8b949e";
    ctx.font = "14px sans-serif";
    ctx.fillText("尚無權益歷史資料", 20, height / 2);
    return;
  }

  const padL = 64, padR = 20, padT = 20, padB = 30;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const vals = points.map((p) => Number(p.adjusted_equity ?? p.equity));
  let minV = Math.min(...vals);
  let maxV = Math.max(...vals);
  if (minV === maxV) {
    minV -= 10;
    maxV += 10;
  } else {
    const pad = (maxV - minV) * 0.1;
    minV -= pad;
    maxV += pad;
  }

  // Grid lines
  ctx.strokeStyle = "#2a313c";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#8b949e";
  ctx.font = "11px sans-serif";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH * i) / 4;
    const val = maxV - ((maxV - minV) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(width - padR, y);
    ctx.stroke();
    ctx.fillText(fmtNum(val, 0), 8, y + 4);
  }

  // Equity line
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((pt, idx) => {
    const x = points.length === 1
      ? padL + plotW / 2
      : padL + (plotW * idx) / (points.length - 1);
    const val = Number(pt.adjusted_equity ?? pt.equity);
    const y = padT + plotH * (1 - (val - minV) / (maxV - minV));
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Annotate deposit/withdrawal points
  points.forEach((pt, idx) => {
    const cf = Number(pt.cash_flow || 0);
    if (cf !== 0) {
      const x = points.length === 1 ? padL + plotW / 2 : padL + (plotW * idx) / (points.length - 1);
      const val = Number(pt.adjusted_equity ?? pt.equity);
      const y = padT + plotH * (1 - (val - minV) / (maxV - minV));
      ctx.fillStyle = cf > 0 ? "#3fb950" : "#f85149";
      ctx.beginPath();
      ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fill();
    }
  });

  // X labels
  ctx.fillStyle = "#8b949e";
  ctx.fillText(points[0].time_tpe || "", padL, height - 8);
  if (points.length > 1) {
    const lastLabel = points[points.length - 1].time_tpe || "";
    const textW = ctx.measureText(lastLabel).width;
    ctx.fillText(lastLabel, width - padR - textW, height - 8);
  }
}

async function renderDashboard() {
  const [summary, equity, strategies, positions, trades, dryOrders] = await Promise.all([
    fetchJson("data/summary.json", null),
    fetchJson("data/equity.json", []),
    fetchJson("data/strategies.json", []),
    fetchJson("data/positions.json", []),
    fetchJson("data/trades.json", []),
    fetchJson("data/dry_run_orders.json", []),
  ]);

  if (summary) {
    document.getElementById("mode-badge").textContent = summary.mode || "-";
    document.getElementById("total-equity").textContent = `${fmtNum(summary.total_equity_usdt)} USDT`;
    document.getElementById("initial-equity").textContent = `${fmtNum(summary.initial_equity_usdt)} USDT`;
    document.getElementById("net-deposits").textContent = `${fmtNum(summary.net_deposits_usdt ?? 0)} USDT`;

    const retEl = document.getElementById("total-return");
    retEl.textContent = `${summary.total_return_pct >= 0 ? "+" : ""}${fmtNum(summary.total_return_pct)}%`;
    retEl.className = `card-value ${pnlClass(summary.total_return_pct)}`;

    document.getElementById("max-dd").textContent = `-${fmtNum(summary.max_drawdown_pct)}%`;
    document.getElementById("open-count").textContent = String(summary.open_positions_count ?? 0);
    document.getElementById("last-updated").textContent = summary.last_updated_tpe || "-";

    const run = summary.latest_run || {};
    const errs = run.errors || [];
    const statusHtml = `
      <div>執行時間：${run.time_tpe || "-"} ｜ 4H 新收盤：${run.new_4h_closed ? "是" : "否"} ｜ 日線新收盤：${run.new_1d_closed ? "是" : "否"} ｜ 對帳平倉：${run.reconciled_closed || 0} 筆</div>
      ${errs.length ? `<ul class="error-list">${errs.map((e) => `<li>${e}</li>`).join("")}</ul>` : `<div style="margin-top:6px;color:#3fb950;">本次執行無錯誤</div>`}
    `;
    document.getElementById("run-status").innerHTML = statusHtml;

    const cfs = summary.cash_flows || [];
    if (cfs.length > 0) {
      document.getElementById("cash-flow-panel").style.display = "block";
      document.getElementById("cash-flow-body").innerHTML = cfs.map((c) => `
        <tr>
          <td>${c.time_tpe}</td>
          <td class="${c.amount_usdt >= 0 ? "pos" : "neg"}">${c.type}</td>
          <td class="${c.amount_usdt >= 0 ? "pos" : "neg"}">${c.amount_usdt >= 0 ? "+" : ""}${fmtNum(c.amount_usdt)}</td>
          <td>${c.source}</td>
          <td class="wrap">${c.note}</td>
        </tr>
      `).join("");
    }
  }

  drawEquityChart(equity);
  window.addEventListener("resize", () => drawEquityChart(equity));

  const stratBody = document.getElementById("strategies-body");
  stratBody.innerHTML = strategies.length
    ? strategies.map((s) => `
      <tr>
        <td><strong>${s.strategy}</strong></td>
        <td>${fmtNum(s.allocation_pct, 0)}%</td>
        <td>${fmtNum(s.equity)}</td>
        <td class="${pnlClass(s.realized_pnl)}">${s.realized_pnl >= 0 ? "+" : ""}${fmtNum(s.realized_pnl)}</td>
        <td>${s.trades_count}</td>
        <td>${fmtNum(s.win_rate_pct, 1)}%</td>
        <td>${fmtNum(s.profit_factor)}</td>
        <td>${fmtNum(s.avg_r)} R</td>
        <td>-${fmtNum(s.max_drawdown_pct)}%</td>
      </tr>
    `).join("")
    : '<tr><td colspan="9">尚無策略資料</td></tr>';

  const posBody = document.getElementById("positions-body");
  posBody.innerHTML = positions.length
    ? positions.map((p) => `
      <tr>
        <td><strong>${p.strategy}</strong></td>
        <td>${p.inst_id}</td>
        <td>${dirText(p.pos_side)}</td>
        <td>${p.entry_price}</td>
        <td>${p.current_price}</td>
        <td>${p.sz}</td>
        <td>${p.stop_loss}</td>
        <td class="${pnlClass(p.unrealized_pnl)}">${p.unrealized_pnl >= 0 ? "+" : ""}${fmtNum(p.unrealized_pnl)}</td>
        <td>${p.bars_held}</td>
        <td class="wrap">${p.exit_rule_desc}</td>
      </tr>
    `).join("")
    : '<tr><td colspan="10">目前無持倉</td></tr>';

  if (dryOrders && dryOrders.length > 0) {
    document.getElementById("dry-run-panel").style.display = "block";
    document.getElementById("dry-run-body").innerHTML = dryOrders.map((o) => `
      <tr>
        <td>${o.time_tpe}</td>
        <td>${o.action}</td>
        <td><strong>${o.strategy}</strong></td>
        <td>${o.inst_id}</td>
        <td>${dirText(o.pos_side)}</td>
        <td>${o.sz}</td>
        <td>${o.ref_price}</td>
        <td>${o.stop_loss}</td>
        <td class="wrap">${o.reason}</td>
      </tr>
    `).join("");
  }

  const tradesBody = document.getElementById("trades-body");
  tradesBody.innerHTML = trades.length
    ? trades.map((t) => `
      <tr>
        <td><strong>${t.strategy}</strong></td>
        <td>${t.inst_id}</td>
        <td>${dirText(t.pos_side)}</td>
        <td>${t.entry_time_tpe}</td>
        <td>${t.exit_time_tpe}</td>
        <td>${t.entry_price}</td>
        <td>${t.exit_price}</td>
        <td class="${pnlClass(t.pnl_usdt)}">${t.pnl_usdt >= 0 ? "+" : ""}${fmtNum(t.pnl_usdt)}</td>
        <td class="${pnlClass(t.r_multiple)}">${fmtNum(t.r_multiple)} R</td>
        <td class="wrap">${t.exit_reason}</td>
      </tr>
    `).join("")
    : '<tr><td colspan="10">尚無已平倉交易</td></tr>';
}

renderDashboard();
