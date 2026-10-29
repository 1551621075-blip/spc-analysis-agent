#!/usr/bin/env node
/** Actual installed Chrome CDP + SPC API/DOM; SyntheticProvider TEST-ONLY, no model.
 * Safe default: --help/import/self-test never launches a browser or creates files.
 * Explicit run: node scripts/browser_smoke.mjs --run-chrome --artifacts-parent /private/tmp
 * A fresh owned directory OUTSIDE the repository holds the temporary app/profile. Only a
 * sanitized summary remains there after cleanup; no screenshots, response dumps or tokens.
 * Credentials cross only the private child stdout pipe and Chrome memory/Authorization.
 */
import assert from 'node:assert/strict';
import {spawn, execFile} from 'node:child_process';
import {constants} from 'node:fs';
import {access, lstat, mkdtemp, open, readFile, readdir, realpath, rm, writeFile} from 'node:fs/promises';
import {get as httpGet} from 'node:http';
import {dirname, isAbsolute, join, relative, resolve} from 'node:path';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

export const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export const SELECTORS = Object.freeze({
  token: '#token-input', login: '#login-button', logout: '#logout-button', workspace: '#workspace',
  baselineDataset: '#baseline-dataset-select', baselineCreate: '#baseline-create-button',
  baselineDecision: '#baseline-decision', baselineNote: '#baseline-review-note',
  baselineConfirm: '#baseline-review-confirm', baselineReview: '#baseline-review-button',
  approvedBaseline: '#evaluation-baseline-select', monitorDataset: '#monitor-dataset-select',
  evaluate: '#evaluate-button', charts: '#analysis-content .chart-panel svg',
  risk: '#risk-select', investigationCreate: '#investigation-create-button',
  investigationRun: '#investigation-run-button', authority: '.authority-panel', draft: '.draft-panel',
  proposalCreate: '#proposal-create-button', proposalReview: '#proposal-review-button',
  proposalExecute: '#proposal-execute-button', proposalFollowup: '#proposal-followup-button',
  proposalClose: '#proposal-close-button', refresh: '#refresh-button',
});
const CHECKS = [
  'isolated_installed_chrome_and_TEST_server', 'anonymous_and_invalid_login',
  'login_real_identity_memory_only_credentials', 'baseline_draft_not_approved_automatically',
  'explicit_baseline_review_bound_to_hash', 'normal_real_statistics_and_Xbar_R_SVG',
  'mean_shift_real_risk_and_Xbar_hits', 'variance_real_R_hits', 'missing_values_break_both_SVG_series',
  'investigation_create_is_not_run', 'explicit_TEST_investigation_authority_separate_from_draft',
  'independent_human_proposal_not_model_autofill', 'separate_proposal_review_without_execution',
  'separate_manual_execution_attestation', 'independent_followup_without_auto_close',
  'explicit_human_closure_exact_followup_hash', 'real_lifecycle_audit_and_literal_text_safety',
  'logout_rejects_late_real_API_response', 'reload_forgets_identity',
  'no_visible_raw_JSON_dump_or_persistent_browser_credentials',
  'security_headers_and_no_external_page_requests', 'owned_process_cleanup',
  'no_plaintext_tokens_in_owned_files', 'owned_runtime_directory_cleanup',
];
export const USAGE = `SPC TEST-ONLY browser smoke (Node 22+, installed macOS Chrome).
Usage:
  node scripts/browser_smoke.mjs --help
  node scripts/browser_smoke.mjs --self-test
  node scripts/browser_smoke.mjs --run-chrome --artifacts-parent /absolute/existing/outside/repo
      [--chrome /absolute/Chrome] [--python /absolute/python]
No browser starts without --run-chrome. No model, downloads, real profile, ambient credentials,
server URL, production data, automatic approval/execution/closure, screenshots or raw API dumps.
A new owned directory is created under artifacts-parent; only summary.json is retained.
`;

export function parseArgs(args) {
  if (args.length === 0 || (args.length === 1 && args[0] === '--help')) return {mode: 'help'};
  if (args.length === 1 && args[0] === '--self-test') return {mode: 'self-test'};
  const options = {mode: 'run', chrome: DEFAULT_CHROME, python: join(PROJECT, '.venv/bin/python')};
  const seen = new Set();
  for (let i = 0; i < args.length; i++) {
    const flag = args[i];
    assert(!seen.has(flag), 'Duplicate argument'); seen.add(flag);
    if (flag === '--run-chrome') { options.authorized = true; continue; }
    assert(['--artifacts-parent', '--chrome', '--python'].includes(flag), 'Unknown argument');
    const value = args[++i];
    assert(typeof value === 'string' && isAbsolute(value) && !/[\x00-\x1f\x7f]/.test(value),
      'An explicit absolute path is required');
    options[{'--artifacts-parent': 'parent', '--chrome': 'chrome', '--python': 'python'}[flag]] = value;
  }
  assert(options.authorized && options.parent, '--run-chrome AND --artifacts-parent are required');
  assert(!isWithin(PROJECT, resolve(options.parent)), 'Artifacts must be outside the repository');
  return options;
}
export function isWithin(parent, child) {
  const path = relative(parent, child);
  return path === '' || (!path.startsWith('../') && path !== '..' && !isAbsolute(path));
}
export function seriesRuns(points, field) {
  const runs = []; let run = [];
  const flush = () => { if (run.length > 1) runs.push(run); run = []; };
  points.forEach((point, i) => {
    if (point.valid === false || !Number.isFinite(point[field])) flush();
    else { if (point.break_before === true) flush(); run.push(i); }
  });
  flush(); return runs;
}

