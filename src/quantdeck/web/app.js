const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function getToken() {
  try { return localStorage.getItem("qd_token") || ""; } catch { return ""; }
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...opts, headers });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) {
    showLogin();
    throw new Error("sign in required");
  }
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

function showLogin() {
  const box = $("#login");
  if (!box.hidden) return;
  box.hidden = false;
  $("#login-token").focus();
}

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  try { localStorage.setItem("qd_token", $("#login-token").value.trim()); } catch {}
  location.reload();
});

const fmt = {
  pct: (v) => (v == null ? "–" : (v * 100).toFixed(2) + "%"),
  num: (v, d = 2) => (v == null ? "–" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })),
  qty: (v) => (v == null ? "–" : Number(v).toPrecision(6).replace(/\.?0+$/, "")),
  date: (s) => (s ? s.slice(0, 16).replace("T", " ") : ""),
};

// ---- tabs -----------------------------------------------------------------

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === btn.dataset.tab));
    if (btn.dataset.tab === "runs") loadRuns();
    if (btn.dataset.tab === "news") loadNews();
  })
);

// ---- chart ----------------------------------------------------------------

function lineChart(el, ts, series, opts = {}) {
  const w = el.clientWidth, h = el.clientHeight;
  const pad = { l: 56, r: 12, t: 8, b: 22 };
  const all = series.flatMap((s) => s.values).filter((v) => v != null && isFinite(v));
  if (!ts.length || !all.length) { el.innerHTML = '<p class="muted">no data</p>'; return; }
  let lo = Math.min(...all), hi = Math.max(...all);
  if (opts.zeroTop) hi = 0;
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => pad.l + (i / Math.max(ts.length - 1, 1)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
  const yfmt = opts.yfmt || ((v) => fmt.num(v, 0));

  let svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><g class="grid">`;
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + ((hi - lo) * i) / ticks;
    svg += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y(v)}" y2="${y(v)}"/>`;
  }
  svg += `</g><g class="axis">`;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + ((hi - lo) * i) / ticks;
    svg += `<text x="${pad.l - 6}" y="${y(v) + 3}" text-anchor="end">${yfmt(v)}</text>`;
  }
  const xt = Math.min(6, ts.length);
  for (let i = 0; i < xt; i++) {
    const idx = Math.round((i / Math.max(xt - 1, 1)) * (ts.length - 1));
    const anchor = i === 0 ? "start" : i === xt - 1 ? "end" : "middle";
    svg += `<text x="${x(idx)}" y="${h - 6}" text-anchor="${anchor}">${ts[idx].slice(0, 10)}</text>`;
  }
  svg += `</g>`;
  for (const s of series) {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v == null || !isFinite(v)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    if (s.fill) {
      const base = y(Math.min(Math.max(0, lo), hi));
      svg += `<path d="${d}L${x(ts.length - 1)},${base}L${x(0)},${base}Z" fill="${s.color}" opacity=".18"/>`;
    }
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
  }
  svg += `<line class="cross" x1="0" x2="0" y1="${pad.t}" y2="${h - pad.b}" stroke="#3a4350" style="display:none"/></svg><div class="tip"></div>`;
  el.innerHTML = svg;

  const cross = $(".cross", el), tip = $(".tip", el);
  el.onmousemove = (e) => {
    const r = el.getBoundingClientRect();
    const px = e.clientX - r.left;
    const i = Math.round(((px - pad.l) / (w - pad.l - pad.r)) * (ts.length - 1));
    if (i < 0 || i >= ts.length) return;
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.style.display = "";
    tip.innerHTML = `${fmt.date(ts[i])}<br>` + series.map((s) => `${s.name}: ${yfmt(s.values[i])}`).join("<br>");
    tip.style.display = "block";
    tip.style.left = Math.min(px + 12, w - tip.offsetWidth - 4) + "px";
    tip.style.top = "8px";
  };
  el.onmouseleave = () => { cross.style.display = "none"; tip.style.display = "none"; };
}

