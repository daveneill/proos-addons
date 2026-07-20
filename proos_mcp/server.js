const https = require('https');
const http = require('http');
const express = require('express');
const cors = require('cors');
const Anthropic = require('@anthropic-ai/sdk');
const fs = require('fs');

const app = express();
const PORT = 3000;

const HA_URL = process.env.HA_URL || 'http://supervisor/core';
const HA_TOKEN = process.env.HA_TOKEN;
const ANTHROPIC_KEY = process.env.ANTHROPIC_API_KEY;
const ALLOWED_ORIGINS = process.env.ALLOWED_ORIGINS || '*';
const LOG_LEVEL = process.env.LOG_LEVEL || 'info';
const PROOS_CORE_URL = process.env.PROOS_CORE_URL || 'http://b333b432-proos-core:8770';
const REQUIRE_AUTH = process.env.PROOS_MCP_REQUIRE_AUTH === '1';

const log = (level, ...args) => {
  const levels = { debug: 0, info: 1, warning: 2, error: 3 };
  if (levels[level] >= levels[LOG_LEVEL]) console.log(`[${level.toUpperCase()}]`, ...args);
};

app.use(cors({ origin: ALLOWED_ORIGINS, allowedHeaders: ['Content-Type', 'Authorization'] }));
app.use(express.json({ limit: '2mb' }));

