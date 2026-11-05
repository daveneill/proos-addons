// Our own certified panels mirror what the box serves, same as every other
// integration — and keep everything they already had.
// Run: node tests/pro_certified_mirror_bench.js
//
// 12 Sep 2026 (register 415). Dave, on the UniFi Protect panel: "we have
// missed all our certified integrations like UniFi Protect as its details need
// to be incorporated into our current settings same as ProOS music etc it
// needs to retain all the info we already have and anything extra the native
// HA mirrors as I guess you added the camera live feeds in past stages but im
// not sure where these settings were added."
//
// HE IS RIGHT, AND IT IS WORTH BEING EXACT ABOUT WHICH ONES. Eleven of the
// twelve certified integrations — Sonos, Samsung, Apple TV, Android TV, Denon,
// HEOS, LIFX, WiZ, Shelly, Cool Automation, Ness — open the GENERIC page and
// have had every one of the last seventeen builds all along. THREE own a
// dedicated panel and never ran the mirror at all: UniFi Protect, UniFi
// Network and ProOS Music. Their tile opens that panel instead of the page.
//
// ON PROTECT THAT IS THE SHARPEST. Thirteen cameras, and from that panel no
// way to reach a single device page — which is exactly where register 411 put
// the live feed. The answer to "I'm not sure where these settings were added"
// is: somewhere he could not get to from there.
//
// THE FIX IS STRUCTURAL, NOT A FOURTH COPY. Everything the integration page
// mirrors was written inside the generic renderer's body. It is now one
// function that returns HTML and touches nothing, so the generic page and the
// three panels all call the SAME mirror. There is one to keep true, not four,
// and a certified panel added later gets it in one line.
//
// AND NOTHING THE PANELS ALREADY HAVE IS TOUCHED. The console IP, the API key,
// the local Protect user, the camera placement, the doorbell and talk-back are
// ProOS's own commissioning; the mirror is APPENDED below them.
const fs = require('fs'), path = require('path');
let pass = 0, fail = 0;
function ok(c, msg) { if (c) { pass++; console.log('  ok  ' + msg); } else { fail++; console.log('  FAIL ' + msg); } }
const P = fs.readFileSync(path.join(__dirname, '..', 'deploy', 'pro.html'), 'utf8');
const code = P.replace(/(^|[\s;{}(),])\/\*[\s\S]*?\*\//g, '$1 ').replace(/(^|[^:'"`\\])\/\/[^\n]*/g, '$1');

// ── THE MIRROR IS ONE THING ────────────────────────────────────────────────
const M = (code.match(/function intgMirrorHTML\(dom, opts\)\{[\s\S]*?\n\}/) || [''])[0];
const R = (code.match(/function renderIntegration\(dom\)\{[\s\S]*?\n\}/) || [''])[0];
ok(M.length > 1500, 'read the mirror (' + M.length + ' bytes)');
ok(R.length < 400, 'the generic page is now a thin caller (' + R.length + ' bytes)');
// RE-PINNED 7 Oct 2026 (register 710, Dave approved the standard-page mocks):
// the generic page now paints intgPageHTML — the standard page — and that
// page takes the SAME mirror for everything that is not a device row. The
// device rows themselves carry the mirror's facts, pinned in
// pro_integration_page_standard_bench.js.
const PG = (code.match(/function intgPageHTML\(dom\)\{[\s\S]*?\n\}/) || [''])[0];
ok(/setHTML\(host, intgPageHTML\(dom\)\)/.test(R) && /intgMirrorHTML\(dom,\{noDevices:true,noAdd:true\}\)/.test(PG),
   '  …that paints the standard page through setHTML, and the page takes the one mirror for the rest');
ok(!/host\.innerHTML/.test(R) && !/host\.innerHTML/.test(M),
   'the mirror touches no element at all — it returns HTML, which is what lets a panel drop it in');
ok(/return \(\(opts&&opts\.noAdd\)\?'':addBtn\)/.test(M),
   'a certified panel can ask for it without the add button, because its own steps do the setting up');

// ── AND THE MIRROR STILL CARRIES EVERY BUILD THAT WENT INTO IT ─────────────
for (const [what, re_] of [
  ['the fault banner',            /_faulted/],
  ['the device rows',             /openDeviceManage/],
  ['their model and count (408)', /const _n=_ents\.filter\(e=>e\.device_id===d\.id\)\.length/],
  ['the attention badge',         /_deviceAttention\(d\)/],
  ['entries with no device',      /Configured \(no device\)/],
  ['entities with no device (412)', /Entities \(no device\)/],
  ['the shape-aware add (412)',   /intgAddLabel\(dom\)/],
]) ok(re_.test(M), 'the mirror still carries ' + what);

// ── THE THREE PANELS CALL IT ───────────────────────────────────────────────
const C = (code.match(/function certMirrorHTML\(dom, wrap\)\{[\s\S]*?\n\}/) || [''])[0];
ok(C.length > 400, 'read the panel painter (' + C.length + ' bytes)');
ok(/intgMirrorHTML\(dom,\{noAdd:true\}\)/.test(C),
   'the panels use the SAME mirror — there is no second copy to drift');
ok(/if\(!has\) return '';/.test(C),
   'a panel whose integration is not configured yet draws nothing, rather than an empty section under a half-done setup');

// ── IT WEARS THE PANEL IT IS IN ────────────────────────────────────────────
// Dave: "dont just add it to the pages make sure it keeps with current layout
// and look." The three panels are not laid out alike, so the mirror is wrapped
// in whichever shape the panel already uses. No fourth shape was invented.
ok(/<div class="mstep"><div class="mstep-head"><span class="mstep-num">\$\{wrap\.step\}/.test(C),
   'in a numbered-step panel it is a numbered .mstep card, the same as that panel\'s own steps');
ok(/<span class="mstep-pill">\$\{nD\} device/.test(C),
   '  …with a count in the pill slot, where those steps put their status');
ok(/<div class="sys-sec"><div class="hub-fl">/.test(C),
   'in the tile page it is a .sys-sec under a .hub-fl label, the same as Library and Hardware');
ok(/class="bld-sec-h"/.test(C),
   '  …and there is a plain heading for a panel that asks for neither');
ok(!/\.certmir|cert-mirror\{/.test(P),
   'no new class was invented for it — it borrows three shapes that already existed');
// RE-PINNED, register 416. Protect took the mirror as a step 3 for exactly one
// build, and Dave refused it: "can we just redesign the UniFi Protect and
// combine section 2 and 3 as it seems doubled up." It was doubled up — step 2
// already listed every camera. Its own list carries the mirrored facts now and
// only the NON-device parts of the mirror ride there, so what this bench holds
// for Protect changes from "has the mirror as a step" to "has the mirror's
// remainder inside the step it already had, and no second list". Nothing got
// weaker: pro_protect_one_list_bench holds the merged row, 34 checks.
ok(!code.includes("certMirror('unifi-mirror'"),
   'UniFi Protect no longer takes the mirror as a step of its own — its own camera list carries it');
// RE-PINNED again, register 417, and this is the last move in that sequence:
// 415 gave Protect the mirror as a step of its own, 416 merged it into the
// panel's own camera row, 417 threw that row away and made the list the shared
// mirror outright. Dave: "can we have the protect follow this look instead as
// the renaming can be done in the main page with everything else and is
// uniform across other." So the check is no longer that Protect subtracts the
// device list — it is that Protect IS the list.
ok(/s2 = intgMirrorHTML\('unifiprotect',\{noAdd:true\}\);/.test(code),
   '  …Protect\'s camera list IS the shared mirror, whole, with no panel-specific row left');
ok(code.includes("certMirror('music-mirror','music_assistant',{label:true"),
   'ProOS Music takes it as a labelled group on its home');
ok(new RegExp('id="music-mirror"').test(P), 'the music-mirror container is in the panel markup');

// ── UNIFI NETWORK: ONE DEVICE LIST, NOT TWO ────────────────────────────────
// That panel already had its own near-copy of the device list, written before
// the mirror existed. Appending beside it would have shown every UniFi device
// twice — and the near-copy was missing everything the last five builds added.
ok(!/_uniNetDeviceRows/.test(code),
   'the near-copy of the device list is GONE, not left beside the mirror');
// Pin rewritten, not weakened (register 1220): the panel now paints through setHTML with its Ports / Integration switch
// in front, and the Integration section is still the card, then the ONE shared mirror under "Devices".
// Re-pinned again (register 1229): the section list moved to the left (wrap(...)); still the card, then the ONE mirror.
ok(/setHTML\(body, wrap\(_unifiNetCard\(\)\s*\n?\s*\+ certMirrorHTML\('unifi',\{name:'Devices'\}\)/.test(code),
   '  …replaced by the shared mirror under the same "Devices" heading that panel always had');
ok(/_uniNetRemoveRow\(\)/.test(code),
   '  …with its remove control still after it, in the same order as before');

// ── AND NOTHING THEY ALREADY HAD IS GONE ───────────────────────────────────
// Named one by one, because "retain all the info we already have" is the half
// of Dave's instruction a refactor is most likely to quietly break.
const U = (code.match(/async function renderUnifi\(\)\{[\s\S]*?\n\}/) || [''])[0];
// REGISTER 687 (Dave, 6 Oct 2026): the console IP, local user and password are now
// asked by the PLATFORM'S OWN setup flow, drawn in Pro's flow sheet — so they are
// still asked, by the integration that owns them, with its own errors. Pinned:
// the setup button opens that flow, and the local login is still kept on success.
// 690: …and when the platform has DISCOVERED the console, that discovered flow is the one finished.
const PS = (code.match(/async function unifiProtectSetup\(\)\{[\s\S]*?\n\}/) || [''])[0];
ok(/unifiProtectSetup\(\)/.test(U) && /addFlow\(f\.flow_id\)/.test(PS) && /_beginFlow\('unifiprotect'\)/.test(PS),
   'Protect setup opens the platform\'s own flow (687/690) — its discovered flow first, else its own setup');
// RE-PINNED 6 Oct 2026 (register 695, Dave "yes"): ProOS keeps NO copy of the login — events and clips
// are the platform's own media (692), and the copy was posted to a retired address.
ok(!/\/unifi\/net\/config/.test(code) && !/FLOW_CRED_CAPTURE/.test(code), '  …and ProOS keeps no copy of the login it was given (695)');
for (const [what, needle] of [
  ['the API key field',      'API Key'],
  ['the camera placement',   'unifiSaveCuration()'],
  ['the doorbell picker',    'uc-doorbell'],
  ['the talk-back toggle',   'unifi-cam'],
  ['the remove control',     'unifiRemove()'],
]) ok(U.includes(needle), 'Protect keeps ' + what);
ok(U.indexOf('intgMirrorHTML(') < U.indexOf('uc-doorbell') && U.indexOf('intgMirrorHTML(') > U.indexOf("unifiProtectSetup()"),
   '  …drawn inside step 2, after the connection step and before the doorbell and Save that follow it');

// ── THE ROUTE THAT WAS MISSING ─────────────────────────────────────────────
// This is the whole point on Protect: from that panel there was no way to
// reach a device page, so there was no way to reach a camera feed.
ok(/onclick="openDeviceManage\('\$\{d\.id\}'\)"/.test(M),
   'every device row in the mirror opens the device page — the route to the live feed (register 411)');
ok(/function openDeviceManage\(deviceId\)\{[\s\S]{0,300}_devFrom=cur/.test(code),
   '  …and Back returns to whichever panel it was opened from, so Protect gets its own devices back');

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
