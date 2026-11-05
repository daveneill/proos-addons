/* Every grid resolves to a KNOWN shape at Mac / iPad / phone.
 * Run: node tests/pro_widths_bench.js
 *
 * Dave, 11 Aug 2026: "The risk is why I push for continuity and fixed sizing
 * etc so they work for all Mac, iPad, Mobile … advise on how this can be
 * standardised across to make future changes or feature additions easier to add
 * without effecting other areas."
 *
 * The audit (docs/ProOS_Pro_Layout_Audit_2026-08-11.md) found the reason it is
 * so easy to break: SIX separate grids each declare "4 wide / 3 wide / 1 wide"
 * in their own words, at nine different breakpoints, and two of them use a
 * recipe that can be burst by a long name. Nothing checked that they agreed,
 * so nothing noticed when they didn't.
 *
 * This bench is step one of the standard, and it changes no product behaviour.
 * It reads every stylesheet in Pro, resolves each grid's ACTUAL columns at
 * three real viewport widths by applying the cascade the way a browser would,
 * and holds them to a registry.
 *
 *   MAC 1728   ·   IPAD 1200   ·   PHONE 390
 *
 * Two properties make it worth having:
 *   1. A NEW grid that nobody registered fails. You cannot add a layout that
 *      quietly disagrees with the rest of the app.
 *   2. A grid that changes shape at any of the three widths fails, naming the
 *      width — so a shared rule widened for one page cannot silently reshape
 *      another. That is exactly what happened to the Network page (register 75).
 *
 * IT ALSO READS THE STYLESHEET NOBODY COULD SEE. Pro has two literal <style>
 * blocks AND a third injected from JavaScript (`aw-screen-css`, ~6.2KB, 51
 * classes). Every earlier bench read only the literal ones. This one takes all
 * three, so the awareness screen is finally covered.
 */
const fs = require('fs');
const path = require('path');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'deploy', 'pro.html'), 'utf8');

let PASS = 0; const FAIL = [];
function check(name, cond) {
  if (cond) { PASS++; console.log('PASS  ' + name); }
  else { FAIL.push(name); console.log('FAIL  ' + name); }
}

const WIDTHS = [['Mac', 1728], ['iPad', 1200], ['phone', 390]];

// ── every stylesheet in the file, including the JS-injected one ─────────────
function allCss() {
  const sheets = [];
  const re = /<style[^>]*>([\s\S]*?)<\/style>/g;
  let m;
  while ((m = re.exec(SRC)) !== null) sheets.push(m[1]);
  // the injected sheet: el.textContent = `…css…`
  const inj = /createElement\(\s*['"]style['"]\s*\)[\s\S]{0,400}?textContent\s*=\s*`([\s\S]*?)`/g;
  let n = 0;
  while ((m = inj.exec(SRC)) !== null) { sheets.push(m[1]); n++; }
  return { css: sheets.join('\n'), sheets: sheets.length, injected: n };
}
const SHEETS = allCss();

// ── flatten to (condition, selector, columns) in source order ───────────────
function gridRules(css) {
  css = css.replace(/(^|[\s;{}(),])\/\*[\s\S]*?\*\//g, '$1 ');
  const out = [];
  const scan = (text, cond) => {
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      const sel = m[1].trim();
      if (!sel || sel.startsWith('@')) continue;
      const g = /grid-template-columns:\s*([^;}]+)/.exec(m[2]);
      if (g) { out.push({ cond, sel, cols: g[1].trim() }); continue; }
      // A MULTI-COLUMN LAYOUT IS A LAYOUT (register 398). This scanned only
      // grid-template-columns, so a page laid out with `columns:` was INVISIBLE
      // to the registry and to the stray check below — the guarantee would have
      // read as kept while a new layout answered to nobody. The device page is
      // packed rather than row-aligned (its cards are not uniform tiles), so it
      // is a columns layout, and it is held to the same 4 / 3 / 1 as the rest.
      const c = /(?:^|;)\s*columns:\s*([^;}]+)/.exec(m[2]);
      if (c) out.push({ cond, sel, cols: c[1].trim() });
    }
  };
  // Walk the sheet IN SOURCE ORDER — a media rule that appears after a
  // top-level rule must win at its width, which is only true if the two are
  // collected in the order a browser reads them. (Collecting all media rules
  // first made every grid resolve to its desktop value at every width.)
  let i = 0;
  while (i < css.length) {
    const at = css.indexOf('@media', i);
    if (at < 0) { scan(css.slice(i), null); break; }
    scan(css.slice(i, at), null);
    const open = css.indexOf('{', at);
    let d = 1, j = open + 1;
    while (j < css.length && d) { if (css[j] === '{') d++; else if (css[j] === '}') d--; j++; }
    scan(css.slice(open + 1, j - 1), css.slice(at + 6, open).trim());
    i = j;
  }
  return out;
}
const RULES = gridRules(SHEETS.css);