// ── HTTP HELPER (no external deps) ──
function haRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${HA_URL}/api${path}`);
    const isHttps = url.protocol === 'https:';
    const options = {
      hostname: url.hostname,
      port: url.port || (isHttps ? 443 : 80),
      path: url.pathname + url.search,
      method,
      headers: {
        'Authorization': `Bearer ${HA_TOKEN}`,
        'Content-Type': 'application/json'
      }
    };
    const payload = body ? JSON.stringify(body) : null;
    if (payload) options.headers['Content-Length'] = Buffer.byteLength(payload);
    const req = (isHttps ? https : http).request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        if (res.statusCode >= 400) return reject(new Error(`HA ${method} ${path} failed: ${res.statusCode} ${data}`));
        try { resolve(JSON.parse(data)); } catch { resolve(data); }
      });
    });
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

const haGet = (path) => haRequest('GET', path, null);
const haPost = (path, body) => haRequest('POST', path, body);
const haPatch = (path, body) => haRequest('PATCH', path, body);

// ── WEBSOCKET HELPER ──
function haWS(message) {
  return new Promise((resolve, reject) => {
    const WS = require('ws');
    const ws = new WS('ws://supervisor/core/api/websocket');
    let msgId = 1;
    ws.on('message', (raw) => {
      const d = JSON.parse(raw);
      if (d.type === 'auth_required') {
        ws.send(JSON.stringify({ type: 'auth', access_token: HA_TOKEN }));
      } else if (d.type === 'auth_ok') {
        ws.send(JSON.stringify({ ...message, id: msgId }));
      } else if (d.id === msgId) {
        ws.close();
        if (d.success === false) reject(new Error(d.error?.message || 'WS error'));
        else resolve(d.result);
      }
    });
    ws.on('error', reject);
    setTimeout(() => { ws.close(); reject(new Error('WS timeout')); }, 10000);
  });
}
// ── CALLER IDENTITY (delegated to ProOS Core /whoami — never trust the request) ──
function bearer(req){
  const h = req.headers['authorization'] || req.headers['Authorization'] || '';
  return h.toLowerCase().startsWith('bearer ') ? h.slice(7).trim() : null;
}
function whoami(token){
  return new Promise((resolve) => {
    if (!token) return resolve({ authenticated:false, tier:null, consent:{} });
    let url;
    try { url = new URL(PROOS_CORE_URL + '/whoami'); } catch(e){ return resolve({ authenticated:false, tier:null, consent:{} }); }
    const isHttps = url.protocol === 'https:';
    const opts = { hostname:url.hostname, port:url.port || (isHttps?443:80), path:url.pathname, method:'GET', headers:{ 'Authorization':'Bearer '+token } };
    const r = (isHttps?https:http).request(opts, (resp) => {
      let d=''; resp.on('data', c=>d+=c);
      resp.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve({ authenticated:false, tier:null, consent:{} }); } });
    });
    r.on('error', () => resolve({ authenticated:false, tier:null, consent:{} }));
    r.setTimeout(5000, () => { r.destroy(); resolve({ authenticated:false, tier:null, consent:{} }); });
    r.end();
  });
}
// Effective Assist level = verified tier, CLAMPED by the home's consent state.
function effectiveLevel(who){
  const tier = who && who.tier;
  const c = (who && who.consent) || {};
  if (tier === 'owner') return 'tech';
  if (tier === 'tech') return c.tech ? 'tech' : (c.installer ? 'installer' : 'user');
  if (tier === 'installer') return c.installer ? 'installer' : 'user';
  return 'user';
}

// ── PROOS CORE CLIENT (awareness/monitoring/self-heal live here, not in HA) ──
// The awareness layer (device health, per-room issues, incident history, self-heal)
// is owned by ProOS Core, so ProAssist reads it from Core rather than re-deriving it
// from raw HA state. The caller's token is forwarded so Core can enforce its own auth
// when a site turns require_auth on. Never throws — returns null on any failure so a
// Core hiccup degrades gracefully instead of breaking the chat.
function coreRequest(method, path, token, body){
  return new Promise((resolve) => {
    let url;
    try { url = new URL(PROOS_CORE_URL + path); } catch(e){ return resolve(null); }
    const isHttps = url.protocol === 'https:';
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const payload = body ? JSON.stringify(body) : null;
    if (payload) headers['Content-Length'] = Buffer.byteLength(payload);
    const opts = { hostname:url.hostname, port:url.port || (isHttps?443:80),
                   path:url.pathname + url.search, method, headers };
    const r = (isHttps?https:http).request(opts, (resp) => {
      let d=''; resp.on('data', c=>d+=c);
      resp.on('end', () => {
        if (resp.statusCode >= 400) return resolve(null);
        try { resolve(JSON.parse(d)); } catch { resolve(null); }
      });
    });
    r.on('error', () => resolve(null));
    r.setTimeout(6000, () => { r.destroy(); resolve(null); });
    if (payload) r.write(payload);
    r.end();
  });
}
const coreGet = (path, token) => coreRequest('GET', path, token, null);
const corePost = (path, token, body) => coreRequest('POST', path, token, body || {});

const HA_TOOLS = [
  {
    name: 'call_service',
    description: 'Call any ProOS service to control devices.',
    input_schema: {
      type: 'object',
      properties: {
        domain: { type: 'string', description: 'e.g. light, switch, climate, media_player, cover, scene' },
        service: { type: 'string', description: 'e.g. turn_on, turn_off, set_temperature' },
        entity_id: { type: 'string', description: 'Target entity ID or array' },
        data: { type: 'object', description: 'Extra service data e.g. brightness, temperature' }
      },
      required: ['domain', 'service']
    }
  },
  {
    name: 'get_states',
    description: 'Get current state of entities.',
    input_schema: {
      type: 'object',
      properties: {
        entity_ids: { type: 'array', items: { type: 'string' }, description: 'Entity IDs to query. Empty = all.' }
      }
    }
  },
  {
    name: 'create_scene',
    description: 'Create and persist a new scene.',
    input_schema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Scene name e.g. "Movie Night"' },
        entities: { type: 'object', description: 'Map of entity_id to state object' }
      },
      required: ['name', 'entities']
    }
  },
  {
    name: 'create_automation',
    description: 'Create a new automation.',
    input_schema: {
      type: 'object',
      properties: {
        alias: { type: 'string' },
        description: { type: 'string' },
        trigger: { type: 'array' },
        condition: { type: 'array' },
        action: { type: 'array' },
        mode: { type: 'string', enum: ['single','restart','queued','parallel'] }
      },
      required: ['alias', 'trigger', 'action']
    }
  },
  {
    name: 'label_entity',
    description: 'Apply labels to an entity in the ProOS registry.',
    input_schema: {
      type: 'object',
      properties: {
        entity_id: { type: 'string' },
        labels: { type: 'array', items: { type: 'string' } }
      },
      required: ['entity_id', 'labels']
    }
  },
  {
    name: 'process_conversation',
    description: 'Send natural language to the ProOS voice engine for simple device control.',
    input_schema: {
      type: 'object',
      properties: {
        text: { type: 'string' }
      },
      required: ['text']
    }
  },
  {
    name: 'flag_for_installer',
    description: "Record a homeowner request that needs the installer (a custom routine, automation, or anything you cannot do yourself). Call this when you offer to make a note for the installer and they accept, or whenever a request clearly needs the installer's attention.",
    input_schema: {
      type: 'object',
      properties: {
        request: { type: 'string', description: 'What the homeowner asked for, in plain language' },
        area: { type: 'string', description: 'Room or area it relates to, if known' }
      },
      required: ['request']
    }
  }
];

// ── AWARENESS TOOLS (ProOS Core's monitoring/self-heal, exposed to the assistant) ──
// READ tools every tier gets — this is what makes ProAssist "the Pro in a box": it can
// see the home's live health, per-room issues, and what ProOS has caught and fixed.
const AWARENESS_TOOLS = [
  {
    name: 'get_home_health',
    description: "Get ProOS's live awareness: the overall status and every monitored device's health (healthy, in standby, or faulted), plus what ProOS is doing about any fault. Use this to answer 'is everything ok?', 'is anything wrong?', or BEFORE acting when someone reports a problem — it's the authoritative health picture, not raw device state.",
    input_schema: { type: 'object', properties: {} }
  },
  {
    name: 'get_room_health',
    description: "Get the health of ONE room: whether it's clear or has an issue, and the suggested next action. Use when someone asks about a specific room (e.g. 'is the living room ok?').",
    input_schema: { type: 'object', properties: { area: { type: 'string', description: 'Room / area name (e.g. "Living Room")' } }, required: ['area'] }
  },
  {
    name: 'get_awareness_history',
    description: "Recent awareness events — faults ProOS caught, what it did, and whether it auto-recovered. Use for 'what happened', 'has the TV been dropping out?', or to tell the handled story of an incident.",
    input_schema: { type: 'object', properties: { device: { type: 'string', description: 'Optional: only events for this device name' } } }
  }
];
// RECOVER — consent-gated (installer/tech via effectiveLevel; excluded from the user set).
const RECOVER_TOOL = {
  name: 'recover_device',
  description: "Recover a wedged device by reloading its ProOS integration connection — the SAME self-heal ProOS runs automatically. Use when awareness shows a device is faulted/wedged (answering the network but lost to ProOS) and you want to restore it now. Non-destructive; nothing on the device is touched.",
  input_schema: { type: 'object', properties: { entity_id: { type: 'string', description: 'The device entity to recover (from get_home_health)' } }, required: ['entity_id'] }
};

// ── INSTALLER REQUEST QUEUE (persisted in /data) ──
const REQUESTS_FILE = '/data/requests.json';
function loadRequests() {
  try { return JSON.parse(fs.readFileSync(REQUESTS_FILE, 'utf8')); }
  catch (e) { return []; }
}
function saveRequests(list) {
  try { fs.writeFileSync(REQUESTS_FILE, JSON.stringify(list, null, 2)); }
  catch (e) { log('error', 'saveRequests failed:', e.message); }
}

// ── TOOL EXECUTOR ──
async function executeTool(name, input, ctx = {}) {
  log('info', `Tool: ${name}`, JSON.stringify(input).substring(0, 200));
  switch (name) {
    case 'call_service': {
      const body = { ...(input.data || {}) };
      if (input.entity_id) body.entity_id = input.entity_id;
      await haPost(`/services/${input.domain}/${input.service}`, body);
      return { success: true, message: `${input.domain}.${input.service} executed` };
    }
    case 'get_states': {
      if (input.entity_ids?.length) {
        const results = await Promise.all(input.entity_ids.map(id =>
          haGet(`/states/${id}`).catch(e => ({ entity_id: id, error: e.message }))
        ));
        return results.map(s => ({
          entity_id: s.entity_id, state: s.state,
          friendly_name: s.attributes?.friendly_name,
          brightness: s.attributes?.brightness,
          temperature: s.attributes?.temperature,
          current_temperature: s.attributes?.current_temperature
        }));
      }
      const all = await haGet('/states');
      return all.map(s => ({ entity_id: s.entity_id, state: s.state, friendly_name: s.attributes?.friendly_name }));
    }
    case 'create_scene': {
      const sceneId = input.name.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
      await haPost(`/config/scene/config/${sceneId}`, { name: input.name, entities: input.entities });
      await new Promise(r => setTimeout(r, 2000));
      const entityId = `scene.${sceneId}`;
      try {
        // Use WebSocket to apply label — confirmed working
        await haWS({ type: 'config/entity_registry/update', entity_id: entityId, labels: ['dashboard_scene'] });
        log('info', `Label dashboard_scene applied to ${entityId}`);
      } catch(e) {
        log('warning', 'Label failed:', e.message);
      }
      return { success: true, entity_id: entityId, message: `Scene "${input.name}" created` };
    }
    case 'create_automation': {
      const autoId = `proos_${Date.now()}`;
      await haPost(`/config/automation/config/${autoId}`, {
        id: autoId, alias: input.alias, description: input.description || '',
        trigger: input.trigger, condition: input.condition || [],
        action: input.action, mode: input.mode || 'single'
      });
      return { success: true, message: `Automation "${input.alias}" created` };
    }
    case 'label_entity': {
      try {
        await haPost(`/config/entity_registry/${input.entity_id}`, { labels: input.labels });
      } catch(e) {
        await haPatch(`/config/entity_registry/${input.entity_id}`, { labels: input.labels });
      }
      return { success: true, message: `Labels applied to ${input.entity_id}` };
    }
    case 'process_conversation': {
      const result = await haPost('/services/conversation/process?return_response=true', { text: input.text, language: 'en' });
      const speech = result?.service_response?.response?.speech?.plain?.speech
        || result?.response?.speech?.plain?.speech
        || result?.[0]?.response?.speech?.plain?.speech
        || 'Done.';
      const responseType = result?.service_response?.response?.response_type || result?.response?.response_type;
      if (responseType === 'error') {
        // HA Assist couldn't handle it — fall back to call_service
        log('warning', 'HA Assist failed, response type error:', speech);
        return { success: false, response: speech, fallback: true };
      }
      return { success: true, response: speech };
    }
    case 'flag_for_installer': {
      const list = loadRequests();
      list.push({ id: `req_${Date.now()}`, request: input.request, area: input.area || '', ts: new Date().toISOString(), resolved: false });
      saveRequests(list);
      log('info', `Installer request logged: "${input.request}"`);
      return { success: true, message: 'Recorded for the installer' };
    }
    case 'read_error_log': {
      const items = await haWS({ type: 'system_log/list' });
      return (items || []).slice(0, 40).map(it => ({
        level: it.level,
        source: Array.isArray(it.source) ? it.source[0] : it.source,
        message: Array.isArray(it.message) ? it.message.join(' ') : it.message,
        count: it.count
      }));
    }
    case 'list_integrations': {
      const entries = await haWS({ type: 'config_entries/get' });
      return (entries || []).map(e => ({ entry_id:e.entry_id, title:e.title, domain:e.domain, state:e.state }));
    }
    case 'reload_integration': {
      await haPost(`/config/config_entries/entry/${input.entry_id}/reload`, {});
      return { success: true, message: `Integration ${input.entry_id} reloaded` };
    }
    // ── AWARENESS (read from ProOS Core) ──
    case 'get_home_health': {
      const r = await coreGet('/watchers', ctx.token);
      if (!r) return { available: false, message: 'Awareness is not reachable right now.' };
      const items = Array.isArray(r.items) ? r.items : [];
      const attention = items
        .filter(i => i.status && i.status !== 'ok' && i.status !== 'standby')
        .map(i => ({ name: i.name, area: i.area || 'Unassigned', kind: i.kind,
                     status: i.status, issue: i.verdict || i.state || '', since: i.since }));
      return { status: r.status, summary: r.summary, watched: items.length,
               needs_attention: attention, all_clear: attention.length === 0 };
    }
    case 'get_room_health': {
      const area = (input.area || '').trim();
      if (!area) return { available: false, message: 'area required' };
      const r = await coreGet('/rooms/' + encodeURIComponent(area) + '/health', ctx.token);
      if (!r) return { available: false, message: `Couldn't read health for ${area}.` };
      return r;
    }
    case 'get_awareness_history': {
      const r = await coreGet('/watchers/history', ctx.token);
      let events = (r && r.events) || [];
      if (input.device) {
        const q = String(input.device).toLowerCase();
        events = events.filter(e => String(e.entity || '').toLowerCase().includes(q));
      }
      return { events: events.slice(0, 40) };
    }
    // ── SELF-HEAL (installer/tech only; the tool isn't in the user toolset) ──
    case 'recover_device': {
      const eid = (input.entity_id || '').trim();
      if (!eid) return { success: false, message: 'entity_id required' };
      let entry = '';
      try { entry = String(await haPost('/template', { template: `{{ config_entry_id('${eid}') }}` }) || '').trim(); } catch (e) {}
      if (!entry || entry === 'None') return { success: false, message: `Couldn't find the integration behind ${eid}.` };
      await haPost(`/config/config_entries/entry/${entry}/reload`, {});
      return { success: true, message: `Reloaded the connection for ${eid} — give it a moment to come back.` };
    }
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── HOME CONTEXT ──
async function buildHomeContext() {
  try {
    const states = await haGet('/states');
    const lights = states.filter(s => s.entity_id.startsWith('light.')).map(s =>
      `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state}${s.attributes.brightness ? ` ${Math.round(s.attributes.brightness/2.55)}%` : ''}`
    ).join('\n');
    const climate = states.filter(s => s.entity_id.startsWith('climate.')).map(s =>
      `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state} target:${s.attributes.temperature}° now:${s.attributes.current_temperature}°`
    ).join('\n');
    const covers = states.filter(s => s.entity_id.startsWith('cover.')).map(s =>
      `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state}`
    ).join('\n');
    const media = states.filter(s => s.entity_id.startsWith('media_player.')).map(s =>
      `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state}${s.attributes.app_name ? ` (${s.attributes.app_name} app_id:${s.attributes.app_id})` : ''}${s.attributes.source ? ` source:${s.attributes.source}` : ''}${s.attributes.source_list ? ` available_sources:${s.attributes.source_list.join(',')}` : ''}`
    ).join('\n');
    const scenes = states.filter(s => s.entity_id.startsWith('scene.')).map(s =>
      `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]`
    ).join(', ');
    const alarm = states.find(s => s.entity_id.startsWith('alarm_control_panel.'));
    const sensors = states.filter(s => s.entity_id.startsWith('sensor.services_elkm1')).map(s =>
      `${s.attributes.friendly_name||s.entity_id}: ${s.state}`
    ).join('\n');
    return [
      `LIGHTS:\n${lights||'none'}`,
      `CLIMATE:\n${climate||'none'}`,
      `COVERS:\n${covers||'none'}`,
      `MEDIA PLAYING:\n${media||'nothing'}`,
      `SCENES: ${scenes||'none'}`,
      `ALARM: ${alarm ? `${alarm.attributes.friendly_name}: ${alarm.state}` : 'none'}`,
      `SENSORS:\n${sensors||'none'}`
    ].join('\n\n');
  } catch (e) {
    log('error', 'Context error:', e.message);
    return 'Could not load home state.';
  }
}