// ---- backtest form --------------------------------------------------------

let strategies = [];
const form = $("#bt-form");

async function loadStrategies() {
  strategies = await api("/api/strategies");
  $("#strategy").innerHTML = strategies.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
  renderParams();
}

function renderParams() {
  const s = strategies.find((x) => x.name === $("#strategy").value);
  if (!s) return;
  $("#strategy-doc").textContent = s.doc.split("\n")[0];
  $("#params").innerHTML = Object.entries(s.params)
    .map(([k, v]) => `<label>${esc(k)}<input data-param="${esc(k)}" value="${v ?? ""}" placeholder="${v == null ? "auto" : ""}"></label>`)
    .join("");
}

$("#strategy").addEventListener("change", renderParams);

function syncSourceFields() {
  const market = form.market.value;
  if (market === "stock") form.source.value = "alpaca";
  $(".ex-field").style.display = form.source.value === "ccxt" ? "" : "none";
}
form.market.addEventListener("change", () => {
  form.symbols.value = form.market.value === "stock" ? "AAPL, MSFT" : "BTC/USDT, ETH/USDT";
  syncSourceFields();
});
form.source.addEventListener("change", syncSourceFields);

function buildConfig() {
  const params = {};
  document.querySelectorAll("[data-param]").forEach((inp) => {
    const raw = inp.value.trim();
    if (raw === "") return;
    params[inp.dataset.param] = isNaN(raw) ? raw : Number(raw);
  });
  const cfg = {
    name: "web",
    market: form.market.value,
    symbols: form.symbols.value.split(",").map((s) => s.trim()).filter(Boolean),
    cash: Number(form.cash.value),
    fractional: form.market.value === "crypto",
    data: { source: form.source.value, timeframe: form.timeframe.value, start: form.start.value || null, end: form.end.value || null },
    fees: { rate: Number(form.fee.value) / 100, slippage: Number(form.slippage.value) / 100 },
    strategy: { name: $("#strategy").value, params },
  };
  if (cfg.data.source === "ccxt") cfg.data.exchange = form.exchange.value.trim();
  return cfg;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("#bt-status"), btn = $("#bt-run");
  status.className = "status";
  btn.disabled = true;
  status.textContent = "queued…";
  try {
    let job = await api("/api/backtests", { method: "POST", body: JSON.stringify(buildConfig()) });
    const t0 = Date.now();
    while (job.status !== "done" && job.status !== "failed") {
      status.textContent = `${job.status}… ${((Date.now() - t0) / 1000).toFixed(0)}s`;
      await new Promise((r) => setTimeout(r, 700));
      job = await api(`/api/backtests/${job.id}`);
    }
    if (job.status === "failed") throw new Error(job.error);
    status.textContent = `done in ${((Date.now() - t0) / 1000).toFixed(1)}s`;
    showResult(job.result);
  } catch (err) {
    status.className = "status err";
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

function tile(k, v, cls = "") {
  return `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
}

function showResult(r) {
  const m = r.metrics;
  const sign = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  $("#metrics").innerHTML = [
    tile("Return", fmt.pct(m.total_return), sign(m.total_return)),
    tile("Buy & hold", fmt.pct(m.benchmark_return), sign(m.benchmark_return)),
    tile("CAGR", fmt.pct(m.cagr), sign(m.cagr)),
    tile("Sharpe", fmt.num(m.sharpe)),
    tile("Max DD", fmt.pct(m.max_drawdown), "down"),
    tile("Trades", m.trades),
    tile("Win rate", fmt.pct(m.win_rate)),
    tile("Profit factor", m.profit_factor == null ? "∞" : fmt.num(m.profit_factor)),
  ].join("");

  const c = r.curve;
  lineChart($("#equity-chart"), c.ts, [
    { name: "strategy", values: c.equity, color: "var(--accent)" },
    { name: "buy & hold", values: c.benchmark, color: "var(--accent2)" },
  ]);
  lineChart($("#dd-chart"), c.ts, [{ name: "drawdown", values: c.drawdown, color: "var(--down)", fill: true }],
    { zeroTop: true, yfmt: (v) => fmt.pct(v) });

  $("#trade-count").textContent = `(${r.trades.length})`;
  $("#trades").innerHTML =
    "<tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>In</th><th>Out</th><th>PnL</th><th>Return</th></tr>" +
    r.trades.slice().reverse().map((t) => `<tr>
      <td>${esc(t.symbol)}</td><td>${fmt.date(t.entry_ts)}</td><td>${fmt.date(t.exit_ts)}</td>
      <td>${fmt.qty(t.qty)}</td><td>${fmt.num(t.entry_price, 4)}</td><td>${fmt.num(t.exit_price, 4)}</td>
      <td class="${sign(t.pnl)}">${fmt.num(t.pnl)}</td><td class="${sign(t.return_pct)}">${fmt.pct(t.return_pct)}</td>
    </tr>`).join("");
}

// ---- runs -----------------------------------------------------------------

let selectedRun = null;
let runTimer = null;

async function loadRuns() {
  const runs = await api("/api/runs").catch(() => []);
  $("#run-list").innerHTML = runs.length
    ? runs.map((r) => `<li data-name="${esc(r.name)}" class="${r.name === selectedRun ? "sel" : ""}">
        <div class="name">${esc(r.name)}<span class="badge ${esc(r.mode)}">${esc(r.mode)}</span></div>
        <div class="meta">${esc(r.strategy)} · ${esc(r.state)}${r.halted ? " · halted" : ""}</div>
        <div class="meta">equity ${fmt.num(r.account?.equity)}</div>
      </li>`).join("")
    : '<li class="muted">nothing running yet</li>';
  document.querySelectorAll("#run-list li[data-name]").forEach((li) =>
    li.addEventListener("click", () => { selectedRun = li.dataset.name; loadRuns(); showRun(); })
  );
}

async function showRun() {
  if (!selectedRun) return;
  const el = $("#run-detail");
  let s, eq, st;
  const base = `/api/runs/${encodeURIComponent(selectedRun)}`;
  try {
    [s, eq, st] = await Promise.all([api(base), api(`${base}/equity`), api(`${base}/stats`)]);
  } catch (err) {
    el.innerHTML = `<p class="status err">${esc(err.message)}</p>`;
    return;
  }
  const a = s.account || {};
  const stale = Date.now() - Date.parse(s.heartbeat) > Math.max(120, 4 * 30) * 1000;
  el.innerHTML = `
    <div class="tiles">
      ${tile("Equity", fmt.num(a.equity))}
      ${tile("Cash", fmt.num(a.cash))}
      ${tile("State", esc(s.state) + (stale && s.state !== "stopped" ? " (stale)" : ""), s.halted ? "down" : "")}
      ${tile("Last beat", fmt.date(s.heartbeat).slice(11))}
      ${tile("Realized", fmt.num(st.realized_pnl), st.realized_pnl > 0 ? "up" : st.realized_pnl < 0 ? "down" : "")}
      ${tile("Trades", st.trades)}
      ${tile("Win rate", st.trades ? fmt.pct(st.win_rate) : "–")}
      ${tile("Fees", fmt.num(st.commissions))}
    </div>
    ${s.halted ? `<div class="panel down">Halted: ${esc(s.halt_reason)}</div>` : ""}
    <div class="panel"><h2>Equity</h2><div id="run-chart" class="chart short"></div></div>
    <div class="panel"><h2>Positions</h2><table>
      <tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>Last</th><th>Value</th><th>PnL</th></tr>
      ${s.positions.map((p) => `<tr><td>${esc(p.symbol)}</td><td>${fmt.qty(p.qty)}</td><td>${fmt.num(p.avg_price, 4)}</td>
        <td>${fmt.num(p.last_price, 4)}</td><td>${fmt.num(p.market_value)}</td>
        <td class="${p.unrealized_pnl >= 0 ? "up" : "down"}">${fmt.num(p.unrealized_pnl)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">flat</td></tr>'}
    </table></div>
    <div class="panel"><h2>Closed trades</h2><div class="table-wrap"><table>
      <tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>In</th><th>Out</th><th>PnL</th></tr>
      ${(st.recent_trades || []).map((t) => `<tr><td>${esc(t.symbol)}</td><td>${fmt.date(t.entry_ts)}</td><td>${fmt.date(t.exit_ts)}</td>
        <td>${fmt.qty(t.qty)}</td><td>${fmt.num(t.entry_price, 4)}</td><td>${fmt.num(t.exit_price, 4)}</td>
        <td class="${t.pnl >= 0 ? "up" : "down"}">${fmt.num(t.pnl)}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">none yet</td></tr>'}
    </table></div></div>
    <div class="panel"><h2>Recent orders</h2><div class="table-wrap"><table>
      <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th><th>Note</th></tr>
      ${s.recent_orders.map((o) => `<tr><td>${fmt.date(o.created_at)}</td><td>${esc(o.symbol)}</td><td>${esc(o.side)}</td>
        <td>${fmt.qty(o.qty)}</td><td>${esc(o.type)}${o.protective ? " (exit)" : ""}</td><td>${esc(o.status)}</td><td>${esc(o.reason)}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">none</td></tr>'}
    </table></div></div>
    ${s.state !== "stopped" ? '<div><button class="btn danger" id="stop-run">Stop run</button></div>' : ""}`;
  lineChart($("#run-chart"), eq.ts, [{ name: "equity", values: eq.equity, color: "var(--accent)" }]);
  const stop = $("#stop-run");
  if (stop) stop.onclick = async () => {
    if (!confirm(`Stop ${selectedRun}? Open orders will be canceled.`)) return;
    await api(`/api/runs/${encodeURIComponent(selectedRun)}/stop`, { method: "POST" });
    stop.disabled = true;
    stop.textContent = "stopping…";
  };
}

function startRunPolling() {
  clearInterval(runTimer);
  runTimer = setInterval(() => {
    if ($("#runs").classList.contains("active")) { loadRuns(); showRun(); }
  }, 5000);
}

// ---- news -----------------------------------------------------------------

const LABEL_TEXT = { strong_bearish: "strong bearish", bearish: "bearish", neutral: "neutral", bullish: "bullish", strong_bullish: "strong bullish" };
let selectedNews = null;

function symName(s) {
  return s === "MARKET:stock" ? "US stocks" : s === "MARKET:crypto" ? "Crypto market" : s;
}

function scoreBar(score) {
  const w = Math.min(Math.abs(score), 1) * 50;
  const left = score >= 0 ? 50 : 50 - w;
  return `<div class="sbar"><span class="${score >= 0 ? "pos" : "neg"}" style="left:${left}%;width:${w}%"></span></div>`;
}

async function loadNews() {
  let body;
  try { body = await api("/api/news/verdicts"); } catch (err) {
    $("#verdict-list").innerHTML = `<li class="muted">${esc(err.message)}</li>`;
    return;
  }
  if (!body.available) {
    $("#verdict-list").innerHTML = '<li class="muted">no news database yet</li>';
    return;
  }
  const vs = body.verdicts.slice().sort((a, b) =>
    (b.symbol.startsWith("MARKET:") - a.symbol.startsWith("MARKET:")) || Math.abs(b.score) - Math.abs(a.score));
  const newest = vs.reduce((m, v) => (v.ts > m ? v.ts : m), "");
  $("#news-updated").textContent = newest ? `· ${fmt.date(newest).slice(11)}` : "";
  if (!selectedNews && vs.length) selectedNews = vs[0].symbol;
  $("#verdict-list").innerHTML = vs.map((v) => `<li data-sym="${esc(v.symbol)}" class="${v.symbol === selectedNews ? "sel" : ""}">
      <div class="name">${esc(symName(v.symbol))}<span class="lbl ${esc(v.label)}">${LABEL_TEXT[v.label] || esc(v.label)}</span></div>
      ${scoreBar(v.score)}
      <div class="meta">${v.score >= 0 ? "+" : ""}${fmt.num(v.score)} · confidence ${fmt.pct(v.confidence).replace(/\.\d+%/, "%")} · ${v.items} item${v.items === 1 ? "" : "s"}</div>
    </li>`).join("") || '<li class="muted">no verdicts yet</li>';
  document.querySelectorAll("#verdict-list li[data-sym]").forEach((li) =>
    li.addEventListener("click", () => { selectedNews = li.dataset.sym; loadNews(); })
  );
  $("#news-sources").innerHTML = "<tr><th>Source</th><th>Items</th><th>Last OK</th></tr>" + body.sources.map((s) =>
    `<tr title="${esc(s.last_error || "")}"><td>${esc(s.source)}${s.last_error ? ' <span class="down">●</span>' : ""}</td>
     <td>${s.fetched}</td><td>${s.last_ok ? fmt.date(s.last_ok).slice(5) : "–"}</td></tr>`).join("");
  const v = vs.find((x) => x.symbol === selectedNews);
  if (v) showNews(v);
}

async function showNews(v) {
  const el = $("#news-detail");
  const q = encodeURIComponent(v.symbol);
  let items = [], hist = [];
  try {
    [items, hist] = await Promise.all([api(`/api/news/items?symbol=${q}&limit=80`), api(`/api/news/history?symbol=${q}`)]);
  } catch (err) {
    el.innerHTML = `<p class="status err">${esc(err.message)}</p>`;
    return;
  }
  const cls = v.score > 0.05 ? "up" : v.score < -0.05 ? "down" : "";
  const arrow = (d) => (d == null ? '<span class="muted">·</span>' : d > 0.05 ? '<span class="up">▲</span>' : d < -0.05 ? '<span class="down">▼</span>' : '<span class="muted">■</span>');
  el.innerHTML = `
    <div class="tiles">
      ${tile("Symbol", esc(symName(v.symbol)))}
      ${tile("Verdict", LABEL_TEXT[v.label] || esc(v.label), cls)}
      ${tile("Score", (v.score >= 0 ? "+" : "") + fmt.num(v.score), cls)}
      ${tile("Confidence", Math.round(v.confidence * 100) + "%")}
      ${tile("Bullish wt", fmt.num(v.bullish_weight), "up")}
      ${tile("Bearish wt", fmt.num(v.bearish_weight), "down")}
      ${tile("Stories", v.items)}
    </div>
    <div class="panel"><h2>Score, last 72h</h2><div id="news-chart" class="chart short"></div></div>
    <div class="panel"><h2>What's driving it</h2>
      ${v.drivers.map((d) => `<div class="driver">
        <div>${arrow(d.direction)} <a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title)}</a></div>
        <div class="meta">${esc(d.source)} · ${fmt.date(d.published_at)} · weight ${fmt.num(d.weight, 3)} · contribution ${d.contribution >= 0 ? "+" : ""}${fmt.num(d.contribution, 3)}</div>
        ${d.reason ? `<div class="meta">${esc(d.reason)}</div>` : ""}
      </div>`).join("") || '<p class="muted">nothing yet</p>'}
    </div>
    <div class="panel"><h2>Feed <span class="muted">(${items.length})</span></h2><div class="table-wrap tall"><table class="feed">
      <tr><th></th><th>Time</th><th>Source</th><th>Headline</th><th>Impact</th><th>Conf</th></tr>
      ${items.map((it) => `<tr title="${esc(it.reason || "")}"><td>${arrow(it.direction)}</td><td>${fmt.date(it.published_at).slice(5)}</td>
        <td>${esc(it.source)}</td><td class="hl"><a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a>
        ${it.scope && it.scope !== "symbol" ? `<span class="badge">${esc(it.scope)}</span>` : ""}</td>
        <td>${it.impact == null ? "–" : fmt.num(it.impact * 5, 1)}</td><td>${it.confidence == null ? "–" : fmt.pct(it.confidence).replace(/\.\d+%/, "%")}</td></tr>`).join("")}
    </table></div></div>`;
  if (hist.length < 2) {
    $("#news-chart").innerHTML = '<p class="muted">the history fills in as <code>news watch</code> keeps running</p>';
  } else {
    lineChart($("#news-chart"), hist.map((h) => h.ts), [{ name: "score", values: hist.map((h) => h.score), color: "var(--accent)" }],
      { yfmt: (x) => fmt.num(x) });
  }
}

setInterval(() => { if ($("#news").classList.contains("active")) loadNews(); }, 30000);

async function waitForRefresh(statusEl) {
  const t0 = Date.now();
  for (;;) {
    const st = await api("/api/news/refresh");
    if (st.state !== "running") return st;
    statusEl.textContent = `working… ${((Date.now() - t0) / 1000).toFixed(0)}s`;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

function refreshSummary(st) {
  if (st.state === "failed") return st.error;
  if (st.state !== "done") return "";
  const errs = Object.keys(st.errors || {}).length;
  return `${st.new_items} new, ${st.scored} scored in ${st.seconds}s` + (errs ? ` · ${errs} source${errs > 1 ? "s" : ""} failed` : "");
}

async function initNewsControls() {
  const btn = $("#news-refresh"), status = $("#news-refresh-status");
  const st = await api("/api/news/refresh").catch(() => ({ enabled: false }));
  if (!st.enabled) {
    btn.disabled = true;
    status.textContent = "start the dashboard with --news-config to refresh from here";
    $("#news-add-btn").disabled = true;
    $("#news-add-status").textContent = "needs --news-config, like the refresh button";
    return;
  }
  if (st.finished) status.textContent = `last: ${fmt.date(st.finished).slice(5)} · ${refreshSummary(st)}`;
  btn.onclick = async () => {
    btn.disabled = true;
    status.className = "status";
    try {
      await api("/api/news/refresh", { method: "POST" });
      const done = await waitForRefresh(status);
      status.className = done.state === "failed" ? "status err" : "status";
      status.textContent = refreshSummary(done);
      loadNews();
    } catch (err) {
      status.className = "status err";
      status.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  };
}

$("#news-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, status = $("#news-add-status"), btn = $("#news-add-btn");
  const text = f.text.value.trim();
  if (!text) return;
  const item = {
    title: text.slice(0, 280), body: text.slice(280), kind: f.kind.value,
    author: f.author.value.trim(), url: f.url.value.trim(), published_at: new Date().toISOString(),
    symbols: f.symbols.value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  if (f.trusted.checked) item.credibility = f.kind.value === "social" ? 0.7 : 0.95;
  btn.disabled = true;
  status.className = "status";
  status.textContent = "scoring…";
  try {
    await api("/api/news/ingest", { method: "POST", body: JSON.stringify({ items: [item], process: true }) });
    const done = await waitForRefresh(status);
    if (done.state === "failed") throw new Error(done.error);
    status.textContent = "added";
    f.text.value = ""; f.url.value = "";
    if (item.symbols.length) selectedNews = item.symbols[0].toUpperCase().split("/")[0];
    loadNews();
  } catch (err) {
    status.className = "status err";
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

initNewsControls();

loadStrategies().catch((e) => ($("#bt-status").textContent = e.message));
syncSourceFields();
startRunPolling();
