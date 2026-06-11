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

// ── HA API HELPERS ──
async function haGet(path) {
  const r = await fetch(`${HA_URL}/api${path}`, {
    headers: { 'Authorization': `Bearer ${HA_TOKEN}`, 'Content-Type': 'application/json' }
  });
  if (!r.ok) throw new Error(`HA GET ${path} failed: ${r.status} ${await r.text()}`);
  return r.json();
}

async function haPost(path, body) {
  const r = await fetch(`${HA_URL}/api${path}`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${HA_TOKEN}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!r.ok) throw new Error(`HA POST ${path} failed: ${r.status} ${await r.text()}`);
  return r.json();
}

async function haWS(message) {
  return new Promise((resolve, reject) => {
    const WS = require('ws');
    const ws = new WS(`ws://supervisor/core/api/websocket`);
    let msgId = 1;
    ws.on('open', () => log('debug', 'WS open'));
    ws.on('message', (raw) => {
      const d = JSON.parse(raw);
      if (d.type === 'auth_required') {
        ws.send(JSON.stringify({ type: 'auth', access_token: HA_TOKEN }));
      } else if (d.type === 'auth_ok') {
        const msg = { ...message, id: msgId++ };
        ws.send(JSON.stringify(msg));
      } else if (d.id === msgId - 1) {
        ws.close();
        if (d.success === false) reject(new Error(d.error?.message || 'WS error'));
        else resolve(d.result);
      }
    });
    ws.on('error', reject);
    setTimeout(() => { ws.close(); reject(new Error('WS timeout')); }, 10000);
  });
}