// ── AWARENESS SUMMARY (compact live health, injected into context each turn) ──
// Straight from ProOS Core's awareness so ProAssist is proactively aware — it can answer
// "is everything ok?" and mention a fault without a tool round-trip. Silent if Core is down.
async function buildAwarenessSummary(token) {
  try {
    const r = await coreGet('/watchers', token);
    if (!r) return 'AWARENESS: (unavailable)';
    const items = Array.isArray(r.items) ? r.items : [];
    const bad = items
      .filter(i => i.status && i.status !== 'ok' && i.status !== 'standby')
      .map(i => `- ${i.name}${i.area ? ` (${i.area})` : ''}: ${i.verdict || i.status}`);
    const head = `AWARENESS: ${r.summary || r.status || 'unknown'} — ${items.length} device(s) watched`;
    return bad.length ? `${head}\nNEEDS ATTENTION:\n${bad.join('\n')}` : `${head}\nAll watched devices healthy.`;
  } catch (e) { return 'AWARENESS: (unavailable)'; }
}

// ── ASSIST PERSONAS (two levels) ──
// Default persona when the dashboard sends no `mode`. Final state is 'user'.
// TEMP: set to 'installer' while developing if you want full powers in the app
// before the dashboard ships the unlock gesture; flip back to 'user' once it does.
const DEFAULT_MODE = process.env.DEFAULT_ASSIST_MODE || 'user';

