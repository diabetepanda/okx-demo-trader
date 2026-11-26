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

function shortTimeLabel(pt) {
  if (pt.time_short_tpe) return pt.time_short_tpe;
  const full = pt.time_tpe || "";
  // "YYYY-MM-DD HH:mm" -> "MM/DD HH:mm"
  const m = full.match(/^\d{4}-(\d{2})-(\d{2})\s+(\d{2}:\d{2})/);
  return m ? `${m[1]}/${m[2]} ${m[3]}` : full;
}

function computeTwrValues(points) {
  if (!points || !points.length) return [];
  const base = Number(points[0].adjusted_equity ?? points[0].equity ?? 1);
  return points.map((p) => {
    if (p.twr_pct !== undefined && p.twr_pct !== null) return Number(p.twr_pct);
    const adj = Number(p.adjusted_equity ?? p.equity ?? base);
    return base > 0 ? ((adj - base) / base) * 100 : 0;
  });
}

function chooseTickIndices(count, maxTicks = 5) {
  if (count <= 1) return [0];
  if (count <= maxTicks) return Array.from({ length: count }, (_, i) => i);
  const indices = [];
  for (let i = 0; i < maxTicks; i++) {
    indices.push(Math.round((i * (count - 1)) / (maxTicks - 1)));
  }
  return Array.from(new Set(indices));
}

function attachChartInteraction(canvas, tooltipEl, coords, formatTooltipHtml, redrawFn) {
  if (!canvas || !tooltipEl) return;

  function showAt(clientX) {
    if (!coords.length) return;
    const rect = canvas.getBoundingClientRect();
    const relX = clientX - rect.left;
    let bestIdx = 0;
    let bestDist = Infinity;
    coords.forEach((c, idx) => {
      const d = Math.abs(c.x - relX);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });
    const chosen = coords[bestIdx];
    redrawFn(bestIdx);
    tooltipEl.innerHTML = formatTooltipHtml(chosen.pt, bestIdx);
    tooltipEl.style.display = "block";

    const tipW = tooltipEl.offsetWidth || 180;
    const tipH = tooltipEl.offsetHeight || 60;
    let left = chosen.x + 12;
    if (left + tipW > rect.width - 6) left = chosen.x - tipW - 12;
    if (left < 6) left = 6;
    let top = chosen.y - tipH - 10;
    if (top < 6) top = chosen.y + 12;
    tooltipEl.style.left = `${left}px`;
    tooltipEl.style.top = `${top}px`;
  }

  function hide() {
    tooltipEl.style.display = "none";
    redrawFn(null);
  }

  canvas.onmousemove = (e) => showAt(e.clientX);
  canvas.onmouseleave = hide;
  canvas.ontouchstart = (e) => {
    if (e.touches && e.touches[0]) showAt(e.touches[0].clientX);
  };
  canvas.ontouchmove = (e) => {
    if (e.touches && e.touches[0]) showAt(e.touches[0].clientX);
  };
}

