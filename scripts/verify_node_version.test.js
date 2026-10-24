'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const {
  DEFAULT_MINIMUM,
  SCHEMA_VERSION,
  parseVersion,
  isVersionAtLeast,
  validateSourceSha,
  actionsIdentity,
  buildReceipt,
  renderReceipt,
} = require('./verify_node_version.js');

const SCRIPT = path.join(__dirname, 'verify_node_version.js');
const SOURCE_SHA = 'a'.repeat(40);

test('the default minimum is the patched Node 22 security release', () => {
  assert.equal(DEFAULT_MINIMUM, '22.23.4');
  assert.equal(isVersionAtLeast('22.23.4', DEFAULT_MINIMUM), true);
  assert.equal(isVersionAtLeast('22.23.5', DEFAULT_MINIMUM), true);
  assert.equal(isVersionAtLeast('24.21.0', DEFAULT_MINIMUM), true);
  assert.equal(isVersionAtLeast('22.23.3', DEFAULT_MINIMUM), false);
  assert.equal(isVersionAtLeast('22.22.9', DEFAULT_MINIMUM), false);
  assert.equal(isVersionAtLeast('20.30.0', DEFAULT_MINIMUM), false);
  assert.equal(isVersionAtLeast('v22.23.4', DEFAULT_MINIMUM), true);
});

test('a receipt is built at or above the minimum', () => {
  for (const nodeVersion of ['22.23.4', '22.23.5', '24.21.0']) {
    const receipt = buildReceipt({ nodeVersion, npmVersion: '11.19.0' });
    assert.equal(receipt.satisfied, true);
    assert.equal(receipt.node_version, nodeVersion.replace(/^v/, ''));
    assert.equal(receipt.minimum, DEFAULT_MINIMUM);
    assert.equal(receipt.source_sha, null);
  }
});

test('a runtime below the minimum is rejected', () => {
  assert.throws(
    () => buildReceipt({ nodeVersion: '22.23.3', npmVersion: '11.19.0' }),
    /below the required minimum 22\.23\.4/,
  );
  assert.throws(
    () => buildReceipt({ nodeVersion: '20.30.0', npmVersion: '11.19.0' }),
    RangeError,
  );
});

test('invalid versions, npm versions, and source SHAs are rejected', () => {
  assert.throws(() => parseVersion('22.23', 'observed Node version'), /stable semantic version/);
  assert.throws(() => parseVersion('22.23.4-rc.1', 'observed Node version'), /stable semantic version/);
  assert.throws(() => buildReceipt({ nodeVersion: 'v22.23', npmVersion: '11.19.0' }), /stable semantic version/);
  assert.throws(() => buildReceipt({ nodeVersion: '22.23.4', npmVersion: '' }), TypeError);
  assert.throws(() => validateSourceSha('A'.repeat(40)), /40-character Git SHA/);
  assert.throws(() => validateSourceSha('a'.repeat(39)), /40-character Git SHA/);
  assert.throws(() => validateSourceSha('../' + 'a'.repeat(37)), /40-character Git SHA/);
  assert.throws(
    () => buildReceipt({ nodeVersion: '22.23.4', npmVersion: '11.19.0', sourceSha: 'not-a-sha' }),
    /40-character Git SHA/,
  );
});

test('the receipt records deterministic Node, npm, source, and actions identity', () => {
  const receipt = buildReceipt({
    nodeVersion: '22.23.4',
    npmVersion: '10.9.4',
    sourceSha: SOURCE_SHA,
    env: {
      GITHUB_ACTIONS: 'true',
      RUNNER_OS: 'Linux',
      RUNNER_ARCH: 'X64',
      ImageOS: 'ubuntu24',
      ImageVersion: '20261020.1.0',
    },
  });

  assert.equal(receipt.schema_version, SCHEMA_VERSION);
  assert.deepEqual(Object.keys(receipt), [
    'schema_version',
    'node_version',
    'npm_version',
    'minimum',
    'satisfied',
    'source_sha',
    'actions',
  ]);
  assert.equal(receipt.node_version, '22.23.4');
  assert.equal(receipt.npm_version, '10.9.4');
  assert.equal(receipt.minimum, DEFAULT_MINIMUM);
  assert.equal(receipt.satisfied, true);
  assert.equal(receipt.source_sha, SOURCE_SHA);
  assert.deepEqual(receipt.actions, {
    github_actions: true,
    runner_os: 'Linux',
    runner_arch: 'X64',
    image_os: 'ubuntu24',
    image_version: '20261020.1.0',
  });
  assert.equal(renderReceipt(receipt), `${JSON.stringify(receipt, null, 2)}\n`);
  assert.deepEqual(actionsIdentity({}), {
    github_actions: false,
    runner_os: null,
    runner_arch: null,
    image_os: null,
    image_version: null,
  });
});

test('the CLI writes a receipt for the running Node and fails below the minimum', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'josh-room-node-'));
  const report = path.join(directory, 'evidence', 'node-runtime.json');
  const accepted = spawnSync(
    process.execPath,
    [SCRIPT, '--minimum', DEFAULT_MINIMUM, '--source-sha', SOURCE_SHA, '--report', report],
    { encoding: 'utf8' },
  );
  assert.equal(accepted.status, 0, accepted.stderr);
  const receipt = JSON.parse(fs.readFileSync(report, 'utf8'));
  assert.equal(receipt.node_version, process.versions.node);
  assert.equal(receipt.minimum, DEFAULT_MINIMUM);
  assert.equal(receipt.satisfied, true);
  assert.equal(receipt.source_sha, SOURCE_SHA);
  assert.match(receipt.npm_version, /^\d+\.\d+\.\d+/);
  assert.equal(receipt.actions.github_actions, process.env.GITHUB_ACTIONS === 'true');

  const rejectedReport = path.join(directory, 'rejected.json');
  const rejected = spawnSync(
    process.execPath,
    [SCRIPT, '--minimum', '99.0.0', '--source-sha', SOURCE_SHA, '--report', rejectedReport],
    { encoding: 'utf8' },
  );
  assert.notEqual(rejected.status, 0);
  assert.match(rejected.stderr, /below the required minimum 99\.0\.0/);
  assert.equal(fs.existsSync(rejectedReport), false);

  const invalidSha = spawnSync(
    process.execPath,
    [SCRIPT, '--source-sha', 'main'],
    { encoding: 'utf8' },
  );
  assert.notEqual(invalidSha.status, 0);
  assert.match(invalidSha.stderr, /40-character Git SHA/);
});