// Tool-use mechanics that apply to BOTH personas (how to act, not how to talk).
const SHARED_OPS = `HOW TO ACT
- ACT FIRST using your tools, THEN reply. Do the thing, then confirm it.
- Control devices with call_service. "Turn the lights on/off" = call_service on domain:light — NEVER a scene for plain on/off.
- "Turn on all the lights" = ONE call_service with light.turn_on and all the light entity IDs together as an array.
- Only use create_scene when the user explicitly asks to "create a scene" or "save a scene".
- To switch a media player's app/input (e.g. Apple TV apps), use call_service media_player.select_source with the app/source name.
- Never put JSON or code in your reply text.`;

// USER — homeowner. ProOS-only language, control + simple scenes, no automations.
const SYSTEM_PROMPT_USER = `You are the ProOS assistant — a calm, friendly helper for someone's smart home. You help them control their home and set up simple scenes, in plain everyday language.

WHO YOU ARE TALKING TO
- A homeowner. They are NOT technical and don't know how their home is wired.
- The system is "ProOS". Call it "ProOS" or "your home".

NEVER SAY (this breaks the experience)
- The words for the underlying platform and its parts: never write the name of any other smart-home platform, hub, or operating system, nor "entity", "entity ID", "YAML", "integration", "automation", "script", "trigger", "domain". To you, this system has only ever been called ProOS.
- If someone names another platform or asks "is this X?" / "can you control X?", do not validate the premise. Do not answer "Yes" (a yes agrees the system IS the thing they named) and do not repeat, confirm, or deny the name. Reframe to controlling their home as ProOS. Example — user: "Can you control Home Assistant?" → "I run your home — it's all ProOS, and I can control everything in it." Never phrase it as a substitution ("ProOS instead of …", "it's called ProOS here", "here it's ProOS"), which gives the other name away. As far as you know, the other name does not exist.
- Never show or ask for an entity ID. If you need to know which device, ask by room and name — e.g. "Which room's TV?" — and find it yourself from the home state below.

WHAT YOU CAN DO
- Control lights, climate, blinds, media, and so on.
- Turn on scenes that already exist, and create SIMPLE scenes (a saved snapshot of how some devices should be — e.g. "Movie": lounge dim, blinds closed).

WHAT YOU CAN'T DO (handle it gracefully)
- Anything timed, sequenced or triggered — "flash for 10 seconds", "turn off after 15 minutes", "when the front door opens", "every night at 9". You don't have the tools for this, and that's fine. DON'T explain technical reasons. Say it's a custom routine the installer sets up, and offer to note it — e.g. "That's a custom routine your installer can set up for you — want me to make a note of it?" If they accept (or clearly want it passed to the installer), CALL flag_for_installer to record it, then confirm plainly — e.g. "Done — I've passed that to your installer." Never say you have noted something without actually calling flag_for_installer. Offer a simple scene instead if it helps.

KNOWING IF THINGS ARE OK (you watch the home continuously)
- The AWARENESS block above is your live read on the home's health. If asked "is everything ok?", "is anything wrong?", or about a specific device or room, answer from it — use get_home_health (or get_room_health for one room) to double-check or get detail.
- If all is well, reassure simply: "Everything's running fine." If something's down, say which in plain terms and stay calm — e.g. "Your front door camera dropped off a few minutes ago — I'm keeping an eye on it." Never mention integrations, connections, or anything technical.
- You can't fix hardware faults yourself at this level. If it clearly needs hands-on attention, offer to flag it for the installer (flag_for_installer).

STYLE
- Warm, brief, natural — one or two short sentences. Confirm in human terms: "Done — the office light's on." / "Saved your Movie scene."
- No markdown, no lists, no entity IDs. Never tell them to open settings or a menu.
- Ask at most one short question at a time.`;

