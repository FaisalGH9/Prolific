// Execute the generated browser guard in an isolated DOM simulation. No network.
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = require('node:fs').readFileSync(0, 'utf8');

function page(options = {}) {
  let now = 1000000;
  let visible = options.button !== false;
  let disabled = !!options.disabled;
  let outcome = options.outcome || '';
  let clicks = 0;
  let title = options.heading === undefined ? 'Target study' : options.heading;
  let loading = !!options.loading;
  let observed = 0;
  let timers = [];
  const logs = [];
  const storage = options.storage || new Map();
  if (!options.storage) {
    storage.set('prolificAutoAttemptKey', 'attempt-1');
    storage.set('prolificAutoTargetStudyId', 'target-id');
    storage.set('prolificAutoTargetStudyTitle', 'Target study');
    storage.set('prolificAutoTargetStudyDetectedAt', String(now));
  }
  const rect = () => ({width: 100, height: 30});
  const heading = {get innerText() { return title; }, getBoundingClientRect: rect};
  const detail = {
    get innerText() { return `${title} ${outcome}`; },
    querySelectorAll: selector => selector === 'h1' ? (title ? [heading] : []) : [],
    getBoundingClientRect: rect,
  };
  const button = {
    get innerText() { return 'Take part in this study'; },
    get disabled() { return disabled; },
    parentElement: detail,
    getAttribute: () => null, getBoundingClientRect: rect,
    querySelectorAll: () => [], scrollIntoView() {},
    click() { clicks++; if (options.synchronousOutcome) outcome = options.synchronousOutcome; },
  };
  const status = {get innerText() { return outcome; }, getBoundingClientRect: rect};
  const document = {
    body: {get innerText() { return loading ? '' : `${title} ${outcome}`; }},
    get readyState() { return loading ? 'loading' : 'complete'; }, documentElement: {},
    querySelectorAll(selector) {
      if (selector.startsWith('button,')) return visible ? [button] : [];
      if (selector.startsWith('h1')) return title && options.headingLevel !== 2 ? [heading] : [];
      if (selector.startsWith("[role='alert']")) return outcome ? [status] : [];
      return [];
    },
  };
  class FakeDate extends Date { static now() { return now; } }
  const context = vm.createContext({
    window: {}, document, Date: FakeDate,
    location: {href: 'https://app.prolific.com/studies', pathname: '/studies'},
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    console: {log: (...args) => logs.push(args)},
    MutationObserver: class { observe() { observed++; } },
    setInterval: callback => timers.push(callback),
  });
  return {
    install: () => vm.runInContext(script, context),
    tick: () => timers.forEach(callback => callback()),
    outcome: value => { outcome = value; },
    showButton: () => { visible = true; },
    loading: value => { loading = value; },
    advance: ms => { now += ms; },
    count: marker => logs.filter(args => args[0] === marker).length,
    get clicks() { return clicks; }, get timers() { return timers.length; },
    get observed() { return observed; }, storage, logs,
    reload() { context.window = {}; timers = []; },
  };
}
const full = 'PROLIFIC_STUDY_FULL';
const reserved = 'PROLIFIC_SEAT_RESERVED';
const timeout = 'PROLIFIC_TAKE_PART_NO_RESPONSE';
const expired = 'PROLIFIC_TARGET_STUDY_EXPIRED';
let cases = 0;
for (const [text, marker] of [['Study is full', full], ['Your place has been reserved', reserved]]) {
  const p = page();
  assert.equal(p.install(), 'clicked');
  assert.equal(p.observed, 1);
  assert.equal(p.timers, 1);
  p.outcome(text); p.tick(); p.tick();
  assert.equal(p.count(marker), 1);
  assert.equal(p.clicks, 1);
  assert.equal(p.install(), 'already-active');
  p.reload(); p.install(); p.tick();
  assert.equal(p.count(marker), 1, 'reload must not duplicate terminal results');
  assert.equal(p.clicks, 1, 'completed attempt must not click again');
  cases++;
}
for (const options of [{button: false}, {disabled: true}]) {
  const p = page({...options, outcome: 'Study is full'});
  p.install(); p.tick(); p.tick();
  assert.equal(p.clicks, 0);
  assert.equal(p.count(full), 1);
  assert.equal(p.logs.find(args => args[0] === full)[1], 'attempt-1');
  cases++;
}
{
  const p = page({button: false});
  p.install(); p.showButton(); p.tick();
  p.outcome('All places have been taken'); p.tick();
  assert.equal(p.clicks, 1); assert.equal(p.count(full), 1); cases++;
}
{
  const p = page({button: false, heading: 'Another study', outcome: 'Study is full'});
  p.install(); p.tick();
  assert.equal(p.count(full), 0, 'unrelated study must not produce full result'); cases++;
}
{
  const p = page({button: false, headingLevel: 2, outcome: 'Study is full'});
  p.install(); p.tick();
  assert.equal(p.count(full), 0, 'matching list card heading is not a study detail'); cases++;
}
{
  const p = page({button: false, heading: 'Waiting for studies'});
  p.install(); p.advance(21000); p.tick(); p.tick();
  assert.equal(p.count(expired), 1); assert.equal(p.count(full), 0); cases++;
}
{
  const p = page();
  p.install(); p.advance(121000); p.tick(); p.tick();
  assert.equal(p.count(timeout), 1); assert.equal(p.clicks, 1); cases++;
}
{
  const p = page({synchronousOutcome: 'Study is full'});
  p.install(); p.tick();
  assert.equal(p.count(full), 1); cases++;
}
{
  const p = page({outcome: 'Essential maintenance: you may see more study full messages'});
  p.install(); p.tick();
  assert.equal(p.count(full), 0); cases++;
}
{
  const p = page();
  p.install(); p.outcome('Study is full'); p.tick();
  p.storage.set('prolificAutoAttemptKey', 'attempt-2');
  p.storage.set('prolificAutoTargetStudyId', 'target-id');
  p.storage.set('prolificAutoTargetStudyTitle', 'Target study');
  p.storage.set('prolificAutoTargetStudyDetectedAt', '1000000');
  p.outcome(''); p.reload(); p.install();
  p.outcome('Your place has been reserved'); p.tick();
  assert.equal(p.clicks, 2); assert.equal(p.count(full), 1); assert.equal(p.count(reserved), 1); cases++;
}
{
  const p = page(); p.install(); p.reload();
  p.outcome('Study is full'); p.install(); p.tick();
  assert.equal(p.clicks, 1, 'restoring pending attempt must not click again');
  assert.equal(p.count(full), 1); cases++;
}
{
  const p = page(); p.install(); p.outcome('High demand. Please try again'); p.tick();
  p.advance(600); p.outcome(''); p.tick();
  assert.equal(p.clicks, 2, 'high demand must still retry');
  p.outcome('Study is full'); p.tick(); p.tick();
  assert.equal(p.count(full), 1); cases++;
}
{
  // Reproduce the report: blank at detection, still loading after 20 seconds,
  // ready around 40 seconds, then an explicit full result shortly afterward.
  const p = page({loading: true});
  p.install(); p.advance(21000); p.tick();
  assert.equal(p.count(expired), 0); assert.equal(p.clicks, 0);
  p.advance(20000); p.loading(false); p.tick();
  assert.equal(p.clicks, 1);
  p.outcome('Study is full'); p.tick(); p.tick();
  assert.equal(p.count(full), 1); assert.equal(p.count(expired), 0); cases++;
}
{
  // Pause the search budget across an empty SPA refresh.
  const p = page({button: false}); p.install();
  p.advance(10000); p.tick(); p.loading(true); p.tick();
  p.advance(50000); p.tick();
  assert.equal(p.count(expired), 0);
  p.loading(false); p.tick(); p.advance(9000); p.tick();
  assert.equal(p.count(expired), 0);
  p.advance(1100); p.tick(); assert.equal(p.count(expired), 1); cases++;
}
{
  // Navigation replaces the document; persisted budgets exclude blank time.
  const p = page({button: false}); p.install(); p.advance(5000); p.tick();
  p.reload(); p.loading(true); p.install(); p.advance(45000); p.tick();
  p.loading(false); p.showButton(); p.tick();
  assert.equal(p.clicks, 1); assert.equal(p.count(expired), 0); cases++;
}
{
  const p = page(); p.install(); p.advance(60000); p.tick();
  p.loading(true); p.tick(); p.advance(80000); p.tick();
  assert.equal(p.count(timeout), 0);
  p.loading(false); p.outcome('Your place has been reserved'); p.tick();
  assert.equal(p.count(reserved), 1); assert.equal(p.count(timeout), 0); cases++;
}
{
  // A Python fallback must cancel a late guard as well, without a second click.
  const p = page({loading: true}); p.install();
  p.storage.set('prolificAutoReportedOutcomeKey', 'attempt-1');
  p.loading(false); p.outcome('Study is full'); p.tick();
  assert.equal(p.clicks, 0); assert.equal(p.count(full), 0); cases++;
}
console.log(`${cases} browser guard scenarios passed`);