// This small wrapper seeds ORIGINAL SYNTHETIC INPUTS only, through the shipped CLI/service.
// It does not seed baselines, approvals, evaluations, risks, investigations or lifecycle state.
export const PY_SERVER = String.raw`
import asyncio, io, json, socket, sys, threading
from contextlib import redirect_stdout
from pathlib import Path
import uvicorn
from spc_agent.api import create_app
from spc_agent.cli import initialize
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.provider import SyntheticProvider

channel = sys.stdout
sys.stdout = sys.stderr
write_lock = threading.Lock()
def send(value):
    with write_lock:
        channel.write(json.dumps(value, ensure_ascii=True) + '\n')
        channel.flush()
try:
    data_root = Path(sys.argv[1])
    capture = io.StringIO()
    with redirect_stdout(capture):
        initialize(data_root)
    setup = json.loads(capture.getvalue())
    capture.close()
    token = setup.pop('token')
    app = create_app(data_root, SyntheticProvider())
    actor = app.state.store.authenticate(token)
    original = demo_datasets()
    risk_end = max(group.timestamp_ms for group in original['mean_shift'].groups)
    execution_ms = risk_end + 1000
    # Reuse the independent, later followup already imported by the shipped CLI.
    # Never generate browser-specific measurements or search for a passing result.
    gate = {'armed': False, 'held': False, 'released': 0}
    release = threading.Event()
    class DelayedResponse:
        async def __call__(self, scope, receive, reply):
            hold = (scope['type'] == 'http' and scope['method'] == 'GET'
                    and scope['path'] == '/api/datasets' and gate['armed'])
            if hold:
                gate['armed'] = False
            async def send_response(message):
                if hold and message['type'] == 'http.response.start':
                    gate['held'] = True
                    await asyncio.to_thread(release.wait, 15)
                    gate['held'] = False
                    gate['released'] += 1
                await reply(message)
            await app(scope, receive, send_response)
    server = uvicorn.Server(uvicorn.Config(DelayedResponse(), host='127.0.0.1',
        log_level='critical', access_log=False, log_config=None,
        server_header=False, proxy_headers=False))
    def control():
        for line in sys.stdin:
            msg = {}
            try:
                msg = json.loads(line)
                command = msg['command']
                if command == 'hold_datasets':
                    release.clear(); gate['armed'] = True; value = {'armed': True}
                elif command == 'release_datasets':
                    release.set(); value = {'released': True}
                elif command == 'state':
                    value = dict(gate)
                elif command == 'shutdown':
                    release.set(); server.should_exit = True; value = {'stopping': True}
                else:
                    raise ValueError('unsupported control')
                send({'id': msg['id'], 'value': value})
            except Exception:
                send({'id': msg.get('id'), 'error': 'fixture_control_failed'})
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0)); sock.listen(128)
    threading.Thread(target=control, daemon=True).start()
    send({'type': 'ready', 'port': sock.getsockname()[1], 'token': token,
          'actor_id': actor.actor_id, 'provider': {'testOnly': True},
          'datasets': {'baseline': baseline_dataset().dataset_id,
                       **{key: value.dataset_id for key, value in original.items()}},
          'execution_ms': execution_ms})
    server.run(sockets=[sock])
except Exception as exc:
    send({'type': 'failed', 'code': getattr(exc, 'code', type(exc).__name__)})
    raise SystemExit(1) from None
`;