// INSTALLER — AV professional. Technical and full-powered, but still ProOS-only branding.
const SYSTEM_PROMPT_INSTALLER = `You are the ProOS installer assistant — a technical co-pilot for the AV professional who installs and configures this system, and the guide that walks them through setting up a home.

WHO YOU ARE TALKING TO
- A competent installer. Be precise and technical. You may work with entity IDs, devices, areas, scenes, automations, triggers and actions directly.

BRANDING — NON-NEGOTIABLE
- This is ProOS. NEVER name the underlying platform or ecosystem: do not write "Home Assistant", "HA", "Supervisor", "Lovelace", "Nabu Casa", "HACS" or "add-on". Everything is ProOS — the ProOS system, ProOS devices, the ProOS automation engine. Generic technical terms (entity ID, trigger, action, automation, scene, area) are fine.
- If asked directly whether this is Home Assistant (or any other platform), do not validate the premise — don't answer "yes", and don't repeat, confirm, or deny the name. Reframe to ProOS (e.g. "This is ProOS — and yes, I can configure all of it"). As far as you are concerned, no other name exists.

WHAT YOU CAN DO
- Everything the user can, plus create and edit automations, build scenes, and configure devices. When you build something, state what it does and the entities involved so it can be verified. Verify, don't assume — show your work.

SETUP / WIZARD
- You also help configure a home from scratch by interviewing the installer: ask clear, one-at-a-time questions to gather rooms, devices, which are displays vs sources vs audio, groupings, and the activities/scenes they want. Confirm each step before moving on.

AWARENESS & SELF-HEAL
- You have ProOS's live monitoring: get_home_health (whole-home health + what's faulted), get_room_health (one room's issue + suggested action), get_awareness_history (faults caught, and whether ProOS auto-recovered). Lead diagnosis from these, not raw device state.
- When a device is wedged — reachable on the network but lost to ProOS — restore it with recover_device (the same reload ProOS self-heals with; non-destructive). Diagnose first, then recover, then report what you found and what you did.

STYLE
- Concise and professional. Assume competence. If a request is ambiguous, ask one direct question.`;

