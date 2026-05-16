// Speculation App — extracted from templates/speculation.html.
//
// Module pattern (no globals beyond `SpecApp`). All UI state lives in the
// closure; nothing reaches into other templates. The Jinja template only
// loads this file and renders the static layout shell — interactions are
// handled here. Use `data-*` attributes on the template side if you ever
// need to thread server-rendered values in.
(function () {
    'use strict';

    const CONSTANTS = {
        DEBOUNCE_MS: 400,
        MAX_PROFIT_UNLIMITED: 999999,
        MAX_LOSS_UNLIMITED: -999999,
        DEFAULT_IV: 0.30,
        QUOTE_CACHE_TTL_MS: 5 * 60 * 1000,
    };

    const STRATEGY_TEMPLATES = {
        'long_call': [{action: 'BUY', type: 'CALL', offset: 0}],
        'long_put':  [{action: 'BUY', type: 'PUT',  offset: 0}],
        'short_put': [{action: 'SELL', type: 'PUT',  offset: 0}],
        'short_call':[{action: 'SELL', type: 'CALL', offset: 0}],
        'bull_call_spread': [
            {action: 'BUY',  type: 'CALL', offset: 0},
            {action: 'SELL', type: 'CALL', offset: 5},
        ],
        'bear_put_spread': [
            {action: 'BUY',  type: 'PUT', offset: 0},
            {action: 'SELL', type: 'PUT', offset: -5},
        ],
        'bull_put_spread': [
            {action: 'SELL', type: 'PUT', offset: 0},
            {action: 'BUY',  type: 'PUT', offset: -5},
        ],
        'bear_call_spread': [
            {action: 'SELL', type: 'CALL', offset: 0},
            {action: 'BUY',  type: 'CALL', offset: 5},
        ],
        'long_straddle': [
            {action: 'BUY', type: 'CALL', offset: 0},
            {action: 'BUY', type: 'PUT',  offset: 0},
        ],
        'short_straddle': [
            {action: 'SELL', type: 'CALL', offset: 0},
            {action: 'SELL', type: 'PUT',  offset: 0},
        ],
        'long_strangle': [
            {action: 'BUY', type: 'CALL', offset: 5},
            {action: 'BUY', type: 'PUT',  offset: -5},
        ],
        'short_strangle': [
            {action: 'SELL', type: 'CALL', offset: 5},
            {action: 'SELL', type: 'PUT',  offset: -5},
        ],
        'iron_condor': [
            {action: 'SELL', type: 'PUT',  offset: -5},
            {action: 'BUY',  type: 'PUT',  offset: -10},
            {action: 'SELL', type: 'CALL', offset: 5},
            {action: 'BUY',  type: 'CALL', offset: 10},
        ],
        'iron_butterfly': [
            {action: 'SELL', type: 'PUT',  offset: 0},
            {action: 'BUY',  type: 'PUT',  offset: -5},
            {action: 'SELL', type: 'CALL', offset: 0},
            {action: 'BUY',  type: 'CALL', offset: 5},
        ],
    };

    const state = {
        symbolData: null,
        legCounter: 0,
        quoteCache: new Map(),
        analysisData: null,
        pendingQuoteFetch: null,
        debounceTimers: new Map(),
    };

    function debounce(fn, key, delay = CONSTANTS.DEBOUNCE_MS) {
        if (state.debounceTimers.has(key)) clearTimeout(state.debounceTimers.get(key));
        state.debounceTimers.set(key, setTimeout(fn, delay));
    }

    function formatCurrency(value, decimals = 2) {
        if (value === null || value === undefined) return '--';
        return '$' + value.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
    }

    function formatPercent(value, decimals = 1) {
        if (value === null || value === undefined) return '--';
        return value.toFixed(decimals) + '%';
    }

    function getProbabilityColor(p) { return p > 70 ? '#22c55e' : p > 40 ? '#f59e0b' : '#ef4444'; }
    function getAssignmentProbColor(p) { return p > 50 ? '#ef4444' : p > 25 ? '#f59e0b' : '#22c55e'; }

    function showError(id, m)   { const el = document.getElementById(id); if (el) { el.innerHTML = `<span class="error-text">${m}</span>`; el.style.display = 'block'; } }
    function showSuccess(id, m) { const el = document.getElementById(id); if (el) { el.innerHTML = `<span class="success-text">${m}</span>`; el.style.display = 'block'; } }
    function showLoading(id, m = 'Loading...') { const el = document.getElementById(id); if (el) { el.innerHTML = `<span class="loading-indicator">${m}</span>`; el.style.display = 'block'; } }

    function buildExpirationOptions() {
        const expirations = state.symbolData?.expirations || [];
        if (expirations.length === 0) {
            const d = new Date(); d.setDate(d.getDate() + 30);
            const s = d.toISOString().split('T')[0];
            return `<option value="${s}">${s}</option>`;
        }
        return expirations.map(e => `<option value="${e}">${e}</option>`).join('');
    }

    function getFirstExpiration() {
        const exps = state.symbolData?.expirations || [];
        if (exps.length) return exps[0];
        const d = new Date(); d.setDate(d.getDate() + 30);
        return d.toISOString().split('T')[0];
    }

    function buildLegHtml(legId, config = {}) {
        const action  = config.action || 'BUY';
        const type    = config.type   || 'CALL';
        const strike  = config.strike || (state.symbolData ? Math.round(state.symbolData.current_price) : 100);
        const premium = config.premium || 0;

        return `
            <div class="leg-row" id="leg-${legId}" data-leg-id="${legId}">
                <div class="leg-row-field">
                    <label class="spec-label">Action</label>
                    <select id="leg-${legId}-action" class="spec-input">
                        <option value="BUY"  ${action === 'BUY'  ? 'selected' : ''}>BUY</option>
                        <option value="SELL" ${action === 'SELL' ? 'selected' : ''}>SELL</option>
                    </select>
                </div>
                <div class="leg-row-field">
                    <label class="spec-label">Type</label>
                    <select id="leg-${legId}-type" class="spec-input" onchange="SpecApp.onLegChange(${legId})">
                        <option value="CALL" ${type === 'CALL' ? 'selected' : ''}>CALL</option>
                        <option value="PUT"  ${type === 'PUT'  ? 'selected' : ''}>PUT</option>
                    </select>
                </div>
                <div class="leg-row-field">
                    <label class="spec-label">Strike</label>
                    <input type="number" id="leg-${legId}-strike" value="${strike}" step="0.5" min="0"
                           class="spec-input" onchange="SpecApp.onLegChange(${legId})">
                </div>
                <div class="leg-row-field">
                    <label class="spec-label">Qty</label>
                    <input type="number" id="leg-${legId}-qty" value="1" min="1" max="100" class="spec-input">
                </div>
                <div class="leg-row-field">
                    <div class="premium-header">
                        <label class="spec-label">Premium</label>
                        <button onclick="SpecApp.fetchSingleQuote(${legId})" class="quote-btn" title="Fetch quote">Quote</button>
                    </div>
                    <input type="number" id="leg-${legId}-premium" value="${premium.toFixed(2)}" step="0.01" min="0" class="spec-input">
                    <div id="leg-${legId}-quote" class="quote-display"></div>
                </div>
                <div class="leg-row-field" style="justify-content: flex-end;">
                    <button onclick="SpecApp.removeLeg(${legId})" class="remove-btn">X</button>
                </div>
            </div>
        `;
    }

    window.SpecApp = {
        getExpiration()        { return document.getElementById('strategy-expiration')?.value || getFirstExpiration(); },
        populateExpirations()  { const sel = document.getElementById('strategy-expiration'); if (sel) sel.innerHTML = buildExpirationOptions(); },
        onExpirationChange()   { state.quoteCache.clear(); this.fetchAllQuotes(); },

        async lookupSymbol() {
            const input = document.getElementById('symbol-input');
            const symbol = input.value.trim().toUpperCase();
            if (!symbol) { showError('lookup-result', 'Please enter a symbol'); return; }
            showLoading('lookup-result', `Looking up ${symbol}...`);
            try {
                const res = await fetch(`/api/speculation/lookup?symbol=${symbol}`);
                const data = await res.json();
                if (data.detail) { showError('lookup-result', `Error: ${data.detail}`); return; }
                if (!data.current_price) { showError('lookup-result', `Could not get price for ${symbol}`); return; }

                state.symbolData = data;
                state.quoteCache.clear();
                showSuccess('lookup-result', `Found ${symbol} - ${formatCurrency(data.current_price)}`);

                document.getElementById('current-price').textContent = formatCurrency(data.current_price);
                const pc = document.getElementById('price-change');
                if (data.price_change_percent !== null) {
                    const sign = data.price_change_percent >= 0 ? '+' : '';
                    const cls  = data.price_change_percent >= 0 ? 'positive' : 'negative';
                    pc.innerHTML = `<span class="${cls}">${sign}${formatPercent(data.price_change_percent)}</span>`;
                } else { pc.textContent = ''; }

                document.getElementById('iv-value').textContent       = data.implied_volatility ? formatPercent(data.implied_volatility * 100, 1) : '--';
                document.getElementById('iv-rank').textContent        = data.iv_rank ? data.iv_rank.toFixed(0) : '--';
                document.getElementById('max-pain').textContent       = data.max_pain ? formatCurrency(data.max_pain) : '--';
                document.getElementById('put-call-ratio').textContent = data.put_call_ratio ? data.put_call_ratio.toFixed(2) : '--';

                document.getElementById('symbol-data').style.display = 'block';
                this.populateExpirations();

                document.getElementById('legs-container').innerHTML = '';
                state.legCounter = 0;
                this.addLeg();
            } catch (e) { showError('lookup-result', `Error: ${e.message}`); }
        },

        addLeg(config = {}) {
            state.legCounter++;
            document.getElementById('legs-container').insertAdjacentHTML('beforeend', buildLegHtml(state.legCounter, config));
        },

        removeLeg(legId) { document.getElementById(`leg-${legId}`)?.remove(); },

        onLegChange(legId) { debounce(() => this.fetchSingleQuote(legId), `leg-${legId}`); },

        async fetchSingleQuote(legId) {
            if (!state.symbolData) return;
            const expiration = this.getExpiration();
            const strike = parseFloat(document.getElementById(`leg-${legId}-strike`)?.value);
            const type   = document.getElementById(`leg-${legId}-type`)?.value;
            if (!expiration || isNaN(strike)) return;

            const cacheKey = `${state.symbolData.symbol}:${expiration}:${strike}:${type}`;
            const cached = state.quoteCache.get(cacheKey);
            if (cached && (Date.now() - cached.timestamp < CONSTANTS.QUOTE_CACHE_TTL_MS)) {
                this.displayQuote(legId, cached.quote); return;
            }

            const qDiv = document.getElementById(`leg-${legId}-quote`);
            if (qDiv) qDiv.innerHTML = '<span class="loading-indicator">...</span>';

            try {
                const res = await fetch('/api/speculation/quotes-batch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        contracts: [{ symbol: state.symbolData.symbol, expiration, strike, option_type: type }],
                    }),
                });
                const data = await res.json();
                if (data.quotes && data.quotes.length > 0) {
                    const quote = data.quotes[0];
                    state.quoteCache.set(cacheKey, {quote, timestamp: Date.now()});
                    this.displayQuote(legId, quote);
                } else if (qDiv) { qDiv.innerHTML = '<span style="color: #666;">No quote</span>'; }
            } catch (err) {
                console.error('Quote fetch error:', err);
                if (qDiv) qDiv.innerHTML = '<span style="color: #666;">Error</span>';
            }
        },

        async fetchAllQuotes() {
            if (!state.symbolData) return;
            const rows = document.querySelectorAll('.leg-row');
            if (rows.length === 0) return;
            const expiration = this.getExpiration();
            const contracts = [], legIds = [];
            rows.forEach(row => {
                const legId = row.dataset.legId;
                const strike = parseFloat(document.getElementById(`leg-${legId}-strike`)?.value);
                const type   = document.getElementById(`leg-${legId}-type`)?.value;
                if (!isNaN(strike)) {
                    contracts.push({ symbol: state.symbolData.symbol, expiration, strike, option_type: type });
                    legIds.push(legId);
                    const qDiv = document.getElementById(`leg-${legId}-quote`);
                    if (qDiv) qDiv.innerHTML = '<span class="loading-indicator">...</span>';
                }
            });
            if (contracts.length === 0) return;
            try {
                const res = await fetch('/api/speculation/quotes-batch', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({contracts}),
                });
                const data = await res.json();
                if (data.quotes) {
                    data.quotes.forEach((quote, i) => {
                        if (quote && legIds[i]) {
                            const c = contracts[i];
                            const cacheKey = `${c.symbol}:${c.expiration}:${c.strike}:${c.option_type}`;
                            state.quoteCache.set(cacheKey, {quote, timestamp: Date.now()});
                            this.displayQuote(legIds[i], quote);
                        }
                    });
                }
            } catch (err) {
                console.error('Batch quote fetch error:', err);
                legIds.forEach(id => { const q = document.getElementById(`leg-${id}-quote`); if (q) q.innerHTML = '<span style="color: #666;">Error</span>'; });
            }
        },

        displayQuote(legId, quote) {
            const qDiv = document.getElementById(`leg-${legId}-quote`);
            const premiumInput = document.getElementById(`leg-${legId}-premium`);
            if (!qDiv) return;

            let html = '';
            if (quote.bid  !== null) html += `<span class="quote-bid">B:${quote.bid.toFixed(2)}</span> `;
            if (quote.mid  !== null) html += `<span class="quote-mid">M:${quote.mid.toFixed(2)}</span> `;
            if (quote.ask  !== null) html += `<span class="quote-ask">A:${quote.ask.toFixed(2)}</span>`;
            if (quote.last !== null && quote.bid === null) html += `<span class="quote-last">L:${quote.last.toFixed(2)}</span>`;

            if (quote.implied_volatility !== null) {
                html += `<br><span style="color: #666; font-size: 0.65rem;">IV:${(quote.implied_volatility * 100).toFixed(0)}%</span>`;
            }

            const sq = quote.spread_quality;
            if (sq === 'wide' || sq === 'very_wide' || sq === 'no_bid' || sq === 'no_quote') {
                const labels = {wide:'WIDE SPREAD', very_wide:'VERY WIDE — MID UNRELIABLE', no_bid:'NO BID', no_quote:'NO QUOTE'};
                html += `<br><span style="color: #ef4444; font-size: 0.65rem; font-weight: 600;">⚠ ${labels[sq]}</span>`;
            } else if (sq === 'moderate') {
                html += `<br><span style="color: #f59e0b; font-size: 0.65rem;">~ wide-ish spread</span>`;
            }
            qDiv.innerHTML = html || '<span style="color: #666;">No data</span>';

            if (premiumInput) {
                premiumInput.classList.remove('premium-chain', 'premium-quote', 'premium-last');
                if (quote.mid !== null) {
                    premiumInput.value = quote.mid.toFixed(2);
                    premiumInput.classList.add('premium-quote');
                    premiumInput.title = `Mid: ${quote.mid} (Bid: ${quote.bid}, Ask: ${quote.ask})`;
                } else if (quote.last !== null) {
                    premiumInput.value = quote.last.toFixed(2);
                    premiumInput.classList.add('premium-last');
                    premiumInput.title = `Last traded: ${quote.last}`;
                }
            }
        },

        applyStrategyTemplate() {
            const tid = document.getElementById('strategy-template').value;
            if (!tid || !STRATEGY_TEMPLATES[tid]) return;
            const tpl = STRATEGY_TEMPLATES[tid];
            const baseStrike = state.symbolData ? Math.round(state.symbolData.current_price) : 100;
            document.getElementById('legs-container').innerHTML = '';
            state.legCounter = 0;
            tpl.forEach(d => this.addLeg({action: d.action, type: d.type, strike: baseStrike + d.offset}));
            setTimeout(() => this.fetchAllQuotes(), 200);
        },

        collectLegs() {
            const rows = document.querySelectorAll('.leg-row');
            const legs = [];
            const expiration = this.getExpiration();
            rows.forEach(row => {
                const id = row.dataset.legId;
                const action  = document.getElementById(`leg-${id}-action`)?.value;
                const type    = document.getElementById(`leg-${id}-type`)?.value;
                const strike  = parseFloat(document.getElementById(`leg-${id}-strike`)?.value);
                const qty     = parseInt(document.getElementById(`leg-${id}-qty`)?.value) || 1;
                const premium = parseFloat(document.getElementById(`leg-${id}-premium`)?.value) || 0;
                if (strike > 0 && expiration) {
                    legs.push({option_type: type, strike, expiration, action, quantity: qty, premium});
                }
            });
            return legs;
        },

        async analyzeStrategy() {
            if (!state.symbolData) { alert('Please lookup a symbol first'); return; }
            const legs = this.collectLegs();
            if (legs.length === 0) { alert('Please add at least one leg'); return; }
            const sel = document.getElementById('strategy-template');
            const strategyName = sel.selectedIndex > 0 ? sel.options[sel.selectedIndex].text : 'Custom Strategy';
            document.getElementById('analysis-results').style.display = 'block';
            document.getElementById('result-strategy').textContent = 'Analyzing...';
            try {
                const res = await fetch('/api/speculation/analyze', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        symbol: state.symbolData.symbol, strategy_name: strategyName,
                        legs, implied_volatility: state.symbolData.implied_volatility,
                    }),
                });
                const data = await res.json();
                if (data.detail) { alert('Error: ' + data.detail); document.getElementById('analysis-results').style.display = 'none'; return; }
                state.analysisData = data;
                this.displayResults(data);
            } catch (err) {
                alert('Error: ' + err.message);
                document.getElementById('analysis-results').style.display = 'none';
            }
        },

        displayResults(data) {
            document.getElementById('result-strategy').textContent = data.strategy_name;
            const np = document.getElementById('result-net-premium');
            np.textContent = formatCurrency(data.net_premium);
            np.className = 'value ' + (data.net_premium >= 0 ? 'positive' : 'negative');
            document.getElementById('result-max-profit').textContent = data.max_profit > CONSTANTS.MAX_PROFIT_UNLIMITED ? 'Unlimited' : formatCurrency(data.max_profit);
            document.getElementById('result-max-loss').textContent   = data.max_loss   < CONSTANTS.MAX_LOSS_UNLIMITED   ? 'Unlimited' : formatCurrency(data.max_loss);
            const bes = data.breakeven_prices.map(be => formatCurrency(be)).join(', ');
            document.getElementById('result-breakeven').textContent = bes || 'N/A';
            document.getElementById('result-dte').textContent = `${data.days_to_expiry}d`;
            const pp = document.getElementById('result-profit-prob');
            if (data.profit_probability !== null) {
                pp.textContent = formatPercent(data.profit_probability);
                pp.className = 'value ' + (data.profit_probability >= 50 ? 'positive' : 'negative');
            } else { pp.textContent = '--'; pp.className = 'value'; }

            const tb = document.getElementById('legs-summary-body');
            tb.innerHTML = data.legs.map(leg => {
                let statusHtml = '--';
                if (leg.itm !== null && leg.itm !== undefined) {
                    const cls  = leg.itm ? 'error-text' : 'success-text';
                    const txt  = leg.itm ? 'ITM' : 'OTM';
                    statusHtml = `<span class="${cls}">${txt}</span> <span style="color: #888; font-size: 0.8rem;">(${leg.distance_to_strike_pct.toFixed(1)}%)</span>`;
                }
                let riskHtml = '--';
                if (leg.assignment_probability !== null && leg.assignment_probability !== undefined) {
                    riskHtml = `<span style="color: ${getAssignmentProbColor(leg.assignment_probability)}; font-weight: 600;">${leg.assignment_probability.toFixed(0)}%</span>`;
                }
                return `
                    <tr>
                        <td><span class="tag ${leg.action === 'BUY' ? 'open' : 'assigned'}">${leg.action}</span></td>
                        <td>${leg.option_type}</td>
                        <td>${formatCurrency(leg.strike)}</td>
                        <td>${leg.expiration}</td>
                        <td>${leg.quantity}</td>
                        <td>${formatCurrency(leg.premium)}</td>
                        <td class="${leg.total_premium >= 0 ? 'positive' : 'negative'}">${formatCurrency(leg.total_premium)}</td>
                        <td>${statusHtml}</td>
                        <td>${riskHtml}</td>
                    </tr>`;
            }).join('');

            this.buildExitScenarioCalculators(data);
            this.build50PctAssignmentIndicators(data);

            const vi = document.getElementById('exit-volatility');
            if (vi) vi.value = data.implied_volatility ? Math.round(data.implied_volatility * 100) : 30;

            this.renderCharts(data.charts);

            const sb = document.getElementById('scenarios-table-body');
            const sampled = data.scenarios.filter((_, i) => i % 5 === 0 || i === data.scenarios.length - 1);
            sb.innerHTML = sampled.map(s => `
                <tr>
                    <td>${formatCurrency(s.underlying_price)}</td>
                    <td class="${s.pnl >= 0 ? 'positive' : 'negative'}">${formatCurrency(s.pnl)}</td>
                    <td style="color: #888;">${formatPercent(s.pnl_percent)}</td>
                </tr>`).join('');

            document.getElementById('analysis-results').scrollIntoView({behavior: 'smooth'});
        },

        renderCharts(charts) {
            if (charts.pnl_script && charts.pnl_div) {
                document.getElementById('pnl-chart').innerHTML = charts.pnl_div;
                const s = document.createElement('script');
                s.textContent = charts.pnl_script.replace(/<script[^>]*>|<\/script>/g, '');
                document.getElementById('pnl-chart').appendChild(s);
            }
            const tc = document.getElementById('theta-chart-container');
            if (charts.theta_script && charts.theta_div) {
                tc.style.display = 'block';
                document.getElementById('theta-chart').innerHTML = charts.theta_div;
                const s = document.createElement('script');
                s.textContent = charts.theta_script.replace(/<script[^>]*>|<\/script>/g, '');
                document.getElementById('theta-chart').appendChild(s);
            } else { tc.style.display = 'none'; }
        },

        buildExitScenarioCalculators(data) {
            const c = document.getElementById('leg-exit-calculators');
            const s = document.getElementById('exit-scenarios-section');
            if (!data.legs || data.legs.length === 0) { s.style.display = 'none'; return; }
            s.style.display = 'block';
            c.innerHTML = data.legs.map((leg, i) => `
                <div class="exit-calc-card">
                    <div style="margin-bottom: 0.5rem; color: #e0e0e0; font-size: 0.9rem;">
                        <span class="tag ${leg.action === 'BUY' ? 'open' : 'assigned'}">${leg.action}</span>
                        <strong>${leg.option_type}</strong> ${formatCurrency(leg.strike)} - ${leg.expiration}
                        <span style="color: #888; margin-left: 0.5rem;">Qty: ${leg.quantity}</span>
                    </div>
                    <div class="exit-calc-grid">
                        <div>
                            <label class="spec-label">Close at option price ($/share):</label>
                            <div style="display: flex; gap: 0.5rem;">
                                <input type="number" step="0.01" min="0" placeholder="e.g. 0.50"
                                       id="close-price-${i}" class="spec-input" style="flex: 1;">
                                <button onclick="SpecApp.calculateExit(${i}, 'close')" class="calc-btn-blue">Calc</button>
                            </div>
                            <div id="close-result-${i}" style="margin-top: 0.5rem; font-size: 0.85rem;"></div>
                        </div>
                        <div>
                            <label class="spec-label">Assignment at underlying price:</label>
                            <div style="display: flex; gap: 0.5rem;">
                                <input type="number" step="0.01" min="0" placeholder="${formatCurrency(leg.strike)}"
                                       id="assign-price-${i}" class="spec-input" style="flex: 1;">
                                <button onclick="SpecApp.calculateExit(${i}, 'assign')" class="calc-btn-orange">Calc</button>
                            </div>
                            <div id="assign-result-${i}" style="margin-top: 0.5rem; font-size: 0.85rem;"></div>
                        </div>
                    </div>
                </div>`).join('');
        },

        build50PctAssignmentIndicators(data) {
            const c = document.getElementById('assignment-price-indicators');
            const shortLegs = data.legs.filter(l => l.action === 'SELL' && l.price_at_50pct_assignment !== null);
            if (shortLegs.length === 0) { c.style.display = 'none'; return; }
            c.style.display = 'block';
            c.innerHTML = `
                <div class="stat-card" style="margin-bottom: 1.5rem;">
                    <h3 style="margin-bottom: 0.75rem; color: #e0e0e0;">50% Assignment Risk Levels</h3>
                    ${shortLegs.map(leg => {
                        const p50 = leg.price_at_50pct_assignment;
                        const diff = data.current_price - p50;
                        const diffPct = (diff / p50) * 100;
                        return `
                            <div style="margin-bottom: 0.5rem; padding: 0.5rem; background-color: #2a2520; border-radius: 4px; border-left: 3px solid #f97316;">
                                <span style="color: #888; font-size: 0.85rem;">${leg.option_type} ${formatCurrency(leg.strike)} (${leg.expiration}):</span>
                                <span style="color: #f97316; font-size: 0.85rem; margin-left: 0.5rem;">50% Risk at: <strong>${formatCurrency(p50)}</strong></span>
                                <span style="color: #888; font-size: 0.8rem; margin-left: 0.5rem;">(${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(1)}% from current)</span>
                            </div>`;
                    }).join('')}
                </div>`;
        },

        toggleExitScenarios() {
            const c = document.getElementById('exit-scenarios-content');
            c.style.display = c.style.display === 'none' ? 'block' : 'none';
        },

        async calculateExit(legIndex, type) {
            const data = state.analysisData;
            if (!data || !data.legs || !data.legs[legIndex]) {
                alert('No analysis data available. Please analyze a strategy first.'); return;
            }
            const leg = data.legs[legIndex];
            const vi = document.getElementById('exit-volatility');
            const vol = (vi ? parseFloat(vi.value) : 30) / 100;
            let resultDiv, url;
            const baseUrl = `/api/speculation/calculate-exit?option_type=${leg.option_type}&action=${leg.action}&strike=${leg.strike}&premium=${leg.premium}&quantity=${leg.quantity}&days_to_expiry=${leg.days_to_expiry}&current_price=${data.current_price}&volatility=${vol}`;
            if (type === 'close') {
                const cp = parseFloat(document.getElementById(`close-price-${legIndex}`)?.value);
                if (!cp || cp <= 0) { alert('Please enter a valid close price'); return; }
                url = baseUrl + `&close_price=${cp}`;
                resultDiv = document.getElementById(`close-result-${legIndex}`);
            } else {
                const ap = parseFloat(document.getElementById(`assign-price-${legIndex}`)?.value);
                if (!ap || ap <= 0) { alert('Please enter a valid assignment price'); return; }
                url = baseUrl + `&assignment_price=${ap}`;
                resultDiv = document.getElementById(`assign-result-${legIndex}`);
            }
            resultDiv.innerHTML = '<span class="loading-indicator">Calculating...</span>';
            try {
                const res = await fetch(url);
                const result = await res.json();
                if (result.error) { resultDiv.innerHTML = `<span class="error-text">Error: ${result.error}</span>`; return; }
                if (type === 'close' && result.close_scenario) {
                    const s = result.close_scenario;
                    resultDiv.innerHTML = `
                        <div class="result-card">
                            <div style="display: flex; justify-content: space-between;">
                                <span style="color: #888;">P&L:</span>
                                <span class="${s.pnl >= 0 ? 'positive' : 'negative'}" style="font-weight: 600;">${formatCurrency(s.pnl)}</span>
                            </div>
                            ${s.probability !== null ? `
                                <div style="display: flex; justify-content: space-between; margin-top: 0.25rem;">
                                    <span style="color: #888;">Likelihood:</span>
                                    <span style="color: ${getProbabilityColor(s.probability)}; font-weight: 600;">${formatPercent(s.probability)}</span>
                                </div>` : ''}
                        </div>`;
                } else if (type === 'assign' && result.assignment_scenario) {
                    const s = result.assignment_scenario;
                    resultDiv.innerHTML = `
                        <div class="result-card">
                            <div style="display: flex; justify-content: space-between;">
                                <span style="color: #888;">P&L:</span>
                                <span class="${s.pnl >= 0 ? 'positive' : 'negative'}" style="font-weight: 600;">${formatCurrency(s.pnl)}</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-top: 0.25rem;">
                                <span style="color: #888;">Assign Prob:</span>
                                <span style="color: ${getAssignmentProbColor(s.assignment_probability)}; font-weight: 600;">${formatPercent(s.assignment_probability)}</span>
                            </div>
                        </div>`;
                } else { resultDiv.innerHTML = '<span style="color: #666;">No result</span>'; }
            } catch (err) { resultDiv.innerHTML = `<span class="error-text">Error: ${err.message}</span>`; }
        },
    };
})();
