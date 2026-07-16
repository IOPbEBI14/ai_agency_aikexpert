#!/usr/bin/env node
/**
 * Локальный валидатор n8n workflow (без npm-зависимостей).
 *
 * Использование:
 *   node validate-n8n.js <workflow.json>
 *   node validate-n8n.js --json <workflow.json>
 *
 * Exit codes:
 *   0 — валиден
 *   1 — найдены ошибки / не удалось прочитать файл
 *
 * Синхронизирован с core/n8n_validator.py (heuristic-слой).
 */

const fs = require('fs');

const args = process.argv.slice(2);
const jsonMode = args.includes('--json');
const filePath = args.find((a) => a !== '--json');

if (!filePath) {
  console.error('Usage: node validate-n8n.js [--json] <workflow.json>');
  process.exit(1);
}

let workflow;
try {
  // strip UTF-8 BOM (часто появляется после PowerShell Set-Content -Encoding UTF8)
  const raw = fs.readFileSync(filePath, 'utf-8').replace(/^\uFEFF/, '');
  workflow = JSON.parse(raw);
} catch (err) {
  emitFail([`Failed to read/parse ${filePath}: ${err.message}`]);
  process.exit(1);
}

const issues = [];
const TRIGGER_TYPES = new Set([
  'scheduleTrigger', 'webhook', 'emailTrigger', 'manualTrigger',
  'mqttTrigger', 'amqpTrigger', 'kafkaTrigger', 'n8nTrigger', 'errorTrigger',
]);
const KNOWN_CRED = {
  nocoDb: ['nocoDbApiToken', 'nocoDbApi'],
  telegram: ['telegramApi'],
  httpRequest: ['httpBasicAuth', 'httpHeaderAuth', 'httpDigestAuth',
                'oAuth1Api', 'oAuth2Api', 'httpCustomAuth'],
  gmail: ['gmailOAuth2'],
  slack: ['slackOAuth2Api', 'slackApi'],
};

function shortType(type) {
  return (type || '').split('.').pop();
}

function emitFail(list) {
  if (jsonMode) {
    console.log(JSON.stringify({
      valid: false,
      issues: list.map((message) => ({ severity: 'error', message })),
    }));
  } else {
    console.error('❌ Workflow validation failed:');
    for (const issue of list) console.error(`  ${issue}`);
  }
}

function emitOk() {
  if (jsonMode) {
    console.log(JSON.stringify({ valid: true, issues: [] }));
  } else {
    console.log('✅ Workflow is valid (validate-n8n.js local mode)');
  }
}

if (!workflow || typeof workflow !== 'object' || Array.isArray(workflow)) {
  emitFail(['Workflow must be an object with "nodes" and "connections"']);
  process.exit(1);
}

const nodes = workflow.nodes;
if (!Array.isArray(nodes) || nodes.length === 0) {
  emitFail(['Workflow must have non-empty "nodes" array']);
  process.exit(1);
}

const connections = workflow.connections;
if (connections == null) {
  issues.push('ERROR: "connections" is missing — nodes will not be linked on import');
} else if (typeof connections !== 'object' || Array.isArray(connections)) {
  issues.push('ERROR: "connections" must be an object');
}

const nodeNames = new Set();
const nameCounts = {};
const ifNodeNames = new Set();

