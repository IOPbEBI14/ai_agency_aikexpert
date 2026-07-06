#!/usr/bin/env node
/**
 * Минималистичный валидатор n8n workflow.
 * Без зависимостей — только встроенные модули Node.js.
 * Использование: node validate-n8n.js <workflow.json>
 */

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
if (args.length === 0) {
  console.error('Usage: node validate-n8n.js <workflow.json>');
  process.exit(1);
}

const filePath = args[0];
let workflow;
try {
  const content = fs.readFileSync(filePath, 'utf-8');
  workflow = JSON.parse(content);
} catch (err) {
  console.error(`ERROR: Failed to read/parse ${filePath}: ${err.message}`);
  process.exit(1);
}

const issues = [];

// ─── Базовые проверки ────────────────────────────────────────────────────

if (!workflow || typeof workflow !== 'object') {
  console.error('ERROR: Workflow must be an object');
  process.exit(1);
}

const nodes = workflow.nodes;
if (!Array.isArray(nodes) || nodes.length === 0) {
  console.error('ERROR: Workflow must have "nodes" array with at least one node');
  process.exit(1);
}

const connections = workflow.connections;
if (connections && typeof connections !== 'object') {
  issues.push('ERROR: "connections" must be an object');
}

const nodeNames = new Set();

// ─── Проверка каждой ноды ─────────────────────────────────────────────────

