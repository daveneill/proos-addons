// NETWORK → DEVICES, CLIENTS, WI-FI, THE PAGE — register 1225. Run: node tests/the_network_sections_page_bench.js
//
// DRIVEN, real Chromium: Pro's REAL _uniNetHTML over Core's /unifi/console/network answer.
//   1. Network's sections are Devices · Ports · Clients · Wi-Fi · Integration, Devices first (as the mockup)
//   2. Devices lists every device with its kind, address, firmware or "Update", clients; a switch offers its ports
//   3. Clients: All / Wired / Wi-Fi count from the answer; a wired client shows its switch and port and links to it,
//      a Wi-Fi client its network and signal; the search narrows the list
//   4. Wi-Fi shows each network's security, bands and how many are on it
//   5. an unreadable network says why and where the sign-in lives; the page only reads (SOURCE)
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '..', 'deploy', 'pro.html'), 'utf8');
let pass = 0, fail = 0;
function ok(c, m) { if (c) { pass++; console.log('  ok  ' + m); } else { fail++; console.log('  FAIL ' + m); } }
let chromium = null; try { ({ chromium } = require('playwright')); } catch (e) { chromium = null; }
function lift(name) {
  const i = html.indexOf('function ' + name + '(');
  if (i < 0) return null;
  let d = 0, j = html.indexOf('{', i);
  for (; j < html.length; j++) { if (html[j] === '{') d++; else if (html[j] === '}') { d--; if (!d) break; } }
  return (html.slice(Math.max(0, i - 6), i) === 'async ' ? 'async ' : '') + html.slice(i, j + 1);
}
const fns = ['esc', '_upTime', '_uniNetHTML'].map(lift);
const NET = { ok: true, internet: { status: 'ok', isp: 'Aussie Broadband' },
  devices: [{ mac: 'g1', name: 'Home', kind: 'Gateway', model: 'UDMPROMAX', online: true, ip: '192.168.8.1', version: '6.0.17', clients: 89, uptime_s: 8640, ports: 11 },
            { mac: 's1', name: 'AV-Rack - USW-48', kind: 'Switch', model: 'USL48P', online: true, ip: '192.168.8.2', version: '7.5.15', upgradable: true, upgrade_to: '7.6.1', clients: 25, uptime_s: 1572480, ports: 52, uplink: 'Home', uplink_port: 11 },
            { mac: 'a1', name: 'Hallway', kind: 'Access Point', model: 'UALR6v2', online: false, ip: '192.168.8.3', version: '6.7.57', clients: 0, ports: null }],
  clients: [{ mac: 'c1', name: 'UC-Display-SE-21', ip: '192.168.8.30', wired: true, switch: 'AV-Rack - USW-48', switch_mac: 's1', port: 9, network: 'Default', uptime_s: 3600 },
            { mac: 'c2', name: 'Alfresco Shelly', ip: '192.168.8.81', wired: false, wifi: 'Home IoT', ap: 'Hallway', signal: -54, network: 'Default', uptime_s: 60 }],
  you_are_on: 'Home',
  wifi: [{ id: 'w1', name: 'Home IoT', enabled: true, security: 'WPA Personal', wpa_mode: 'wpa2', bands: ['2.4 GHz'], clients: 1, control: { entity_id: 'switch.home_iot', enabled: true } },
         { id: 'w2', name: 'Home', enabled: true, security: 'WPA Personal', bands: ['2.4 GHz', '5 GHz'], clients: 0, control: { entity_id: 'switch.home', enabled: true } },
         { id: 'w3', name: 'Guests', enabled: false, guest: true, security: 'Open', bands: ['5 GHz'], clients: 0, control: { entity_id: 'switch.guests', enabled: true } },
         { id: 'w4', name: 'Lab', enabled: true, security: 'WPA Personal', bands: ['5 GHz'], clients: 0, control: { entity_id: 'switch.lab', enabled: false } }] };
