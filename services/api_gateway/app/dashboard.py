"""Local operational dashboard served by the API gateway."""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MT5 Trading Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#07111f; --panel:#0d1b2d; --line:#1e3550; --text:#e7eef8; --muted:#91a5bd; --ok:#38d39f; --warn:#ffbd59; --bad:#ff6b7a; }
    * { box-sizing:border-box } body { margin:0; background:linear-gradient(135deg,#07111f,#0a1728); color:var(--text); font-family:Inter,system-ui,sans-serif; }
    main { max-width:1400px; margin:auto; padding:32px 20px 48px } header { display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:26px }
    h1 { font-size:clamp(1.5rem,3vw,2.2rem); margin:0 } h2 { font-size:1rem; margin:0 0 14px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.08em }
    .subtitle { color:var(--muted); margin:7px 0 0 } .updated { color:var(--muted); font-size:.85rem; text-align:right } .pill { display:inline-flex; align-items:center; gap:7px; padding:7px 11px; border:1px solid var(--line); border-radius:999px; font-weight:700; font-size:.82rem }
    .dot { width:8px; height:8px; border-radius:50%; background:currentColor } .ok { color:var(--ok) } .warn { color:var(--warn) } .bad { color:var(--bad) }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); gap:14px; margin-bottom:24px } .card { background:rgba(13,27,45,.92); border:1px solid var(--line); border-radius:13px; padding:17px; box-shadow:0 12px 30px #0002 }
    .value { font-size:1.7rem; font-weight:750; margin-top:8px } .detail { color:var(--muted); font-size:.87rem; margin-top:5px; overflow-wrap:anywhere }
    .section { background:rgba(13,27,45,.92); border:1px solid var(--line); border-radius:13px; padding:20px; margin-top:18px } .services { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px }
    .service { padding:13px; border:1px solid var(--line); border-radius:9px; background:#091727 } .service-name { font-weight:700 } .service-status { margin-top:6px; font-size:.87rem; text-transform:uppercase }
    table { width:100%; border-collapse:collapse; font-size:.9rem } th,td { text-align:left; padding:11px 8px; border-bottom:1px solid var(--line) } th { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.06em } .empty { color:var(--muted); padding:14px 0 }
    @media (max-width:600px) { header { align-items:flex-start; flex-direction:column } .updated { text-align:left } th:nth-child(3),td:nth-child(3) { display:none } }
  </style>
</head>
<body><main>
  <header><div><h1>MT5 Trading Monitor</h1><p class="subtitle">Local operational overview · auto-refreshes every 10 seconds</p></div><div class="updated" id="updated">Loading…</div></header>
  <div class="grid" id="summary"></div>
  <section class="section"><h2>Broker account</h2><div class="grid" id="account"></div></section>
  <section class="section"><h2>Live MT5 positions</h2><div id="positions"></div></section>
  <section class="section"><h2>Tracked executions</h2><div id="trades"></div></section>
  <section class="section"><h2>Service health</h2><div class="services" id="services"></div></section>
</main>
<script>
const el = id => document.getElementById(id);
const statusClass = s => s === 'ok' ? 'ok' : s === 'degraded' ? 'warn' : 'bad';
const safe = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const card = (label, value, detail, cls='') => `<article class="card"><div class="${cls}">${safe(label)}</div><div class="value ${cls}">${safe(value)}</div><div class="detail">${safe(detail)}</div></article>`;
const money = (value, currency) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const options = {minimumFractionDigits:2, maximumFractionDigits:2};
  try { return new Intl.NumberFormat(undefined, {...options, style:'currency', currency:currency || 'USD'}).format(Number(value)); }
  catch { return `${Number(value).toFixed(2)} ${currency || ''}`.trim(); }
};
const when = value => value ? new Date(value).toLocaleString() : '—';
const table = (headers, rows, empty) => rows.length
  ? `<div style="overflow-x:auto"><table><thead><tr>${headers.map(h => `<th>${safe(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`
  : `<div class="empty">${safe(empty)}</div>`;
function render(data) {
  const health = data.health || {status:'unreachable',services:{}}; const services = health.services || {};
  const reconciliation = services.reconciliation || {}; const execution = services['execution-engine'] || {};
  const openTrades = Array.isArray(data.open_trades) ? data.open_trades : [];
  const positions = Array.isArray(data.broker_positions) ? data.broker_positions : [];
  const account = data.account;
  el('summary').innerHTML = card('System status', (health.status || 'unknown').toUpperCase(), `${Object.keys(services).length} services checked`, statusClass(health.status)) +
    card('Open trades', String(openTrades.length), 'Tracked by trade monitor', openTrades.length ? 'warn' : 'ok') +
    card('Execution mode', (execution.mode || 'unknown').toUpperCase(), 'Check broker routing before trading', execution.mode === 'live' ? 'warn' : '') +
    card('SAFE_MODE', (reconciliation.safe_mode || 'unknown').toUpperCase(), reconciliation.last_reconciliation || 'Reconciliation status', reconciliation.safe_mode === 'disabled' ? 'ok' : 'warn');
  const currency = account && account.currency;
  el('account').innerHTML = account
    ? card('Balance', money(account.balance, currency), `Account currency: ${currency || 'unknown'}`) +
      card('Equity', money(account.equity, currency), `Account ${account.login ? `ending ${String(account.login).slice(-4)}` : 'balance'}`) +
      card('Used margin', money(account.margin, currency), 'Margin currently in use') +
      card('Free margin', money(account.free_margin, currency), 'Available margin') +
      card('Live positions', String(positions.length), 'Read directly from the MT5 broker') +
      card('Floating P/L', money(positions.reduce((sum,p) => sum + (Number(p.profit) || 0) + (Number(p.swap) || 0), 0), currency), 'Open-position profit + swap')
    : card('Account data unavailable', '—', data.account_error || 'MT5 account information is not available.', 'warn');
  el('positions').innerHTML = table(
    ['Ticket','Symbol','Side','Volume','Entry','Stop loss','Take profit','Floating P/L','Opened'],
    positions.map(p => `<tr><td>${safe(p.ticket)}</td><td>${safe(p.symbol)}</td><td>${safe(p.side)}</td><td>${safe(p.volume)}</td><td>${safe(p.price_open)}</td><td>${safe(p.stop_loss)}</td><td>${safe(p.take_profit)}</td><td>${safe(money((Number(p.profit) || 0) + (Number(p.swap) || 0), currency))}</td><td>${safe(when(p.opened_at))}</td></tr>`),
    data.account_error || 'No open broker positions.'
  );
  el('services').innerHTML = Object.entries(services).map(([name,item]) => `<div class="service"><div class="service-name">${safe(name)}</div><div class="service-status ${statusClass(item.status)}"><span class="dot"></span> ${safe(item.status || 'unknown')}</div><div class="detail">${safe(Object.entries(item).filter(([k]) => !['status','service'].includes(k)).map(([k,v]) => `${k}: ${v}`).join(' · ') || 'No additional detail')}</div></div>`).join('') || '<div class="empty">No services reported.</div>';
  el('trades').innerHTML = table(
    ['Execution','Order','Status','Volume','Fill price','Opened'],
    openTrades.map(t => `<tr><td>${safe(t.execution_id)}</td><td>${safe(t.order_id)}</td><td>${safe(t.status)}</td><td>${safe(t.executed_volume)}</td><td>${safe(t.fill_price)}</td><td>${safe(when(t.opened_at))}</td></tr>`),
    'No executions are currently tracked.'
  );
  el('updated').textContent = `Updated ${new Date().toLocaleTimeString()} · refreshes every 10 seconds`;
}
async function refresh() { try { const r = await fetch('/dashboard/api/overview', {cache:'no-store'}); if (!r.ok) throw Error(r.status); render(await r.json()); } catch (e) { el('updated').textContent = `Dashboard data unavailable (${e.message})`; } }
refresh(); setInterval(refresh, 10000);
</script></body></html>"""
