// Risk page interactivity: per-position tabs, IV override, assignment
// calculator, and the Scenario Lab (GET /api/risk/scenario-lab).
//
// Each position card is keyed by its 1-based loop index `i`; element ids
// follow `<role>-<i>` and the card root carries data-contract.

const GREEN = '#22c55e', AMBER = '#f59e0b', RED = '#ef4444', BLUE = '#3b82f6', MUTED = '#888', DIM = '#666';

const fmt = {
    usd: (v, d = 2) => v == null ? '--' : `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d})}`,
    signedUsd: (v) => v == null ? '--' : `${v >= 0 ? '+' : '-'}$${Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`,
    pct: (v, d = 1) => v == null ? '--' : `${v.toFixed(d)}%`,
    signedPct: (v, d = 1) => v == null ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`,
    prob: (p) => p == null ? '--' : `${(p * 100).toFixed(1)}%`,
    iv: (v) => v == null ? '--' : `${(v * 100).toFixed(1)}%`,
    date: (iso) => {
        if (!iso) return '--';
        const [y, m, d] = iso.split('-').map(Number);
        return new Date(y, m - 1, d).toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
    },
};

const pnlColor = (v) => v == null ? DIM : v >= 0 ? GREEN : RED;
const probColor = (p) => p > 0.7 ? GREEN : p > 0.4 ? AMBER : RED;
const assignColor = (pct) => pct > 50 ? RED : pct > 25 ? AMBER : GREEN;

const SPREAD_LABEL = {
    tight: ['tight', GREEN], moderate: ['moderate', GREEN], wide: ['wide', AMBER],
    very_wide: ['very wide', RED], no_bid: ['no bid', RED], no_quote: ['no quote', RED],
};

const IV_SOURCE_LABEL = {
    override: 'pinned override', implied_from_mid: 'implied from mid', contract: 'contract (scraped)',
    symbol: 'symbol average', default: 'default', manual: 'manual',
};

const labState = {};  // i -> {loaded, data}

function el(id) { return document.getElementById(id); }

// Symbol, strategy and error text originate from imported CSVs and scraped
// pages; escape before interpolating into innerHTML.
function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

// ---------------------------------------------------------------- tabs

function showTab(i, tab) {
    const card = el(`position-${i}`);
    card.querySelectorAll('.pos-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    card.querySelectorAll('.pos-pane').forEach(p => { p.hidden = p.dataset.pane !== tab; });
    // The lab's tables need more room than a half-width grid cell.
    card.classList.toggle('lab-wide', tab === 'lab');
    if (tab === 'lab' && !labState[i]?.loaded) loadLab(i);
}

function openLab(i) {
    showTab(i, 'lab');
    el(`position-${i}`).scrollIntoView({behavior: 'smooth', block: 'start'});
}

// ---------------------------------------------------------------- IV override

function volInput(i) { return el(`volatility-${i}`); }

// The IV box follows whatever the lab auto-selected until the user edits it.
function manualVolatility(i) {
    const input = volInput(i);
    if (!input || input.dataset.dirty !== '1') return null;
    const v = parseFloat(input.value);
    return v > 0 ? v / 100 : null;
}

function resetVolatility(i) {
    const input = volInput(i);
    input.dataset.dirty = '0';
    loadLab(i, currentTarget(i));
}

async function saveIvOverride(contractId, i) {
    const status = el(`iv-override-status-${i}`);
    const vol = parseFloat(volInput(i).value);
    if (!vol || vol <= 0) {
        status.innerHTML = `<span style="color:${RED}">Invalid IV</span>`;
        return;
    }
    try {
        const res = await fetch(`/api/positions/${encodeURIComponent(contractId)}/iv-override`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({volatility: vol / 100}),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({detail: res.statusText}));
            status.innerHTML = `<span style="color:${RED}">${esc(err.detail || 'error')}</span>`;
            return;
        }
        status.innerHTML = `<span style="color:${GREEN}">Pinned. Reload page to update summary.</span>`;
    } catch (e) {
        status.innerHTML = `<span style="color:${RED}">${esc(e.message)}</span>`;
    }
}

async function clearIvOverride(contractId) {
    try {
        const res = await fetch(`/api/positions/${encodeURIComponent(contractId)}/iv-override`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({volatility: null}),
        });
        if (!res.ok) { alert('Failed to clear override'); return; }
        location.reload();
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

// ---------------------------------------------------------------- assignment calculator

async function calculateAssignment(contractId, i) {
    const price = parseFloat(el(`assign-price-${i}`).value);
    const out = el(`assign-result-${i}`);
    if (!(price > 0)) { alert('Please enter a valid assignment price'); return; }

    const vol = manualVolatility(i) ?? labState[i]?.data?.volatility?.used ?? (parseFloat(volInput(i).value) / 100);
    const url = `/api/risk/calculate-exit?contract_id=${encodeURIComponent(contractId)}&volatility=${vol}&assignment_price=${price}`;
    out.innerHTML = `<span style="color:${MUTED}">Calculating...</span>`;
    try {
        const data = await (await fetch(url)).json();
        if (data.error) { out.innerHTML = `<span style="color:${RED}">Error: ${esc(data.error)}</span>`; return; }
        const s = data.assignment_scenario;
        if (!s) { out.innerHTML = `<span style="color:${MUTED}">No result</span>`; return; }
        out.innerHTML = `
            <div class="lab-box">
                ${row('P&L', `<span style="color:${pnlColor(s.pnl)};font-weight:600">${fmt.usd(s.pnl)}</span>`)}
                ${row('Return on risk', fmt.pct(s.pnl_percent))}
                ${row('Assignment prob (at price)', `<span style="color:${assignColor(s.assignment_probability)};font-weight:600">${fmt.pct(s.assignment_probability)}</span>`)}
                ${s.price_probability != null ? row('Price likelihood', `<span style="color:${probColor(s.price_probability / 100)};font-weight:600">${fmt.pct(s.price_probability)}</span>`) : ''}
                ${s.expected_range_low != null ? `<div class="lab-note">3&sigma; expected range: ${fmt.usd(s.expected_range_low)} – ${fmt.usd(s.expected_range_high)}</div>` : ''}
                <div class="lab-note">${esc(s.description)}</div>
            </div>`;
    } catch (e) {
        out.innerHTML = `<span style="color:${RED}">Error: ${esc(e.message)}</span>`;
    }
}

function row(label, value) {
    return `<div class="lab-row"><span>${label}</span><span>${value}</span></div>`;
}

// ---------------------------------------------------------------- scenario lab

// Checkpoint percents are a per-browser preference: the last value used on
// any card seeds every card. localStorage can throw (private mode, blocked
// site data), so every access is guarded.
const GRID_KEY = 'risk.gridPct';

function loadGridPref() {
    try { return localStorage.getItem(GRID_KEY) || ''; } catch { return ''; }
}

function saveGridPref(value) {
    try {
        if (value) localStorage.setItem(GRID_KEY, value);
        else localStorage.removeItem(GRID_KEY);
    } catch { /* preference only */ }
}

function gridInput(i) {
    const input = el(`lab-grid-${i}`);
    if (input.dataset.seeded !== '1') {
        input.value = loadGridPref();
        input.dataset.seeded = '1';
    }
    return input.value.trim();
}

function currentTarget(i) {
    const v = parseFloat(el(`lab-target-${i}`).value);
    return v > 0 ? v : null;
}

function setTarget(i, value) {
    el(`lab-target-${i}`).value = value.toFixed(2);
    loadLab(i, value);
}

function analyzeTarget(i) {
    const t = currentTarget(i);
    if (t == null) { alert('Enter a target option price'); return; }
    loadLab(i, t);
}

async function loadLab(i, target = null) {
    const card = el(`position-${i}`);
    const contractId = card.dataset.contract;
    const status = el(`lab-status-${i}`);
    const params = new URLSearchParams({contract_id: contractId});
    if (target != null) params.set('target_price', target);
    const vol = manualVolatility(i);
    if (vol != null) params.set('volatility', vol);
    const grid = gridInput(i);
    if (grid) params.set('grid_pct', grid);

    status.textContent = 'Loading live contract data...';
    try {
        const res = await fetch(`/api/risk/scenario-lab?${params}`);
        const data = await res.json();
        if (!res.ok) { status.innerHTML = `<span style="color:${RED}">${esc(data.detail || res.statusText)}</span>`; return; }
        labState[i] = {...labState[i], loaded: true, data};
        status.textContent = '';
        // Echo the server-normalized list (sorted, deduped) and remember it.
        const normalized = data.grid_percents.map(p => +p.toFixed(2)).join(',');
        el(`lab-grid-${i}`).value = normalized;
        saveGridPref(grid ? normalized : '');
        renderLab(i, data);

        // First open: seed a target (half the current market price) so the
        // conditions tables appear without an extra click.
        if (target == null && currentTarget(i) == null) {
            const base = data.market_price ?? data.model.price;
            const seed = Math.max(0.01, Math.round(base * 50) / 100);
            if (seed > 0) setTarget(i, seed);
        }
    } catch (e) {
        status.innerHTML = `<span style="color:${RED}">Error: ${esc(e.message)}</span>`;
    }
}

function renderLab(i, d) {
    const input = volInput(i);
    if (input.dataset.dirty !== '1') input.value = (d.volatility.used * 100).toFixed(1);
    el(`lab-iv-source-${i}`).innerHTML = ivSourceBadge(d.volatility.source);

    renderQuickTargets(i, d);
    el(`lab-market-${i}`).innerHTML = renderMarket(d);
    el(`lab-target-result-${i}`).innerHTML = d.target ? renderTarget(d) : '';
    el(`lab-matrix-${i}`).innerHTML = d.value_matrix ? renderMatrix(i, d) : '';
}

function ivSourceBadge(source) {
    const color = {override: BLUE, implied_from_mid: GREEN, contract: GREEN, symbol: AMBER, default: RED, manual: BLUE}[source] || MUTED;
    return `<span class="iv-chip" style="color:${color};border-color:${color}55">${IV_SOURCE_LABEL[source] || source}</span>`;
}

function renderQuickTargets(i, d) {
    const p = d.position;
    const mkt = d.market_price ?? d.model.price;
    const options = [
        ['50% of market', mkt * 0.5],
        ['25% of market', mkt * 0.25],
        [`50% of premium`, p.premium_per_share * 0.5],
        ['$0.05', 0.05],
    ].filter(([, v]) => v >= 0.01);
    el(`lab-quick-${i}`).innerHTML = options.map(([label, v]) =>
        `<button class="lab-chip" onclick="setTarget(${i}, ${Math.round(v * 100) / 100})" title="${label}">${label}: $${v.toFixed(2)}</button>`
    ).join('');
}

function renderMarket(d) {
    const p = d.position, u = d.underlying, m = d.contract_market, model = d.model;
    const sign = p.strategy.includes('SHORT') ? -1 : 1;
    const mult = 100 * p.num_contracts * sign;
    const spread = m ? SPREAD_LABEL[m.spread_quality] || [m.spread_quality, MUTED] : null;

    const cand = Object.entries(d.volatility.candidates)
        .filter(([, v]) => v != null)
        .map(([k, v]) => `${IV_SOURCE_LABEL[k]} ${fmt.iv(v)}`)
        .join(' · ');

    return `
    <div class="lab-grid">
        <div class="lab-card">
            <div class="lab-card-title">Underlying</div>
            <div class="lab-big">${fmt.usd(u.price)} <span style="color:${pnlColor(u.change_percent)};font-size:0.8rem">${fmt.signedPct(u.change_percent, 2)}</span></div>
            ${row('Strike', `${fmt.usd(p.strike)} (${fmt.signedPct((u.price / p.strike - 1) * 100)})`)}
            ${row('Max pain', fmt.usd(u.max_pain))}
            ${row('IV rank / pctl', `${u.iv_rank?.toFixed(0) ?? '--'} / ${u.iv_percentile?.toFixed(0) ?? '--'}`)}
            ${row('Historical vol', fmt.iv(u.historical_volatility))}
        </div>
        <div class="lab-card">
            <div class="lab-card-title">Contract market</div>
            ${m ? `
                <div class="lab-big">${fmt.usd(m.bid)} / ${fmt.usd(m.ask)}</div>
                ${row('Mid / last', `${fmt.usd(m.mid)} / ${fmt.usd(m.last)}`)}
                ${row('Spread', `<span style="color:${spread[1]}">${spread[0]}</span>`)}
                ${row('Volume / OI', `${m.volume ?? '--'} / ${m.open_interest ?? '--'}`)}
            ` : `<div class="lab-note" style="color:${AMBER}">No live contract quote — model uses ${IV_SOURCE_LABEL[d.volatility.source]} IV. Check the browser-profile mount (./run.sh).</div>`}
            ${row('Premium at open', fmt.usd(p.premium_per_share))}
        </div>
        <div class="lab-card">
            <div class="lab-card-title">Model (${fmt.iv(d.volatility.used)} IV)</div>
            <div class="lab-big">${fmt.usd(model.price, 3)}
                ${model.vs_market != null ? `<span style="color:${MUTED};font-size:0.8rem">${model.vs_market >= 0 ? '+' : ''}${model.vs_market.toFixed(3)} vs ${d.market_price_source}</span>` : ''}
            </div>
            ${row('Position Δ (shares)', (model.delta * mult).toFixed(0))}
            ${row('Position Θ / day', `<span style="color:${pnlColor(model.theta * mult)}">${fmt.signedUsd(model.theta * mult)}</span>`)}
            ${row('Position vega / 1% IV', fmt.signedUsd(model.vega * mult))}
            ${row('Γ per share', model.gamma.toFixed(4))}
        </div>
    </div>
    <div class="lab-note">IV candidates: ${cand || 'none'} · r = ${fmt.pct(model.risk_free_rate * 100)}</div>`;
}

function renderTarget(d) {
    const t = d.target, p = d.position;
    const moveWord = t.spot_needed_now != null && t.spot_needed_now > d.underlying.price ? '≥' : '≤';
    const keepPct = p.strategy.includes('SHORT') && p.premium_per_share > 0
        ? ` (${fmt.pct((1 - t.target_price / p.premium_per_share) * 100)} of opening premium kept)` : '';

    const summary = `
    <div class="lab-box" style="margin-bottom:0.75rem">
        <div style="font-size:0.95rem">
            ${p.strategy.includes('SHORT') ? 'Buy' : 'Sell'} to close at <strong>${fmt.usd(t.target_price)}</strong>
            → <span style="color:${pnlColor(t.close_pnl)};font-weight:600">${fmt.signedUsd(t.close_pnl)}</span>${keepPct}
        </div>
        <div style="margin-top:0.35rem;color:#ccc">
            ${t.spot_needed_now != null
                ? `Today it needs ${esc(p.symbol)} ${moveWord} <strong style="color:${BLUE}">${fmt.usd(t.spot_needed_now)}</strong> (${fmt.signedPct(t.move_pct)}) at constant IV.
                   <span style="color:${probColor(t.prob_touch)}">P(touch before expiry) ${fmt.prob(t.prob_touch)}</span>
                   · P(finish beyond) ${fmt.prob(t.prob_finish_beyond)}`
                : `No underlying price produces this option value at ${fmt.iv(d.volatility.used)} IV.`}
        </div>
        <div class="lab-note">Model value now ${fmt.usd(t.current_model_price, 3)} — target is a ${t.direction}.
            A resting limit order fills on a touch, so the touch probability is the fill estimate.</div>
    </div>`;

    const spotRows = t.spot_path.map(r => `
        <tr><td>${r.days_left}d</td><td>${fmt.date(r.date)}</td>
            <td style="color:${BLUE}">${r.spot_needed != null ? `${moveWord} ${fmt.usd(r.spot_needed)}` : 'n/a'}</td>
            <td style="color:${MUTED}">${fmt.signedPct(r.move_pct)}</td></tr>`).join('');

    // Hold levels are rounded to cents, so match the current price by nearest row.
    const holdNearest = t.hold_path.reduce((best, r, k, a) =>
        Math.abs(r.move_pct) < Math.abs(a[best].move_pct) ? k : best, 0);
    const holdTable = t.hold_path.length ? `
        <div class="lab-card">
            <div class="lab-card-title">If ${esc(p.symbol)} holds at…</div>
            <table class="lab-table"><thead><tr><th>Price</th><th>Move</th><th>Target on</th></tr></thead><tbody>
            ${t.hold_path.map((r, k) => `
                <tr${k === holdNearest ? ' class="lab-current"' : ''}>
                    <td>${fmt.usd(r.spot)}</td><td style="color:${MUTED}">${fmt.signedPct(r.move_pct)}</td>
                    <td>${r.days_left == null ? `<span style="color:${RED}">never</span>` : `${fmt.date(r.date)} <span style="color:${DIM}">(${r.days_left}d left)</span>`}</td>
                </tr>`).join('')}
            </tbody></table>
            <div class="lab-note">Time decay alone, IV constant.</div>
        </div>` : `
        <div class="lab-card"><div class="lab-card-title">Time decay</div>
            <div class="lab-note">Target is above the current value — time works against it.</div></div>`;

    const ivNow = d.volatility.used;
    const ivRows = t.iv_path.map(r => `
        <tr><td>${r.days_left}d</td><td>${fmt.date(r.date)}</td>
            <td>${r.iv_needed == null ? '<span style="color:#666">unreachable</span>'
                : `${t.direction === 'decrease' ? '≤' : '≥'} ${fmt.iv(r.iv_needed)}`}</td>
            <td>${r.already_met ? `<span style="color:${GREEN}">met</span>`
                : r.iv_needed != null ? `<span style="color:${MUTED}">${((r.iv_needed - ivNow) * 100 >= 0 ? '+' : '')}${((r.iv_needed - ivNow) * 100).toFixed(1)} pts</span>` : ''}</td></tr>`).join('');

    return summary + `
    <div class="lab-grid">
        <div class="lab-card">
            <div class="lab-card-title">Price needed by date</div>
            <table class="lab-table"><thead><tr><th>Left</th><th>Date</th><th>${esc(p.symbol)}</th><th>Move</th></tr></thead>
            <tbody>${spotRows}</tbody></table>
            <div class="lab-note">IV held at ${fmt.iv(ivNow)}.</div>
        </div>
        ${holdTable}
        <div class="lab-card">
            <div class="lab-card-title">IV needed (price unchanged)</div>
            <table class="lab-table"><thead><tr><th>Left</th><th>Date</th><th>IV</th><th>vs now</th></tr></thead>
            <tbody>${ivRows}</tbody></table>
            <div class="lab-note">"met" = current IV already gets there by that date.</div>
        </div>
    </div>`;
}

function renderMatrix(i, d) {
    const m = d.value_matrix;
    const mode = labState[i].matrixMode || 'pnl';
    const cells = mode === 'pnl' ? m.pnl : m.values;
    const maxAbs = Math.max(1e-9, ...m.pnl.flat().map(Math.abs));
    const spot = d.underlying.price;
    const nearest = m.spots.reduce((best, s, k) => Math.abs(s - spot) < Math.abs(m.spots[best] - spot) ? k : best, 0);

    const head = m.days_left.map((dl, k) =>
        `<th>${dl === 0 ? 'Expiry' : `${dl}d`}<div style="color:${DIM};font-weight:normal">${fmt.date(m.dates[k])}</div></th>`).join('');

    const body = m.spots.map((s, r) => {
        const tds = cells[r].map((v, c) => {
            const pnl = m.pnl[r][c];
            const alpha = Math.min(0.55, 0.08 + 0.47 * Math.abs(pnl) / maxAbs);
            const bg = pnl >= 0 ? `rgba(34,197,94,${alpha})` : `rgba(239,68,68,${alpha})`;
            return `<td style="background:${bg}">${mode === 'pnl' ? fmt.usd(v, 0) : fmt.usd(v, 3)}</td>`;
        }).join('');
        return `<tr${r === nearest ? ' class="lab-current"' : ''}><th>${fmt.usd(s)}<div style="color:${DIM};font-weight:normal">${fmt.signedPct((s / spot - 1) * 100, 0)}</div></th>${tds}</tr>`;
    }).join('');

    return `
    <div class="lab-card" style="margin-top:0.75rem">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="lab-card-title">What-if grid — ${esc(d.position.symbol)} price × date at ${fmt.iv(d.volatility.used)} IV</div>
            <div>
                <button class="lab-chip${mode === 'pnl' ? ' active' : ''}" onclick="setMatrixMode(${i}, 'pnl')">Position P&amp;L</button>
                <button class="lab-chip${mode === 'value' ? ' active' : ''}" onclick="setMatrixMode(${i}, 'value')">Option value</button>
            </div>
        </div>
        <div style="overflow-x:auto"><table class="lab-table lab-matrix"><thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table></div>
        <div class="lab-note">Rows span ±1.5σ of the move to expiry. Highlighted row is nearest the current price.</div>
    </div>`;
}

function setMatrixMode(i, mode) {
    labState[i].matrixMode = mode;
    el(`lab-matrix-${i}`).innerHTML = renderMatrix(i, labState[i].data);
}

// Mark the IV box dirty when the user types, so the lab stops auto-selecting.
document.addEventListener('input', (e) => {
    if (e.target.matches('input[id^="volatility-"]')) e.target.dataset.dirty = '1';
});