for (let i = 0; i < nodes.length; i++) {
  const node = nodes[i];
  const label = (node && (node.name || node.id)) || `node #${i}`;

  if (!node || typeof node !== 'object') {
    issues.push(`[${label}] ERROR: node must be an object`);
    continue;
  }

  if (node.name) {
    nameCounts[node.name] = (nameCounts[node.name] || 0) + 1;
    nodeNames.add(node.name);
  }

  if (!node.type || typeof node.type !== 'string') {
    issues.push(`[${label}] ERROR: Missing or invalid "type" field`);
    continue;
  }

  if (!node.type.startsWith('n8n-nodes-base.') &&
      !node.type.startsWith('n8n-nodes-') &&
      !node.type.startsWith('@n8n/')) {
    issues.push(
      `[${label}] ERROR: type="${node.type}" has invalid prefix ` +
      `(expected n8n-nodes-base.<node>)`
    );
  }

  if (typeof node.typeVersion !== 'number') {
    issues.push(`[${label}] ERROR: "typeVersion" must be a number`);
  }

  if (!Array.isArray(node.position) || node.position.length !== 2) {
    issues.push(`[${label}] ERROR: "position" must be [x, y] array`);
  }

  if (node.parameters == null || typeof node.parameters !== 'object' || Array.isArray(node.parameters)) {
    issues.push(`[${label}] ERROR: "parameters" must be an object`);
    continue;
  }

  const params = node.parameters;
  const typeVersion = node.typeVersion || 1;
  const st = shortType(node.type);
  if (st === 'if') ifNodeNames.add(node.name);

  if (st === 'scheduleTrigger') {
    const rule = params.rule;
    if (rule && !Array.isArray(rule.interval)) {
      issues.push(
        `[${label}] ERROR: scheduleTrigger "rule.interval" must be an array, got ${typeof rule.interval}. ` +
        `This causes "is not iterable". Fix: "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}`
      );
    }
  }

  if (st === 'if') {
    const cond = params.conditions;
    if (cond == null) {
      issues.push(`[${label}] ERROR: if missing parameters.conditions`);
    } else if (typeof cond !== 'object') {
      issues.push(`[${label}] ERROR: if "conditions" must be an object`);
    } else if (typeVersion >= 2) {
      if (!Array.isArray(cond.conditions)) {
        issues.push(
          `[${label}] ERROR: if (v${typeVersion}) "conditions.conditions" must be an array. ` +
          `Use: "conditions": {"combinator": "and", "conditions": [...]}`
        );
      } else if (cond.conditions.length === 0) {
        issues.push(
          `[${label}] ERROR: if "conditions.conditions" is empty — non-working IF. ` +
          `Add at least one condition with leftValue/operator/rightValue.`
        );
      } else {
        cond.conditions.forEach((c, idx) => {
          if (!c || typeof c !== 'object') {
            issues.push(`[${label}] ERROR: conditions.conditions[${idx}] must be object`);
            return;
          }
          if (c.leftValue == null || String(c.leftValue).trim() === '') {
            issues.push(
              `[${label}] ERROR: conditions.conditions[${idx}] has empty leftValue. ` +
              `Example: "={{ $json.statusCode }}"`
            );
          }
          if (!('operator' in c)) {
            issues.push(
              `[${label}] ERROR: conditions.conditions[${idx}] missing operator. ` +
              `Need: "operator": {"type":"number","operation":"equals"}`
            );
          }
        });
      }
    }
  }

  if (st === 'set' && typeVersion >= 3) {
    if ('values' in params && !('assignments' in params)) {
      issues.push(
        `[${label}] ERROR: set (v${typeVersion}) uses old "values" field. ` +
        `v3+ requires "assignments": {"assignments": [...]}`
      );
    }
    if (params.assignments && !Array.isArray(params.assignments.assignments)) {
      issues.push(
        `[${label}] ERROR: set (v${typeVersion}) "assignments.assignments" must be array`
      );
    }
  }

  if (st === 'httpRequest') {
    if (typeVersion >= 4) {
      if ('bodyContentType' in params ||
          (typeof params.body === 'object' && params.body !== null && !Array.isArray(params.body))) {
        issues.push(
          `[${label}] ERROR: httpRequest (v${typeVersion}) uses old "body"/"bodyContentType". ` +
          `v4+ requires: "sendBody": true, "specifyBody": "json", "jsonBody": "=..."`
        );
      }
    }
    if (params.url == null || String(params.url).trim() === '') {
      issues.push(`[${label}] ERROR: httpRequest missing "parameters.url"`);
    }
  }

  if (st === 'webhook') {
    if (params.path == null || String(params.path).trim() === '') {
      issues.push(`[${label}] ERROR: webhook missing "parameters.path"`);
    }
  }

  // Пустой options у if/switch ломает импорт (сравнение по содержимому, не по === {})
  if ((st === 'if' || st === 'switch') &&
      params.options &&
      typeof params.options === 'object' &&
      !Array.isArray(params.options) &&
      Object.keys(params.options).length === 0) {
    issues.push(
      `[${label}] ERROR: ${st} has empty "options": {} which breaks import. Remove the key.`
    );
  }

  if (st === 'nocoDb' && params.operation === 'update') {
    if ('data' in params && typeof params.data === 'object' && !Array.isArray(params.data)) {
      issues.push(
        `[${label}] ERROR: nocoDb update uses "data": {} — fields will NOT be saved. ` +
        `Use "fieldsUi": {"fieldValues": [{"fieldName":"synced","fieldValue":"true"}]} ` +
        `(typeVersion 2) or "updateFields": {"fieldValues":[...]} (typeVersion 1).`
      );
    } else {
      const hasFields =
        (params.fieldsUi && Array.isArray(params.fieldsUi.fieldValues)) ||
        (params.updateFields && Array.isArray(params.updateFields.fieldValues));
      if (!hasFields) {
        issues.push(
          `[${label}] ERROR: nocoDb update missing fieldsUi.fieldValues / updateFields.fieldValues`
        );
      }
    }
  }

  const expectedCreds = KNOWN_CRED[st];
  if (expectedCreds && node.credentials && typeof node.credentials === 'object') {
    for (const credKey of Object.keys(node.credentials)) {
      if (!expectedCreds.includes(credKey)) {
        issues.push(
          `[${label}] ERROR: credential key "${credKey}" is invalid for ${st}. ` +
          `Expected one of: ${expectedCreds.join(', ')}.`
        );
      }
    }
  }
}