function drawTwrChart(points) {
  const canvas = document.getElementById("twr-canvas");
  const tooltip = document.getElementById("twr-tooltip");
  if (!canvas) return;

  const twrVals = computeTwrValues(points);
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.parentElement.clientWidth || 800;
  const height = width < 500 ? 240 : 270;
  const isMobile = width < 500;
  const padL = isMobile ? 50 : 62;
  const padR = 16;
  const padT = 18;
  const padB = 30;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  let minV = -2.0;
  let maxV = 2.0;
  if (twrVals.length > 0) {
    const dataMin = Math.min(...twrVals);
    const dataMax = Math.max(...twrVals);
    if (dataMin < minV) minV = Math.floor((dataMin - 0.3) * 10) / 10;
    if (dataMax > maxV) maxV = Math.ceil((dataMax + 0.3) * 10) / 10;
  }

  const coords = points.map((pt, idx) => {
    const x = points.length === 1 ? padL + plotW / 2 : padL + (plotW * idx) / (points.length - 1);
    const val = twrVals[idx];
    const y = padT + plotH * (1 - (val - minV) / (maxV - minV));
    return { x, y, val, pt };
  });

  function render(highlightIdx) {
    const ctx = canvas.getContext("2d");
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    if (!points || points.length === 0) {
      ctx.fillStyle = "#8b949e";
      ctx.font = "14px sans-serif";
      ctx.fillText("尚無策略報酬資料", 20, height / 2);
      return;
    }

    // Horizontal grid & Y labels
    const yTicks = 4;
    ctx.font = isMobile ? "10px sans-serif" : "11px sans-serif";
    for (let i = 0; i <= yTicks; i++) {
      const y = padT + (plotH * i) / yTicks;
      const val = maxV - ((maxV - minV) * i) / yTicks;
      ctx.strokeStyle = "#2a313c";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(width - padR, y);
      ctx.stroke();

      const digits = Math.abs(maxV - minV) <= 5 ? 1 : 2;
      const sign = val > 0 ? "+" : "";
      ctx.fillStyle = "#8b949e";
      ctx.fillText(`${sign}${val.toFixed(digits)}%`, 6, y + 4);
    }

    // 0% dashed baseline
    const zeroY = padT + plotH * (1 - (0 - minV) / (maxV - minV));
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = "#8b949e";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, zeroY);
    ctx.lineTo(width - padR, zeroY);
    ctx.stroke();
    ctx.restore();

    // Colored line segments (green >= 0, red < 0)
    ctx.lineWidth = 2.2;
    for (let i = 1; i < coords.length; i++) {
      const a = coords[i - 1];
      const b = coords[i];
      if ((a.val >= 0 && b.val >= 0) || (a.val < 0 && b.val < 0)) {
        ctx.strokeStyle = (a.val + b.val) / 2 >= 0 ? "#3fb950" : "#f85149";
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      } else {
        const t = (0 - a.val) / (b.val - a.val);
        const crossX = a.x + (b.x - a.x) * t;
        ctx.strokeStyle = a.val >= 0 ? "#3fb950" : "#f85149";
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(crossX, zeroY);
        ctx.stroke();
        ctx.strokeStyle = b.val >= 0 ? "#3fb950" : "#f85149";
        ctx.beginPath();
        ctx.moveTo(crossX, zeroY);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }

    // Cash flow dots (deposit green, withdrawal orange)
    coords.forEach((c) => {
      const cf = Number(c.pt.cash_flow || 0);
      if (cf !== 0) {
        ctx.fillStyle = cf > 0 ? "#3fb950" : "#f0883e";
        ctx.strokeStyle = "#0d1117";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(c.x, c.y, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    });

    // X-axis 4~6 time ticks (prevent overlap on mobile)
    const tickIndices = chooseTickIndices(coords.length, isMobile ? 4 : 5);
    ctx.fillStyle = "#8b949e";
    ctx.font = isMobile ? "10px sans-serif" : "11px sans-serif";
    let lastRight = -Infinity;
    tickIndices.forEach((idx, posIdx) => {
      const label = shortTimeLabel(coords[idx].pt);
      const tw = ctx.measureText(label).width;
      let tx = coords[idx].x - tw / 2;
      if (posIdx === 0) tx = Math.max(padL, coords[idx].x);
      if (posIdx === tickIndices.length - 1) tx = Math.min(width - padR - tw, coords[idx].x - tw);
      if (tx >= lastRight + 8 || posIdx === tickIndices.length - 1) {
        if (tx < lastRight + 8 && posIdx === tickIndices.length - 1) return;
        ctx.fillText(label, tx, height - 8);
        lastRight = tx + tw;
      }
    });

    // Hover crosshair & active dot
    if (highlightIdx !== null && coords[highlightIdx]) {
      const c = coords[highlightIdx];
      ctx.strokeStyle = "rgba(139, 148, 158, 0.45)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(c.x, padT);
      ctx.lineTo(c.x, padT + plotH);
      ctx.stroke();

      ctx.fillStyle = c.val >= 0 ? "#3fb950" : "#f85149";
      ctx.beginPath();
      ctx.arc(c.x, c.y, 4.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  render(null);
  attachChartInteraction(
    canvas,
    tooltip,
    coords,
    (pt, idx) => {
      const val = twrVals[idx];
      const sign = val > 0 ? "+" : "";
      const cf = Number(pt.cash_flow || 0);
      let cfLine = "";
      if (cf !== 0) {
        const cfLabel = cf > 0 ? "入金" : "出金";
        const cfSign = cf > 0 ? "+" : "";
        const cfTime = pt.cash_flow_time_tpe || pt.time_tpe || "";
        cfLine = `<div class="${cf > 0 ? "pos" : "warn"}">${cfLabel} ${cfSign}${fmtNum(cf)} USDT，${cfTime}</div>`;
      }
      return `
        <div>時間：${pt.time_tpe || "-"}</div>
        <div>報酬率：<strong class="${pnlClass(val)}">${sign}${val.toFixed(2)}%</strong></div>
        <div>實際權益：<strong>${fmtNum(pt.equity)} USDT</strong></div>
        ${cfLine}
      `;
    },
    render
  );
}

function drawActualEquityChart(points) {
  const canvas = document.getElementById("actual-equity-canvas");
  const tooltip = document.getElementById("actual-tooltip");
  if (!canvas) return;

  const dpr = window.devicePixelRatio || 1;
  const width = canvas.parentElement.clientWidth || 800;
  const height = width < 500 ? 160 : 170;
  const isMobile = width < 500;
  const padL = isMobile ? 54 : 66;
  const padR = 16;
  const padT = 14;
  const padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const vals = points.map((p) => Number(p.equity));
  let minV = vals.length ? Math.min(...vals) : 0;
  let maxV = vals.length ? Math.max(...vals) : 100;
  if (minV === maxV) {
    minV = Math.max(0, minV - 10);
    maxV += 10;
  } else {
    const pad = (maxV - minV) * 0.1;
    minV = Math.max(0, minV - pad);
    maxV += pad;
  }

  const coords = points.map((pt, idx) => {
    const x = points.length === 1 ? padL + plotW / 2 : padL + (plotW * idx) / (points.length - 1);
    const val = Number(pt.equity);
    const y = padT + plotH * (1 - (val - minV) / (maxV - minV));
    return { x, y, val, pt };
  });

  function render(highlightIdx) {
    const ctx = canvas.getContext("2d");
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    if (!points || points.length === 0) {
      ctx.fillStyle = "#8b949e";
      ctx.font = "14px sans-serif";
      ctx.fillText("尚無實際權益資料", 20, height / 2);
      return;
    }

    ctx.font = isMobile ? "10px sans-serif" : "11px sans-serif";
    for (let i = 0; i <= 3; i++) {
      const y = padT + (plotH * i) / 3;
      const val = maxV - ((maxV - minV) * i) / 3;
      ctx.strokeStyle = "#2a313c";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(width - padR, y);
      ctx.stroke();
      ctx.fillStyle = "#8b949e";
      ctx.fillText(fmtNum(val, 0), 6, y + 4);
    }

    ctx.strokeStyle = "#58a6ff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    coords.forEach((c, idx) => {
      if (idx === 0) ctx.moveTo(c.x, c.y);
      else ctx.lineTo(c.x, c.y);
    });
    ctx.stroke();

    const tickIndices = chooseTickIndices(coords.length, isMobile ? 4 : 5);
    ctx.fillStyle = "#8b949e";
    let lastRight = -Infinity;
    tickIndices.forEach((idx, posIdx) => {
      const label = shortTimeLabel(coords[idx].pt);
      const tw = ctx.measureText(label).width;
      let tx = coords[idx].x - tw / 2;
      if (posIdx === 0) tx = Math.max(padL, coords[idx].x);
      if (posIdx === tickIndices.length - 1) tx = Math.min(width - padR - tw, coords[idx].x - tw);
      if (tx >= lastRight + 8) {
        ctx.fillText(label, tx, height - 6);
        lastRight = tx + tw;
      }
    });

    if (highlightIdx !== null && coords[highlightIdx]) {
      const c = coords[highlightIdx];
      ctx.strokeStyle = "rgba(139, 148, 158, 0.45)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(c.x, padT);
      ctx.lineTo(c.x, padT + plotH);
      ctx.stroke();

      ctx.fillStyle = "#58a6ff";
      ctx.beginPath();
      ctx.arc(c.x, c.y, 4, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  render(null);
  attachChartInteraction(
    canvas,
    tooltip,
    coords,
    (pt) => `
      <div>時間：${pt.time_tpe || "-"}</div>
      <div>實際權益：<strong>${fmtNum(pt.equity)} USDT</strong></div>
    `,
    render
  );
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

  drawTwrChart(equity);
  drawActualEquityChart(equity);
  window.addEventListener("resize", () => {
    drawTwrChart(equity);
    drawActualEquityChart(equity);
  });

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
