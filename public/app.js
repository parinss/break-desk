/* ---------------------------------------------------------------------------
   app.js — renders public/data/breaks.json into a two-pane console.

   No framework and no build step, for the same reason the server is stdlib:
   the argument this project makes is "you can check every number", and that
   argument does not survive a reader having to install a toolchain before they
   can read the code that produced the page.

   Every string from the report reaches the DOM through textContent. None of it
   is trusted enough for innerHTML — the excerpts are literal lines lifted out
   of custodian files, which is precisely the category of input you do not
   interpolate into markup.
--------------------------------------------------------------------------- */

'use strict';

var SEVERITIES = ['critical', 'high', 'medium', 'low'];

/* Severity drives three custom properties per element rather than four classes
   per component, so a new severity is one row here and nothing else. */
var SEV_VAR = { critical: 'crit', high: 'high', medium: 'med', low: 'low' };

/* The order a reconciler reads a finding in: what the action was, what was
   held, what moved, what should be there, what is there, and only then the
   gap. Keys absent from this list still render — they fall to the end
   alphabetically — so a new rule with new detail keys is never dropped. */
var DETAIL_ORDER = [
  'action', 'description', 'ex_date', 'effective_date', 'exchange_ratio',
  'acquirer', 'target', 'leg_failed', 'method', 'pair',
  'opening_quantity', 'opening_cost_basis', 'target_quantity_held',
  'transactions_seen', 'transactions_applied', 'booked_transactions',
  'expected_quantity', 'expected_acquirer_quantity', 'shares_due',
  'expected_cost_basis', 'expected_amount', 'expected_base_value',
  'reported_quantity', 'reported_acquirer_quantity', 'reported_cost_basis',
  'reported_base_value', 'local_value', 'position_value',
  'quoted_rate', 'implied_rate',
  'custodian_a', 'custodian_b', 'quantity_a', 'quantity_b',
  'ratio_between_custodians',
  'bases_reported', 'basis_a', 'basis_b',
  'movements_in_flight', 'value_in_flight',
  'reported_difference', 'explained_by_settlement',
  'reported_symbol', 'current_symbol', 'identifier_used_to_match',
  'prior_period_fees', 'current_period_fees', 'prior_period_amount',
  'transaction_delta', 'unapplied_quantity', 'unexplained_quantity',
  'shares_missing', 'difference', 'basis_drift', 'drift_pct', 'rate_drift_pct',
  'position_reconciles', 'note'
];

/* The rows a reviewer's eye should land on: the discrepancy itself, as opposed
   to the inputs that produced it. Rendered in the severity colour. */
var GAP_KEYS = {
  unapplied_quantity: 1, unexplained_quantity: 1, shares_missing: 1,
  difference: 1, basis_drift: 1, drift_pct: 1, rate_drift_pct: 1,
  transaction_delta: 1, value_in_flight: 1
};

var state = { report: null, filter: 'all', shown: [], current: -1 };

/* --- small DOM helpers --------------------------------------------------- */

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  return node;
}

function clear(node) {
  while (node.firstChild) { node.removeChild(node.firstChild); }
}

function byId(id) { return document.getElementById(id); }

function labelise(key) { return key.replace(/_/g, ' '); }

function paintSeverity(node, severity) {
  var name = SEV_VAR[severity] || 'low';
  node.style.setProperty('--sev', 'var(--' + name + ')');
  node.style.setProperty('--sev-wash', 'var(--' + name + '-wash)');
}

/* Thousands separators applied to the digits, never by parsing the number.

   Every figure in the report is a string on purpose — a Decimal that went
   through JSON.parse would be a float, and this is a system that refuses floats
   for money everywhere else. Reformatting via Number() to get grouping would
   undo that at the last possible moment, in the one layer a client actually
   reads. So the grouping is done on the characters. */