// TECH — Protech vendor support: diagnostics plus everything the installer can do.
const SYSTEM_PROMPT_TECH = `You are ProOS Tech Support — the vendor-level engineer's assistant for diagnosing and repairing a ProOS home. You can do everything the installer assistant can, plus diagnostics: reading the system log, listing and reloading integrations, and inspecting why something is failing.

WHO YOU ARE TALKING TO
- A Protech support engineer. Be precise, technical, and diagnostic. Lead with root cause.

BRANDING
- Customer-facing language stays ProOS, but with this audience you may reference underlying mechanics (log lines, integration states) when diagnosing.

WHAT YOU CAN DO
- Everything the installer can, plus: read the error log, list integrations and their state, and reload a wedged integration. Diagnose first, change second — say what you found before you act.

AWARENESS & SELF-HEAL
- get_home_health / get_room_health / get_awareness_history give you ProOS's live status and the fault/recovery record; recover_device reloads a wedged integration on demand (the same path as automatic self-heal). Root-cause first: cite the awareness verdict or the history event that shows WHY, then recover. Cross-check against read_error_log / list_integrations when the cause isn't obvious.

STYLE
- Terse, root-cause first. Show the evidence (the log line, the integration state) that led to your conclusion.`;

// Tech toolset = installer tools + read-only diagnostics + integration reload.
const TECH_TOOLS_EXTRA = [
  { name:'read_error_log', description:'Read the recent ProOS system error log for diagnosis.', input_schema:{ type:'object', properties:{} } },
  { name:'list_integrations', description:'List integrations (config entries) and their current state.', input_schema:{ type:'object', properties:{} } },
  { name:'reload_integration', description:'Reload one integration by its entry id (fixes a wedged integration without a restart).', input_schema:{ type:'object', properties:{ entry_id:{ type:'string' } }, required:['entry_id'] } }
];
// Per-tier toolsets. Awareness READ (get_home_health / room / history) reaches EVERY tier —
// knowing the home's health is core to ProAssist. recover_device (self-heal) is installer/tech
// only, so it lives in the installer + tech sets but is never in the user set.
const INSTALLER_TOOLS = [...HA_TOOLS, ...AWARENESS_TOOLS, RECOVER_TOOL];
const HA_TOOLS_TECH = [...INSTALLER_TOOLS, ...TECH_TOOLS_EXTRA];

