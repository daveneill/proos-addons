const https = require('https');
const http = require('http');
const express = require('express');
const cors = require('cors');
const Anthropic = require('@anthropic-ai/sdk');

const app = express();
const PORT = 3000;

const HA_URL = process.env.HA_URL || 'http://supervisor/core';
const HA_TOKEN = process.env.HA_TOKEN;
const ANTHROPIC_KEY = process.env.ANTHROPIC_API_KEY;
const ALLOWED_ORIGINS = process.env.ALLOWED_ORIGINS || '*';
const LOG_LEVEL = process.env.LOG_LEVEL || 'info';

const log = (level, ...args) => {
  const levels = { debug: 0, info: 1, warning: 2, error: 3 };
  if (levels[level] >= levels[LOG_LEVEL]) console.log(`[${level.toUpperCase()}]`, ...args);
};

app.use(cors({ origin: ALLOWED_ORIGINS }));
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
  }
];

// ── TOOL EXECUTOR ──
async function executeTool(name, input) {
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
- Anything timed, sequenced or triggered — "flash for 10 seconds", "turn off after 15 minutes", "when the front door opens", "every night at 9". You don't have the tools for this, and that's fine. DON'T explain technical reasons. Say it's a custom routine the installer sets up, and offer to note it — e.g. "That's a custom routine your installer can set up for you — want me to make a note of it?" Offer a simple scene instead if it helps.

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

STYLE
- Concise and professional. Assume competence. If a request is ambiguous, ask one direct question.`;

// User mode gets a restricted toolset — control + scenes, but NO automation/registry writes.
// Explicit allow-list (safer than a deny-list: a new tool is locked out by default).
const USER_TOOL_NAMES = ['call_service', 'get_states', 'create_scene', 'process_conversation'];
const USER_TOOLS = HA_TOOLS.filter(t => USER_TOOL_NAMES.includes(t.name));

// ── ASSIST ENDPOINT ──
app.post('/assist', async (req, res) => {
  const { message, conversation_history = [], mode: reqMode } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });
  if (!ANTHROPIC_KEY) return res.status(500).json({ error: 'ANTHROPIC_API_KEY not configured' });
  const mode = (reqMode || DEFAULT_MODE) === 'installer' ? 'installer' : 'user';
  log('info', `Request [${mode}]: "${message}"`);
  try {
    const client = new Anthropic({ apiKey: ANTHROPIC_KEY });
    const homeContext = await buildHomeContext();
    const persona = mode === 'installer' ? SYSTEM_PROMPT_INSTALLER : SYSTEM_PROMPT_USER;
    const tools = mode === 'installer' ? HA_TOOLS : USER_TOOLS;
    const system = `${persona}\n\n${SHARED_OPS}\n\nHOME STATE:\n${homeContext}`;

    const messages = [...conversation_history.slice(-10), { role: 'user', content: message }];
    let response = await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 1024, system, tools, messages });

    while (response.stop_reason === 'tool_use') {
      const toolUses = response.content.filter(b => b.type === 'tool_use');
      const toolResults = [];
      for (const tu of toolUses) {
        try {
          const result = await executeTool(tu.name, tu.input);
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

app.get('/health', (req, res) => res.json({ status: 'ok', version: '1.0.0' }));

app.listen(PORT, '0.0.0.0', () => {
  log('info', `ProOS MCP Server running on port ${PORT}`);
  log('info', `HA URL: ${HA_URL}`);
  log('info', `Anthropic key: ${ANTHROPIC_KEY ? 'configured' : 'MISSING'}`);
});