(async () => {
  const seg = (lift('renderUniNet') || '');
  // Pin rewritten, not weakened (register 1229): the tab row became the mockup's left-side list, drawn from
  // UNI_NET_SECTIONS. The rule is unchanged — the built sections, in order, Devices first — and now also: every
  // mockup section is listed, and one not built yet cannot be opened.
  const m = /const UNI_NET_SECTIONS=(\[[\s\S]*?\]\]);/.exec(html);
  const S = m ? Function('return ' + m[1])() : [];
  const built = S.filter(x => x[2]).map(x => x[1]).join(' · '), soon = S.filter(x => !x[2]).map(x => x[1]).join(' · ');
  ok(built === 'Devices · Ports · Clients · Wi-Fi · Integration' && soon === 'Networks · Internet · Guests · Firewall & Rules · Power · Events'
     && /_uniSide\(UNI_NET_SECTIONS,_uniNetSection,'uniNetSection'\)/.test(seg) && /let _uniNetSection='devices'/.test(html)
     && /:`<button disabled title="Not built yet">/.test(lift('_uniSide') || ''),
     '1 · sections Devices · Ports · Clients · Wi-Fi · Integration, Devices first; the mockup\'s other sections listed as Coming and not openable');
  if (!chromium || !fns.every(Boolean)) { fail++; console.log('  FAIL NOT RUN — needs Chromium and the page functions.'); }
  else {
    const b = await chromium.launch(); const pg = await b.newPage();
    await pg.setContent('<!doctype html><body><div id="o"></div></body>');
    const r = await pg.evaluate(([src, net]) => {
      (0, eval)(src + '\nwindow.__go=1;');
      const o = document.getElementById('o'), out = {};
      const draw = () => { o.innerHTML = _uniNetHTML(); return o; };
      _uniNet = net; _uniNetSection = 'devices'; draw(); out.dev = o.innerText;
      _uniNetSel = 's1'; draw(); out.devPanel = o.querySelector('.unp-panel').innerHTML;
      _uniNetSection = 'clients'; _uniNetSel = null; draw(); out.chips = [...o.querySelectorAll('.unp-chip')].map(x => x.textContent.trim()); out.cli = o.innerText;
      _uniNetSel = 'c1'; draw(); out.cliPanel = o.querySelector('.unp-panel').innerHTML;
      _uniCliQ = 'shelly'; _uniNetSel = null; draw(); out.searched = o.querySelectorAll('.unp-row').length; _uniCliQ = '';
      _uniNetSection = 'wifi'; draw(); out.wifi = o.innerText;
      out.wifiBtns = [...o.querySelectorAll('.und-wifi')].map(c => c.querySelector('b').textContent + ':' + ([...c.querySelectorAll('.unp-acts button')].map(x => x.textContent).join('|') || '-'));
      _uniNet = { ok: false, why: 'the console answered 401 for /proxy/network/api/s/default/stat/device' }; draw(); out.bad = o.innerText;
      return out;
    }, ['var _uniNet=null,_uniNetSel=null,_uniCliFilter="all",_uniCliQ="",_uniNetSection="devices";\n' + fns.join('\n'), NET]);
    ok(/Home/.test(r.dev) && /Gateway · UDMPROMAX/.test(r.dev) && /Update/.test(r.dev) && /Aussie Broadband/.test(r.dev)
       && /7\.6\.1 waiting/.test(r.devPanel) && /See Its 52 Ports/.test(r.devPanel) && /Home · port 11/.test(r.devPanel),
       '2 · devices with kind, firmware or Update; the switch panel shows its update, uplink and links to its 52 ports');
    ok(r.chips.includes('All 2') && r.chips.includes('Wired 1') && r.chips.includes('Wi-Fi 1') && /AV-Rack - USW-48 · port 9/.test(r.cli)
       && /Home IoT · -54 dBm/.test(r.cli) && /uniGoPort\('s1',9\)/.test(r.cliPanel) && r.searched === 1,
       '3 · clients: counts, wired by switch and port (linked), Wi-Fi by network and signal, search narrows');
    ok(/Home IoT/.test(r.wifi) && /WPA Personal \(WPA2\)/.test(r.wifi) && /2\.4 GHz · 5 GHz/.test(r.wifi) && /1 client\b/.test(r.wifi) && /0 clients/.test(r.wifi),
       '4 · Wi-Fi: security, bands and how many are on each');
    ok(JSON.stringify(r.wifiBtns) === JSON.stringify(['Home IoT:Turn Network Off', 'Home:Turn Network Off', 'Guests:Turn Guest Network On', 'Lab:-'])
       && /This screen is on this network/.test(r.wifi) && /The platform's switch for this network is switched off/.test(r.wifi),
       '6 · register 1227: on/off where the platform\'s switch is on (guest networks named so), none where it is off; this screen\'s network marked');
    ok(/Couldn't read the network/.test(r.bad) && /answered 401/.test(r.bad) && /Console Settings/.test(r.bad), '5a · an unreadable network says why and where the sign-in lives');
    await b.close();
  }
  const code = ['_uniNetLoad', '_uniNetHTML', 'uniNetPick', 'uniCliFilter', 'uniCliSearch'].map(lift).join('\n');
  ok(code && !/method:\s*'(POST|PATCH|PUT|DELETE)'/.test(code), '5b · the reading sections only read');
  const ws = lift('uniWifiSet') || '';
  ok(/if\(!await proAsk\(q\)\) return;/.test(ws) && /\/unifi\/console\/network\/wifi'/.test(ws) && (ws.match(/fetch\(/g) || []).length === 1
     && /will drop off/.test(ws) && /This screen is on/.test(ws),
     '7 · Wi-Fi on/off asks first (devices that drop off, this screen\'s own network) and goes only to Core\'s Wi-Fi route');
  console.log('\n' + pass + ' checks passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
