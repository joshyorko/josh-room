#!/usr/bin/env node
/**
 * Headless Chrome (CDP) verification and synthetic screenshot receipt generator
 * for Josh Room View (issue #202 — Pass 2: Isla Nublar UNIX FSN + NAVIGATE + INSPECT).
 *
 * Uses zero external npm dependencies (native Node 22+ WebSocket + Chrome DevTools Protocol).
 */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function launchChrome({ chromePath, disableWebgl = false, width = 1440, height = 900 }) {
  const shortTmp = fs.existsSync('/tmp') ? '/tmp' : os.tmpdir();
  const userDataDir = fs.mkdtempSync(path.join(shortTmp, 'jr-chr-'));
  const args = [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${userDataDir}`,
    `--window-size=${width},${height}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--disable-sync',
    '--disable-extensions',
    '--disable-component-update',
    '--mute-audio',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
  ];
  if (disableWebgl) {
    args.push('--disable-webgl', '--disable-webgl2', '--disable-3d-apis', '--disable-gpu');
  } else {
    args.push(
      '--enable-webgl',
      '--ignore-gpu-blocklist',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader'
    );
  }
  args.push('about:blank');

  const proc = spawn(chromePath, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, TMPDIR: shortTmp },
  });
  let stderrBuf = '';
  const wsUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Chrome startup timeout. stderr=${stderrBuf}`)), 20000);
    proc.stderr.on('data', (chunk) => {
      stderrBuf += chunk.toString('utf8');
      const m = stderrBuf.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (m) {
        clearTimeout(timer);
        resolve(m[1]);
      }
    });
    proc.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`Chrome exited early (${code}). stderr=${stderrBuf}`));
    });
  });

  const port = new URL(wsUrl).port;
  const targetsResp = await fetch(`http://127.0.0.1:${port}/json/list`);
  const targets = await targetsResp.json();
  const pageTarget = targets.find((t) => t.type === 'page') || targets[0];
  if (!pageTarget || !pageTarget.webSocketDebuggerUrl) {
    proc.kill('SIGKILL');
    throw new Error('Could not find Chrome page target');
  }

  const ws = new WebSocket(pageTarget.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });

  let nextId = 1;
  const pending = new Map();
  const listeners = new Map();

  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(typeof event.data === 'string' ? event.data : event.data.toString('utf8'));
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.code}: ${msg.error.message}`));
      else resolve(msg.result);
    } else if (msg.method) {
      const handlers = listeners.get(msg.method) || [];
      for (const fn of handlers) fn(msg.params);
    }
  });

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const on = (method, fn) => {
    if (!listeners.has(method)) listeners.set(method, []);
    listeners.get(method).push(fn);
  };

  const evalJs = async (expression, awaitPromise = true) => {
    const res = await send('Runtime.evaluate', {
      expression,
      awaitPromise,
      returnByValue: true,
    });
    if (res.exceptionDetails) {
      throw new Error(`JS exception: ${JSON.stringify(res.exceptionDetails)}`);
    }
    return res.result ? res.result.value : undefined;
  };

  const screenshot = async (outPath) => {
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, Buffer.from(data, 'base64'));
  };

  const close = async () => {
    try { ws.close(); } catch (_) {}
    try { proc.kill('SIGTERM'); } catch (_) {}
    await sleep(250);
    try { proc.kill('SIGKILL'); } catch (_) {}
    fs.rmSync(userDataDir, { recursive: true, force: true });
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride', {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });

  return { send, on, evalJs, screenshot, close };
}

async function waitForAppBoot(cdp, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const status = await cdp.evalJs(`(() => ({
      ready: Boolean(window.__JOSH_ROOM_VIEW_DEBUG__),
      err: window.__JOSH_ROOM_VIEW_BOOT_ERROR__ || null,
      bootHidden: Boolean(document.getElementById('boot-overlay')?.hidden),
    }))()`);
    if (status.err) throw new Error(`Viewer boot error: ${status.err}`);
    if (status.ready && status.bootHidden) return;
    await sleep(100);
  }
  throw new Error('Timed out waiting for Room View boot');
}

async function main() {
  const args = process.argv.slice(2);
  const opt = {};
  for (let i = 0; i < args.length; i += 2) {
    opt[args[i].replace(/^--/, '')] = args[i + 1];
  }
  const htmlUnix = opt.htmlunix ? path.resolve(opt.htmlunix) : null;
  const html300 = opt.html300 ? path.resolve(opt.html300) : null;
  const html10 = opt.html10 ? path.resolve(opt.html10) : null;
  const html540 = opt.html540 ? path.resolve(opt.html540) : null;
  const liveUrl = opt.liveurl || null;
  const outDir = path.resolve(opt.outdir);
  const chromePath = opt.chrome || '/usr/bin/google-chrome';
  fs.mkdirSync(outDir, { recursive: true });

  if (liveUrl) {
    const cdpLive = await launchChrome({ chromePath, disableWebgl: false });
    try {
      await cdpLive.send('Page.navigate', { url: liveUrl });
      await waitForAppBoot(cdpLive);
      await cdpLive.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.settle()');
      await sleep(220);
      await cdpLive.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('run-var-gate-verify', true, true);
      })()`);
      await sleep(300);
      // Verify live /content/<id>/<slot> endpoint + PDF evidence + SQL + live inspector refresh
      const contentApi = await cdpLive.evalJs(`(async () => {
        const r = await fetch('/content/file-src-scheduler/diff', { cache: 'no-store' });
        const data = await r.json();
        return { status: r.status, slot: data?.preview?.slot || null, media: data?.preview?.media_type || null };
      })()`);
      await cdpLive.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.openEvidenceTab();
      })()`);
      await sleep(500);
      const liveCheck = await cdpLive.evalJs(`(() => ({
        mode: window.__JOSH_ROOM_VIEW_DEBUG__.mode,
        webglActive: window.__JOSH_ROOM_VIEW_DEBUG__.webglActive,
        itemCount: window.__JOSH_ROOM_VIEW_DEBUG__.itemCount,
        exportBtnVisible: !document.getElementById('btn-export-live')?.hidden,
        pdfCanvases: document.querySelectorAll('#tab-evidence .pdf-page-slot canvas').length,
        sqlNodesRows: window.__JOSH_ROOM_VIEW_DEBUG__.runSql('SELECT COUNT(*) AS n FROM nodes').rowCount,
        sqlLinksRows: window.__JOSH_ROOM_VIEW_DEBUG__.runSql('SELECT COUNT(*) AS n FROM links').rowCount,
      }))()`);
      const ok = (
        liveCheck.mode === 'live' &&
        liveCheck.webglActive &&
        liveCheck.itemCount > 0 &&
        liveCheck.exportBtnVisible &&
        liveCheck.pdfCanvases >= 1 &&
        liveCheck.sqlNodesRows === 1 &&
        liveCheck.sqlLinksRows === 1 &&
        contentApi.status === 200 &&
        contentApi.slot === 'diff'
      );
      console.log(JSON.stringify({ ok, live: liveCheck, contentApi }, null, 2));
      if (!ok) process.exitCode = 1;
    } finally {
      await cdpLive.close();
    }
    return;
  }

  const receipt = {
    schema_version: 2,
    chrome: chromePath,
    viewport: '1440x900',
    zero_network: {},
    render_on_demand: {},
    unix_navigate_inspect: {},
    pr_review_regressions: {},
    performance: {},
    accessibility: {},
    security: {},
    fallback_webgl_off: {},
    screenshots: [],
  };

  const cdp = await launchChrome({ chromePath, disableWebgl: false });
  const networkRequests = [];
  const blockedExternalRequests = [];
  const consoleErrors = [];
  const cspViolations = [];

  cdp.on('Network.requestWillBeSent', (p) => {
    const url = p.request?.url || '';
    networkRequests.push(url);
    if (/^https?:\/\//i.test(url) || /^data:/i.test(url)) {
      blockedExternalRequests.push(url);
    }
  });
  cdp.on('Runtime.consoleAPICalled', (p) => {
    const text = (p.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
    if (p.type === 'error') consoleErrors.push(text);
    if (/Content Security Policy|CSP/i.test(text)) cspViolations.push(text);
  });
  cdp.on('Runtime.exceptionThrown', (p) => {
    consoleErrors.push(p.exceptionDetails?.text || 'runtime-exception');
  });

  await cdp.send('Fetch.enable', {
    patterns: [
      { urlPattern: 'http://*' },
      { urlPattern: 'https://*' },
    ],
  });
  cdp.on('Fetch.requestPaused', async (p) => {
    blockedExternalRequests.push(p.request?.url || 'unknown');
    try {
      await cdp.send('Fetch.failRequest', { requestId: p.requestId, errorReason: 'BlockedByClient' });
    } catch (_) {}
  });

  try {
    // =========================================================================
    // PART A: Flagship Synthetic UNIX Filesystem Navigator (synthetic-unix)
    // =========================================================================
    if (htmlUnix) {
      const unixUrl = pathToFileURL(htmlUnix).href;
      const unixStart = Date.now();
      await cdp.send('Page.navigate', { url: unixUrl });
      await waitForAppBoot(cdp);
      const unixWallBootMs = Date.now() - unixStart;
      await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.settle()');
      await sleep(260);

      const unixPerf = await cdp.evalJs(`(() => {
        const d = window.__JOSH_ROOM_VIEW_DEBUG__;
        return {
          mode: d.mode,
          webglActive: d.webglActive,
          itemCount: d.itemCount,
          firstFrameMs: d.firstFrameMs,
          drawCalls: d.drawCalls,
          cameraY: d.cameraY,
          layoutSummary: d.layoutSummary,
          inspectorHiddenOnLoad: Boolean(document.getElementById('inspector-drawer')?.hidden),
          legendItems: Array.from(document.querySelectorAll('#legend-strip .legend-item')).map((el) => el.textContent.trim()),
        };
      })()`);

      // 1. Night UNIX Root Overview (Isla Nublar near-ground entry!)
      const pUnix01 = path.join(outDir, '01-night-unix-overview.png');
      await cdp.screenshot(pUnix01);
      receipt.screenshots.push(path.basename(pUnix01));

      // 2. Fly INTO /src/core (arbitrary-depth corridor view showing files + /src/core/codec -> /vectors receding)
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.focusNode('node/dir-src-core', true);
      })()`);
      await sleep(220);
      const breadcrumbsDir = await cdp.evalJs(`Array.from(document.querySelectorAll('#breadcrumbs .crumb-btn')).map((b) => b.textContent)`);
      const pUnix02 = path.join(outDir, '02-night-unix-directory.png');
      await cdp.screenshot(pUnix02);
      receipt.screenshots.push(path.basename(pUnix02));

      // 3. Spotlight v2 on /var/runs/gate-verify (no inspector so full overhead BEAM + floor pool is unobstructed)
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('v2');
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('run-var-gate-verify', false, true);
      })()`);
      await sleep(240);
      const pUnix03 = path.join(outDir, '03-night-spotlight-v2.png');
      await cdp.screenshot(pUnix03);
      receipt.screenshots.push(path.basename(pUnix03));

      // 4. Spotlight Baseline on same selected object for A/B comparison
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('baseline');
        window.__JOSH_ROOM_VIEW_DEBUG__.settle();
      })()`);
      await sleep(220);
      const pUnix04 = path.join(outDir, '04-night-spotlight-baseline.png');
      await cdp.screenshot(pUnix04);
      receipt.screenshots.push(path.basename(pUnix04));

      // 5. Inspect Source & Unified Diff (/src/core/scheduler.py) with Spotlight v2
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('v2');
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('file-src-scheduler', true, true);
      })()`);
      await sleep(220);
      const sourceCheck = await cdp.evalJs(`(() => ({
        title: document.getElementById('inspector-title')?.textContent || '',
        path: document.getElementById('inspector-path')?.textContent || '',
        codeLines: document.querySelectorAll('#tab-content .code-line').length,
        slots: Array.from(document.querySelectorAll('#tab-content [data-preview-slot]')).map((b) => b.dataset.previewSlot),
        linkTargets: Array.from(document.querySelectorAll('#tab-content .link-chip')).map((b) => ({
          rel: b.dataset.linkRelation,
          target: b.dataset.linkTarget,
        })),
      }))()`);
      // Switch to diff slot to verify unified diff highlighting too
      await cdp.evalJs(`(() => {
        const diffBtn = document.querySelector('#tab-content [data-preview-slot="diff"]');
        if (diffBtn) diffBtn.click();
      })()`);
      await sleep(160);
      const diffLineCount = await cdp.evalJs(`document.querySelectorAll('#tab-content .code-line.diff-add, #tab-content .code-line.diff-del').length`);
      const pUnixSrc = path.join(outDir, '04b-night-inspect-source-diff.png');
      await cdp.screenshot(pUnixSrc);
      receipt.screenshots.push(path.basename(pUnixSrc));

      // 6. Click producer -> /var/runs/gate-verify relationship link chip to jump across branches!
      await cdp.evalJs(`(() => {
        const chip = document.querySelector('#tab-content .link-chip[data-link-target="run-var-gate-verify"]');
        if (chip) chip.click();
        window.__JOSH_ROOM_VIEW_DEBUG__.settle();
      })()`);
      await sleep(240);
      const runJumpCheck = await cdp.evalJs(`(() => ({
        currentNodeId: window.__JOSH_ROOM_VIEW_DEBUG__.currentNodeId,
        title: document.getElementById('inspector-title')?.textContent || '',
        path: document.getElementById('inspector-path')?.textContent || '',
        stepRows: document.querySelectorAll('#tab-content .run-step-row').length,
        hasErrorBox: Boolean(document.querySelector('#tab-content .run-error-box')),
        linkTargets: Array.from(document.querySelectorAll('#tab-content .link-chip')).map((b) => b.dataset.linkTarget),
      }))()`);
      const pUnixRun = path.join(outDir, '04c-night-inspect-run-jump.png');
      await cdp.screenshot(pUnixRun);
      receipt.screenshots.push(path.basename(pUnixRun));

      // 7. Inspect collapsible JSON tree (/etc/policy.json) & explicit REDACTED card (/etc/vault.policy)
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('file-etc-policy-json', true, true);
      })()`);
      await sleep(200);
      const jsonCheck = await cdp.evalJs(`(() => ({
        detailsCount: document.querySelectorAll('#tab-content .json-tree details').length,
        keyCount: document.querySelectorAll('#tab-content .json-key').length,
      }))()`);
      const pUnixJson = path.join(outDir, '04d-night-inspect-json-tree.png');
      await cdp.screenshot(pUnixJson);
      receipt.screenshots.push(path.basename(pUnixJson));

      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('file-etc-vault-policy', true, true);
      })()`);
      await sleep(160);
      const redactedCheck = await cdp.evalJs(`(() => ({
        status: document.querySelector('#tab-content .unavail-card')?.dataset?.previewStatus || '',
        badge: document.querySelector('#tab-content .unavail-badge')?.textContent || '',
      }))()`);

      // 8. Click through to /var/artifacts/report.html -> verify PDF + Inert HTML XSS safety on UNIX fixture!
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('file-var-report-html', true, true);
        window.__JOSH_ROOM_VIEW_DEBUG__.openEvidenceTab();
      })()`);
      let unixPdfInfo = { canvasCount: 0, firstPageMs: -1 };
      for (let attempt = 0; attempt < 25; attempt += 1) {
        await sleep(160);
        unixPdfInfo = await cdp.evalJs(`(() => {
          const canvases = document.querySelectorAll('#tab-evidence .pdf-page-slot canvas');
          const infoEl = document.querySelector('#tab-evidence .pdf-controls .muted-text');
          return { canvasCount: canvases.length, firstPageMs: Number(infoEl?.dataset?.firstPageMs || -1) };
        })()`);
        if (unixPdfInfo.canvasCount >= 1 && unixPdfInfo.firstPageMs >= 0) break;
      }

      // 9. Verify Grass Theme on UNIX filesystem (Overview, Directory, Spotlight v2, Spotlight Baseline)
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.hideInspector();
        window.__JOSH_ROOM_VIEW_DEBUG__.setTheme('grass');
        window.__JOSH_ROOM_VIEW_DEBUG__.frameAll(true);
      })()`);
      await sleep(220);
      const pGrass05 = path.join(outDir, '05-grass-unix-overview.png');
      await cdp.screenshot(pGrass05);
      receipt.screenshots.push(path.basename(pGrass05));

      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.focusNode('node/dir-src-core', true);
      })()`);
      await sleep(200);
      const pGrass06 = path.join(outDir, '06-grass-unix-directory.png');
      await cdp.screenshot(pGrass06);
      receipt.screenshots.push(path.basename(pGrass06));

      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('v2');
        window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('run-var-gate-verify', false, true);
      })()`);
      await sleep(220);
      const pGrass07 = path.join(outDir, '07-grass-spotlight-v2.png');
      await cdp.screenshot(pGrass07);
      receipt.screenshots.push(path.basename(pGrass07));

      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('baseline');
        window.__JOSH_ROOM_VIEW_DEBUG__.settle();
      })()`);
      await sleep(200);
      const pGrass08 = path.join(outDir, '08-grass-spotlight-baseline.png');
      await cdp.screenshot(pGrass08);
      receipt.screenshots.push(path.basename(pGrass08));

      // 10. Verify single-key dispatch inside APG ARIA tree (PR #205 review regression test!)
      const treeKeyCheck = await cdp.evalJs(`(() => {
        const app = window.__JOSH_ROOM_VIEW_APP__;
        app.frameAll({ instant: true });
        const rootEl = document.getElementById('aria-tree-root');
        const firstTreeItem = rootEl.querySelector('[role="treeitem"]');
        const beforeOrder = app.ariaTree.flatOrder.slice();
        const beforeId = app.ariaTree.focusedId;
        firstTreeItem.focus();
        firstTreeItem.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }));
        const afterId = app.ariaTree.focusedId;
        const expectedNext = beforeOrder[1];
        return { beforeId, afterId, expectedNext, singleStepOnly: afterId === expectedNext };
      })()`);

      // 11. Verify collision-free group/stage IDs (PR #205 review regression test!)
      const collisionCheck = await cdp.evalJs(`(() => {
        const L = window.RoomViewLayout;
        const desc = {
          title: 'Colon Test',
          stages: [{ id: 'b::c', name: 'BC', order: 0 }, { id: 'c', name: 'C', order: 1 }],
          legend: { error: { label: 'Error', color: '#E11D48', shape: 'notch' } },
        };
        const st = {
          groups: [{ id: 'a', name: 'A', order: 0 }, { id: 'a::b', name: 'AB', order: 1 }],
          items: [
            { id: 'i1', group: 'a', stage: 'b::c', kind: 'error', label: 'one', updated_at: '2026-11-20T00:00:00Z', metric: 10 },
            { id: 'i2', group: 'a::b', stage: 'c', kind: 'error', label: 'two', updated_at: '2026-11-20T00:00:00Z', metric: 20 },
          ],
        };
        const lay = L.buildFsnLayout(desc, st);
        const stagePeds = lay.pedestals.filter((p) => p.nodeType === 'stage');
        const counts = L.outcomeCounts(st.items, desc);
        const badge = L.formatOutcomeBadge(desc, 'error');
        return {
          distinctStagePedestals: stagePeds.length === 2 && stagePeds[0].id !== stagePeds[1].id,
          noDuplicateBlocks: lay.blocks.length === 2,
          customLegendPassIsZero: counts.pass === 0 && counts.fail === 2,
          customLegendBadge: badge,
        };
      })()`);

      // Switch back to night & verify SQL nodes/links query
      await cdp.evalJs(`(() => {
        window.__JOSH_ROOM_VIEW_DEBUG__.setTheme('night');
        window.__JOSH_ROOM_VIEW_DEBUG__.setViewMode('sql');
      })()`);
      const unixSqlLinks = await cdp.evalJs(`window.__JOSH_ROOM_VIEW_DEBUG__.runSql("SELECT s.path, l.relation, t.path FROM links l JOIN nodes s ON s.id = l.source_id JOIN nodes t ON t.id = l.target_id")`);
      await sleep(160);
      const pSql = path.join(outDir, '10-sql-console.png');
      await cdp.screenshot(pSql);
      receipt.screenshots.push(path.basename(pSql));

      receipt.unix_navigate_inspect = {
        ...unixPerf,
        wallBootMs: unixWallBootMs,
        file_bytes: fs.statSync(htmlUnix).size,
        breadcrumbs_inside_dir: breadcrumbsDir,
        source_preview: sourceCheck,
        diff_highlight_lines: diffLineCount,
        relationship_link_jump: runJumpCheck,
        json_tree: jsonCheck,
        redacted_card: redactedCheck,
        unix_pdf: unixPdfInfo,
        sql_links_rows: unixSqlLinks.rowCount,
        pass: (
          unixPerf.webglActive === true &&
          unixPerf.layoutSummary.isUnixTree === true &&
          unixPerf.layoutSummary.maxDepth >= 4 &&
          sourceCheck.codeLines >= 15 &&
          diffLineCount >= 3 &&
          runJumpCheck.currentNodeId === 'node/run-var-gate-verify' &&
          runJumpCheck.stepRows >= 4 &&
          runJumpCheck.hasErrorBox === true &&
          jsonCheck.detailsCount >= 3 &&
          redactedCheck.status === 'redacted' &&
          unixPdfInfo.canvasCount >= 1 &&
          unixSqlLinks.rowCount >= 10
        ),
      };
      receipt.pr_review_regressions = {
        tree_single_key_dispatch: treeKeyCheck,
        collision_free_and_custom_legend: collisionCheck,
        pass: (
          treeKeyCheck.singleStepOnly === true &&
          collisionCheck.distinctStagePedestals === true &&
          collisionCheck.noDuplicateBlocks === true &&
          collisionCheck.customLegendPassIsZero === true &&
          collisionCheck.customLegendBadge === '× Error'
        ),
      };
    }

    // =========================================================================
    // PART B: Legacy 300-item Pipeline Backwards-Compat, Render-on-Demand,
    // PDF, Inert HTML XSS Probe, Reduced Motion & Scale (10 / 540 items)
    // =========================================================================
    const fileUrl300 = pathToFileURL(html300).href;
    const navStart = Date.now();
    await cdp.send('Page.navigate', { url: fileUrl300 });
    await waitForAppBoot(cdp);
    const wallBootMs = Date.now() - navStart;
    await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.settle()');
    await sleep(250);

    const perf300 = await cdp.evalJs(`(() => {
      const d = window.__JOSH_ROOM_VIEW_DEBUG__;
      return {
        mode: d.mode,
        viewMode: d.viewMode,
        webglActive: d.webglActive,
        itemCount: d.itemCount,
        firstFrameMs: d.firstFrameMs,
        drawCalls: d.drawCalls,
        renderCount: d.renderCount,
        shadowUpdateCount: d.shadowUpdateCount,
        cameraY: d.cameraY,
        layoutSummary: d.layoutSummary,
        inspectorHiddenOnLoad: Boolean(document.getElementById('inspector-drawer')?.hidden),
        canvasAriaHidden: document.getElementById('fsn-canvas')?.getAttribute('aria-hidden'),
        treeRole: document.getElementById('aria-tree-root')?.getAttribute('role'),
        treeItemCount: document.querySelectorAll('#aria-tree-root [role="treeitem"]').length,
        livePolite: document.getElementById('aria-live-region')?.getAttribute('aria-live'),
        hasImportMap: Boolean(document.querySelector('script[type="importmap"]')),
      };
    })()`);

    const beforeIdle = await cdp.evalJs('({ r: window.__JOSH_ROOM_VIEW_DEBUG__.renderCount, s: window.__JOSH_ROOM_VIEW_DEBUG__.shadowUpdateCount })');
    await sleep(1200);
    const afterIdle = await cdp.evalJs('({ r: window.__JOSH_ROOM_VIEW_DEBUG__.renderCount, s: window.__JOSH_ROOM_VIEW_DEBUG__.shadowUpdateCount })');
    receipt.render_on_demand = {
      idle_ms: 1200,
      renders_during_idle: afterIdle.r - beforeIdle.r,
      shadow_updates_during_idle: afterIdle.s - beforeIdle.s,
      pass: (afterIdle.r - beforeIdle.r) === 0 && (afterIdle.s - beforeIdle.s) === 0,
    };

    const pPipe01 = path.join(outDir, '01b-night-pipeline-300-overview.png');
    await cdp.screenshot(pPipe01);
    receipt.screenshots.push(path.basename(pPipe01));

    const targetFailId = await cdp.evalJs(`(() => {
      const app = window.__JOSH_ROOM_VIEW_APP__;
      const fail = app.data.state.items.find((it) => it.group === 'sector-beta' && it.stage === 'verify' && it.kind === 'fail');
      return fail ? fail.id : app.data.state.items.find((it) => it.kind === 'fail').id;
    })()`);

    const shadowBeforeSelect = await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.shadowUpdateCount');
    await cdp.evalJs(`(() => {
      window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('v2');
      window.__JOSH_ROOM_VIEW_DEBUG__.selectItem(${JSON.stringify(targetFailId)}, true, true);
      window.__JOSH_ROOM_VIEW_DEBUG__.openEvidenceTab();
      const pdfBtn = Array.from(document.querySelectorAll('#tab-evidence .att-pill')).find((b) => b.dataset.attachmentName === 'evidence-report.pdf');
      if (pdfBtn) pdfBtn.click();
    })()`);
    const shadowAfterSelect = await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.shadowUpdateCount');
    const firstClickMs = await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.lastFirstClickMs');
    const liveAnnouncement = await cdp.evalJs('document.getElementById("aria-live-region")?.textContent || ""');

    let pdfInfo = { canvasCount: 0, infoText: '', firstPageMs: -1, statusText: '' };
    for (let attempt = 0; attempt < 25; attempt += 1) {
      await sleep(160);
      pdfInfo = await cdp.evalJs(`(() => {
        const canvases = document.querySelectorAll('#tab-evidence .pdf-page-slot canvas');
        const infoEl = document.querySelector('#tab-evidence .pdf-controls .muted-text');
        const statusText = document.querySelector('#tab-evidence .att-stage')?.textContent?.slice(0, 160) || '';
        return {
          canvasCount: canvases.length,
          infoText: infoEl?.textContent || '',
          firstPageMs: Number(infoEl?.dataset?.firstPageMs || -1),
          statusText,
        };
      })()`);
      if (pdfInfo.canvasCount >= 1 && pdfInfo.firstPageMs >= 0) break;
    }

    const p3b = path.join(outDir, '03b-night-spotlight-v2-evidence-pdf.png');
    await cdp.screenshot(p3b);
    receipt.screenshots.push(path.basename(p3b));

    // Inert HTML attachment XSS safety probe
    await cdp.evalJs(`(() => {
      window.__JOSH_ROOM_VIEW_DEBUG__.selectItem('item-0001', true, true);
      window.__JOSH_ROOM_VIEW_DEBUG__.openEvidenceTab();
      const htmlBtn = Array.from(document.querySelectorAll('#tab-evidence .att-pill')).find((b) => b.dataset.attachmentName === 'comparison.html');
      if (htmlBtn) htmlBtn.click();
    })()`);
    await sleep(280);
    const htmlSafety = await cdp.evalJs(`(() => ({
      xssExecuted: typeof window.__JOSH_ROOM_HTML_ATTACHMENT_XSS__ !== 'undefined',
      inertBadge: document.querySelector('#tab-evidence .inert-badge')?.textContent || '',
      tableRows: document.querySelectorAll('#tab-evidence .inert-table tr').length,
    }))()`);
    receipt.security = {
      html_attachment_script_executed: htmlSafety.xssExecuted,
      inert_report_badge: htmlSafety.inertBadge,
      inert_table_rows: htmlSafety.tableRows,
      pass: htmlSafety.xssExecuted === false && htmlSafety.tableRows >= 3,
    };

    const sqlRes = await cdp.evalJs(`window.__JOSH_ROOM_VIEW_DEBUG__.runSql()`);
    const sqlMutationRes = await cdp.evalJs(`window.__JOSH_ROOM_VIEW_DEBUG__.runSql("DROP TABLE items;")`);

    // Reduced motion capture
    await cdp.send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
    });
    await cdp.evalJs(`(() => {
      window.__JOSH_ROOM_VIEW_DEBUG__.setTheme('night');
      window.__JOSH_ROOM_VIEW_DEBUG__.setSpotlightMode('v2');
      window.__JOSH_ROOM_VIEW_DEBUG__.setReducedMotion(true);
      window.__JOSH_ROOM_VIEW_DEBUG__.selectItem(${JSON.stringify(targetFailId)}, false, true);
    })()`);
    await sleep(180);
    const p9 = path.join(outDir, '09-night-reduced-motion.png');
    await cdp.screenshot(p9);
    receipt.screenshots.push(path.basename(p9));

    // Keyboard ] shortcut sync check
    await cdp.evalJs(`(() => {
      window.__JOSH_ROOM_VIEW_DEBUG__.frameAll(true);
      document.dispatchEvent(new KeyboardEvent('keydown', { key: ']', bubbles: true }));
    })()`);
    await sleep(150);
    const afterNextFail = await cdp.evalJs(`(() => ({
      currentNodeId: window.__JOSH_ROOM_VIEW_DEBUG__.currentNodeId,
      selectedTreeNode: document.querySelector('#aria-tree-root [role="treeitem"][aria-selected="true"]')?.getAttribute('data-node-id') || null,
      liveText: document.getElementById('aria-live-region')?.textContent || '',
    }))()`);

    // Scale checks: 10 & 540 items
    let perf540 = null;
    if (html10) {
      await cdp.send('Page.navigate', { url: pathToFileURL(html10).href });
      await waitForAppBoot(cdp);
      await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.settle()');
      await sleep(180);
      const p10 = path.join(outDir, '11-scale-10-items.png');
      await cdp.screenshot(p10);
      receipt.screenshots.push(path.basename(p10));
    }
    if (html540) {
      const start540 = Date.now();
      await cdp.send('Page.navigate', { url: pathToFileURL(html540).href });
      await waitForAppBoot(cdp);
      const boot540Ms = Date.now() - start540;
      await cdp.evalJs('window.__JOSH_ROOM_VIEW_DEBUG__.settle()');
      await sleep(180);
      perf540 = await cdp.evalJs(`(() => ({
        itemCount: window.__JOSH_ROOM_VIEW_DEBUG__.itemCount,
        firstFrameMs: window.__JOSH_ROOM_VIEW_DEBUG__.firstFrameMs,
        drawCalls: window.__JOSH_ROOM_VIEW_DEBUG__.drawCalls,
        layoutSummary: window.__JOSH_ROOM_VIEW_DEBUG__.layoutSummary,
        wallBootMs: ${boot540Ms},
      }))()`);
      const p540 = path.join(outDir, '12-scale-540-items.png');
      await cdp.screenshot(p540);
      receipt.screenshots.push(path.basename(p540));
    }

    receipt.zero_network = {
      total_observed_requests: networkRequests.length,
      blocked_external_requests: blockedExternalRequests,
      csp_violations: cspViolations,
      console_errors: consoleErrors,
      pass: blockedExternalRequests.length === 0 && cspViolations.length === 0 && consoleErrors.length === 0,
    };

    receipt.performance = {
      fixture_300: {
        ...perf300,
        wallBootMs,
        file_bytes: fs.statSync(html300).size,
        first_click_ms: firstClickMs,
        shadow_updates_on_select: shadowAfterSelect - shadowBeforeSelect,
        pdf_first_page: pdfInfo,
        sql_preset_rows: sqlRes.rowCount,
        sql_mutation_blocked: sqlMutationRes.ok === false,
      },
      fixture_540: perf540,
    };

    receipt.accessibility = {
      canvas_aria_hidden: perf300.canvasAriaHidden === 'true',
      tree_role: perf300.treeRole,
      tree_items_rendered: perf300.treeItemCount,
      live_region_politeness: perf300.livePolite,
      live_announcement_sample: liveAnnouncement,
      keyboard_next_fail_synced_tree: afterNextFail.currentNodeId === afterNextFail.selectedTreeNode,
      after_next_fail: afterNextFail,
    };
  } finally {
    await cdp.close();
  }

  // --- WebGL disabled 2D Grid fallback verification ---
  const cdpNoGl = await launchChrome({ chromePath, disableWebgl: true });
  try {
    await cdpNoGl.send('Page.navigate', { url: pathToFileURL(htmlUnix || html300).href });
    await waitForAppBoot(cdpNoGl);
    await sleep(300);
    const fb = await cdpNoGl.evalJs(`(() => {
      const d = window.__JOSH_ROOM_VIEW_DEBUG__;
      const sqlOut = d.runSql('SELECT COUNT(*) AS n FROM items');
      d.selectItem(${JSON.stringify(htmlUnix ? 'file-src-scheduler' : 'item-0001')}, true, true);
      return {
        webglActive: d.webglActive,
        viewMode: d.viewMode,
        gridVisible: !document.getElementById('grid-view')?.hidden,
        noticeVisible: !document.getElementById('webgl-fallback-notice')?.hidden,
        tileCount: document.querySelectorAll('.grid-tile').length,
        filterChipCount: document.querySelectorAll('#grid-filters .filter-chip').length,
        sqlRowCount: sqlOut.rowCount,
        inspectorOpen: !document.getElementById('inspector-drawer')?.hidden,
      };
    })()`);
    await sleep(220);
    const pFb = path.join(outDir, '13-webgl-off-grid-fallback.png');
    await cdpNoGl.screenshot(pFb);
    receipt.screenshots.push(path.basename(pFb));
    receipt.fallback_webgl_off = {
      ...fb,
      pass: (
        fb.webglActive === false &&
        fb.viewMode === 'grid' &&
        fb.gridVisible &&
        fb.tileCount > 0 &&
        fb.filterChipCount >= 4 &&
        fb.sqlRowCount === 1 &&
        fb.inspectorOpen
      ),
    };
  } finally {
    await cdpNoGl.close();
  }

  const receiptPath = path.join(outDir, 'browser-verification-receipt.json');
  fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify(receipt, null, 2));

  const allPassed =
    receipt.zero_network.pass &&
    receipt.render_on_demand.pass &&
    receipt.security.pass &&
    receipt.fallback_webgl_off.pass &&
    (!htmlUnix || receipt.unix_navigate_inspect.pass) &&
    (!htmlUnix || receipt.pr_review_regressions.pass) &&
    receipt.performance.fixture_300.webglActive === true &&
    receipt.performance.fixture_300.pdf_first_page.canvasCount >= 1 &&
    receipt.performance.fixture_300.sql_preset_rows > 0 &&
    receipt.performance.fixture_300.sql_mutation_blocked === true &&
    receipt.accessibility.canvas_aria_hidden === true &&
    receipt.accessibility.keyboard_next_fail_synced_tree === true;

  if (!allPassed) {
    process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