function group(amount) {
  var m = /^(-?)(\d+)(\.\d+)?$/.exec(String(amount));
  if (!m) { return String(amount); }
  return m[1] + m[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (m[3] || '');
}

function riskOf(brk) {
  /* Most findings name their exposure `value_at_risk` in the detail, already
     formatted by the desk. Account-level ones name it something truer to what
     it is — `value_in_flight` — so the raw figure is the fallback, and it
     arrives ungrouped. It showed as `USD 119450.00` in a queue whose other
     rows read `USD 542,600.00`, which is the sort of thing only a screenshot
     catches. */
  if (brk.detail && brk.detail.value_at_risk) { return brk.detail.value_at_risk; }
  if (brk.value_at_risk) { return brk.value_ccy + ' ' + group(brk.value_at_risk); }
  return '—';
}

/* A citation's role is read off its filename rather than stored, because the
   report's job is to record where a line came from, not to editorialise about
   it. Anything unrecognised is labelled plainly as a source. */
function citeRole(file) {
  if (/corporate_action/i.test(file)) { return { tag: 'reference feed', ref: true }; }
  if (/transaction|activity/i.test(file)) { return { tag: 'activity', ref: false }; }
  if (/position|vermoegen|statement/i.test(file)) { return { tag: 'statement', ref: false }; }
  return { tag: 'source', ref: false };
}

/* --- chrome, banner, status ---------------------------------------------- */

function renderChrome(report) {
  var meta = byId('meta');
  clear(meta);
  [['Account', report.account],
   ['Period', report.period.prior + ' → ' + report.period.current],
   ['Custodians', String(report.custodians.length)],
   ['Built', report.generated_at.replace('T', ' ').replace('+00:00', 'Z')]
  ].forEach(function (pair) {
    var cell = el('div');
    cell.appendChild(el('dt', null, pair[0]));
    cell.appendChild(el('dd', null, pair[1]));
    meta.appendChild(cell);
  });

  var risk = byId('risk');
  clear(risk);
  var exposure = report.summary.exposure_by_currency;
  Object.keys(exposure).forEach(function (ccy) {
    var cell = el('div');
    cell.appendChild(el('span', 'k', 'at risk'));
    cell.appendChild(el('span', 'v', exposure[ccy]));
    risk.appendChild(cell);
  });
  if (!Object.keys(exposure).length) {
    var none = el('div');
    none.appendChild(el('span', 'k', 'at risk'));
    none.appendChild(el('span', 'v', 'none'));
    risk.appendChild(none);
  }
}

function renderBanner(report) {
  var banner = byId('banner');
  var fallbacks = report.summary.narrative_fallbacks;
  if (!fallbacks) { banner.hidden = true; return; }
  banner.textContent =
    fallbacks + ' narrative(s) were rejected and rewritten from templates. ' +
    'A rejection means the model produced a figure the desk had not computed; ' +
    'the finding and its numbers are unaffected.';
  banner.hidden = false;
}

function renderStatus(report) {
  var facts = byId('statusfacts');
  clear(facts);
  [[report.coverage.instruments_examined, 'examined'],
   [report.coverage.instruments_clean.length, 'clean'],
   [report.coverage.rules_run.length, 'rules run'],
   [report.summary.narrative_fallbacks, 'fallbacks']
  ].forEach(function (pair) {
    var span = el('span');
    span.appendChild(el('b', null, String(pair[0])));
    span.appendChild(document.createTextNode(' ' + pair[1]));
    facts.appendChild(span);
  });
}

/* --- drawer -------------------------------------------------------------- */

function renderDrawer(report) {
  var sources = byId('sources');
  clear(sources);
  report.custodians.forEach(function (cust) {
    var box = el('div', 'source');
    box.appendChild(el('div', 'source-name', cust.name));
    box.appendChild(el('div', 'source-file', cust.source_file));
    box.appendChild(el('div', 'source-facts',
      cust.positions + ' positions · ' + cust.transactions + ' movements · base ' +
      cust.base_currency + ' · ' +
      (cust.reports_cost_basis ? 'reports cost basis' : 'no cost basis')));
    sources.appendChild(box);
  });

  var clean = byId('clean');
  clear(clean);
  report.coverage.instruments_clean.forEach(function (item) {
    var li = el('li');
    li.appendChild(el('span', null, item.name));
    li.appendChild(el('span', 'isin', item.isin));
    clean.appendChild(li);
  });

  var rules = byId('rules');
  clear(rules);
  var fired = {};
  report.breaks.forEach(function (brk) { fired[brk.kind] = true; });
  report.coverage.rules_run.forEach(function (rule) {
    var li = el('li');
    li.appendChild(el('div',
      'rule-kind ' + (fired[rule.kind] ? 'is-fired' : 'is-quiet'), rule.kind));
    li.appendChild(el('div', 'rule-desc', rule.checks));
    rules.appendChild(li);
  });
}

function wireDrawer() {
  var toggle = byId('drawer-toggle');
  var drawer = byId('drawer');
  toggle.addEventListener('click', function () {
    var open = drawer.hidden;
    drawer.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  });
}

/* --- list ---------------------------------------------------------------- */

function renderFilters(report) {
  var host = byId('filters');
  clear(host);

  var options = [{ key: 'all', label: 'all', count: report.summary.total_breaks }];
  SEVERITIES.forEach(function (sev) {
    var count = report.summary.by_severity[sev] || 0;
    if (count) { options.push({ key: sev, label: sev, count: count }); }
  });

  options.forEach(function (opt) {
    var button = el('button', 'filter');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(state.filter === opt.key));
    button.appendChild(el('span', null, opt.label));
    button.appendChild(el('span', 'count', String(opt.count)));
    button.addEventListener('click', function () {
      state.filter = opt.key;
      renderFilters(state.report);
      renderList(state.report);
      select(state.shown.length ? 0 : -1);
    });
    host.appendChild(button);
  });
}