for (let i = 0; i < nodes.length; i++) {
  const node = nodes[i];
  const label = node.name || node.id || `node #${i}`;

  if (!node.type || typeof node.type !== 'string') {
    issues.push(`[${label}] ERROR: Missing or invalid "type" field`);
    continue;
  }

  nodeNames.add(node.name);

  if (typeof node.typeVersion !== 'number') {
    issues.push(`[${label}] ERROR: "typeVersion" must be a number`);
  }

  if (!Array.isArray(node.position) || node.position.length !== 2) {
    issues.push(`[${label}] ERROR: "position" must be [x, y] array`);
  }

  if (typeof node.parameters !== 'object') {
    issues.push(`[${label}] ERROR: "parameters" must be an object`);
    continue;
  }

  const params = node.parameters;
  const typeVersion = node.typeVersion || 1;
  const shortType = node.type.split('.').pop();

  // Специфичные проверки по типу ноды
  if (shortType === 'scheduleTrigger') {
    const rule = params.rule;
    if (rule && Array.isArray(rule.interval) === false) {
      issues.push(
        `[${label}] ERROR: scheduleTrigger "rule.interval" must be an array, got ${typeof rule.interval}. ` +
        `This is the "is not iterable" error cause. Fix: "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}`
      );
    }
  }

  if (shortType === 'if') {
    const cond = params.conditions;
    if (cond && typeVersion >= 2 && !Array.isArray(cond.conditions)) {
      issues.push(
        `[${label}] ERROR: if (v${typeVersion}) "conditions.conditions" must be an array. ` +
        `Detected old v1 structure (conditions.string/number). ` +
        `Use: "conditions": {"combinator": "and", "conditions": [...]}`
      );
    }
  }

  if (shortType === 'set') {
    if (typeVersion >= 3) {
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
  }

  if (shortType === 'httpRequest') {
    if (typeVersion >= 4) {
      if ('bodyContentType' in params || (typeof params.body === 'object' && params.body !== null)) {
        issues.push(
          `[${label}] ERROR: httpRequest (v${typeVersion}) uses old "body"/"bodyContentType". ` +
          `v4+ requires: "sendBody": true, "specifyBody": "json", "jsonBody": "=..."`
        );
      }
    }
  }

  if ((shortType === 'if' || shortType === 'switch') && params.options === {}) {
    issues.push(
      `[${label}] ERROR: ${shortType} has empty "options": {} which breaks import. Remove it or fill it.`
    );
  }

  // ─── NocoDB update: fieldsUi vs data ──────────────────────────────────────
  if (shortType === 'nocoDb' && params.operation === 'update') {
    if ('data' in params && typeof params.data === 'object' && !Array.isArray(params.data)) {
      issues.push(
        `[${label}] ERROR: nocoDb update uses "data": {} dict — fields will NOT be saved. ` +
        `Use "fieldsUi": {"fieldValues": [{"fieldName": "synced", "fieldValue": "true"}, ...]} ` +
        `for typeVersion 2, or "updateFields": {"fieldValues": [...]} for typeVersion 1.`
      );
    }
  }

  // ─── Credentials key names ─────────────────────────────────────────────────
  const knownCredTypes = {
    nocoDb: ['nocoDbApiToken', 'nocoDbApi'],
    telegram: ['telegramApi'],
    httpRequest: ['httpBasicAuth', 'httpHeaderAuth', 'httpDigestAuth',
                  'oAuth1Api', 'oAuth2Api', 'httpCustomAuth'],
    gmail: ['gmailOAuth2'],
    slack: ['slackOAuth2Api', 'slackApi'],
  };
  const expectedCreds = knownCredTypes[shortType];
  if (expectedCreds && node.credentials) {
    for (const credKey of Object.keys(node.credentials)) {
      if (!expectedCreds.includes(credKey)) {
        issues.push(
          `[${label}] ERROR: credential key "${credKey}" is invalid for ${shortType}. ` +
          `Expected one of: ${expectedCreds.join(', ')}. ` +
          `Wrong credential key causes "Cannot read properties of undefined" at runtime.`
        );
      }
    }
  }
}

// ─── Проверка связности и connections ──────────────────────────────────────

const connectedSources = new Set();  // ноды, которые что-то отдают
const connectedTargets = new Set();  // ноды, которые что-то получают

// Тип ноды — trigger (не должна быть в targets)
const triggerTypes = new Set(['scheduleTrigger', 'webhook', 'emailTrigger', 'manualTrigger',
  'mqttTrigger', 'amqpTrigger', 'kafkaTrigger', 'n8nTrigger', 'errorTrigger']);
// Тип ноды — terminal (не должна быть в sources, если нет дальнейших шагов)
// Не блокируем — терминалы зависят от логики

if (connections && typeof connections === 'object') {
  for (const [src, outputs] of Object.entries(connections)) {
    if (!nodeNames.has(src)) {
      issues.push(`ERROR: connections source "${src}" not found in nodes`);
    }
    connectedSources.add(src);

    if (typeof outputs !== 'object' || Array.isArray(outputs)) {
      issues.push(`ERROR: connections["${src}"] must be object (got ${typeof outputs})`);
      continue;
    }
    for (const [_, buckets] of Object.entries(outputs)) {
      if (!Array.isArray(buckets)) {
        issues.push(`ERROR: connections["${src}"].main must be array of arrays`);
        continue;
      }
      for (const bucket of buckets) {
        if (!Array.isArray(bucket)) continue;
        for (const link of bucket) {
          // ─── inputIndex vs index ─────────────────────────────────────────
          if ('inputIndex' in link) {
            issues.push(
              `ERROR: connections["${src}"] link to "${link.node}" uses "inputIndex" ` +
              `instead of "index". This silently breaks connections on import. ` +
              `Fix: replace "inputIndex" with "index".`
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

// ─── Изолированные ноды (не подключены ни к чему) ──────────────────────────
for (const node of nodes) {
  const name = node.name;
  if (!name) continue;
  const shortType = (node.type || '').split('.').pop();
  const isTrigger = triggerTypes.has(shortType);

  // Trigger-нода не должна быть target (не получает данных)
  // Но должна быть source (должна что-то отдавать)
  if (isTrigger && !connectedSources.has(name)) {
    issues.push(
      `[${name}] WARNING: trigger node is not connected to any downstream node. ` +
      `Add it to "connections" as a source.`
    );
  }

  // Не-trigger нода должна быть либо source, либо target (или оба)
  if (!isTrigger && !connectedSources.has(name) && !connectedTargets.has(name)) {
    issues.push(
      `[${name}] ERROR: node is completely isolated — not present in "connections" ` +
      `as source or target. Either connect it or remove it from "nodes".`
    );
  }
}

// ─── Вывод результатов ─────────────────────────────────────────────────────

if (issues.length === 0) {
  console.log('✅ Workflow is valid (n8n-workflow-validator local mode)');
  process.exit(0);
} else {
  console.error('❌ Workflow validation failed:');
  for (const issue of issues) {
    console.error(`  ${issue}`);
  }
  process.exit(1);
}