for (const [name, count] of Object.entries(nameCounts)) {
  if (count > 1) {
    issues.push(
      `ERROR: duplicate node name "${name}" (${count} times). Names must be unique.`
    );
  }
}

const connectedSources = new Set();
const connectedTargets = new Set();

if (connections && typeof connections === 'object' && !Array.isArray(connections)) {
  for (const [src, outputs] of Object.entries(connections)) {
    if (!nodeNames.has(src)) {
      issues.push(`ERROR: connections source "${src}" not found in nodes`);
    }
    connectedSources.add(src);

    if (typeof outputs !== 'object' || Array.isArray(outputs)) {
      issues.push(`ERROR: connections["${src}"] must be object`);
      continue;
    }

    for (const [okey, buckets] of Object.entries(outputs)) {
      if (!Array.isArray(buckets)) {
        issues.push(`ERROR: connections["${src}"].${okey} must be array of arrays`);
        continue;
      }
      if (ifNodeNames.has(src) && okey === 'main' && buckets.length < 2) {
        issues.push(
          `ERROR: connections["${src}"]: IF must have both branches in main ` +
          `(true=index 0, false=index 1). Got ${buckets.length} branch(es).`
        );
      }
      for (const bucket of buckets) {
        if (!Array.isArray(bucket)) continue;
        for (const link of bucket) {
          if (!link || typeof link !== 'object') continue;
          if ('inputIndex' in link) {
            issues.push(
              `ERROR: connections["${src}"] link to "${link.node}" uses "inputIndex" ` +
              `instead of "index". This silently breaks connections on import.`
            );
          }
          if (!('index' in link) && !('inputIndex' in link)) {
            issues.push(
              `ERROR: connections["${src}"] → "${link.node}" missing "index" (usually 0)`
            );
          }
          if (link.node) {
            if (!nodeNames.has(link.node)) {
              issues.push(`ERROR: connections target "${link.node}" not found in nodes`);
            }
            connectedTargets.add(link.node);
          }
        }
      }
    }
  }
}

for (const node of nodes) {
  if (!node || !node.name) continue;
  const st = shortType(node.type);
  const isTrigger = TRIGGER_TYPES.has(st);
  const inSrc = connectedSources.has(node.name);
  const inTgt = connectedTargets.has(node.name);

  if (isTrigger && !inSrc) {
    issues.push(
      `[${node.name}] ERROR: trigger node is not connected to any downstream node`
    );
  } else if (!isTrigger && !inSrc && !inTgt) {
    issues.push(
      `[${node.name}] ERROR: node is completely isolated — not in "connections" ` +
      `as source or target`
    );
  }
}

if (issues.length === 0) {
  emitOk();
  process.exit(0);
}

emitFail(issues);
process.exit(1);