function renderList(report) {
  var host = byId('list');
  clear(host);

  state.shown = report.breaks.filter(function (brk) {
    return state.filter === 'all' || brk.severity === state.filter;
  });

  byId('empty').hidden = state.shown.length > 0;
  byId('listcount').textContent = state.shown.length === report.breaks.length
    ? state.shown.length + ' findings, worst first'
    : state.shown.length + ' of ' + report.breaks.length + ' findings';

  state.shown.forEach(function (brk, index) {
    var li = el('li');
    var row = el('button', 'row');
    row.type = 'button';
    row.id = 'row-' + index;
    paintSeverity(row, brk.severity);

    var top = el('div', 'row-top');
    top.appendChild(el('span', 'row-sev', brk.severity));
    top.appendChild(el('span', 'row-risk', riskOf(brk)));
    row.appendChild(top);

    row.appendChild(el('div', 'row-security', brk.security || '(account level)'));
    row.appendChild(el('div', 'row-kind', brk.kind));

    row.addEventListener('click', function () { select(index, true); });
    li.appendChild(row);
    host.appendChild(li);
  });
}

/* --- detail -------------------------------------------------------------- */

function select(index, fromClick) {
  var rows = document.querySelectorAll('.row');
  for (var i = 0; i < rows.length; i++) {
    rows[i].classList.toggle('is-current', i === index);
    rows[i].setAttribute('aria-current', i === index ? 'true' : 'false');
  }

  state.current = index;
  var pane = byId('detail');
  clear(pane);

  if (index < 0 || !state.shown[index]) {
    pane.appendChild(el('div', 'd-placeholder',
      'Select a finding to see its evidence.'));
    return;
  }

  pane.appendChild(renderDetail(state.shown[index]));
  pane.scrollTop = 0;

  if (fromClick) {
    byId('shell').setAttribute('data-pane', 'detail');
    // Only steal focus on a deliberate pick, never on the initial render —
    // landing a screen reader mid-page on load is disorienting.
    pane.focus();
  }
}