class CDP {
  constructor(socket, fault) {
    this.socket = socket; this.next = 0; this.pending = new Map(); this.listeners = new Set();
    socket.addEventListener('message', event => {
      let value; try { value = JSON.parse(event.data); } catch { this.close(); return; }
      const pending = this.pending.get(value.id);
      if (pending) {
        clearTimeout(pending.timer); this.pending.delete(value.id);
        if (value.error) pending.reject(new Error(`CDP ${pending.method} failed`));
        else pending.resolve(value.result);
      } else for (const listener of this.listeners) Promise.resolve(listener(value)).catch(() => fault('CDP observer failed'));
    });
    socket.addEventListener('close', () => this.close());
    socket.addEventListener('error', () => this.close());
  }
  send(method, params = {}, sessionId) {
    assert(this.socket.readyState === WebSocket.OPEN, 'CDP unavailable');
    return new Promise((resolvePromise, reject) => {
      const id = ++this.next;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP ${method} timeout`)); }, 12000);
      this.pending.set(id, {resolve: resolvePromise, reject, timer, method});
      this.socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  close() {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer); pending.reject(new Error('CDP closed'));
    }
    this.pending.clear();
    if (this.socket.readyState === WebSocket.OPEN) this.socket.close();
  }
}
const delay = ms => new Promise(resolvePromise => setTimeout(resolvePromise, ms));
async function deadline(promise, ms, label) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), ms);
  })]); } finally { clearTimeout(timer); }
}

export class Smoke {
  constructor(options) {
    this.options = options; this.started = Date.now(); this.children = []; this.secrets = [];
    this.results = []; this.responses = []; this.requests = new Map(); this.controls = new Map();
    this.faults = []; this.external = new Set(); this.sequence = 0; this.stopping = false;
    this.owned = new Map(); this.knownProcesses = new Map();
  }
  safe(value) {
    let text = String(value);
    for (const secret of this.secrets) text = text.split(secret).join('[REDACTED]');
    // AssertionError may append entire compared objects; retain only its human headline.
    return text.split(/\r?\n/, 1)[0].slice(0, 800);
  }
  record(name, status, detail) {
    this.results.push({name, status, ...(detail ? {detail: this.safe(detail)} : {})});
  }
  async check(name, work) {
    await work();
    await this.snapshotOwnedProcesses();
    this.record(name, 'pass');
  }
  async until(work, label, timeout = 12000) {
    const end = Math.min(Date.now() + timeout, this.started + 240000);
    while (!this.stopping && Date.now() < end) {
      const result = await work(); if (result) return result;
      await delay(40);
    }
    throw new Error(`Timed out: ${label}`);
  }
  environment() {
    return {PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: this.runtime, TMPDIR: this.runtime,
      LANG: 'en_US.UTF-8', PYTHONPATH: join(PROJECT, 'src'),
      PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1'};
  }
  launch(executable, args, stdio) {
    assert(!this.stopping, 'No child launches during cleanup');
    const child = spawn(executable, args, {cwd: this.runtime, env: this.environment(), detached: true, stdio});
    this.children.push(child);
    child.on('error', () => { child.launchFailed = true; });
    child.stdin?.on('error', () => { this.faults.push('Owned child control pipe closed'); });
    child.stderr?.resume();
    return child;
  }
  async remember(path) {
    const info = await lstat(path);
    assert(info.isDirectory() && !info.isSymbolicLink(), 'Owned path must be a directory');
    this.owned.set(path, {dev: info.dev, ino: info.ino});
  }
  async ownedDirectory(path) {
    const info = await lstat(path); const owned = this.owned.get(path);
    assert(owned && info.isDirectory() && !info.isSymbolicLink() && owned.dev === info.dev && owned.ino === info.ino,
      'Owned directory identity changed; refusing recursive cleanup');
  }
  async start() {
    assert(process.platform === 'darwin', 'Installed macOS Chrome is required');
    assert(typeof WebSocket === 'function', 'Node built-in WebSocket required (Node 22+)');
    const parent = await realpath(this.options.parent);
    assert((await lstat(parent)).isDirectory() && !isWithin(await realpath(PROJECT), parent),
      'Artifacts parent must be an existing directory outside this repository');
    await Promise.all([this.options.chrome, this.options.python].map(path => access(path, constants.X_OK)));
    this.root = await mkdtemp(join(parent, 'spc-browser-'));
    await this.remember(this.root);
    this.runtime = await mkdtemp(join(this.root, 'owned-runtime-'));
    await this.remember(this.runtime);
    this.server = this.launch(this.options.python, ['-u', '-c', PY_SERVER, join(this.runtime, 'app')], ['pipe', 'pipe', 'ignore']);
    this.bootstrap = await deadline(new Promise((resolvePromise, reject) => {
      createInterface({input: this.server.stdout}).on('line', line => {
        let value; try { value = JSON.parse(line); } catch { reject(new Error('Invalid private setup protocol')); return; }
        if (value.type === 'ready') {
          this.secrets.push(value.token); resolvePromise(value); return;
        }
        if (value.type === 'failed') { reject(new Error('TEST app bootstrap failed (diagnostics withheld)')); return; }
        const pending = this.controls.get(value.id);
        if (pending) { clearTimeout(pending.timer); this.controls.delete(value.id);
          if (value.error) pending.reject(new Error('TEST control failed')); else pending.resolve(value.value); }
      });
      this.server.once('exit', () => reject(new Error('TEST server exited before readiness')));
      this.server.once('error', () => reject(new Error('Project Python launch failed')));
    }), 30000, 'TEST app startup deadline');
    assert(Number.isInteger(this.bootstrap.port) && this.bootstrap.port > 0 && this.bootstrap.port <= 65535,
      'Invalid owned server port');
    await this.snapshotOwnedProcesses();
    this.origin = `http://127.0.0.1:${this.bootstrap.port}`;
    const profile = join(this.runtime, 'chrome-profile');
    this.chrome = this.launch(this.options.chrome, [
      '--headless=new', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
      `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-default-apps', '--disable-sync',
      '--disable-breakpad', '--disable-crash-reporter', '--disable-domain-reliability',
      '--safebrowsing-disable-auto-update', '--disable-client-side-phishing-detection',
      '--metrics-recording-only', '--password-store=basic', '--use-mock-keychain',
      '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter,Translate,PasswordLeakDetection',
      '--window-size=1440,1100', 'about:blank',
    ], ['ignore', 'ignore', 'ignore']);
    const active = await this.until(async () => {
      assert(this.chrome.exitCode === null && !this.chrome.launchFailed, 'Owned Chrome exited before readiness');
      try {
        const lines = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).trim().split('\n');
        return /^\d+$/.test(lines[0]) && Number(lines[0]) <= 65535 &&
          /^\/devtools\/browser\/[A-Za-z0-9-]+$/.test(lines[1] || '') ? lines : null;
      } catch { return null; }
    }, 'isolated DevToolsActivePort', 20000);
    const socket = new WebSocket(`ws://127.0.0.1:${active[0]}${active[1]}`);
    await deadline(new Promise((resolvePromise, reject) => {
      socket.addEventListener('open', resolvePromise, {once: true});
      socket.addEventListener('error', () => reject(new Error('Owned Chrome CDP failed')), {once: true});
    }), 10000, 'CDP connection deadline');
    this.cdp = new CDP(socket, fault => this.faults.push(fault));
    const version = await this.cdp.send('Browser.getVersion');
    assert(/Chrome\//.test(version.product), 'Actual Chrome product was not reported');
    const target = await this.cdp.send('Target.createTarget', {url: 'about:blank'});
    this.sessionId = (await this.cdp.send('Target.attachToTarget', {targetId: target.targetId, flatten: true})).sessionId;
    this.cdp.listeners.add(event => this.observe(event));
    await this.cmd('Page.enable'); await this.cmd('Runtime.enable'); await this.cmd('Network.enable');
    const healthResponse = await this.until(async () => {
      assert(this.server.exitCode === null && !this.server.launchFailed, 'Owned TEST server exited');
      return new Promise(resolvePromise => {
        // Bare numeric-loopback HTTP: no proxy env, redirects, retries or caller URL.
        const request = httpGet({hostname: '127.0.0.1', port: this.bootstrap.port, path: '/health',
          agent: false, timeout: 1000}, response => {
          let raw = ''; response.setEncoding('utf8');
          response.on('data', data => { raw += data; if (raw.length > 4096) request.destroy(); });
          response.on('end', () => {
            try {
              const body = JSON.parse(raw);
              resolvePromise(response.statusCode === 200 && body.testOnly === true && body.mode === 'synthetic-test' ? body : null);
            } catch { resolvePromise(null); }
          });
          response.on('error', () => resolvePromise(null));
        });
        request.on('timeout', () => request.destroy());
        request.on('error', () => resolvePromise(null));
      });
    }, 'TEST server HTTP readiness');
    assert(healthResponse.inferenceChecked === false, 'Smoke must not run inference health checks');
    await this.cmd('Page.navigate', {url: this.origin});
    await this.until(() => this.evaluate(`document.readyState === 'complete' && !!document.querySelector('#login-form')`), 'SPC login DOM');
    const health = await this.probe('/health');
    assert(health.status === 200 && health.body.testOnly === true && health.body.mode === 'synthetic-test' &&
      health.body.inferenceChecked === false, 'Smoke server is not explicitly TEST-ONLY');
    await this.snapshotOwnedProcesses();
    this.record('isolated_installed_chrome_and_TEST_server', 'pass', version.product);
  }
  cmd(method, params = {}) { return this.cdp.send(method, params, this.sessionId); }
  async evaluate(expression) {
    const value = await this.cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    assert(!value.exceptionDetails, 'Browser evaluation failed (payload withheld)');
    return value.result?.value;
  }
  async observe(event) {
    if (event.sessionId !== this.sessionId) return;
    const p = event.params;
    if (event.method === 'Runtime.exceptionThrown') this.faults.push('Uncaught browser exception');
    if (event.method === 'Runtime.consoleAPICalled' && this.secrets.some(secret => JSON.stringify(p.args).includes(secret)))
      this.faults.push('Credential in browser console');
    if (event.method === 'Page.javascriptDialogOpening') {
      this.faults.push('Unexpected browser dialog'); await this.cmd('Page.handleJavaScriptDialog', {accept: false});
    }
    if (event.method === 'Network.requestWillBeSent') {
      const url = new URL(p.request.url);
      if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) && url.origin !== this.origin)
        this.external.add(url.origin);
      if (this.secrets.some(secret => p.request.url.includes(secret) || (p.request.postData || '').includes(secret)))
        this.faults.push('Credential in URL/request body');
      // Authorization headers are intentionally NEVER retained.
      this.requests.set(p.requestId, {path: url.pathname, method: p.request.method, rawRequest: p.request.postData});
    }
    if (event.method === 'Network.responseReceived') {
      const request = this.requests.get(p.requestId);
      if (request) this.responses.push({...request, requestId: p.requestId, status: p.response.status,
        headers: p.response.headers, cached: Boolean(p.response.fromDiskCache || p.response.fromServiceWorker), done: false});
    }
    if (event.method === 'Network.loadingFinished') {
      const entry = this.responses.find(row => row.requestId === p.requestId && !row.done);
      if (!entry) return;
      if (entry.path.startsWith('/api/') || entry.path === '/health') {
        try {
          const result = await this.cmd('Network.getResponseBody', {requestId: p.requestId});
          const raw = result.base64Encoded ? Buffer.from(result.body, 'base64').toString('utf8') : result.body;
          assert(raw.length < 2_000_000, 'API response exceeds smoke bound');
          if (this.secrets.some(secret => raw.includes(secret))) this.faults.push('Credential in API response');
          entry.body = JSON.parse(raw);
        } catch { entry.body = null; }
      }
      entry.done = true;
    }
  }
  async click(selector) {
    const point = await this.evaluate(`(() => {
      const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled || e.closest('[hidden]')) return null;
      e.scrollIntoView({block:'center',behavior:'instant'}); const r = e.getBoundingClientRect();
      return r.width && r.height ? {x:r.x+r.width/2,y:r.y+r.height/2} : null;
    })()`);
    assert(point, `Not a visible enabled click target: ${selector}`);
    await this.cmd('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...point});
    await this.cmd('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...point});
  }
  async fill(selector, value) {
    assert(await this.evaluate(`(() => {const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled || e.closest('[hidden]')) return false;
      e.scrollIntoView({block:'center',behavior:'instant'}); e.focus(); e.select(); return true;})()`), `Missing input: ${selector}`);
    await this.cmd('Input.insertText', {text: String(value)});
  }
  async choose(selector, value) {
    const state = await this.evaluate(`(() => {const e = document.querySelector(${JSON.stringify(selector)});
      if (!e) return 'missing select';
      if (e.disabled) return 'disabled select';
      if (![...e.options].some(o => o.value === ${JSON.stringify(value)})) return 'missing option';
      e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event('change',{bubbles:true})); return 'selected';
    })()`);
    assert.equal(state, 'selected', `Cannot choose ${selector}: ${state}`);
  }
  async idle() {
    await this.until(() => this.evaluate(`document.querySelector('#workspace')?.getAttribute('aria-busy') === 'false'`), 'UI idle');
  }
  async responseAfter(mark, path, method = 'POST') {
    return this.until(() => this.responses.slice(mark).find(row => row.path === path && row.method === method && row.done),
      `${method} ${path}`, 20000);
  }
  async submit(selector, path, method = 'POST') {
    const mark = this.responses.length;
    await this.click(selector);
    const response = await this.responseAfter(mark, path, method); await this.idle();
    assert(response.status === 200 && response.body, `UI operation failed: ${method} ${path} HTTP ${response.status}`);
    return response;
  }
  async probe(path, authenticated = false) {
    assert(path.startsWith('/') && !path.startsWith('//'), 'Only same-origin probes');
    const headers = authenticated ? {Authorization: `Bearer ${this.bootstrap.token}`} : {};
    return this.evaluate(`(async () => {const r = await fetch(${JSON.stringify(path)}, {
      headers:${JSON.stringify(headers)}, credentials:'omit', cache:'no-store', mode:'same-origin', redirect:'error'});
      return {status:r.status, body:await r.json()};})()`);
  }
  async control(command) {
    const id = ++this.sequence;
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => { this.controls.delete(id); reject(new Error('TEST control deadline')); }, 5000);
      this.controls.set(id, {resolve: resolvePromise, reject, timer});
      this.server.stdin.write(JSON.stringify({id, command}) + '\n');
    });
  }
  count(path) { return [...this.requests.values()].filter(row => row.method === 'POST' && row.path === path).length; }
  async disabled(selector) { return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.disabled === true`); }
  async text(selector) { return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent || ''`); }
  async unchecked(selector) { return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.checked === false`); }
  requestBody(response, keys) {
    const body = JSON.parse(response.rawRequest);
    assert.deepEqual(Object.keys(body).sort(), keys.sort(), 'UI request contains missing/extra/automatic flags');
    return body;
  }
  async login() {
    await this.fill(SELECTORS.token, this.bootstrap.token);
    const result = (await this.submit(SELECTORS.login, '/api/me', 'GET')).body;
    assert.equal(result.actor.actor_id, this.bootstrap.actor_id);
    assert(result.provider.testOnly === true, 'Authenticated provider is not TEST');
    assert((await this.text('#mode-badge')).includes('TEST'), 'TEST disclosure missing');
    assert(await this.evaluate(`!document.querySelector('#workspace').hidden && document.querySelector('#token-input').value === ''`),
      'Login did not clear input and display workspace');
    await this.storageCheck();
  }
  async storageCheck() {
    const storage = await this.evaluate(`(async () => ({local:localStorage.length, session:sessionStorage.length,
      cookies:document.cookie, databases:await indexedDB.databases(), caches:await caches.keys(),
      registrations:(await navigator.serviceWorker.getRegistrations()).length,
      tokenInput:document.querySelector('#token-input').value}))()`);
    assert(storage.local === 0 && storage.session === 0 && storage.cookies === '' &&
      storage.databases.length === 0 && storage.caches.length === 0 && storage.registrations === 0 && storage.tokenInput === '',
    'Browser persisted credentials or retained token input');
    const browserCookies = await this.cmd('Network.getCookies', {urls: [this.origin]});
    assert(browserCookies.cookies.length === 0, 'Browser cookie storage is not empty');
    const dom = await this.evaluate('document.documentElement.outerHTML');
    assert(!this.secrets.some(secret => dom.includes(secret)), 'Credential rendered into DOM');
  }
  async noRawDump() {
    assert(await this.evaluate(`![...document.querySelectorAll('#workspace pre')].some(e =>
      !e.closest('[hidden]') && !e.closest('details:not([open])') && e.getClientRects().length > 0)`),
    'A raw JSON dump is visible instead of the structured UI');
  }
  async chartCheck(evaluation, baseline, expectMissing = false) {
    const charts = await this.evaluate(`([...document.querySelectorAll(${JSON.stringify(SELECTORS.charts)})].map(svg => ({
      title:svg.querySelector('title')?.textContent, role:svg.getAttribute('role'),
      dots:[...svg.querySelectorAll('.chart-point')].map(e => ({title:e.querySelector('title')?.textContent,
        hit:e.classList.contains('hit'), x:Number(e.getAttribute('cx')), y:Number(e.getAttribute('cy'))})),
      runs:[...svg.querySelectorAll('.chart-series')].map(e => e.getAttribute('points').trim().split(/\\s+/).map(pair => pair.split(',').map(Number))),
      limits:[...svg.querySelectorAll('.chart-center,.chart-limit')].map(e => ({y1:Number(e.getAttribute('y1')),y2:Number(e.getAttribute('y2'))})),
      missing:svg.querySelectorAll('.chart-missing').length,
      facts:[...svg.closest('.chart-panel').querySelectorAll('.facts dd')].map(e => e.textContent)
    })))`);
    assert.equal(charts.length, 2, 'Both real SVG charts required');
    const points = evaluation.analysis.points;
    const allHits = [...evaluation.analysis.rule_hits, ...(evaluation.analysis.range_hits || [])];
    for (const [index, field, kind, limits] of [
      [0, 'mean', 'xbar', [baseline.payload.xbar_lcl, baseline.payload.center, baseline.payload.xbar_ucl]],
      [1, 'range', 'r', [baseline.payload.r_lcl, baseline.payload.rbar, baseline.payload.r_ucl]],
    ]) {
      const chart = charts[index];
      assert(chart.role === 'img' && chart.title, 'Chart accessibility labels missing');
      const valid = points.filter(point => point.valid !== false && Number.isFinite(point[field]));
      const domain = [...valid.map(point => point[field]), ...limits];
      const low = Math.min(...domain), high = Math.max(...domain);
      const padding = high === low ? Math.max(Math.abs(high) * 0.01, 0.001) : (high - low) * 0.14;
      const x = i => 95 + 800 * (points.length === 1 ? 0.5 : i / (points.length - 1));
      const y = value => 22 + (high + padding - value) / (high - low + 2 * padding) * 216;
      assert.equal(chart.limits.length, 3, 'Control-limit SVG lines are missing');
      [...limits].reverse().forEach((limit, i) => assert(Math.abs(chart.limits[i].y1 - y(limit)) < 1e-8 &&
        chart.limits[i].y1 === chart.limits[i].y2, 'SVG control limit disagrees with frozen baseline'));
      assert.equal(chart.dots.length, valid.length, 'Rendered point count differs from server');
      valid.forEach((point, i) => {
        assert(Math.abs(chart.dots[i].x - x(points.indexOf(point))) < 1e-8 && Math.abs(chart.dots[i].y - y(point[field])) < 1e-8,
          'SVG dot position disagrees with server statistics');
        assert(chart.dots[i].title.includes(`${field}: ${point[field]}`) && chart.dots[i].title.includes(point.group_id),
          'SVG value or group identity differs from actual statistics');
        const hit = allHits.some(h => (h.chart || (h.rule === 'R_LIMIT' ? 'r' : 'xbar')) === kind &&
          point.index >= h.start_index && point.index <= h.end_index);
        assert.equal(chart.dots[i].hit, hit, 'SVG hit does not match server rule windows');
      });
      assert.deepEqual(chart.facts, limits.map(String), 'Control limits not the frozen baseline values');
      const runs = seriesRuns(points, field);
      assert.deepEqual(chart.runs.map(run => run.length), runs.map(run => run.length), 'Missing/break point joined across an SVG series');
      runs.forEach((run, r) => run.forEach((i, j) => {
        assert(Math.abs(chart.runs[r][j][0] - x(i)) < 1e-8 && Math.abs(chart.runs[r][j][1] - y(points[i][field])) < 1e-8,
          'SVG series bridged a missing subgroup or changed a statistic');
      }));
      assert.equal(chart.missing, 2 * (points.length - valid.length), 'Missing values were filled as zero');
      if (expectMissing) assert(chart.missing > 0 && chart.runs.length > 1, 'Missing case has no visible series break');
    }
    assert((await this.text('#analysis-content')).includes(evaluation.analysis.status), 'Analysis status not rendered');
    await this.noRawDump();
  }
  async evaluateCase(name, baseline) {
    await this.choose(SELECTORS.approvedBaseline, baseline.baseline_id);
    await this.choose(SELECTORS.monitorDataset, this.bootstrap.datasets[name]);
    const response = await this.submit(SELECTORS.evaluate, '/api/evaluations');
    const body = this.requestBody(response, ['action_id', 'dataset_id', 'baseline_id']);
    assert(body.dataset_id === this.bootstrap.datasets[name] && body.baseline_id === baseline.baseline_id);
    const result = response.body;
    assert.equal(result.analysis.baseline_hash, baseline.payload.baseline_hash);
    await this.chartCheck(result, baseline, name === 'missing');
    return result;
  }
  async cleared() {
    const result = await this.evaluate(`({hidden:document.querySelector('#workspace').hidden,
      loginHidden:document.querySelector('#login-section').hidden, actor:document.querySelector('#actor-label').textContent,
      values:[...document.querySelectorAll('input,textarea')].map(e => e.type === 'checkbox' ? e.checked : e.value),
      ids:['baseline-select','evaluation-select','risk-select','proposal-select'].map(id => [...document.getElementById(id).options].filter(o=>o.value).length),
      panels:['baseline-detail','analysis-content','risk-detail','investigation-detail','investigation-report','proposal-detail','audit-result'].map(id=>document.getElementById(id).textContent)})`);
    assert(result.hidden && !result.loginHidden && result.actor === '未登录', 'Logged-out identity revived');
    assert(result.values.every(value => value === '' || value === false) && result.ids.every(count => count === 0),
      'Logout retained sensitive inputs/objects');
    for (const id of [this.baseline?.baseline_id, this.shift?.risk_id, this.investigation?.investigation_id, this.proposal?.proposal_id].filter(Boolean))
      assert(!result.panels.some(panel => panel.includes(id)), 'Logout retained an old object in DOM');
    await this.storageCheck();
  }
  async run() {
    await this.start();
    await this.check('anonymous_and_invalid_login', async () => {
      assert.equal((await this.probe('/api/me')).status, 401);
      await this.fill(SELECTORS.token, 'browser-wrong-token');
      const mark = this.responses.length; await this.click(SELECTORS.login);
      assert.equal((await this.responseAfter(mark, '/api/me', 'GET')).status, 401); await this.idle();
      assert((await this.text('#notice')).includes('认证'), 'Invalid token error not shown');
      await this.cleared();
    });
    await this.check('login_real_identity_memory_only_credentials', () => this.login());
    await this.check('baseline_draft_not_approved_automatically', async () => {
      await this.choose(SELECTORS.baselineDataset, this.bootstrap.datasets.baseline);
      const response = await this.submit(SELECTORS.baselineCreate, '/api/baselines');
      this.baseline = response.body;
      this.requestBody(response, ['action_id', 'dataset_id']);
      assert(this.baseline.status === 'draft' && !this.baseline.review, 'Baseline auto-approved');
      assert(await this.unchecked(SELECTORS.baselineConfirm) && await this.disabled(SELECTORS.baselineReview));
      assert(await this.disabled(SELECTORS.evaluate), 'Draft is eligible for evaluation');
      assert.equal(this.count(`/api/baselines/${this.baseline.baseline_id}/review`), 0);
      assert.equal(this.count('/api/evaluations'), 0);
      assert((await this.text('#baseline-review-hash')).includes(this.baseline.content_hash));
    });
    const literal = '<img id="spc-smoke-literal" src="/smoke-never-image" onerror="window.__smokeXSS=1">';
    const note = `TEST-ONLY synthetic human review; no physical validation or production approval. ${literal}`;
    await this.check('explicit_baseline_review_bound_to_hash', async () => {
      await this.choose(SELECTORS.baselineDecision, 'approved');
      await this.fill(SELECTORS.baselineNote, note);
      assert(await this.disabled(SELECTORS.baselineReview), 'Baseline confirm is not required');
      await this.click(SELECTORS.baselineConfirm);
      const response = await this.submit(SELECTORS.baselineReview, `/api/baselines/${this.baseline.baseline_id}/review`);
      const posted = this.requestBody(response, ['action_id', 'content_hash', 'decision', 'note']);
      assert(posted.content_hash === this.baseline.content_hash && posted.decision === 'approved');
      this.baseline = response.body;
      assert(this.baseline.status === 'approved' && this.baseline.review.note === note);
      assert.equal(this.count('/api/evaluations'), 0);
      assert(await this.unchecked(SELECTORS.baselineConfirm), 'Baseline confirmation was not reset');
    });
    await this.check('normal_real_statistics_and_Xbar_R_SVG', async () => {
      const normal = await this.evaluateCase('normal', this.baseline);
      assert(normal.analysis.status === 'stable' && normal.risk_id === null);
    });
    await this.check('mean_shift_real_risk_and_Xbar_hits', async () => {
      this.shift = await this.evaluateCase('mean_shift', this.baseline);
      assert(this.shift.analysis.status === 'signals' && this.shift.risk_id && this.shift.analysis.rule_hits.length);
      assert((await this.text('#risk-detail')).includes(this.shift.risk_id));
      assert.equal(this.count('/api/investigations'), 0); assert.equal(this.count('/api/proposals'), 0);
    });
    await this.check('variance_real_R_hits', async () => {
      const variance = await this.evaluateCase('variance', this.baseline);
      assert((variance.analysis.range_hits || []).length > 0 || variance.analysis.rule_hits.some(hit => hit.chart === 'r'));
    });
    await this.check('missing_values_break_both_SVG_series', async () => {
      const missing = await this.evaluateCase('missing', this.baseline);
      assert(missing.analysis.status === 'not_evaluable' && missing.analysis.capability.status === 'not_evaluable');
      assert((await this.text('#analysis-content')).includes('不可评价'));
    });
    await this.choose(SELECTORS.risk, this.shift.risk_id);
    await this.check('investigation_create_is_not_run', async () => {
      this.investigation = (await this.submit(SELECTORS.investigationCreate, '/api/investigations')).body;
      assert(this.investigation.status === 'ready' && this.investigation.calls === 0 && !this.investigation.report);
      assert.equal(this.count(`/api/investigations/${this.investigation.investigation_id}/run`), 0);
      assert.equal(this.count('/api/proposals'), 0);
    });
    await this.check('explicit_TEST_investigation_authority_separate_from_draft', async () => {
      const response = await this.submit(SELECTORS.investigationRun, `/api/investigations/${this.investigation.investigation_id}/run`);
      this.requestBody(response, []); this.investigation = response.body;
      const report = this.investigation.report;
      assert(this.investigation.status === 'completed' && this.investigation.tokens > 0 && report);
      assert(report.review_status === 'unreviewed' && report.action_created === false && report.equipment_operation === false);
      assert.deepEqual(report.authoritative.risk.analysis, this.shift.analysis, 'Investigation changed deterministic facts');
      const sources = report.authoritative.evidence.map(item => item.source);
      assert(sources.includes('get_process_context') && sources.includes('get_measurement_context'));
      assert.deepEqual([...report.untrusted_model_draft.citations].sort(), report.authoritative.evidence.map(item => item.evidence_id).sort());
      const authority = await this.text(SELECTORS.authority), draft = await this.text(SELECTORS.draft);
      assert(authority.includes(report.authoritative.conclusion) && draft.includes(report.untrusted_model_draft.summary));
      assert(!authority.includes(report.untrusted_model_draft.summary), 'Model draft merged with authority');
      assert(await this.evaluate(`document.querySelector('#proposal-description').value === '' && document.querySelector('#proposal-expected-result').value === ''`),
        'Model draft autofilled a human measure');
      assert.equal(this.count('/api/proposals'), 0);
    });
    await this.check('independent_human_proposal_not_model_autofill', async () => {
      await this.fill('#proposal-description', `TEST-ONLY independent human proposal: review synthetic setup records. ${literal}`);
      await this.fill('#proposal-expected-result', 'TEST-ONLY expectation: inspect later independent observations, not causal proof.');
      const response = await this.submit(SELECTORS.proposalCreate, '/api/proposals');
      this.requestBody(response, ['action_id', 'risk_id', 'description', 'expected_result']);
      this.proposal = response.body;
      assert(this.proposal.status === 'proposed' && !this.proposal.review && !this.proposal.execution && !this.proposal.closure);
      assert(await this.unchecked('#proposal-review-confirm') && await this.disabled(SELECTORS.proposalReview));
      assert.equal(this.count(`/api/proposals/${this.proposal.proposal_id}/review`), 0);
    });
    const proposalPath = `/api/proposals/${this.proposal.proposal_id}`;
    await this.check('separate_proposal_review_without_execution', async () => {
      await this.choose('#proposal-decision', 'approved'); await this.fill('#proposal-review-note', note);
      assert(await this.disabled(SELECTORS.proposalReview)); await this.click('#proposal-review-confirm');
      const response = await this.submit(SELECTORS.proposalReview, `${proposalPath}/review`);
      const posted = this.requestBody(response, ['action_id', 'content_hash', 'decision', 'note']);
      assert.equal(posted.content_hash, this.proposal.content_hash); this.proposal = response.body;
      assert(this.proposal.status === 'approved' && !this.proposal.execution && !this.proposal.closure);
      assert.equal(this.count(`${proposalPath}/execute`), 0);
      assert(await this.unchecked('#execution-confirm') && await this.disabled(SELECTORS.proposalExecute));
    });
    await this.check('separate_manual_execution_attestation', async () => {
      // Set the real datetime-local input in the browser's timezone; no private application state.
      assert(await this.evaluate(`(() => {const e = document.querySelector('#execution-observed-at');
        if (e.disabled || e.value) return false;
        const d = new Date(${this.bootstrap.execution_ms});
        e.value = new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,19);
        e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));
        return new Date(e.value).getTime() === ${this.bootstrap.execution_ms};})()`), 'Execution time was auto-filled or changed');
      await this.fill('#execution-note', 'TEST-ONLY simulated human observation timestamp; no physical execution occurred.');
      assert(await this.disabled(SELECTORS.proposalExecute)); await this.click('#execution-confirm');
      const response = await this.submit(SELECTORS.proposalExecute, `${proposalPath}/execute`);
      const posted = this.requestBody(response, ['action_id', 'proposal_hash', 'observed_at_ms', 'note', 'confirmed']);
      assert(posted.confirmed === true && posted.observed_at_ms === this.bootstrap.execution_ms && posted.proposal_hash === this.proposal.content_hash);
      this.proposal = response.body;
      assert(this.proposal.status === 'executed' && this.proposal.execution.payload.physical_execution_performed === false);
      assert.equal(this.proposal.followups.length, 0); assert.equal(this.count(`${proposalPath}/followup`), 0);
    });
    await this.check('independent_followup_without_auto_close', async () => {
      await this.choose('#followup-dataset-select', this.bootstrap.datasets.followup);
      const response = await this.submit(SELECTORS.proposalFollowup, `${proposalPath}/followup`);
      this.requestBody(response, ['action_id', 'dataset_id']); this.proposal = response.body;
      const followup = this.proposal.followups.at(-1);
      assert(this.proposal.status === 'followup_recorded' && followup.payload.eligible === true && !this.proposal.closure,
        'Fixed independent followup did not qualify; no fixture/result alteration is permitted');
      assert.equal(followup.payload.baseline_hash, this.baseline.payload.baseline_hash);
      assert(followup.payload.window.start_ms > this.bootstrap.execution_ms && followup.payload.analysis.status === 'stable');
      assert(await this.unchecked('#proposal-close-confirm') && await this.disabled(SELECTORS.proposalClose));
      assert.equal(this.count(`${proposalPath}/close`), 0);
      assert((await this.text('#followup-close-hash')).includes(followup.followup_hash));
    });
    await this.check('explicit_human_closure_exact_followup_hash', async () => {
      await this.fill('#proposal-close-note', 'TEST-ONLY independent review of the declared later synthetic window; not proof of causation.');
      assert(await this.disabled(SELECTORS.proposalClose)); await this.click('#proposal-close-confirm');
      const response = await this.submit(SELECTORS.proposalClose, `${proposalPath}/close`);
      const posted = this.requestBody(response, ['action_id', 'followup_hash', 'note']);
      assert.equal(posted.followup_hash, this.proposal.followups.at(-1).followup_hash);
      this.proposal = response.body; assert.equal(this.proposal.status, 'closed');
      assert.equal(this.proposal.closure.payload.followup_hash, posted.followup_hash);
      assert((await this.text('#proposal-detail')).includes('closed'));
      for (const action of ['review', 'execute', 'followup', 'close']) assert.equal(this.count(`${proposalPath}/${action}`), 1);
    });
    await this.check('real_lifecycle_audit_and_literal_text_safety', async () => {
      await this.fill('#audit-object-input', this.proposal.proposal_id);
      const audit = (await this.submit('#audit-load-button', `/api/audit/${this.proposal.proposal_id}`, 'GET')).body.audit;
      const events = audit.map(item => item.event);
      for (const event of ['action_proposed', 'action_reviewed', 'action_execution_attested', 'action_followup_recorded', 'action_closed'])
        assert(events.includes(event) && (await this.text('#audit-result')).includes(event), 'Actual lifecycle event not rendered');
      assert((await this.text('#proposal-detail')).includes(literal), 'Untrusted text was not retained literally');
      assert(await this.evaluate(`!window.__smokeXSS && !document.querySelector('#spc-smoke-literal') && !document.querySelector('#workspace img')`),
        'Untrusted text became executable HTML');
    });
    await this.check('no_visible_raw_JSON_dump_or_persistent_browser_credentials', async () => {
      await this.noRawDump(); await this.storageCheck();
    });
    await this.check('logout_rejects_late_real_API_response', async () => {
      await this.control('hold_datasets'); await this.click(SELECTORS.refresh);
      await this.until(async () => (await this.control('state')).held, 'real datasets response held');
      await this.click(SELECTORS.logout); await this.cleared();
      await this.control('release_datasets');
      await this.until(async () => (await this.control('state')).released > 0, 'real response released');
      await delay(250); await this.cleared();
    });
    await this.check('reload_forgets_identity', async () => {
      await this.login(); await this.cmd('Page.reload', {ignoreCache: true});
      await this.until(() => this.evaluate(`document.readyState === 'complete' && document.querySelector('#workspace')?.hidden === true`), 'reload clears identity');
      await this.cleared();
    });
    await this.check('security_headers_and_no_external_page_requests', async () => {
      const responses = this.responses.filter(row => row.path.startsWith('/api/') && row.done);
      assert(responses.length > 20 && responses.every(row => !row.cached), 'Insufficient uncached actual API traffic');
      for (const response of responses) {
        const headers = Object.fromEntries(Object.entries(response.headers).map(([key, value]) => [key.toLowerCase(), value]));
        assert(headers['cache-control'] === 'no-store' && headers['x-content-type-options'] === 'nosniff' &&
          headers['referrer-policy'] === 'no-referrer' && headers['x-frame-options'] === 'DENY' &&
          headers['content-security-policy']?.includes("default-src 'self'"), 'API security headers missing');
      }
      assert.equal(this.external.size, 0, 'Page attempted outside-origin traffic');
      assert.equal(this.faults.length, 0, this.faults.join('; '));
    });
  }
  async snapshotOwnedProcesses() {
    if (!this.children.length) return [];
    const table = await this.processTable();
    // Remember start times while parents are alive so known descendants stay owned
    // even if a parent crashes/reparents them before final cleanup. Never match names.
    const pids = new Set(this.children.filter(child => child.pid && !child.launchFailed &&
      child.exitCode === null && child.signalCode === null).map(child => child.pid));
    for (const row of table) if (this.knownProcesses.get(row.pid)?.started === row.started) pids.add(row.pid);
    let changed = true;
    while (changed) {
      changed = false;
      for (const row of table) if (pids.has(row.parent) && !pids.has(row.pid)) { pids.add(row.pid); changed = true; }
    }
    for (const row of table) if (pids.has(row.pid)) this.knownProcesses.set(row.pid, row);
    return table.filter(row => pids.has(row.pid));
  }
  async processTable() {
    const {stdout} = await promisify(execFile)('/bin/ps', ['-axo', 'pid=,ppid=,lstart='], {maxBuffer: 2 * 1024 * 1024, timeout: 3000});
    return stdout.trim().split('\n').map(line => {
      const [pid, parent, ...started] = line.trim().split(/\s+/);
      return {pid: Number(pid), parent: Number(parent), started: started.join(' ')};
    });
  }
  killGroup(child, signal) {
    if (!child?.pid || child.launchFailed || child.exitCode !== null || child.signalCode !== null) return;
    try { process.kill(-child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  async cleanup() {
    this.stopping = true;
    let descendants = [], enumerationFailed = false;
    try {
      if (this.children.length) {
        const owned = await this.snapshotOwnedProcesses();
        descendants = owned.filter(row => !this.children.some(child => child.pid === row.pid)).reverse();
      }
    } catch { enumerationFailed = true; }
    this.cdp?.close();
    for (const pending of this.controls.values()) { clearTimeout(pending.timer); pending.reject(new Error('Smoke cleanup')); }
    this.controls.clear();
    try {
      for (const child of this.children) this.killGroup(child, 'SIGTERM');
      await Promise.race([Promise.all(this.children.map(child => child.exitCode !== null || child.signalCode !== null || child.launchFailed
        ? Promise.resolve() : new Promise(resolvePromise => child.once('exit', resolvePromise)))), delay(2000)]);
      for (const child of this.children) this.killGroup(child, 'SIGKILL');
      if (descendants.length) {
        const current = await this.processTable();
        for (const owned of descendants) if (current.some(row => row.pid === owned.pid && row.started === owned.started)) {
          try { process.kill(owned.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
        }
      }
      await delay(200);
      assert(this.children.every(child => child.exitCode !== null || child.signalCode !== null || child.launchFailed), 'Owned direct child still running');
      if (descendants.length) {
        const remaining = await this.processTable();
        assert(!descendants.some(owned => remaining.some(row => row.pid === owned.pid && row.started === owned.started)), 'Owned descendant still running');
      }
      assert(!enumerationFailed, 'Owned process enumeration failed; only direct groups could be reaped');
      this.record('owned_process_cleanup', 'pass');
    } catch (error) {
      for (const child of this.children) { try { this.killGroup(child, 'SIGKILL'); } catch {} }
      this.record('owned_process_cleanup', 'fail', error.message);
    }
    if (this.runtime) {
      try {
        await this.ownedDirectory(this.root); await this.ownedDirectory(this.runtime);
        const needles = this.secrets.flatMap(secret => [Buffer.from(secret), Buffer.from(secret, 'utf16le')]);
        let files = 0, total = 0;
        const scan = async path => {
          for (const entry of await readdir(path, {withFileTypes: true})) {
            if (entry.isSymbolicLink()) continue; // Never follow Chrome lock links or an outside target.
            const file = join(path, entry.name);
            if (entry.isDirectory()) await scan(file);
            else if (entry.isFile()) {
              files++; assert(files <= 20000, 'Owned file scan count bound exceeded');
              const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
              try {
                const stat = await handle.stat(); assert(stat.isFile(), 'Nonregular owned file');
                total += stat.size; assert(total <= 512 * 1024 * 1024, 'Owned file scan size bound exceeded');
                let previous = Buffer.alloc(0), chunk = Buffer.alloc(65536);
                const overlap = Math.max(1, ...needles.map(needle => needle.length)) - 1;
                for (;;) {
                  const {bytesRead} = await handle.read(chunk, 0, chunk.length, null); if (!bytesRead) break;
                  const combined = Buffer.concat([previous, chunk.subarray(0, bytesRead)]);
                  assert(!needles.some(needle => combined.includes(needle)), 'Plaintext token persisted in an owned file (content withheld)');
                  previous = Buffer.from(combined.subarray(Math.max(0, combined.length - overlap)));
                }
              } finally { await handle.close(); }
            }
          }
        };
        await scan(this.runtime);
        this.record('no_plaintext_tokens_in_owned_files', 'pass', `${files} owned files scanned after shutdown; content not retained`);
      } catch (error) { this.record('no_plaintext_tokens_in_owned_files', 'fail', error.message); }
      try {
        await this.ownedDirectory(this.root); await this.ownedDirectory(this.runtime);
        await rm(this.runtime, {recursive: true, force: false, maxRetries: 3, retryDelay: 100});
        this.record('owned_runtime_directory_cleanup', 'pass');
      } catch (error) { this.record('owned_runtime_directory_cleanup', 'fail', error.message); }
    }
  }
  async report() {
    for (const name of CHECKS) if (!this.results.some(row => row.name === name)) this.record(name, 'not_tested', 'Prerequisite failed; no UI/API bypass');
    const counts = Object.fromEntries(['pass', 'fail', 'not_tested'].map(status => [status, this.results.filter(row => row.status === status).length]));
    const report = {status: counts.fail || counts.not_tested ? 'fail' : 'pass', mode: 'actual Chrome CDP + real SPC API/statistics; SyntheticProvider TEST-ONLY',
      elapsed_seconds: Number(((Date.now() - this.started) / 1000).toFixed(2)), counts, results: this.results,
      not_tested: ['actual model/runtime', 'production/physical action', 'multi-tab concurrency', 'other browsers', 'OS-level browser background traffic'],
      artifacts: this.results.some(row => row.name === 'owned_runtime_directory_cleanup' && row.status === 'pass')
        ? 'Only sanitized summary retained; owned app/profile removed; no screenshots/raw API or credential dumps.'
        : 'Runtime cleanup was not verified. Inspect the cleanup result; do not assume app/profile deletion.'};
    const output = this.safeReport(report);
    if (this.root) {
      await this.ownedDirectory(this.root);
      await writeFile(join(this.root, 'summary.json'), output, {flag: 'wx', mode: 0o600});
    }
    process.stdout.write(output);
    if (this.root) process.stdout.write(`Owned summary: ${this.safe(join(this.root, 'summary.json'))}\n`);
    return report.status === 'pass' ? 0 : 1;
  }
  safeReport(report) {
    let output = JSON.stringify(report, null, 2) + '\n';
    for (const secret of this.secrets) output = output.split(secret).join('[REDACTED]');
    return output;
  }
}

export function selfTest() {
  assert.equal(parseArgs([]).mode, 'help'); assert.equal(parseArgs(['--self-test']).mode, 'self-test');
  for (const args of [['--run-chrome'], ['--artifacts-parent', '/private/tmp'], ['--run-chrome', '--artifacts-parent', PROJECT],
    ['--run-chrome', '--artifacts-parent', '/private/tmp', '--model-url', 'http://127.0.0.1'],
    ['--run-chrome', '--artifacts-parent', '/private/tmp', '--chrome', 'relative'],
    ['--run-chrome', '--run-chrome', '--artifacts-parent', '/private/tmp']]) assert.throws(() => parseArgs(args));
  assert.equal(parseArgs(['--run-chrome', '--artifacts-parent', '/private/tmp']).authorized, true);
  assert.deepEqual(seriesRuns([{mean:1},{mean:2},{mean:null},{mean:4},{mean:5},{mean:6,break_before:true},{mean:7}], 'mean'), [[0,1],[3,4],[5,6]]);
  const smoke = new Smoke({}); smoke.secrets.push('TEST-SECRET');
  assert(!smoke.safeReport({message: 'TEST-SECRET'}).includes('TEST-SECRET'));
  process.stdout.write('PASS inert argument, no-launch default, redaction and SVG segmentation self-tests\n');
}

async function main() {
  let options;
  try { options = parseArgs(process.argv.slice(2)); }
  catch { process.stderr.write(USAGE); process.exitCode = 2; return; }
  if (options.mode === 'help') { process.stdout.write(USAGE); return; }
  if (options.mode === 'self-test') { selfTest(); return; }
  const smoke = new Smoke(options);
  let interrupt;
  const interrupted = new Promise((_, reject) => { interrupt = reject; });
  const stop = reason => {
    if (smoke.stopping) return;
    smoke.stopping = true;
    smoke.cdp?.close();
    // The cleanup phase enumerates descendants BEFORE terminating their live parents.
    interrupt(new Error(reason));
  };
  const timer = setTimeout(() => stop('250s absolute smoke deadline'), 250000);
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  const handlers = signals.map(signal => { const handler = () => stop(`Interrupted by ${signal}`); process.on(signal, handler); return handler; });
  const work = smoke.run();
  try { await Promise.race([work, interrupted]); }
  catch (error) { smoke.record('browser_smoke', 'fail', error.message); }
  finally {
    smoke.stopping = true;
    // Wait for bounded startup/DOM operations to settle before deleting any directory.
    // In particular, a signal cannot race mkdtemp and leave an untracked owned profile.
    await work.catch(() => {});
    clearTimeout(timer);
    await smoke.cleanup();
    try { process.exitCode = await smoke.report(); }
    catch { process.stderr.write('SPC smoke summary could not be retained safely.\n'); process.exitCode = 1; }
    signals.forEach((signal, i) => process.removeListener(signal, handlers[i]));
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
