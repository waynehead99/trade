// ---------------------------------------------------------------------------
// Tiny UI glue. Polls account/positions/orders every 5s and streams job logs
// by polling /api/jobs/<id> while a job is running.
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

const MODE = document.body.classList.contains("mode-live") ? "LIVE" : "PAPER";

const fmtMoney = (n) =>
  n == null || Number.isNaN(n)
    ? "—"
    : (n < 0 ? "-$" : "$") +
      Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmtPct = (n) => (n == null || Number.isNaN(n) ? "—" : (n * 100).toFixed(2) + "%");
const fmtQty = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const cls = (n) => (n == null || n === 0 ? "" : n > 0 ? "pos" : "neg");

function fmtAcquired(iso) {
  if (!iso) return '<span class="subtle">—</span>';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '<span class="subtle">—</span>';
  const pad = (n) => String(n).padStart(2, "0");
  const datePart = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const timePart = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const today = new Date();
  const isToday =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate();
  return isToday
    ? `<span class="warn-text" title="Bought today — selling same-day would count as a day trade">${datePart} ${timePart}</span>`
    : `${datePart} <span class="subtle">${timePart}</span>`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

// ---- Snapshot refreshers -------------------------------------------------

async function refreshAccount() {
  try {
    const a = await api("/api/account");
    $("#stat-bp").textContent = fmtMoney(a.buying_power);
    $("#stat-cash").textContent = fmtMoney(a.cash);
    $("#stat-equity").textContent = fmtMoney(a.equity);
    $("#stat-portfolio").textContent = fmtMoney(a.portfolio_value);
    const daypl = $("#stat-daypl");
    daypl.textContent = fmtMoney(a.day_pl);
    daypl.className = "value " + cls(a.day_pl);

    // Day-trade counter
    const dt = $("#stat-dt");
    dt.textContent = `${a.daytrade_count} / ${a.pdt_daytrade_limit}`;
    let dtCls = "value";
    if (a.daytrade_count >= a.pdt_daytrade_limit) dtCls += " neg";
    else if (a.daytrade_count >= a.pdt_daytrade_limit - 1) dtCls += " warn-text";
    dt.className = dtCls;

    // PDT banner — takes precedence over market-closed
    const pdtBanner = $("#pdt-banner");
    if (a.pdt_block_reason) {
      pdtBanner.textContent = `PDT guard: ${a.pdt_block_reason} New opening buys are blocked.`;
      pdtBanner.hidden = false;
    } else if (a.pattern_day_trader) {
      pdtBanner.textContent =
        `Account flagged as Pattern Day Trader (equity $${a.equity.toLocaleString()}). ` +
        `Day trading allowed while equity >= $${a.pdt_equity_min.toLocaleString()}.`;
      pdtBanner.hidden = false;
    } else {
      pdtBanner.hidden = true;
    }

    const banner = $("#market-banner");
    if (!a.market_open) {
      banner.textContent =
        "Market is closed. Mirror runs will abort; manual market orders would queue until next open.";
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  } catch (e) {
    console.error("account refresh failed:", e);
  }
}

async function refreshPositions() {
  try {
    const positions = await api("/api/positions");
    const tbody = $("#positions-tbody");
    if (!positions.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = positions
      .map(
        (p) => `
          <tr>
            <td class="ticker-cell">${p.symbol}</td>
            <td class="num">${fmtQty(p.qty)}</td>
            <td>${fmtAcquired(p.acquired_at)}</td>
            <td class="num">${fmtMoney(p.avg_entry_price)}</td>
            <td class="num">${fmtMoney(p.current_price)}</td>
            <td class="num">${fmtMoney(p.market_value)}</td>
            <td class="num ${cls(p.unrealized_pl)}">${fmtMoney(p.unrealized_pl)} <span class="subtle">(${fmtPct(p.unrealized_plpc)})</span></td>
            <td class="row-action"><button class="danger" data-sell="${p.symbol}" data-qty="${p.qty}">Sell</button></td>
          </tr>
        `
      )
      .join("");
  } catch (e) {
    console.error("positions refresh failed:", e);
  }
}

async function refreshOrders() {
  try {
    const orders = await api("/api/orders");
    const tbody = $("#orders-tbody");
    if (!orders.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No open orders</td></tr>';
      return;
    }
    tbody.innerHTML = orders
      .map((o) => {
        const tl =
          o.trail_percent != null
            ? `${o.trail_percent}%`
            : o.trail_price != null
            ? fmtMoney(o.trail_price)
            : o.limit_price != null
            ? fmtMoney(o.limit_price)
            : "—";
        return `
          <tr>
            <td class="ticker-cell">${o.symbol}</td>
            <td>${o.side}</td>
            <td>${o.type}</td>
            <td class="num">${fmtQty(o.qty)}</td>
            <td class="num">${tl}</td>
            <td class="row-action"><button class="danger" data-cancel="${o.id}">Cancel</button></td>
          </tr>
        `;
      })
      .join("");
  } catch (e) {
    console.error("orders refresh failed:", e);
  }
}

// ---- Markets strip -------------------------------------------------------
// Snapshot tiles for broad-market ETFs. Polled alongside refreshAll so it
// respects the active-window gate — when polling is paused, tiles just hold
// the last values they had.

async function refreshMarkets() {
  try {
    const tiles = await api("/api/market/snapshots");
    const el = $("#markets-tiles");
    if (!Array.isArray(tiles) || !tiles.length) {
      el.innerHTML = '<div class="empty">No market data.</div>';
      return;
    }
    el.innerHTML = tiles
      .map((t) => {
        const c = cls(t.change);
        const sign = t.change != null && t.change >= 0 ? "+" : "";
        // Indexes have prices in the thousands with commas; ETFs have $ amounts.
        // Detect by symbol prefix — ^ indicates a Yahoo-sourced index.
        const isIndex = (t.symbol || "").startsWith("^");
        const fmtLevel = (n) =>
          n == null
            ? "—"
            : n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const last = isIndex ? fmtLevel(t.last) : fmtMoney(t.last);
        const chg = t.change != null ? `${sign}${isIndex ? fmtLevel(t.change) : fmtMoney(t.change)}` : "—";
        const pct = t.change_pct != null ? `${sign}${t.change_pct.toFixed(2)}%` : "—";
        const display = t.display_symbol || t.symbol;
        return `
          <div class="market-tile ${c}">
            <div class="mt-head">
              <span class="mt-sym">${display}</span>
              <span class="mt-lbl">${t.label || ""}</span>
            </div>
            <div class="mt-last">${last}</div>
            <div class="mt-change ${c}">${chg} <span class="mt-pct">${pct}</span></div>
          </div>
        `;
      })
      .join("");
  } catch (e) {
    console.error("markets refresh failed:", e);
  }
}

// ---- Equity progress chart ----------------------------------------------

let currentPeriod = "1M";

function fmtChartDate(unixSec, period) {
  const d = new Date(unixSec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  if (period === "1D") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (period === "1W") return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function fmtTooltipDate(unixSec) {
  const d = new Date(unixSec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderChart(data, period) {
  const el = $("#progress-chart");
  const { timestamp = [], equity = [], base_value } = data || {};

  // Alpaca can return zero/null equity points for times before the account existed.
  // Filter those out so the line doesn't dive to zero on the left edge.
  const pts = [];
  for (let i = 0; i < equity.length; i++) {
    if (equity[i] != null && equity[i] > 0) pts.push({ t: timestamp[i], e: equity[i] });
  }

  if (pts.length < 2) {
    el.innerHTML = '<div class="empty">Not enough history yet for this period.</div>';
    $("#progress-stats").textContent = "";
    return;
  }

  const W = 1000, H = 240;
  const padL = 70, padR = 16, padT = 16, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  let minE = Infinity, maxE = -Infinity;
  for (const p of pts) { if (p.e < minE) minE = p.e; if (p.e > maxE) maxE = p.e; }
  // Pad y-range so the line isn't glued to the edges when volatility is tiny.
  const yPad = (maxE - minE) * 0.1 || Math.max(1, maxE * 0.01);
  minE -= yPad; maxE += yPad;
  const rangeE = maxE - minE || 1;

  const minT = pts[0].t, maxT = pts[pts.length - 1].t;
  const rangeT = maxT - minT || 1;
  const x = (t) => padL + ((t - minT) / rangeT) * plotW;
  const y = (e) => padT + plotH - ((e - minE) / rangeE) * plotH;

  const startE = pts[0].e;
  const endE = pts[pts.length - 1].e;
  const base = base_value && base_value > 0 ? base_value : startE;
  const isUp = endE >= base;
  const lineColor = isUp ? "#3fb950" : "#f85149";
  const fillColor = isUp ? "rgba(63,185,80,0.15)" : "rgba(248,81,73,0.15)";

  const linePath = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.e).toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${x(maxT).toFixed(1)},${(padT + plotH).toFixed(1)} L${x(minT).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;
  const baselineY = y(base);

  // Y-axis gridlines at min, mid, max
  const midE = (minE + maxE) / 2;
  const gridLines = [minE, midE, maxE]
    .map((v) => {
      const gy = y(v).toFixed(1);
      return `
        <line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="#21262d" stroke-width="1"/>
        <text x="${padL - 8}" y="${(parseFloat(gy) + 4).toFixed(1)}" text-anchor="end" fill="#8b949e" font-size="11" font-family="SF Mono, monospace">${fmtMoney(v)}</text>
      `;
    })
    .join("");

  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart-svg" xmlns="http://www.w3.org/2000/svg">
      ${gridLines}
      <line x1="${padL}" y1="${baselineY.toFixed(1)}" x2="${W - padR}" y2="${baselineY.toFixed(1)}" stroke="#8b949e" stroke-width="1" stroke-dasharray="4,4" opacity="0.6"/>
      <path d="${areaPath}" fill="${fillColor}" stroke="none"/>
      <path d="${linePath}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="${x(maxT).toFixed(1)}" cy="${y(endE).toFixed(1)}" r="4" fill="${lineColor}"/>
      <line class="chart-crosshair" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="#8b949e" stroke-width="1" stroke-dasharray="2,2" opacity="0" pointer-events="none"/>
      <circle class="chart-cursor" cx="-999" cy="-999" r="5" fill="${lineColor}" stroke="#0d1117" stroke-width="2" opacity="0" pointer-events="none"/>
      <text x="${padL}" y="${H - 8}" text-anchor="start" fill="#8b949e" font-size="11" font-family="SF Mono, monospace">${fmtChartDate(minT, period)}</text>
      <text x="${W - padR}" y="${H - 8}" text-anchor="end" fill="#8b949e" font-size="11" font-family="SF Mono, monospace">${fmtChartDate(maxT, period)}</text>
      <rect class="chart-hitbox" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent"/>
    </svg>
    <div class="chart-tooltip" hidden></div>
  `;

  const pl = endE - base;
  const plPct = base > 0 ? (pl / base) * 100 : 0;
  const sign = pl >= 0 ? "+" : "";
  $("#progress-stats").innerHTML =
    `— ${fmtMoney(base)} → ${fmtMoney(endE)} &nbsp; <span class="${isUp ? "pos" : "neg"}">${sign}${fmtMoney(pl)} (${sign}${plPct.toFixed(2)}%)</span>`;

  // --- Hover: crosshair + tooltip -----------------------------------------
  const svg = el.querySelector("svg");
  const hitbox = el.querySelector(".chart-hitbox");
  const crosshair = el.querySelector(".chart-crosshair");
  const cursor = el.querySelector(".chart-cursor");
  const tooltip = el.querySelector(".chart-tooltip");

  hitbox.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    // Mouse pixel → viewBox x (SVG uses preserveAspectRatio=none so scale x/y independently).
    const svgX = ((e.clientX - rect.left) / rect.width) * W;

    // Find nearest point by x distance. Linear scan — pts is never large here.
    let idx = 0, minDist = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const d = Math.abs(x(pts[i].t) - svgX);
      if (d < minDist) { minDist = d; idx = i; }
    }
    const p = pts[idx];
    const px = x(p.t);
    const py = y(p.e);

    crosshair.setAttribute("x1", px);
    crosshair.setAttribute("x2", px);
    crosshair.setAttribute("opacity", "1");
    cursor.setAttribute("cx", px);
    cursor.setAttribute("cy", py);
    cursor.setAttribute("opacity", "1");

    const tpl = p.e - base;
    const tplPct = base > 0 ? (tpl / base) * 100 : 0;
    const tSign = tpl >= 0 ? "+" : "";
    tooltip.innerHTML = `
      <div class="tip-date">${fmtTooltipDate(p.t)}</div>
      <div class="tip-equity">${fmtMoney(p.e)}</div>
      <div class="tip-pl ${tpl >= 0 ? "pos" : "neg"}">${tSign}${fmtMoney(tpl)} (${tSign}${tplPct.toFixed(2)}%)</div>
    `;
    tooltip.hidden = false;

    // Position tooltip in container pixel space (SVG fills container width, fixed height).
    const containerRect = el.getBoundingClientRect();
    const pixelX = (px / W) * containerRect.width;
    const pixelY = (py / H) * containerRect.height;
    const tipRect = tooltip.getBoundingClientRect();
    let left = pixelX - tipRect.width / 2;
    let top = pixelY - tipRect.height - 12;
    if (top < 0) top = pixelY + 16; // flip below when near the top edge
    left = Math.max(4, Math.min(left, containerRect.width - tipRect.width - 4));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  });

  hitbox.addEventListener("mouseleave", () => {
    crosshair.setAttribute("opacity", "0");
    cursor.setAttribute("opacity", "0");
    tooltip.hidden = true;
  });
}

async function refreshProgress(period) {
  currentPeriod = period;
  const el = $("#progress-chart");
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const data = await api(`/api/portfolio-history?period=${encodeURIComponent(period)}`);
    renderChart(data, period);
  } catch (e) {
    el.innerHTML = `<div class="empty neg">Failed to load: ${e.message}</div>`;
    $("#progress-stats").textContent = "";
  }
}

async function refreshHistory() {
  try {
    const side = $("#history-side").value;
    const days = $("#history-days").value;
    const symbol = $("#history-symbol").value.trim().toUpperCase();
    const params = new URLSearchParams({ side });
    if (days) params.set("days", days);
    if (symbol) params.set("symbol", symbol);
    const orders = await api(`/api/history/orders?${params.toString()}`);
    const tbody = $("#history-tbody");
    if (!orders.length) {
      tbody.innerHTML =
        '<tr><td colspan="8" class="empty">No orders in the local cache yet. Click "sync" to pull from Alpaca.</td></tr>';
      $("#sell-stats-line").textContent = "";
      return;
    }
    $("#sell-stats-line").textContent = ` — ${orders.length} row${orders.length === 1 ? "" : "s"}`;
    tbody.innerHTML = orders
      .map((o) => {
        const when = o.filled_at || o.canceled_at || o.submitted_at;
        const whenStr = when ? when.slice(0, 16).replace("T", " ") : "—";
        const price = o.filled_avg_price != null ? fmtMoney(o.filled_avg_price) : "—";
        const qty = o.filled_qty != null && o.filled_qty > 0 ? o.filled_qty : o.qty;
        const value =
          o.filled_avg_price != null && qty != null
            ? fmtMoney(o.filled_avg_price * qty)
            : "—";
        const statusCls =
          o.status === "filled" ? "pos" : o.status === "canceled" ? "subtle" : "warn-text";
        const sideCls = o.side === "buy" ? "pos" : "neg";
        return `
          <tr>
            <td>${whenStr}</td>
            <td class="ticker-cell">${o.symbol}</td>
            <td class="${sideCls}">${o.side}</td>
            <td>${o.type}</td>
            <td class="num">${fmtQty(qty)}</td>
            <td class="num">${price}</td>
            <td class="num">${value}</td>
            <td class="${statusCls}">${o.status}</td>
          </tr>
        `;
      })
      .join("");
  } catch (e) {
    console.error("history refresh failed:", e);
  }
}

async function refreshCongress() {
  try {
    const mode = $("#congress-mode").value;
    const days = $("#congress-days").value;
    const trades = await api(`/api/congress?mode=${mode}&days=${days}`);
    const tbody = $("#congress-tbody");
    if (!trades.length) {
      tbody.innerHTML =
        '<tr><td colspan="7" class="empty">No trades in this window. Click "Refresh from Capitol Trades".</td></tr>';
      $("#stats-line").textContent = "";
      return;
    }
    $("#stats-line").textContent = ` — ${trades.length} row${trades.length === 1 ? "" : "s"}`;
    tbody.innerHTML = trades
      .map((t) => {
        const party =
          t.party === "republican" ? '<span class="neg">R</span>' :
          t.party === "democrat" ? '<span style="color:#58a6ff">D</span>' :
          "";
        return `
          <tr>
            <td>${t.date || "—"}</td>
            <td>${t.pub_date || "—"}</td>
            <td class="${t.type === "buy" ? "pos" : "neg"}">${t.type}</td>
            <td class="ticker-cell">${t.ticker}</td>
            <td>${t.politician} ${party}</td>
            <td class="num">${fmtMoney(t.value)}</td>
            <td class="row-action"><button data-quickbuy="${t.ticker}">Buy</button></td>
          </tr>
        `;
      })
      .join("");
  } catch (e) {
    console.error("congress refresh failed:", e);
  }
}

// ---- Log pane + job polling ---------------------------------------------

function setLog(text) {
  const log = $("#log");
  log.textContent = text;
  log.scrollTop = log.scrollHeight;
}

function appendLog(text) {
  const log = $("#log");
  log.textContent += text;
  log.scrollTop = log.scrollHeight;
}

async function pollJob(jobId) {
  while (true) {
    const j = await api(`/api/jobs/${jobId}`);
    setLog(j.log || "(no output yet...)");
    if (j.status === "done") {
      if (j.error) appendLog(`\n[ERROR] ${j.error}\n`);
      return j;
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

// ---- Event handlers ------------------------------------------------------

async function handleSell(symbol, qty) {
  if (!confirm(`[${MODE}] Market SELL ${qty} share(s) of ${symbol}?\n\nThis is a market order — fills at the next available price.`))
    return;
  try {
    const r = await api("/api/sell", {
      method: "POST",
      body: JSON.stringify({ symbol, qty }),
    });
    setLog(r.log || "Sold.");
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshAll();
}

async function handleCancel(orderId) {
  if (!confirm("Cancel this order?")) return;
  try {
    await api(`/api/orders/${orderId}/cancel`, { method: "POST" });
    appendLog(`\nCancelled order ${orderId}.\n`);
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshOrders();
}

function handleQuickBuy(symbol) {
  $("#buy-symbol").value = symbol;
  $("#buy-symbol").scrollIntoView({ behavior: "smooth", block: "center" });
  $("#buy-qty").focus();
  $("#buy-qty").select();
}

// Delegated clicks for all row buttons
document.body.addEventListener("click", (e) => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.dataset.sell) handleSell(t.dataset.sell, parseFloat(t.dataset.qty || "0"));
  else if (t.dataset.cancel) handleCancel(t.dataset.cancel);
  else if (t.dataset.quickbuy) handleQuickBuy(t.dataset.quickbuy);
});

$("#buy-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const symbol = $("#buy-symbol").value.trim().toUpperCase();
  const qty = parseInt($("#buy-qty").value, 10);
  const trail_percent = parseFloat($("#buy-trail").value);
  if (!symbol || qty < 1) return;
  if (!confirm(`[${MODE}] BUY ${qty} share(s) of ${symbol}\n  + trailing stop sell at ${trail_percent}%\n\nConfirm?`))
    return;
  try {
    const { job_id } = await api("/api/jobs/buy", {
      method: "POST",
      body: JSON.stringify({ symbol, qty, trail_percent }),
    });
    appendLog(`\n--- Starting buy job ${job_id} ---\n`);
    await pollJob(job_id);
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshAll();
});

$("#mirror-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const days = parseInt($("#mirror-days").value, 10);
  const qty = parseInt($("#mirror-qty").value, 10);
  const trail_percent = parseFloat($("#mirror-trail").value);
  const msRaw = $("#mirror-max-spend").value;
  const max_spend = msRaw ? parseFloat(msRaw) : null;
  const cap = max_spend != null ? `$${max_spend.toLocaleString()}` : "full buying power";
  const msg =
    `[${MODE}] Mirror congressional BUYS\n\n` +
    `  Window:     last ${days} days\n` +
    `  Per ticker: ${qty} share(s) @ market\n` +
    `  Trail:      ${trail_percent}%\n` +
    `  Budget cap: ${cap}\n\n` +
    `Proceed?`;
  if (!confirm(msg)) return;
  try {
    const { job_id } = await api("/api/jobs/mirror", {
      method: "POST",
      body: JSON.stringify({ days, qty, trail_percent, max_spend }),
    });
    appendLog(`\n--- Starting mirror job ${job_id} ---\n`);
    await pollJob(job_id);
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshAll();
});

$("#refresh-congress-btn").addEventListener("click", async () => {
  const days = parseInt($("#congress-days").value, 10);
  try {
    const { job_id } = await api("/api/jobs/refresh-congress", {
      method: "POST",
      body: JSON.stringify({ days }),
    });
    appendLog(`\n--- Fetching congress trades (last ${days} days) ---\n`);
    await pollJob(job_id);
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshCongress();
});

$("#congress-mode").addEventListener("change", refreshCongress);
$("#congress-days").addEventListener("change", refreshCongress);

$("#history-side").addEventListener("change", refreshHistory);
$("#history-days").addEventListener("change", refreshHistory);
$("#history-symbol").addEventListener("change", refreshHistory);

$("#sync-history-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/history/sync", { method: "POST" });
    appendLog(`\nSynced ${r.synced} orders from Alpaca.\n`);
  } catch (e) {
    appendLog(`\n[ERROR] ${e.message}\n`);
  }
  refreshHistory();
});

$("#clear-log-btn").addEventListener("click", () => setLog(""));

document.querySelectorAll("[data-period]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-period]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    refreshProgress(btn.dataset.period);
  });
});

// ---- Boot ----------------------------------------------------------------

function refreshAll() {
  refreshAccount();
  refreshPositions();
  refreshOrders();
  refreshMarkets();
}

// ---- Active-window gating -------------------------------------------------
// Alpaca polling only runs inside [market_open - 30min, market_close + 30min].
// Outside that window we pause refreshAll entirely; the status probe every
// 60s tells us when to resume. refreshHistory/Congress/Progress are local DB
// or user-triggered, so they're not gated.

let pollingActive = false;

function fmtRelTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diff = Math.max(0, then - now);
  const h = Math.floor(diff / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function updatePollingBadge(s) {
  const badge = $("#polling-badge");
  if (s.active) {
    badge.textContent = "● LIVE";
    badge.className = "polling-badge active";
    badge.title = `Polling active. Window closes ${new Date(s.window_end).toLocaleString()}`;
  } else {
    const rel = fmtRelTime(s.window_start);
    badge.textContent = rel ? `◌ PAUSED · ${rel}` : "◌ PAUSED";
    badge.className = "polling-badge paused";
    badge.title = s.window_start
      ? `Polling paused. Resumes ${new Date(s.window_start).toLocaleString()}`
      : "Polling paused — no upcoming session scheduled";
  }
}

async function checkStatus() {
  try {
    const s = await api("/api/status");
    const wasActive = pollingActive;
    pollingActive = !!s.active;
    updatePollingBadge(s);
    if (pollingActive && !wasActive) refreshAll();  // just entered window
  } catch (e) {
    console.error("status check failed:", e);
  }
}

async function boot() {
  await checkStatus();
  refreshAll();            // always once on load, so the UI isn't blank outside hours
  refreshCongress();
  refreshHistory();
  refreshProgress(currentPeriod);

  setInterval(checkStatus, 60_000);
  setInterval(() => { if (pollingActive) refreshAll(); }, 5_000);
  setInterval(refreshHistory, 30_000);
}

boot();