function renderDetail(brk) {
  var wrap = el('div', 'd-wrap');
  paintSeverity(wrap, brk.severity);

  var back = el('button', 'd-back', '← All findings');
  back.type = 'button';
  back.addEventListener('click', function () {
    byId('shell').setAttribute('data-pane', 'list');
    var row = byId('row-' + state.current);
    if (row) { row.focus(); }
  });
  wrap.appendChild(back);

  var head = el('div', 'd-head');
  var titles = el('div');
  titles.appendChild(el('h2', 'd-security', brk.security || '(account level)'));
  var sub = [];
  if (brk.isin) { sub.push(brk.isin); }
  sub.push(brk.custodian);
  sub.push('as of ' + brk.as_of);
  titles.appendChild(el('div', 'd-sub', sub.join('  ·  ')));
  head.appendChild(titles);

  var risk = el('div', 'd-risk');
  risk.appendChild(el('span', 'd-risk-label', 'value at risk'));
  risk.appendChild(el('span', 'd-risk-fig', riskOf(brk)));
  head.appendChild(risk);
  wrap.appendChild(head);

  var tags = el('div', 'd-tags');
  tags.appendChild(el('span', 'chip', brk.severity));
  tags.appendChild(el('span', 'kindtag', brk.kind));
  wrap.appendChild(tags);

  var cols = el('div', 'd-cols');
  var main = el('div', 'd-main');
  var side = el('div', 'd-side');

  if (brk.narrative) {
    var assess = el('section');
    assess.appendChild(el('h3', 'd-h', 'Assessment'));
    assess.appendChild(el('p', 'narrative', brk.narrative));
    main.appendChild(assess);
  }

  if (brk.proposed_fix) {
    var action = el('section');
    action.appendChild(el('h3', 'd-h', 'Proposed action'));
    action.appendChild(el('p', 'action', brk.proposed_fix));
    side.appendChild(action);
  }

  cols.appendChild(main);
  cols.appendChild(side);
  wrap.appendChild(cols);

  var keys = orderedDetailKeys(brk.detail || {});
  if (keys.length) {
    var figures = el('section', 'd-figures');
    figures.appendChild(el('h3', 'd-h', 'Computed'));
    var dl = el('dl', 'figures');
    keys.forEach(function (key) {
      var cell = el('div', GAP_KEYS[key] ? 'is-gap' : null);
      cell.appendChild(el('dt', null, labelise(key)));
      cell.appendChild(el('dd', null, brk.detail[key]));
      dl.appendChild(cell);
    });
    figures.appendChild(dl);
    wrap.appendChild(figures);
  }

  if (brk.citations && brk.citations.length) {
    var evidence = el('section', 'd-evidence');
    evidence.appendChild(el('h3', 'd-h',
      'Evidence · ' + brk.citations.length + ' source line' +
      (brk.citations.length === 1 ? '' : 's')));
    brk.citations.forEach(function (cite) {
      evidence.appendChild(citeBlock(cite));
    });
    wrap.appendChild(evidence);
  }

  return wrap;
}

function citeBlock(cite) {
  var role = citeRole(cite.file);
  var box = el('div', 'cite');

  var head = el('div', 'cite-role');
  head.appendChild(el('span', 'cite-tag' + (role.ref ? ' is-ref' : ''), role.tag));
  head.appendChild(el('span', 'cite-where', cite.file + ':' + cite.line));
  box.appendChild(head);

  var line = el('div', 'cite-line');
  line.appendChild(el('span', 'cite-no', String(cite.line)));
  line.appendChild(el('pre', 'cite-text', cite.excerpt));
  box.appendChild(line);
  return box;
}

function orderedDetailKeys(detail) {
  // value_at_risk is already the headline figure on the row and in the detail
  // header; repeating it here reads as two different numbers to anyone
  // skimming the column.
  var keys = Object.keys(detail).filter(function (k) { return k !== 'value_at_risk'; });
  var known = DETAIL_ORDER.filter(function (k) { return keys.indexOf(k) !== -1; });
  var rest = keys.filter(function (k) { return DETAIL_ORDER.indexOf(k) === -1; }).sort();
  return known.concat(rest);
}

/* --- keyboard ------------------------------------------------------------ */

function wireKeys() {
  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    var step = 0;
    if (event.key === 'ArrowDown' || event.key === 'j') { step = 1; }
    if (event.key === 'ArrowUp' || event.key === 'k') { step = -1; }
    if (!step || !state.shown.length) { return; }

    event.preventDefault();
    var next = state.current + step;
    if (next < 0) { next = 0; }
    if (next > state.shown.length - 1) { next = state.shown.length - 1; }
    if (next === state.current) { return; }

    select(next);
    var row = byId('row-' + next);
    if (row) { row.scrollIntoView({ block: 'nearest' }); }
  });
}

/* --- boot ---------------------------------------------------------------- */

function fail(message) {
  var banner = byId('banner');
  banner.textContent = message;
  banner.hidden = false;
  byId('detail').appendChild(el('div', 'd-placeholder', message));
}

function render(report) {
  state.report = report;
  renderChrome(report);
  renderBanner(report);
  renderStatus(report);
  renderDrawer(report);
  renderFilters(report);
  renderList(report);
  select(state.shown.length ? 0 : -1);
  wireDrawer();
  wireKeys();
}

fetch('data/breaks.json', { cache: 'no-store' })
  .then(function (response) {
    if (!response.ok) { throw new Error('HTTP ' + response.status); }
    return response.json();
  })
  .then(render)
  .catch(function (err) {
    fail('Could not load data/breaks.json (' + err.message + '). ' +
         'Build the report first: python3 scripts/build.py');
  });