// ── HA TOOLS DEFINITIONS ──
const HA_TOOLS = [
  {
    name: 'call_service',
    description: 'Call any Home Assistant service to control devices.',
    input_schema: {
      type: 'object',
      properties: {
        domain: { type: 'string', description: 'Service domain e.g. light, switch, climate, media_player, cover, scene, alarm_control_panel' },
        service: { type: 'string', description: 'Service name e.g. turn_on, turn_off, set_temperature' },
        entity_id: { type: 'string', description: 'Target entity ID or comma-separated list' },
        data: { type: 'object', description: 'Additional service data e.g. brightness, temperature, hvac_mode' }
      },
      required: ['domain', 'service']
    }
  },
  {
    name: 'get_states',
    description: 'Get the current state of one or more entities.',
    input_schema: {
      type: 'object',
      properties: {
        entity_ids: { type: 'array', items: { type: 'string' }, description: 'List of entity IDs to query. If empty, returns all states.' }
      }
    }
  },
  {
    name: 'get_areas',
    description: 'Get all areas and their entities.',
    input_schema: { type: 'object', properties: {} }
  },
  {
    name: 'create_scene',
    description: 'Create a new scene in Home Assistant that persists across restarts.',
    input_schema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Human readable scene name e.g. "Movie Night"' },
        entities: { type: 'object', description: 'Map of entity_id to state e.g. {"light.kitchen": {"state": "on", "brightness": 128}}' }
      },
      required: ['name', 'entities']
    }
  },
  {
    name: 'create_automation',
    description: 'Create a new automation in Home Assistant.',
    input_schema: {
      type: 'object',
      properties: {
        alias: { type: 'string', description: 'Automation name' },
        description: { type: 'string', description: 'What this automation does' },
        trigger: { type: 'array', description: 'Trigger conditions' },
        condition: { type: 'array', description: 'Optional conditions' },
        action: { type: 'array', description: 'Actions to perform' },
        mode: { type: 'string', enum: ['single', 'restart', 'queued', 'parallel'], description: 'Automation mode' }
      },
      required: ['alias', 'trigger', 'action']
    }
  },
  {
    name: 'label_entity',
    description: 'Apply a label to an entity in Home Assistant for dashboard categorisation.',
    input_schema: {
      type: 'object',
      properties: {
        entity_id: { type: 'string' },
        labels: { type: 'array', items: { type: 'string' }, description: 'Labels to apply e.g. ["dashboard_scene"]' }
      },
      required: ['entity_id', 'labels']
    }
  },
  {
    name: 'process_conversation',
    description: 'Send a natural language command directly to HA built-in Assist for simple device control.',
    input_schema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'Natural language command e.g. "turn off all lights"' }
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
      return { success: true, message: `${input.domain}.${input.service} called on ${input.entity_id || 'all'}` };
    }

    case 'get_states': {
      if (input.entity_ids?.length) {
        const results = await Promise.all(input.entity_ids.map(id => haGet(`/states/${id}`).catch(e => ({ entity_id: id, error: e.message }))));
        return results.map(s => ({ entity_id: s.entity_id, state: s.state, attributes: { friendly_name: s.attributes?.friendly_name, brightness: s.attributes?.brightness, temperature: s.attributes?.temperature, current_temperature: s.attributes?.current_temperature } }));
      }
      const all = await haGet('/states');
      return all.map(s => ({ entity_id: s.entity_id, state: s.state, friendly_name: s.attributes?.friendly_name }));
    }

    case 'get_areas': {
      const [areas, states] = await Promise.all([haGet('/config/area_registry/list').catch(() => []), haGet('/states')]);
      return areas.map(a => ({
        id: a.area_id, name: a.name,
        entities: states.filter(s => s.attributes?.area_id === a.area_id || s.entity_id.includes(a.area_id)).map(s => s.entity_id)
      }));
    }

    case 'create_scene': {
      const sceneId = input.name.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
      // Create via config API for persistence
      await haPost(`/config/scene/config/${sceneId}`, { name: input.name, entities: input.entities });
      // Wait for registration
      await new Promise(r => setTimeout(r, 1500));
      // Apply dashboard_scene label
      try {
        await haPost(`/config/entity_registry/scene.${sceneId}`, { labels: ['dashboard_scene'] });
      } catch (e) {
        log('warning', 'Could not auto-label scene:', e.message);
      }
      return { success: true, entity_id: `scene.${sceneId}`, message: `Scene "${input.name}" created with dashboard_scene label` };
    }

    case 'create_automation': {
      const autoId = `proos_${Date.now()}`;
      const config = {
        id: autoId,
        alias: input.alias,
        description: input.description || '',
        trigger: input.trigger,
        condition: input.condition || [],
        action: input.action,
        mode: input.mode || 'single'
      };
      await haPost(`/config/automation/config/${autoId}`, config);
      return { success: true, automation_id: autoId, message: `Automation "${input.alias}" created` };
    }

    case 'label_entity': {
      await haPost(`/config/entity_registry/${input.entity_id}`, { labels: input.labels });
      return { success: true, message: `Labels ${input.labels.join(', ')} applied to ${input.entity_id}` };
    }

    case 'process_conversation': {
      const result = await haPost('/services/conversation/process', { text: input.text, language: 'en' });
      const speech = result?.[0]?.response?.speech?.plain?.speech || result?.response?.speech?.plain?.speech || 'Done.';
      return { success: true, response: speech };
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── BUILD HOME CONTEXT ──
async function buildHomeContext() {
  try {
    const states = await haGet('/states');
    const lights = states.filter(s => s.entity_id.startsWith('light.')).map(s => `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state}${s.attributes.brightness ? ` ${Math.round(s.attributes.brightness/2.55)}%` : ''}`).join('\n');
    const climate = states.filter(s => s.entity_id.startsWith('climate.')).map(s => `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state} → ${s.attributes.temperature}° (now ${s.attributes.current_temperature}°)`).join('\n');
    const covers = states.filter(s => s.entity_id.startsWith('cover.')).map(s => `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]: ${s.state}`).join('\n');
    const media = states.filter(s => s.entity_id.startsWith('media_player.') && s.state === 'playing').map(s => `${s.attributes.friendly_name||s.entity_id}: ${s.attributes.media_title||'playing'}`).join('\n');
    const scenes = states.filter(s => s.entity_id.startsWith('scene.')).map(s => `${s.attributes.friendly_name||s.entity_id} [${s.entity_id}]`).join(', ');
    const alarm = states.find(s => s.entity_id.startsWith('alarm_control_panel.'));
    const alarmStr = alarm ? `${alarm.attributes.friendly_name||alarm.entity_id}: ${alarm.state}` : 'none';
    return `LIGHTS:\n${lights||'none'}\n\nCLIMATE:\n${climate||'none'}\n\nCOVERS:\n${covers||'none'}\n\nMEDIA PLAYING:\n${media||'nothing'}\n\nSCENES: ${scenes||'none'}\n\nALARM: ${alarmStr}`;
  } catch (e) {
    log('error', 'Could not build context:', e.message);
    return 'Could not load home state.';
  }
}

// ── MAIN CHAT ENDPOINT ──
app.post('/assist', async (req, res) => {
  const { message, conversation_history = [] } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });
  if (!ANTHROPIC_KEY) return res.status(500).json({ error: 'ANTHROPIC_API_KEY not configured in add-on options' });

  log('info', `Assist request: "${message}"`);

  try {
    const client = new Anthropic({ apiKey: ANTHROPIC_KEY });
    const homeContext = await buildHomeContext();
    const systemPrompt = `You are the AI assistant for this smart home. You have full control via tools.

CURRENT HOME STATE:
${homeContext}

RULES:
- Use entity IDs exactly as shown in [brackets] above
- For simple device control, use process_conversation first — it's faster
- Use call_service for complex or multi-entity control
- For questions about state, use get_states or answer from context above
- Respond like a voice assistant: short, direct, no markdown
- Examples: "Yes, closed." / "Done, lights off." / "Playing Triple M 80s in the family room."
- For create requests, use create_scene or create_automation then confirm what was created`;

    const messages = [
      ...conversation_history.slice(-10),
      { role: 'user', content: message }
    ];

    let response = await client.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 1024,
      system: systemPrompt,
      tools: HA_TOOLS,
      messages
    });

    const toolResults = [];
    // Agentic loop
    while (response.stop_reason === 'tool_use') {
      const toolUses = response.content.filter(b => b.type === 'tool_use');
      for (const tu of toolUses) {
        try {
          const result = await executeTool(tu.name, tu.input);
          toolResults.push({ type: 'tool_result', tool_use_id: tu.id, content: JSON.stringify(result) });
        } catch (e) {
          log('error', `Tool ${tu.name} failed:`, e.message);
          toolResults.push({ type: 'tool_result', tool_use_id: tu.id, content: `Error: ${e.message}`, is_error: true });
        }
      }
      messages.push({ role: 'assistant', content: response.content });
      messages.push({ role: 'user', content: toolResults });
      response = await client.messages.create({
        model: 'claude-sonnet-4-6',
        max_tokens: 512,
        system: systemPrompt,
        tools: HA_TOOLS,
        messages
      });
    }

    const text = response.content.filter(b => b.type === 'text').map(b => b.text).join('').trim();
    const cleanText = text.replace(/\*\*(.*?)\*\*/g, '$1').replace(/\*(.*?)\*/g, '$1').replace(/`(.*?)`/g, '$1');

    log('info', `Response: "${cleanText}"`);
    res.json({ response: cleanText, conversation_history: messages.slice(-20) });

  } catch (e) {
    log('error', 'Assist error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

// ── HEALTH CHECK ──
app.get('/health', (req, res) => res.json({ status: 'ok', version: '1.0.0' }));

app.listen(PORT, '0.0.0.0', () => {
  log('info', `ProOS MCP Server running on port ${PORT}`);
  log('info', `HA URL: ${HA_URL}`);
  log('info', `Anthropic key: ${ANTHROPIC_KEY ? 'configured' : 'MISSING'}`);
});
