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

// ── HA TOOLS ──
const HA_TOOLS = [
  {
    name: 'call_service',
    description: 'Call any Home Assistant service to control devices.',
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
    description: 'Create and persist a new scene in Home Assistant.',
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
    description: 'Create a new automation in Home Assistant.',
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
    description: 'Apply labels to an entity in the HA registry.',
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
    description: 'Send natural language to HA Assist for simple device control.',
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
      await new Promise(r => setTimeout(r, 1500));
      try { await haPost(`/config/entity_registry/scene.${sceneId}`, { labels: ['dashboard_scene'] }); } catch(e) { log('warning', 'Label failed:', e.message); }
      return { success: true, entity_id: `scene.${sceneId}`, message: `Scene "${input.name}" created` };
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
      await haPost(`/config/entity_registry/${input.entity_id}`, { labels: input.labels });
      return { success: true, message: `Labels applied to ${input.entity_id}` };
    }
    case 'process_conversation': {
      const result = await haPost('/services/conversation/process', { text: input.text, language: 'en' });
      const speech = result?.[0]?.response?.speech?.plain?.speech || 'Done.';
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
    const media = states.filter(s => s.entity_id.startsWith('media_player.') && s.state === 'playing').map(s =>
      `${s.attributes.friendly_name||s.entity_id}: ${s.attributes.media_title||'playing'}`
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

// ── ASSIST ENDPOINT ──
app.post('/assist', async (req, res) => {
  const { message, conversation_history = [] } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });
  if (!ANTHROPIC_KEY) return res.status(500).json({ error: 'ANTHROPIC_API_KEY not configured' });
  log('info', `Request: "${message}"`);
  try {
    const client = new Anthropic({ apiKey: ANTHROPIC_KEY });
    const homeContext = await buildHomeContext();
    const system = `You are the AI assistant for this smart home. Full control via tools.\n\nHOME STATE:\n${homeContext}\n\nRULES:\n- Use exact entity IDs from [brackets]\n- Short direct responses, no markdown\n- "Yes, closed." / "Done." / "Office lights off."\n- Use process_conversation for simple commands\n- Use call_service for precise control\n- For create requests, use create_scene or create_automation`;

    const messages = [...conversation_history.slice(-10), { role: 'user', content: message }];
    let response = await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 1024, system, tools: HA_TOOLS, messages });

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
      response = await client.messages.create({ model: 'claude-sonnet-4-6', max_tokens: 512, system, tools: HA_TOOLS, messages });
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
