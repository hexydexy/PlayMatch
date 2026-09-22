/* PlayMatch UI: hash router (#/ = games list, #/game/<id> = profile), no build step. */
(() => {
"use strict";

const API = "/api";
const DAY = 86400000;
const DENSE = 120;   // above this many samples, markers shrink to keep the line readable
const STORE_LOOK = {steam: "circle", gog: "square", epic: "diamond"};
const STORE_NAMES = {steam: "Steam", gog: "GOG", epic: "Epic Games Store"};

const $ = (s, r = document) => r.querySelector(s);
const view = $("#view");
const store = {
  get: k => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};
const state = {view: null, seq: 0, searchSeq: 0, searching: false, q: "", shownQ: "", games: null, total: 0, scrollY: 0,
               gameId: null, region: "US", regions: [], currency: "USD", ro: null, epicTried: null};

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const get = async p => { const r = await fetch(API + p); if (!r.ok) throw new Error(r.status); return r.json(); };
const post = async p => { const r = await fetch(API + p, {method: "POST"}); if (!r.ok) throw new Error(r.status); return r.json(); };
const EPIC_STALE_MS = 24 * 3600e3, OLD_PRICE_MS = 14 * 24 * 3600e3;
// A persistent live region outside #view, so a screen reader hears Epic-check updates even
// when the profile is fully re-rendered (a freshly-inserted role="status" node with content
// already in place is not reliably announced).
const announce = msg => { const el = $("#live"); if (el) el.textContent = msg; };
const money = (c, cur, compact) => c == null ? "—" : new Intl.NumberFormat(undefined,
  {style: "currency", currency: cur, ...(compact && c % 100 === 0 ? {minimumFractionDigits: 0, maximumFractionDigits: 0} : {})}).format(c / 100);
const fmtDate = (t, year) => new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", ...(year ? {year: "numeric"} : {})}).format(t);
const dayKey = t => { const d = new Date(t); return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); };
const color = id => `var(--c-${STORE_LOOK[id] ? id : "other"})`;

/* ---- cover art ---------------------------------------------------------- */

function tile(g, cls) {
  let h = 0;
  for (const ch of g.title) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `<div class="${cls} art tile" style="--h:${h}" aria-hidden="true">${esc(g.title)}</div>`;
}
function art(g, cls = "", eager = false) {
  return g.image_url
    ? `<img class="${cls} art" data-art data-title="${esc(g.title)}" src="${esc(g.image_url)}" alt="" width="460" height="215" loading="${eager ? "eager" : "lazy"}">`
    : tile(g, cls);
}
// Broken or blocked cover image: swap in the typographic tile. Error events don't bubble, hence capture.
document.addEventListener("error", e => {
  const img = e.target;
  if (img instanceof HTMLImageElement && img.dataset.art !== undefined) {
    const t = document.createElement("template");
    t.innerHTML = tile({title: img.dataset.title}, img.className.replace(/\bart\b/, "").trim());
    img.replaceWith(t.content.firstChild);
  }
}, true);

/* ---- shell: nav + selectors -------------------------------------------- */

function syncNav() {
  // "Games" stays highlighted on a profile too, since a profile lives under it.
  const a = $("#nav-games");
  a.classList.add("is-active");
  if (state.view === "list") a.setAttribute("aria-current", "page");
  else a.removeAttribute("aria-current");
}

async function init() {
  try {
    const r = await get("/regions");
    state.regions = r.enabled;
    const saved = store.get("pm_reg");
    state.region = saved && state.regions.some(x => x.code === saved) ? saved : r.default;
    if (state.regions.length > 1) {
      $("#reg").hidden = false;
      $("#reg").innerHTML = state.regions.map(x => `<option value="${esc(x.code)}" ${x.code === state.region ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    }
  } catch {}
  const home = (state.regions.find(x => x.code === state.region) || {currency: "USD"}).currency;
  state.currency = store.get("pm_cur") || home;
  try {
    const c = await get("/currencies");
    const list = [...new Set([home, c.base, ...c.supported])];
    if (!list.includes(state.currency)) state.currency = home;
    $("#cur").innerHTML = list.map(x => `<option ${x === state.currency ? "selected" : ""}>${esc(x)}</option>`).join("");
  } catch { $("#cur").innerHTML = `<option>${esc(state.currency)}</option>`; }

  $("#cur").addEventListener("change", e => { state.currency = e.target.value; store.set("pm_cur", state.currency); reloadGame(); });
  $("#reg").addEventListener("change", e => {
    state.region = e.target.value; store.set("pm_reg", state.region);
    state.currency = state.regions.find(x => x.code === state.region).currency; store.set("pm_cur", state.currency);
    $("#cur").value = state.currency; reloadGame();
  });
  $("[data-skip]").addEventListener("click", e => { e.preventDefault(); view.focus({preventScroll: false}); });
  $(".brand").addEventListener("click", () => { if (state.view === "list") window.scrollTo(0, 0); });
  window.addEventListener("hashchange", route);
  route();
}

/* ---- router ------------------------------------------------------------- */

function route() {
  if (state.view === "list") state.scrollY = window.scrollY;   // remember where the list was
  const m = /^#\/game\/(\d+)\/?$/.exec(location.hash);
  const seq = ++state.seq;
  announce("");   // a stale or repeated Epic-check announcement should not linger or go unheard on the new page
  if (m) { state.view = "game"; state.gameId = +m[1]; state.epicTried = null; showGame(seq); }
  else { state.view = "list"; showList(); }
  syncNav();
}
const reloadGame = () => { if (state.view === "game") showGame(++state.seq, true); };
const focusHeading = () => { const h = $("h1", view); if (h) { h.tabIndex = -1; h.focus({preventScroll: true}); } };

/* ---- games list --------------------------------------------------------- */

const PAGE = 48;

async function getPage(q, offset) {
  const r = await fetch(`${API}/games?limit=${PAGE}&offset=${offset}&q=${encodeURIComponent(q)}`);
  if (!r.ok) throw new Error(r.status);
  return {games: await r.json(), total: Number(r.headers.get("X-Total-Count")) || 0};
}

function showList() {
  document.title = "PlayMatch";
  if (state.ro) { state.ro.disconnect(); state.ro = null; }
  view.innerHTML = `<div class="wrap">
    <section class="intro">
      <h1>Find the lowest price for a PC game</h1>
      <p>Compare Steam, GOG and Epic side by side, and see whether today's price is a good one.</p>
      <input id="q" class="search" type="search" placeholder="Search a game, for example Hades" aria-label="Search games" autocomplete="off">
    </section>
    <p id="count" class="count muted" role="status"></p>
    <ul id="grid" class="grid"></ul>
    <div id="more-wrap" class="more-wrap"></div>
    <div id="notice"></div>
  </div>`;
  const q = $("#q");
  q.value = state.q;
  let timer;
  q.addEventListener("input", e => { clearTimeout(timer); timer = setTimeout(() => search(e.target.value.trim()), 200); });
  const cached = state.games !== null;
  if (cached) renderGrid();
  else search(state.q);
  window.scrollTo(0, cached ? state.scrollY : 0);
  if (!cached) focusHeading();
}

async function search(q) {
  state.q = q;
  const n = ++state.searchSeq;
  state.searching = true;
  try {
    const {games, total} = await getPage(q, 0);
    if (n !== state.searchSeq || state.view !== "list") return;
    state.games = games; state.total = total; state.shownQ = q;
    renderGrid();
  } catch {
    if (n !== state.searchSeq || state.view !== "list") return;
    $("#grid").innerHTML = ""; $("#count").textContent = ""; $("#more-wrap").innerHTML = "";
    $("#notice").innerHTML = `<div class="notice"><strong>Can't reach the price service</strong>
      <p>Check that PlayMatch is running, then try again.</p><button class="btn" id="retry">Try again</button></div>`;
    $("#retry").onclick = () => search(state.q);
  } finally {
    if (n === state.searchSeq) state.searching = false;
  }
}

async function loadMore() {
  if (state.searching) return;
  const seq = state.searchSeq, shown = state.games.length, btn = $("#more");
  btn.disabled = true; btn.textContent = "Loading…";
  try {
    const {games, total} = await getPage(state.q, shown);
    if (seq !== state.searchSeq || state.view !== "list") return;   // a newer search replaced this list
    state.games = state.games.concat(games); state.total = total;
    renderGrid();
    const first = view.querySelectorAll("#grid .game")[shown];
    if (first) first.focus({preventScroll: true});
  } catch {
    if (seq === state.searchSeq && btn.isConnected) { btn.disabled = false; btn.textContent = "Couldn't load more. Try again"; }
  }
}

function renderGrid() {
  // Use the query the grid was actually fetched with, not state.q, which may already have
  // moved on to a newer (still in-flight) search by the time this cached list is re-shown.
  const games = state.games || [], total = state.total.toLocaleString(), q = state.shownQ;
  $("#notice").innerHTML = "";
  $("#count").textContent = q
    ? `${total} ${state.total === 1 ? "game matches" : "games match"} “${q}”`
    : `${total} tracked ${state.total === 1 ? "game" : "games"}`;
  $("#grid").innerHTML = games.map(g => `<li><a class="game" href="#/game/${g.id}">${art(g)}
      <span class="game-body"><span class="game-title">${esc(g.title)}</span><span class="game-year">${g.release_year ?? ""}</span></span></a></li>`).join("");
  $("#more-wrap").innerHTML = games.length < state.total
    ? `<button class="btn more" id="more">Load more</button><p class="muted">Showing ${games.length.toLocaleString()} of ${total}</p>` : "";
  const more = $("#more"); if (more) more.onclick = loadMore;
  if (!games.length) $("#notice").innerHTML = q
    ? `<div class="notice"><strong>No tracked game matches “${esc(q)}”</strong><p>Check the spelling, or try a shorter title.</p></div>`
    : `<div class="notice"><strong>No games are tracked yet</strong><p>Seed the list with <code>snapshot --seed</code>, or add a game through the admin API.</p></div>`;
}

/* ---- game profile ------------------------------------------------------- */

const backLink = `<a class="back" href="#/"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 3 5 8l5 5"/></svg>All games</a>`;

async function showGame(seq, keepScroll, quiet) {
  if (state.ro) { state.ro.disconnect(); state.ro = null; }
  if (!quiet) {
    if (!keepScroll) window.scrollTo(0, 0);
    view.innerHTML = `<div class="wrap">${backLink}<p class="loading" role="status">Loading prices…</p></div>`;
  }
  const id = state.gameId, qs = `currency=${state.currency}&region=${state.region}`;
  try {
    const [p, h] = await Promise.all([get(`/games/${id}/prices?${qs}`), get(`/games/${id}/history?${qs}&days=365`)]);
    if (seq !== state.seq) return;
    const epicKey = `${id}:${state.region}`;
    const lookup = !quiet && needsEpicCheck(p) && state.epicTried !== epicKey;
    // Capture right before the DOM swap and restore right after it, not around the fetch above:
    // that window is synchronous, so nothing the user does in between can be clobbered.
    const restore = quiet ? captureViewState() : null;
    document.title = `${p.game.title} – PlayMatch`;
    view.innerHTML = renderProfile(p, h, lookup ? "checking" : null);
    const model = buildModel(h, p);
    if (model) mountChart($("#chart"), model, h.currency, p.game.title);
    if (restore) restoreViewState(restore);
    if (!quiet && !keepScroll) focusHeading();
    if (lookup) { state.epicTried = epicKey; announce(EPIC_NOTE.checking); runEpicCheck(seq); }
  } catch (e) {
    if (seq !== state.seq) return;
    const missing = e.message === "404";
    view.innerHTML = `<div class="wrap">${backLink}<div class="notice"><strong>${missing ? "PlayMatch doesn't track that game" : "Couldn't load prices for this game"}</strong>
      <p>${missing ? "It may have been removed. Go back to the list to pick another." : "The price service didn't respond. Try again in a moment."}</p>
      ${missing ? "" : `<button class="btn" id="retry">Try again</button>`}</div></div>`;
    const r = $("#retry"); if (r) r.onclick = () => showGame(++state.seq);
  }
}

// The Epic price is fetched when a game is opened and it was not checked in the last day.
function needsEpicCheck(p) {
  const t = p.checks && p.checks.epic;
  return !t || Date.now() - Date.parse(t) > EPIC_STALE_MS;
}

const EPIC_NOTE = {
  checking: "Checking the Epic price…",
  busy: "Epic lookups are busy. Open this game again in a minute.",
  unavailable: "Epic price unavailable right now. Open this game again later.",
};

// The quiet re-render replaces the whole profile, which would otherwise silently drop focus
// to <body>, collapse an open "All sample dates" and move the scroll position; save and put
// them back so a background price refresh the user did not ask for does not disturb them.
function captureViewState() {
  return {scrollY: window.scrollY, hadFocus: view.contains(document.activeElement),
          samplesOpen: $(".samples", view)?.open};
}
function restoreViewState(st) {
  const samples = $(".samples", view);
  if (samples && st.samplesOpen) samples.open = true;
  if (st.hadFocus) focusHeading();
  window.scrollTo(0, st.scrollY);
}

async function runEpicCheck(seq) {
  let status = "unavailable";
  try { status = (await post(`/games/${state.gameId}/epic-check?region=${state.region}`)).status; } catch {}
  if (seq !== state.seq) return;
  if (status === "checked" || status === "fresh") {
    announce("Epic price checked.");
    showGame(seq, true, true);   // re-render with the new prices; showGame itself preserves scroll/focus/details
    return;
  }
  announce(EPIC_NOTE[status] || EPIC_NOTE.unavailable);
  const cell = $("#epic-row td.muted");
  if (cell) cell.textContent = EPIC_NOTE[status] || EPIC_NOTE.unavailable;
}

const ptag = (cents, cur, cls = "") => `<span class="ptag ${cls}">${money(cents, cur)}</span>`;

function renderProfile(p, h, epic) {
  const g = p.game, cur = p.currency;
  const best = p.prices.find(r => r.is_lowest) || p.prices[0];
  const safeArt = g.image_url && /^https:\/\/[^\s'"()]+$/.test(g.image_url) ? `style="--art:url(${g.image_url})"` : "";
  const headInfo = best ? `<div class="pf-best">
      <span class="label">Lowest price right now</span>
      <div class="pf-best-row">${ptag(best.price_cents, best.currency, "big is-low")}
        <span><b class="store-name">${esc(best.store)}</b>${best.discount_pct ? `<span class="off">−${best.discount_pct}%</span>` : ""}</span></div>
      <p class="pf-facts"><span>Historical low <b>${money(p.historical_low_cents, cur)}</b></span>
        ${p.at_historical_low ? `<span class="at-low"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 8.5 3.2 3L13 4.5"/></svg>Matches the historical low</span>` : ""}
        <span>${esc(p.region)} storefront</span></p></div>`
    : `<div class="pf-best"><p class="muted">No prices recorded yet. They appear after the next daily snapshot.</p></div>`;
  return `<section class="pf-head" ${safeArt}><div class="wrap">${backLink}
      <div class="pf-grid"><div class="pf-cover">${art(g, "", true)}</div>
        <div><h1>${esc(g.title)}</h1>${g.release_year ? `<p class="pf-year">${g.release_year}</p>` : ""}${headInfo}</div></div></div></section>
    <div class="wrap pf-body">${p.prices.length || epic ? pricesPanel(p, epic) : ""}${historyPanel(h, p)}</div>`;
}

function pricesPanel(p, epic) {
  const rows = p.prices.map(r => {
    const old = r.checked_at && Date.now() - Date.parse(r.checked_at) > OLD_PRICE_MS;
    const checked = r.checked_at ? `<span class="conv muted ${old ? "stale" : ""}">${old ? "Price may be out of date. " : ""}Checked ${fmtDate(Date.parse(r.checked_at), true)}</span>` : "";
    return `<tr>
    <td><span class="store-name">${esc(r.store)}</span><span class="kind ${r.store_type === "marketplace" ? "mk" : ""}">${esc(r.label)}</span></td>
    <td>${ptag(r.price_cents, r.currency, r.is_lowest ? "is-low" : "")}${r.is_lowest ? `<span class="vh"> lowest</span>` : ""}
      ${r.discount_pct ? `<span class="was">${money(r.base_price_cents, r.currency)}</span><span class="off">−${r.discount_pct}%</span>` : ""}
      ${r.converted ? `<span class="conv muted">converted from ${money(r.native_price_cents, r.native_currency)}</span>` : ""}${checked}</td>
    <td class="go">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">View<span class="vh"> ${esc(p.game.title)} on ${esc(r.store)}</span></a>` : ""}</td></tr>`;
  }).join("");
  const hasEpic = p.prices.some(r => r.store_id === "epic");
  const epicRow = epic && !hasEpic
    ? `<tr id="epic-row"><td><span class="store-name">Epic Games</span></td><td colspan="2" class="muted">${EPIC_NOTE[epic]}</td></tr>` : "";
  return `<section class="panel"><div class="panel-head"><h2>Prices by store</h2></div>
    <div class="scroll"><table class="prices"><thead><tr><th>Store</th><th>Price</th><th><span class="vh">Link</span></th></tr></thead><tbody>${rows}${epicRow}</tbody></table></div></section>`;
}

/* ---- price history ------------------------------------------------------ */

// One place that turns the API history into series + a list of sample days,
// shared by the chart, the stats table and the sample list.
function buildModel(h, p) {
  const stores = Object.entries(h.stores).map(([id, s]) => ({
    id, name: (p.prices.find(r => r.store_id === id) || {}).store || STORE_NAMES[id] || id,
    points: s.points.map(x => ({t: Date.parse(x.t), v: x.price_cents})).sort((a, b) => a.t - b.t),
    low: s.low_cents, avg: s.avg_cents, high: s.high_cents,
  })).filter(s => s.points.length);
  if (!stores.length) return null;
  const byDay = new Map();
  for (const s of stores) for (const pt of s.points) {
    const k = dayKey(pt.t), cur = byDay.get(k);
    if (!cur || pt.t < cur.t) byDay.set(k, {key: k, t: pt.t});
  }
  const days = [...byDay.values()].sort((a, b) => a.t - b.t);
  for (const d of days) { const x = new Date(d.t); d.end = new Date(x.getFullYear(), x.getMonth(), x.getDate() + 1).getTime(); }
  // Price in force at the end of a day: the last sample on or before it (prices hold until the next sample).
  const valueAt = (s, day) => { let v = null; for (const pt of s.points) { if (pt.t < day.end) v = pt.v; else break; } return v; };
  // A price only if this store was actually sampled that day.
  const sampledOn = (s, day) => { let v = null; for (const pt of s.points) if (dayKey(pt.t) === day.key) v = pt.v; return v; };
  const all = stores.flatMap(s => s.points);
  return {stores, days, valueAt, sampledOn, t0: days[0].t, t1: Math.max(...all.map(x => x.t)), vmax: Math.max(...all.map(x => x.v))};
}

function markerSvg(shape, c, size = 12) {
  const h = size / 2;
  const el = shape === "square" ? `<rect x="1.5" y="1.5" width="${size - 3}" height="${size - 3}" rx="1.5" fill="${c}"/>`
    : shape === "diamond" ? `<path d="M${h} 0.5 ${size - .5} ${h} ${h} ${size - .5} .5 ${h}z" fill="${c}"/>`
    : `<circle cx="${h}" cy="${h}" r="${h - 1}" fill="${c}"/>`;
  return `<svg viewBox="0 0 ${size} ${size}" aria-hidden="true">${el}</svg>`;
}

function historyPanel(h, p) {
  const m = buildModel(h, p);
  if (!m) return `<section class="panel"><div class="panel-head"><h2>Price history</h2></div>
    <p class="muted">No price history yet. A price is recorded the first time PlayMatch sees it, and again whenever it changes.</p></section>`;
  const cur = h.currency, n = m.stores.reduce((a, s) => a + s.points.length, 0);
  const span = m.days.length === 1 ? fmtDate(m.t0, true) : `${fmtDate(m.t0, true)} to ${fmtDate(m.t1, true)}`;
  const caption = `${m.days.length} sample ${m.days.length === 1 ? "date" : "dates"}, ${span}. ${n <= DENSE ? "Each marker is one recorded price." : "Hover or use the arrow keys to read the price on any date."}`;
  const legend = m.stores.map(s => `<li>${markerSvg(STORE_LOOK[s.id] || "circle", color(s.id))}${esc(s.name)}</li>`).join("");
  const stats = m.stores.map(s => `<tr><td>${esc(s.name)}</td><td class="num">${money(s.low, cur)}</td><td class="num">${money(s.avg, cur)}</td><td class="num">${money(s.high, cur)}</td>
    <td class="num">${s.points.length}</td><td class="num">${s.points.length > 1 && s.points[0].t !== s.points.at(-1).t ? `${fmtDate(s.points[0].t, true)} to ${fmtDate(s.points.at(-1).t, true)}` : fmtDate(s.points[0].t, true)}</td></tr>`).join("");
  const sampleRows = m.days.map(d => `<tr><td class="num">${fmtDate(d.t, true)}</td>${m.stores.map(s => `<td class="num">${money(m.sampledOn(s, d), cur)}</td>`).join("")}</tr>`).join("");
  return `<section class="panel"><div class="panel-head"><h2>Price history</h2>
      <p class="muted">${caption}</p></div>
    <ul class="legend">${legend}</ul>
    <div id="chart" class="chart"></div>
    <div class="scroll stats"><table><thead><tr><th>Store</th><th class="num">Low</th><th class="num">Average per sample</th><th class="num">High</th><th class="num">Samples</th><th>Sampled</th></tr></thead><tbody>${stats}</tbody></table></div>
    <details class="samples"><summary>All sample dates</summary><div class="scroll"><table><thead><tr><th>Date</th>${m.stores.map(s => `<th>${esc(s.name)}</th>`).join("")}</tr></thead><tbody>${sampleRows}</tbody></table></div></details>
  </section>`;
}

function niceScale(vmax) {
  if (vmax <= 0) return {step: 100, max: 400};
  const raw = vmax / 4, pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(k => k * pow).find(s => s >= raw);
  return {step, max: Math.ceil(vmax / step) * step};
}

function mountChart(host, m, cur, title) {
  let lastW = 0;
  const multiYear = new Date(m.t0).getFullYear() !== new Date(m.t1).getFullYear() || m.t1 - m.t0 > 300 * DAY;

  function draw() {
    const W = lastW = Math.max(280, Math.round(host.clientWidth)), H = W < 520 ? 250 : 300;
    const mg = {l: 52, r: 24, t: 14, b: 36}, inset = 8;
    const pw = W - mg.l - mg.r, ph = H - mg.t - mg.b;
    let t0 = m.t0, t1 = m.t1;
    if (t1 <= t0) { t0 -= DAY / 2; t1 += DAY / 2; }   // a single sample sits in the middle
    const X = t => mg.l + inset + (t - t0) / (t1 - t0) * (pw - 2 * inset);
    const {step, max} = niceScale(m.vmax);
    const Y = v => mg.t + ph - v / max * ph;

    let grid = "";
    for (let v = 0; v <= max; v += step) {
      grid += `<line x1="${mg.l}" x2="${W - mg.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/>
        <text x="${mg.l - 8}" y="${Y(v) + 4}" text-anchor="end">${money(Math.round(v), cur, true)}</text>`;
    }

    // Date labels sit on real sample dates when they fit; otherwise on evenly spaced dates.
    const room = pw - 2 * inset;
    let labelW = multiYear ? 92 : 62, gap = labelW + 10;
    let maxTicks = Math.max(2, Math.floor(room / gap));
    let ticks, fmt = t => fmtDate(t, multiYear);
    if (m.days.length <= maxTicks) {
      ticks = [m.days[0].t];
      for (const d of m.days.slice(1, -1)) if (X(d.t) - X(ticks.at(-1)) >= gap) ticks.push(d.t);
      const last = m.days.at(-1).t;
      if (last !== ticks[0]) { if (X(last) - X(ticks.at(-1)) >= gap) ticks.push(last); else if (ticks.length > 1) ticks[ticks.length - 1] = last; else ticks.push(last); }
    } else {
      // Too many sample dates to label each: space ticks evenly. Over a year or more, month + year reads better than a day.
      if (multiYear) { labelW = 66; fmt = t => new Intl.DateTimeFormat(undefined, {month: "short", year: "numeric"}).format(t); }
      maxTicks = Math.max(2, Math.floor(room / (labelW + 10)));
      ticks = Array.from({length: maxTicks}, (_, i) => t0 + (t1 - t0) * i / (maxTicks - 1));
    }
    const xAxis = ticks.map(t => {
      const x = X(t), anchor = x - labelW / 2 < 0 ? "start" : x + labelW / 2 > W ? "end" : "middle";
      return `<line x1="${x}" x2="${x}" y1="${mg.t + ph}" y2="${mg.t + ph + 5}" stroke="var(--slate)"/>
        <text x="${x}" y="${H - 10}" text-anchor="${anchor}">${fmt(t)}</text>`;
    }).join("");

    const n = m.stores.reduce((a, s) => a + s.points.length, 0), dense = n > DENSE;
    const r = n <= 40 ? 4.5 : dense ? 1.5 : 3;
    const outline = dense ? "" : ` stroke="var(--surface)" stroke-width="1.5"`;
    const mark = (shape, x, y, c, k) => shape === "square" ? `<rect x="${x - k}" y="${y - k}" width="${2 * k}" height="${2 * k}" rx="1" fill="${c}"${outline}/>`
      : shape === "diamond" ? `<path d="M${x} ${y - k * 1.35}L${x + k * 1.35} ${y}L${x} ${y + k * 1.35}L${x - k * 1.35} ${y}Z" fill="${c}"${outline}/>`
      : `<circle cx="${x}" cy="${y}" r="${k}" fill="${c}"${outline}/>`;
    // Stores often share a price (Steam and Epic list the same MSRP). Earlier series are drawn thicker and larger
    // underneath, so identical prices show as nested layers instead of one hiding the other.
    const series = m.stores.map((s, i) => {
      const c = color(s.id), shape = STORE_LOOK[s.id] || "circle", extra = m.stores.length - 1 - i;
      const d = s.points.map((p, j) => j ? `H${X(p.t)}V${Y(p.v)}` : `M${X(p.t)} ${Y(p.v)}`).join("");   // step line: a price holds until the next sample
      return `<path d="${d}" fill="none" stroke="${c}" stroke-width="${2.25 + extra * 1.7}" stroke-linejoin="round"/>${s.points.map(p => mark(shape, X(p.t), Y(p.v), c, r + extra * 1.7)).join("")}`;
    }).join("");
    const rings = m.stores.map(s => `<circle data-ring="${s.id}" r="${r + 4}" fill="none" stroke="${color(s.id)}" stroke-width="2" visibility="hidden"/>`).join("");
    const label = `Price history for ${title}: ${m.days.length} sample dates from ${fmtDate(m.t0, true)} to ${fmtDate(m.t1, true)}. Use the left and right arrow keys to step through dates.`;

    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" tabindex="0" role="img" aria-label="${esc(label)}">
        ${grid}${xAxis}${series}
        <line data-guide y1="${mg.t}" y2="${mg.t + ph}" stroke="var(--ink)" stroke-dasharray="3 3" visibility="hidden"/>${rings}
        <rect data-hit x="${mg.l}" y="${mg.t}" width="${pw}" height="${ph}" fill="transparent"/></svg>
      <div class="tip" hidden></div><div class="vh" aria-live="polite"></div>`;

    const svg = $("svg", host), tip = $(".tip", host), live = $("[aria-live]", host), guide = $("[data-guide]", host);
    let active = -1;

    function setActive(i, announce) {
      active = i;
      const d = m.days[i], x = X(d.t);
      guide.setAttribute("x1", x); guide.setAttribute("x2", x); guide.setAttribute("visibility", "visible");
      const rows = m.stores.map(s => {
        const v = m.valueAt(s, d), ring = $(`[data-ring="${s.id}"]`, host);
        if (v == null) ring.setAttribute("visibility", "hidden");
        else { ring.setAttribute("cx", x); ring.setAttribute("cy", Y(v)); ring.setAttribute("visibility", "visible"); }
        return {s, v};
      });
      tip.innerHTML = `<div class="d">${fmtDate(d.t, true)}</div>` + rows.map(({s, v}) =>
        `<div class="r"><span>${markerSvg(STORE_LOOK[s.id] || "circle", color(s.id), 10)}${esc(s.name)}</span><b>${money(v, cur)}</b></div>`).join("");
      tip.hidden = false;
      const tw = tip.offsetWidth;
      tip.style.left = Math.max(4, x + 14 + tw > W - 4 ? x - 14 - tw : x + 14) + "px";
      tip.style.top = mg.t + "px";
      if (announce) live.textContent = `${fmtDate(d.t, true)}: ` + rows.map(({s, v}) => `${s.name} ${money(v, cur)}`).join(", ");
    }
    function clear() {
      active = -1; tip.hidden = true; guide.setAttribute("visibility", "hidden");
      host.querySelectorAll("[data-ring]").forEach(c => c.setAttribute("visibility", "hidden"));
    }
    const nearest = clientX => {
      const x = clientX - svg.getBoundingClientRect().left;
      let bi = 0, bd = Infinity;
      m.days.forEach((d, i) => { const dd = Math.abs(X(d.t) - x); if (dd < bd) { bd = dd; bi = i; } });
      return bi;
    };
    $("[data-hit]", host).addEventListener("pointermove", e => setActive(nearest(e.clientX)));
    svg.addEventListener("pointerleave", () => { if (document.activeElement !== svg) clear(); });
    svg.addEventListener("focus", () => { if (active < 0) setActive(m.days.length - 1, true); });
    svg.addEventListener("blur", clear);
    svg.addEventListener("keydown", e => {
      const k = {ArrowLeft: active - 1, ArrowRight: active + 1, Home: 0, End: m.days.length - 1}[e.key];
      if (k === undefined) return;
      e.preventDefault();
      setActive(Math.min(m.days.length - 1, Math.max(0, k)), true);
    });
  }

  draw();
  state.ro = new ResizeObserver(() => { if (Math.round(host.clientWidth) !== lastW) draw(); });
  state.ro.observe(host);
}

init();
})();