function condMatches(cond, w) {
  if (!cond) return true;
  if (/prefers-reduced-motion|hover|pointer|orientation/.test(cond)) return false;
  let ok = true;
  const mx = /max-width:\s*(\d+)px/g, mn = /min-width:\s*(\d+)px/g;
  let m;
  while ((m = mx.exec(cond)) !== null) if (!(w <= +m[1])) ok = false;
  while ((m = mn.exec(cond)) !== null) if (!(w >= +m[1])) ok = false;
  return ok;
}
// last matching declaration wins, as in the cascade
function colsAt(cls, w) {
  let val = null;
  for (const r of RULES) {
    const hit = r.sel.split(',').some(s => s.trim() === '.' + cls);
    if (hit && condMatches(r.cond, w)) val = r.cols;
  }
  return val;
}
function countOf(cols) {
  if (!cols) return null;
  // a bare number is a `columns:` count
  if (/^\d+$/.test(cols.trim())) return +cols.trim();
  let m = /^repeat\(\s*(\d+)\s*,/.exec(cols);
  if (m) return +m[1];
  if (/^(minmax\(0,\s*1fr\)|1fr)$/.test(cols)) return 1;
  if (/auto-fill|auto-fit/.test(cols)) return 'fluid';
  return cols.trim().split(/\s+/).length;
}

// ── THE REGISTRY ────────────────────────────────────────────────────────────
// Every grid in Pro, with the shape it is meant to hold at the three widths.
// `safe` means it uses minmax(0,…) so a long word cannot burst the column;
// bare `repeat(N,1fr)` carries min-width:auto and CAN overflow.
const REGISTRY = [
  { cls: 'grid-4',      shape: [4, 3, 1], safe: true,  note: 'THE card grid — the name for new work' },
  // ADDED (register 398). Dave: "keep with the standard column layouts we have
  // always used in pro — 1 x Mobile 3 x iPad and 4 x Mac". Same counts, same
  // breakpoints as the card family. It PACKS instead of aligning to rows
  // because its cards are not uniform tiles — Settings is seventeen rows and
  // Identity is three, and a strict row would leave a dead gap beside every
  // short card. `safe` is not meaningful for a columns layout: there is no
  // track to burst, the content just wraps.
  { cls: 'dgrid',       shape: [4, 3, 1], safe: true,  note: 'the device page — packed, not row-aligned' },
  // ADDED 7 Oct 2026 (register 713): the standard device page — three columns of one height. Mac 1:2:1 (four
  // parts, the house's Mac count), iPad three equal, phone one. Dave: "mobile 1 column, ipad 3 x columns and mac 4".
  // ADDED 7 Oct 2026 (register 714): the standard sensor page — what it is beside its settings; phone stacks them.
  { cls: 'sp-cols',     shape: [2, 2, 1], safe: true,  note: 'the standard sensor page — now and settings side by side, stacked on a phone' },
  { cls: 'dp-cols',     shape: [3, 3, 1], safe: true,  note: 'the standard device page — 1:2:1 on Mac, 3 on iPad, 1 on phone' },
  { cls: 'itile-grid',  shape: [4, 3, 1], safe: true,  note: 'integrations tiles' },
  { cls: 'sys-grid',    shape: [4, 3, 1], safe: true,  note: 'systems / toolbox / music tiles' },
  { cls: 'aw-list',     shape: [4, 3, 1], safe: true,  note: 'awareness list' },
  { cls: 'scene-grid',  shape: [4, 3, 1], safe: true,  note: 'scenes' },
  { cls: 'room-grid',   shape: [4, 3, 1], safe: true,  note: 'rooms' },
  { cls: 'hubgrid',     shape: [4, 3, 1], safe: true,  note: 'hub' },
  // MEASURED, not assumed. Its comment says "2 on iPad portrait" and that is
  // true — but its breakpoints are 1023/768/767, not the card family's
  // 1439/700. So a 12.9" iPad in landscape (1200) gets 3, and only portrait
  // (768–1023) gets 2. The shape is right; the BREAKPOINTS disagree with every
  // other grid in the app. Recorded here so the disagreement is visible, and
  // named by the breakpoint check below rather than quietly tolerated.
  { cls: 'hub-grid',    shape: [3, 3, 1], safe: false, note: '3/2/1 on ITS OWN breakpoints (1023/768/767)' },
  // `.mc-board` was registered here as 3/3/1 for one build. It is NO LONGER A
  // GRID: Dave asked twice for one list, and the finished-site case proved him
  // right — with no outstanding work the three columns render two empty panels
  // either side of a full one. One list has no width question to answer, which
  // is why it left this registry rather than changing shape in it.
  { cls: 'hlt-stats',   shape: [3, 3, 3], safe: false, note: 'health counters — 3/3/3 since the Open Alarms tile went (689)' },
  { cls: 'bays',        shape: [3, 3, 1], safe: false, note: 'commissioning bays' },
  { cls: 'ra-styles',   shape: [3, 3, 3], safe: false, note: 'art styles picker' },
  // (.hlt-cards was registered here 23 Sep, 583, and removed 24 Sep, 588:
  // Dave ruled the Health rooms back to rows. The grid no longer exists.)
  // ADDED 24 Sep 2026 (register 589): the Health room list — rows on a phone,
  // three-across tiles on the iPad and Mac (Dave: "I meant only mobile").
  { cls: 'hlt-rooms',   shape: [3, 3, 1], safe: true,  note: 'Health rooms — tiles above a phone, rows on it' },
  // ADDED 16 Oct 2026 (the Josh.ai flow, Dave's approved catalogue AR-02 / DV-01): the room's own pages listed two
  // across beside the area list, one across on a phone; and the three counts that head All Devices and Software.
  { cls: 'v3rgrid',     shape: [2, 2, 1], safe: true,  note: 'a room\'s set-up rows — two across, one on a phone' },
  { cls: 'v3kp',        shape: [3, 3, 3], safe: true,  note: 'three counts heading a page (All Devices, Software)' },
  // ADDED 5 Nov 2026 (register 1216, the UniFi suite mockup Dave agreed): a tile per UniFi app — three across on the
  // Mac and iPad as the mockup drew it, one across on a phone; standard breakpoints only.
  { cls: 'uni-tiles',   shape: [3, 3, 1], safe: true,  note: 'UniFi suite: a tile per app (Network, Protect, Access, Connect, Talk)' },
  // ADDED 6 Nov 2026 (register 1223): Protect's Live View — camera tiles four across on the Mac, three on the iPad (beside
  // the camera panel), two on a phone, as the mockup drew it; standard breakpoints only.
  { cls: 'uni-cams',    shape: [4, 3, 2], safe: true,  note: 'Protect Live View: a snapshot tile per camera' },
  // ADDED 6 Nov 2026 (register 1229): the mockup's left-side section list beside the page, for Protect and Network —
  // list + page on the Mac and iPad, stacked on a phone (the list becomes a scrolling row); standard breakpoints only.
  { cls: 'uni-split',   shape: [2, 2, 1], safe: true,  note: 'UniFi Protect / Network: section list beside the page' },
];

// ── STEP 2 OF THE STANDARD: ONE STYLESHEET ─────────────────────────────────
// Pro used to inject a third sheet from JavaScript (`aw-screen-css`, 6.2KB, 51
// classes for the awareness screen). It worked — but it was invisible to every
// tool that reads <style> tags, including every bench written before this one.
// A rule you cannot see is a rule you cannot check, and 51 classes had no cover
// at all. Styles now live in the markup, where one search finds every use.
check('styles live in the markup — nothing is injected from JavaScript',
      !/createElement\(\s*['"]style['"]\s*\)/.test(SRC));
check('  and the awareness screen\u2019s classes are in a literal stylesheet',
      /<style[^>]*>[\s\S]*\.aw-screen\{[\s\S]*<\/style>/.test(SRC)
      && /<style[^>]*>[\s\S]*\.awl-row\{[\s\S]*<\/style>/.test(SRC));
check('  the injector is gone, and nothing still calls it',
      !/_awInjectCss/.test(SRC));

console.log('stylesheets read: ' + SHEETS.sheets + ' (including ' + SHEETS.injected + ' injected from JS)\n');
// (Pin rewritten, not weakened. It used to assert there WERE three sheets, one
// injected — true when this bench was written, and the reason it was written.
// Step 2 folded the injected one in, so the rule it protected — every CSS rule
// in Pro is visible to this bench — is now met by there being no injected sheet
// left to miss. Asserting the old shape would have demanded the defect back.)
check('every CSS rule in Pro is visible to this bench (' + SHEETS.sheets
      + ' sheets, ' + SHEETS.injected + ' injected)',
      SHEETS.sheets >= 2 && SHEETS.injected === 0
      && /\.awl-row\{/.test(SHEETS.css) && /\.aw-screen\{/.test(SHEETS.css));

// 1 · every registered grid holds its shape at all three widths
REGISTRY.forEach(g => {
  const got = WIDTHS.map(([, w]) => countOf(colsAt(g.cls, w)));
  const want = g.shape;
  const ok = got.every((n, i) => n === want[i]);
  check('.' + g.cls + ' — ' + WIDTHS.map(([n], i) => n + ' ' + got[i]).join(' · ')
        + (ok ? '' : '  WANTED ' + want.join(' · ')),
        ok);
});

// 2 · no UNREGISTERED full-width grid — a new layout must be declared
const KNOWN = new Set(REGISTRY.map(g => g.cls));
// component-internal grids (a row's own columns) are not page layouts
// component-internal grids (a row's own columns) are not page layouts
// idgrid: the key/value pairs inside one card on the device page (register 398)
// act-r:  one activity line's own three columns — dot, name, value (register 402)
// as2-wrap LEFT this set (register 432): the Assist page used to be its own
// two-column grid (conversation + 300px rail) on its own 1023px breakpoint —
// exactly the kind of one-off this bench exists to name. It now WEARS .grid-4
// and declares no columns of its own, so it is held by the registry above.
// sp-kinds / sp-save: the Shows As picture buttons and the Back / Save pair inside the sensor page's settings card (714)
// dp-zgrid: the zone tiles inside the Zones card, two across (register 713)
// ip-row: one device row's own five columns on the integration page — icon,
//         name, model, room, now (register 710, Dave's approved mock, 7 Oct 2026)
// chl-grid / chl-split / chl-tr / chl-two: inside the ONE Channel Lists card (register 1050) — the list cards, the
//         list-beside-editor pair, one channel row's own columns, the number/name pair. They answer to the CARD's width
//         (container queries on #channels-body and .chl-tbl), not the viewport, so this bench's three viewport widths
//         don't describe them; pro_channel_lists_bench drives them at the iPad and phone page widths instead.
// unp-face: one switch's sockets (register 1220) — its column count is the switch's port count (--cols), drawn as the
//         switch's own face inside a faceplate that scrolls sideways when a 48-port switch is wider than a phone. It is
//         a picture of the hardware, not a page layout, so no viewport width changes its columns.
const INTERNAL = new Set(['aw-row', 'tile', 'nw-list', 'ipk-grid', 'awl-row', 'awi-row', 'rooms-grid', 'idgrid', 'act-r', 'ip-row', 'dp-zgrid', 'sp-kinds', 'sp-save',
  'chl-grid', 'chl-split', 'chl-tr', 'chl-two', 'unp-face']);
const seen = new Set();
RULES.forEach(r => r.sel.split(',').forEach(s => {
  const m = /^\.([a-zA-Z][\w-]*)$/.exec(s.trim());
  if (m) seen.add(m[1]);
}));
const stray = [...seen].filter(c => !KNOWN.has(c) && !INTERNAL.has(c)).sort();
check('no unregistered grid' + (stray.length ? ' — found: ' + stray.join(', ') : '')
      + ' (' + seen.size + ' grids seen)',
      stray.length === 0);

// 3 · a full-width card grid may not use a recipe that can overflow.
// `repeat(N,1fr)` carries min-width:auto — one long room name pushes the column
// past the container. `minmax(0,1fr)` clamps it. The audit found two grids on
// the unsafe recipe; they are not a style difference, they are a latent defect.
REGISTRY.filter(g => g.safe).forEach(g => {
  const bad = [];
  WIDTHS.forEach(([nm, w]) => {
    const c = colsAt(g.cls, w);
    if (c && /repeat\(\s*\d+\s*,\s*1fr\s*\)/.test(c)) bad.push(nm);
  });
  check('  .' + g.cls + ' cannot be burst by a long name'
        + (bad.length ? ' — bare 1fr at ' + bad.join(', ') : ''),
        bad.length === 0);
});

// 4 · the card grids all break at the SAME widths — one system, not six
const family = ['itile-grid', 'sys-grid', 'aw-list', 'scene-grid', 'room-grid', 'hubgrid'];
const shapes = family.map(c => WIDTHS.map(([, w]) => countOf(colsAt(c, w))).join('/'));
check('all six card grids agree at every width (' + shapes[0] + ')',
      new Set(shapes).size === 1);

// ── STEP 3 OF THE STANDARD: ONE GRID, DECLARED ONCE ────────────────────────
// Six grids each said "4 wide / 3 wide / 1 wide" in their own words. Six copies
// of one intent is six chances to drift — and two of them HAD drifted onto a
// recipe that could be burst by a long name. The recipe is now declared ONCE
// and worn by all six, so there is nowhere for a difference to live. Each grid
// keeps its own gap and margin: those genuinely differ, and always did.
//
// NO MARKUP WAS TOUCHED. The six class names stay exactly as they are, which is
// the whole reason this is safe — a rename can miss a use, and a missed use is
// how the Network page broke (register 75). `.grid-4` is the name for new work.
const FAMILY = ['grid-4', 'itile-grid', 'sys-grid', 'aw-list', 'scene-grid', 'room-grid', 'hubgrid'];
const sharedRule = new RegExp(
  FAMILY.map(c => '\\.' + c).join(',') + '\\{grid-template-columns:repeat\\(4,minmax\\(0,1fr\\)\\)\\}');
check('the 4-up recipe is declared ONCE, worn by all six grids plus .grid-4',
      sharedRule.test(SHEETS.css.replace(/\s+/g, '')));
check('  and no card grid declares its own columns any more',
      FAMILY.slice(1).every(c => {
        // preceded by start-of-sheet or a closing brace — NOT a comma, which
        // would match the class's place inside the shared selector itself.
        const own = new RegExp('(?:^|\\})\\.' + c + '\\{[^}]*grid-template-columns', 'm');
        return !own.test(SHEETS.css.replace(/\s*\n\s*/g, ''));
      }));
check('  .grid-4 is a usable class on its own — the name for new work',
      /\.grid-4[^{]*\{[^}]*display:grid/.test(SHEETS.css));
// The phone rule mattered too: `1fr` at one column carries the same
// min-width:auto trap as `repeat(4,1fr)` did at four.
check('  even at one column the recipe cannot be burst',
      !/\.(room-grid|hubgrid)\{grid-template-columns:1fr\}/.test(SHEETS.css.replace(/\s+/g, '')));

// 5 · THE BREAKPOINTS THEMSELVES. The audit found nine in use across Pro. The
// card family already agrees on two — 1439 and 700 — and that pair is the
// proposed standard. This check does not yet force every grid onto it (that
// moves every page at once, and is the LAST step of the plan by design); it
// reports which grids are on the standard and which are not, so the number can
// only go down.
const STANDARD = [1439, 700];
function bpsOf(cls) {
  const out = new Set();
  RULES.forEach(r => {
    if (!r.cond) return;
    if (!r.sel.split(',').some(x => x.trim() === '.' + cls)) return;
    (r.cond.match(/\d+/g) || []).forEach(n => out.add(+n));
  });
  return [...out].sort((a, b) => b - a);
}
const offStandard = REGISTRY
  .map(g => [g.cls, bpsOf(g.cls)])
  .filter(([, bps]) => bps.length && bps.some(b => !STANDARD.includes(b)));
console.log('\nbreakpoints per grid:');
REGISTRY.forEach(g => {
  const b = bpsOf(g.cls);
  console.log('  .%s%s %s', g.cls, ' '.repeat(Math.max(0, 12 - g.cls.length)),
              b.length ? b.join(', ') : '(none — one shape at every width)');
});
check('the six card grids are all on the standard breakpoints (1439 / 700)',
      family.every(c => bpsOf(c).every(b => STANDARD.includes(b))));
// A RATCHET, not a gate. Three grids are off the standard today (measured 11
// Aug). This passes at three and fails at four — so the number can only go
// down, and a new page cannot quietly add a tenth breakpoint. When the last
// step of the plan moves them, this number comes down with it.
const OFF_STANDARD_TODAY = 3;
check('  grids off the standard: ' + (offStandard.length
        ? offStandard.map(([c, b]) => '.' + c + ' (' + b.join('/') + ')').join(', ')
        : 'none') + ' — ' + offStandard.length + ' of ' + OFF_STANDARD_TODAY + ' allowed',
      offStandard.length <= OFF_STANDARD_TODAY);

// ── STEP 5 OF THE STANDARD: THE BREAKPOINTS THEMSELVES ─────────────────────
// The audit proposed collapsing nine breakpoints to two. Measured against real
// device widths, that was too simple — the same way rule 3 was (register 80).
//
//   1439 / 700  is the CONTENT question: how many columns fit.
//   1024 / 1023 / 768 / 767 is the CHROME question: how much nav rail fits. It
//   is a deliberate THREE-state rail — expanded, mid, collapsed — and no
//   two-number system can express three states. Forcing it would move the rail
//   on iPad 12.9 portrait (1024) and iPad mini (744).
//   820 is `.bays` alone: 3-up above, 1-up below. Moving it to 700 changes
//   iPad mini portrait from 1-up to three 248px bays.
//
// So three breakpoints were retired at ZERO device cost — 640, 600 and min-600
// all moved to 700, and no real device sits between them. The rest are declared
// exceptions, not accidents. This check is a RATCHET: the count can go down,
// never up.
const ALL_BP = new Set();
{
  const re = /@media[^{]*?(\d+)px/g;
  let m;
  while ((m = re.exec(SHEETS.css)) !== null) ALL_BP.add(+m[1]);
}
const STANDARD_BP = [1439, 700];
const DECLARED_EXCEPTIONS = {
  1024: 'nav rail — expanded (min-width)',
  1023: 'nav rail — mid band with 768',
  768:  'nav rail — mid band with 1023',
  767:  'nav rail — collapsed, and the assist dock',
  820:  '.bays — 3-up above, 1-up below; 700 would give iPad mini three 248px bays',
};
const undeclared = [...ALL_BP].filter(b => !STANDARD_BP.includes(b) && !DECLARED_EXCEPTIONS[b]).sort((a, b) => b - a);
console.log('\nbreakpoints in Pro: ' + [...ALL_BP].sort((a, b) => b - a).join(', '));
check('every breakpoint is either standard (1439/700) or a DECLARED exception'
      + (undeclared.length ? ' — undeclared: ' + undeclared.join(', ') : ''),
      undeclared.length === 0);
const BP_COUNT_TODAY = 7;
check('  breakpoint count is ' + ALL_BP.size + ', ratchet allows at most ' + BP_COUNT_TODAY
      + ' (was 9 before step 5)',
      ALL_BP.size <= BP_COUNT_TODAY);
check('  the three retired breakpoints are gone (640, 600, and min-600)',
      !ALL_BP.has(640) && !ALL_BP.has(600));

// ── ROOM ARTWORK IS A BACKDROP, NOT A BILLBOARD (register 141) ─────────────
// Dave, 14 Aug: "I like the room artwork but I think they are too bright — can
// we make them darker with just showing the artwork through." The photo tile
// needs BOTH halves and they guard different things: the image is dimmed so
// the grid settles, and the scrim covers the WHOLE tile (it used to start at
// 45%, leaving the top at full brightness) so the room name and state stay
// legible over a busy picture. Removing either one brings the complaint back.
check('the room photo is dimmed, not shown at full brightness',
      /\.hub-room \.ph-img\{[^}]*filter:brightness\(\.[0-9]+\)/.test(SRC), true);
check('  …and the scrim veils the WHOLE tile, not just its foot',
      /\.hub-room \.ph-scrim\{[^}]*linear-gradient\(180deg,rgba\(0,0,0,0\.[0-9]+\) 0%/.test(SRC), true);
check('  …still deepening at the bottom, where the words sit',
      /\.hub-room \.ph-scrim\{[^}]*rgba\(0,0,0,0\.9[0-9]?\)\)/.test(SRC), true);

console.log('\n' + PASS + ' checks passed, ' + FAIL.length + ' failed');
process.exit(FAIL.length ? 1 : 0);
