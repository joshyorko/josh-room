#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execSync } = require('node:child_process');

// Node 22.23.4 contains the October 2026 high-severity security fixes.
const DEFAULT_MINIMUM = '22.23.4';
const SCHEMA_VERSION = 1;
const SOURCE_SHA = /^[0-9a-f]{40}$/;
const STABLE_VERSION = /^v?(\d+)\.(\d+)\.(\d+)$/;

const USAGE = `usage: node scripts/verify_node_version.js [--minimum <semver>] [--source-sha <40-hex>] [--report <path>]`;

function parseVersion(value, label) {
  if (typeof value !== 'string') {
    throw new TypeError(`${label} must be a string`);
  }
  const match = STABLE_VERSION.exec(value.trim());
  if (match === null) {
    throw new Error(`${label} must be a stable semantic version: ${value}`);
  }
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
  };
}

function versionString(version) {
  return `${version.major}.${version.minor}.${version.patch}`;
}

function compareVersionParts(observed, minimum) {
  for (const key of ['major', 'minor', 'patch']) {
    if (observed[key] !== minimum[key]) {
      return observed[key] < minimum[key] ? -1 : 1;
    }
  }
  return 0;
}

function compareVersions(observed, minimum) {
  return compareVersionParts(
    parseVersion(observed, 'observed version'),
    parseVersion(minimum, 'minimum version'),
  );
}

function isVersionAtLeast(observed, minimum) {
  return compareVersions(observed, minimum) >= 0;
}

function validateSourceSha(sourceSha) {
  if (sourceSha === null || sourceSha === undefined) {
    return null;
  }
  if (typeof sourceSha !== 'string' || !SOURCE_SHA.test(sourceSha)) {
    throw new Error('source SHA must be a full lowercase 40-character Git SHA');
  }
  return sourceSha;
}

function actionsIdentity(env) {
  const environment = env || {};
  return {
    github_actions: environment.GITHUB_ACTIONS === 'true',
    runner_os: environment.RUNNER_OS || null,
    runner_arch: environment.RUNNER_ARCH || null,
    image_os: environment.ImageOS || null,
    image_version: environment.ImageVersion || null,
  };
}

function buildReceipt({ nodeVersion, minimum = DEFAULT_MINIMUM, npmVersion, sourceSha = null, env = {} }) {
  const observed = parseVersion(nodeVersion, 'observed Node version');
  const floor = parseVersion(minimum, 'minimum Node version');
  if (compareVersionParts(observed, floor) < 0) {
    throw new RangeError(`Node ${versionString(observed)} is below the required minimum ${versionString(floor)}`);
  }
  if (typeof npmVersion !== 'string' || npmVersion.trim() === '') {
    throw new TypeError('npm version must be a non-empty string');
  }
  return {
    schema_version: SCHEMA_VERSION,
    node_version: versionString(observed),
    npm_version: npmVersion.trim(),
    minimum: versionString(floor),
    satisfied: true,
    source_sha: validateSourceSha(sourceSha),
    actions: actionsIdentity(env),
  };
}

function renderReceipt(receipt) {
  return `${JSON.stringify(receipt, null, 2)}\n`;
}

function parseArgs(argv) {
  const options = {
    minimum: DEFAULT_MINIMUM,
    report: null,
    sourceSha: null,
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') {
      options.help = true;
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined) {
      throw new Error(`missing value for ${argument}`);
    }
    if (argument === '--minimum') {
      options.minimum = value;
    } else if (argument === '--report') {
      options.report = value;
    } else if (argument === '--source-sha') {
      options.sourceSha = value;
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
    index += 1;
  }
  return options;
}

function npmVersion() {
  return execSync('npm --version', { encoding: 'utf8' }).trim();
}

function main(argv = process.argv.slice(2), env = process.env) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    console.error(`node runtime verification failed: ${error.message}`);
    return 2;
  }
  if (options.help) {
    console.log(USAGE);
    return 0;
  }
  try {
    const receipt = buildReceipt({
      nodeVersion: process.versions.node,
      minimum: options.minimum,
      npmVersion: npmVersion(),
      sourceSha: options.sourceSha,
      env,
    });
    if (options.report) {
      const report = path.resolve(options.report);
      fs.mkdirSync(path.dirname(report), { recursive: true });
      fs.writeFileSync(report, renderReceipt(receipt));
    }
    console.log(renderReceipt(receipt));
    return 0;
  } catch (error) {
    console.error(`node runtime verification failed: ${error.message}`);
    return 1;
  }
}

module.exports = {
  DEFAULT_MINIMUM,
  SCHEMA_VERSION,
  parseVersion,
  versionString,
  compareVersions,
  isVersionAtLeast,
  validateSourceSha,
  actionsIdentity,
  buildReceipt,
  renderReceipt,
  parseArgs,
  main,
};

if (require.main === module) {
  process.exitCode = main();
}