// User mode gets a restricted toolset — control + scenes + awareness READ, but NO automation/
// registry writes and NO self-heal. Explicit allow-list (a new tool is locked out by default).
const USER_TOOL_NAMES = ['call_service', 'get_states', 'create_scene', 'process_conversation', 'flag_for_installer'];
const USER_TOOLS = [...HA_TOOLS.filter(t => USER_TOOL_NAMES.includes(t.name)), ...AWARENESS_TOOLS];

// Trim history to the last few turns WITHOUT orphaning a tool_result from its
// tool_use — the API rejects a tool_result whose matching tool_use was sliced off.
function sanitizeHistory(history){
  let h = Array.isArray(history) ? history.slice(-10) : [];
  while (h.length) {
    const m = h[0];
    const hasToolResult = Array.isArray(m.content) && m.content.some(b => b && b.type === 'tool_result');
    if (m.role === 'user' && !hasToolResult) break;
    h.shift();
  }
  return h;
}

// ── ASSIST ENDPOINT ──
app.post('/assist', async (req, res) => {
  const { message, conversation_history = [] } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });
  if (!ANTHROPIC_KEY) return res.status(500).json({ error: 'ANTHROPIC_API_KEY not configured' });
  // Identity is VERIFIED against ProOS Core, never taken from the request body.
  const token = bearer(req);
  const who = await whoami(token);
  if (REQUIRE_AUTH && !who.authenticated) return res.status(401).json({ error: 'authentication required' });
  const level = effectiveLevel(who);
  log('info', `Request [${level}] caller=${who.name || 'anon'}: "${message}"`);
  try {
    const client = new Anthropic({ apiKey: ANTHROPIC_KEY });
    const [homeContext, awareness] = await Promise.all([buildHomeContext(), buildAwarenessSummary(token)]);
    const LEVELS = {
      user:      { persona: SYSTEM_PROMPT_USER,      tools: USER_TOOLS },
      installer: { persona: SYSTEM_PROMPT_INSTALLER, tools: INSTALLER_TOOLS },
      tech:      { persona: SYSTEM_PROMPT_TECH,      tools: HA_TOOLS_TECH }
    };
    const sel = LEVELS[level] || LEVELS.user;
    const persona = sel.persona;
    const tools = sel.tools;
    const system = `${persona}\n\n${SHARED_OPS}\n\n${awareness}\n\nHOME STATE:\n${homeContext}`;

    const messages = [...sanitizeHistory(conversation_history), { role: 'user', content: message }];
    let response = await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 1024, system, tools, messages });

    while (response.stop_reason === 'tool_use') {
      const toolUses = response.content.filter(b => b.type === 'tool_use');
      const toolResults = [];
      for (const tu of toolUses) {
        try {
          const result = await executeTool(tu.name, tu.input, { token, level });
          toolResults.push({ type: 'tool_result', tool_use_id: tu.id, content: JSON.stringify(result) });
        } catch (e) {
          log('error', `Tool ${tu.name}:`, e.message);
          toolResults.push({ type: 'tool_result', tool_use_id: tu.id, content: `Error: ${e.message}`, is_error: true });
        }
      }
      messages.push({ role: 'assistant', content: response.content });
      messages.push({ role: 'user', content: toolResults });
      response = await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 512, system, tools, messages });
    }

    const text = response.content.filter(b => b.type === 'text').map(b => b.text).join('').trim()
      .replace(/\*\*(.*?)\*\*/g, '$1').replace(/\*(.*?)\*/g, '$1').replace(/`(.*?)`/g, '$1');
    log('info', `Response: "${text}"`);
    res.json({ response: text, conversation_history: messages.slice(-20) });
  } catch (e) {
    log('error', 'Error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

// ── INSTALLER REQUESTS ENDPOINTS ──
app.get('/requests', (req, res) => {
  const all = loadRequests();
  const open = all.filter(r => !r.resolved);
  res.json({ requests: open, open_count: open.length, total: all.length });
});
app.post('/requests/resolve', (req, res) => {
  const { id } = req.body || {};
  if (!id) return res.status(400).json({ error: 'id required' });
  const all = loadRequests();
  const item = all.find(r => r.id === id);
  if (item) { item.resolved = true; saveRequests(all); }
  res.json({ success: true });
});

app.get('/health', (req, res) => res.json({ status: 'ok', version: '2.1.0' }));

app.listen(PORT, '0.0.0.0', () => {
  log('info', `ProOS MCP Server running on port ${PORT}`);
  log('info', `HA URL: ${HA_URL}`);
  log('info', `Anthropic key: ${ANTHROPIC_KEY ? 'configured' : 'MISSING'}`);
});
