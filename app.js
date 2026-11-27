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

function fmtSignedUsdt(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}${fmtNum(n, 2)} USDT`;
}

/**
 * Format price with at least 4 significant digits, NEVER scientific notation (no 1e-06).
 */
function fmtPrice(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  if (n === 0) return "0";
  const abs = Math.abs(n);
  let decimals;
  if (abs >= 1000) decimals = 2;
  else if (abs >= 1) decimals = 4;
  else {
    const exp = Math.floor(Math.log10(abs));
    decimals = Math.min(12, Math.max(4, 4 - 1 - exp));
  }
  const fixed = n.toFixed(decimals);
  return fixed.includes(".") ? fixed.replace(/\.?0+$/, "") : fixed;
}

function shortCoin(instId) {
  return String(instId || "").replace(/-USDT-SWAP$/, "");
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

function formatHoldDuration(pos) {
  let entryMs = Number(pos.entry_ts || 0);
  if (!entryMs && pos.entry_time_tpe) {
    // Parse "YYYY-MM-DD HH:mm" as UTC+8
    const iso = `${pos.entry_time_tpe.replace(" ", "T")}:00+08:00`;
    const parsed = Date.parse(iso);
    if (!Number.isNaN(parsed)) entryMs = parsed;
  }
  if (!entryMs) return "-";
  const diffMin = Math.max(0, Math.floor((Date.now() - entryMs) / 60000));
  const days = Math.floor(diffMin / 1440);
  const hours = Math.floor((diffMin % 1440) / 60);
  const mins = diffMin % 60;
  if (days > 0) return `${days} 天 ${hours} 小時`;
  if (hours > 0) return `${hours} 小時 ${mins} 分`;
  return `${mins} 分鐘`;
}

function calcStopDistance(pos) {
  const cur = Number(pos.current_price);
  const sl = Number(pos.stop_loss);
  if (!cur || !sl || Number.isNaN(cur) || Number.isNaN(sl)) {
    return { text: "-", cls: "sl-safe" };
  }
  const distPct = Math.abs(cur - sl) / cur * 100;
  let cls = "sl-safe";
  if (distPct <= 2.0) cls = "sl-danger";
  else if (distPct <= 5.0) cls = "sl-near";
  return { text: `${distPct.toFixed(2)}%`, cls };
}

function shortTimeLabel(pt) {
  if (pt.time_short_tpe) return pt.time_short_tpe;
  const full = pt.time_tpe || "";
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
      if (tx >= lastRight + 8) {
        ctx.fillText(label, tx, height - 8);
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
  if (!canvas || !canvas.parentElement.clientWidth) return;

  const dpr = window.devicePixelRatio || 1;
  const width = canvas.parentElement.clientWidth || 800;
  const height = width < 500 ? 160 : 180;
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

function updateHealthIndicator(summary) {
  const dot = document.getElementById("health-dot");
  const text = document.getElementById("health-text");
  const topUpdated = document.getElementById("last-updated-top");
  if (!summary) return;

  topUpdated.textContent = `最後更新：${summary.last_updated_tpe || "-"}`;

  let ts = Number(summary.last_updated_ts || 0);
  if (!ts && summary.last_updated_tpe) {
    const clean = summary.last_updated_tpe.replace(" (台北時間)", "").replace(" ", "T") + "+08:00";
    const parsed = Date.parse(clean);
    if (!Number.isNaN(parsed)) ts = parsed;
  }

  if (!ts) {
    dot.className = "health-dot warn";
    text.textContent = "狀態未知";
    return;
  }

  const ageMin = (Date.now() - ts) / 60000;
  if (ageMin <= 30) {
    dot.className = "health-dot ok";
    text.textContent = "運作中";
  } else if (ageMin <= 120) {
    dot.className = "health-dot warn";
    text.textContent = "可能延遲";
  } else {
    dot.className = "health-dot err";
    text.textContent = "已停止";
  }
}

function setupHelpPopovers() {
  const pop = document.getElementById("help-popover");
  if (!pop) return;

  function show(btn) {
    const text = btn.getAttribute("data-help");
    if (!text) return;
    pop.textContent = text;
    pop.style.display = "block";
    const rect = btn.getBoundingClientRect();
    const popW = pop.offsetWidth || 260;
    let left = rect.left;
    if (left + popW > window.innerWidth - 12) left = window.innerWidth - popW - 12;
    if (left < 12) left = 12;
    let top = rect.bottom + 8;
    if (top + (pop.offsetHeight || 60) > window.innerHeight - 12) {
      top = rect.top - (pop.offsetHeight || 60) - 8;
    }
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }

  function hide() {
    pop.style.display = "none";
  }

  document.querySelectorAll(".help-btn").forEach((btn) => {
    btn.addEventListener("mouseenter", () => show(btn));
    btn.addEventListener("mouseleave", hide);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (pop.style.display === "block" && pop.textContent === btn.getAttribute("data-help")) {
        hide();
      } else {
        show(btn);
      }
    });
  });

  document.addEventListener("click", hide);
}

async function renderDashboard() {
  setupHelpPopovers();

  const [summary, equity, strategies, positions, trades] = await Promise.all([
    fetchJson("data/summary.json", null),
    fetchJson("data/equity.json", []),
    fetchJson("data/strategies.json", []),
    fetchJson("data/positions.json", []),
    fetchJson("data/trades.json", []),
  ]);

  const posUnrealizedSum = positions.reduce((acc, p) => acc + Number(p.unrealized_pnl || 0), 0);

  if (summary) {
    updateHealthIndicator(summary);
    document.getElementById("mode-badge").textContent = summary.mode || "-";
    document.getElementById("total-equity").textContent = `${fmtNum(summary.total_equity_usdt)} USDT`;

    const realizedVal = summary.realized_pnl_usdt !== undefined
      ? Number(summary.realized_pnl_usdt)
      : strategies.reduce((acc, s) => acc + Number(s.realized_pnl || 0), 0);
    const unrealizedVal = summary.unrealized_pnl_usdt !== undefined
      ? Number(summary.unrealized_pnl_usdt)
      : posUnrealizedSum;
    const totalPnlVal = summary.total_pnl_usdt !== undefined
      ? Number(summary.total_pnl_usdt)
      : realizedVal + unrealizedVal;

    const realEl = document.getElementById("realized-pnl");
    realEl.textContent = fmtSignedUsdt(realizedVal);
    realEl.className = `card-value ${pnlClass(realizedVal)}`;

    const unrealEl = document.getElementById("unrealized-pnl");
    unrealEl.textContent = fmtSignedUsdt(unrealizedVal);
    unrealEl.className = `card-value ${pnlClass(unrealizedVal)}`;

    const totPnlEl = document.getElementById("total-pnl");
    totPnlEl.textContent = fmtSignedUsdt(totalPnlVal);
    totPnlEl.className = `card-value ${pnlClass(totalPnlVal)}`;

    const retEl = document.getElementById("total-return");
    retEl.textContent = `${summary.total_return_pct >= 0 ? "+" : ""}${fmtNum(summary.total_return_pct)}%`;
    retEl.className = `card-value ${pnlClass(summary.total_return_pct)}`;

    document.getElementById("initial-equity").textContent = `${fmtNum(summary.initial_equity_usdt)} USDT`;
    document.getElementById("net-deposits").textContent = `${fmtNum(summary.net_deposits_usdt ?? 0)} USDT`;
    document.getElementById("max-dd").textContent = `-${fmtNum(summary.max_drawdown_pct)}%`;

    const run = summary.latest_run || {};
    const errs = run.errors || [];
    document.getElementById("run-status").innerHTML = `
      <div>執行時間：${run.time_tpe || "-"} ｜ 4H 新收盤：${run.new_4h_closed ? "是" : "否"} ｜ 日線新收盤：${run.new_1d_closed ? "是" : "否"} ｜ 對帳平倉：${run.reconciled_closed || 0} 筆</div>
      ${errs.length ? `<ul class="error-list">${errs.map((e) => `<li>${e}</li>`).join("")}</ul>` : `<div style="margin-top:6px;color:#3fb950;">本次執行無錯誤</div>`}
    `;

    const cfs = summary.cash_flows || [];
    if (cfs.length > 0) {
      document.getElementById("cash-flow-wrap").style.display = "block";
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

    const rejected = summary.rejected_signals || [];
    document.getElementById("rejected-count-badge").textContent = `${rejected.length} 筆`;
    document.getElementById("rejected-body").innerHTML = rejected.length
      ? rejected.map((r) => `
        <tr>
          <td>${r.time_tpe}</td>
          <td><strong>${r.strategy}</strong></td>
          <td>${shortCoin(r.inst_id)}</td>
          <td>${dirText(r.pos_side)}</td>
          <td class="wrap warn">${r.reason}</td>
        </tr>
      `).join("")
      : '<tr><td colspan="5">目前無拒絕紀錄</td></tr>';
  }

  // ② 主圖
  drawTwrChart(equity);
  window.addEventListener("resize", () => {
    drawTwrChart(equity);
    if (document.getElementById("actual-equity-details").open) {
      drawActualEquityChart(equity);
    }
  });
  document.getElementById("actual-equity-details").addEventListener("toggle", (e) => {
    if (e.target.open) drawActualEquityChart(equity);
  });

  // ③ 目前持倉
  document.getElementById("positions-count-badge").textContent = `${positions.length} 筆`;
  const posBody = document.getElementById("positions-body");
  const posFoot = document.getElementById("positions-foot");
  if (positions.length) {
    posBody.innerHTML = positions.map((p) => {
      const dist = calcStopDistance(p);
      return `
        <tr>
          <td><strong>${p.strategy}</strong></td>
          <td><strong>${shortCoin(p.inst_id)}</strong></td>
          <td>${dirText(p.pos_side)}</td>
          <td>${fmtPrice(p.entry_price)}</td>
          <td>${fmtPrice(p.current_price)}</td>
          <td>${fmtPrice(p.stop_loss)}</td>
          <td class="${dist.cls}">${dist.text}</td>
          <td class="${pnlClass(p.unrealized_pnl)}">${fmtSignedUsdt(p.unrealized_pnl)}</td>
          <td>${formatHoldDuration(p)}</td>
          <td>${p.entry_time_tpe || "-"}</td>
          <td class="wrap">${p.exit_rule_desc || "-"}</td>
        </tr>
      `;
    }).join("");
    posFoot.innerHTML = `
      <tr>
        <td colspan="7">合計（共 ${positions.length} 筆持倉）</td>
        <td class="${pnlClass(posUnrealizedSum)}">${fmtSignedUsdt(posUnrealizedSum)}</td>
        <td colspan="3"></td>
      </tr>
    `;
  } else {
    posBody.innerHTML = '<tr><td colspan="11">目前無持倉</td></tr>';
    posFoot.innerHTML = "";
  }

  // ④ 已平倉紀錄
  document.getElementById("trades-count-badge").textContent = `${trades.length} 筆`;
  document.getElementById("trades-body").innerHTML = trades.length
    ? trades.map((t) => `
      <tr>
        <td><strong>${t.strategy}</strong></td>
        <td><strong>${shortCoin(t.inst_id)}</strong></td>
        <td>${dirText(t.pos_side)}</td>
        <td>${t.entry_time_tpe}</td>
        <td>${t.exit_time_tpe}</td>
        <td>${fmtPrice(t.entry_price)}</td>
        <td>${fmtPrice(t.exit_price)}</td>
        <td class="${pnlClass(t.pnl_usdt)}">${fmtSignedUsdt(t.pnl_usdt)}</td>
        <td class="${pnlClass(t.r_multiple)}">${fmtNum(t.r_multiple)} R</td>
        <td class="wrap">${t.exit_reason}</td>
      </tr>
    `).join("")
    : '<tr><td colspan="10">尚無已平倉交易</td></tr>';

  // ⑤ 依策略分組的績效
  const unrealByStrat = {};
  positions.forEach((p) => {
    unrealByStrat[p.strategy] = (unrealByStrat[p.strategy] || 0) + Number(p.unrealized_pnl || 0);
  });
  document.getElementById("strategies-body").innerHTML = strategies.length
    ? strategies.map((s) => {
      const sUnreal = s.unrealized_pnl !== undefined ? Number(s.unrealized_pnl) : (unrealByStrat[s.strategy] || 0);
      return `
        <tr>
          <td><strong>${s.strategy}</strong></td>
          <td>${fmtNum(s.allocation_pct, 0)}%</td>
          <td>${fmtNum(s.equity)}</td>
          <td>${s.trades_count}</td>
          <td>${fmtNum(s.win_rate_pct, 1)}%</td>
          <td class="${pnlClass(s.realized_pnl)}">${fmtSignedUsdt(s.realized_pnl)}</td>
          <td class="${pnlClass(sUnreal)}">${fmtSignedUsdt(sUnreal)}</td>
          <td>${fmtNum(s.avg_r)} R</td>
          <td>-${fmtNum(s.max_drawdown_pct)}%</td>
        </tr>
      `;
    }).join("")
    : '<tr><td colspan="9">尚無策略資料</td></tr>';
}

renderDashboard();
