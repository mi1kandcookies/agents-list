import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadConfig } from '../lib/config.mjs';
import { start } from '../server.mjs';

const TOKEN = 'test-token';
let server;
let base;

before(async () => {
  const config = { ...loadConfig({ DRY_RUN: '1', ENS_SIDECAR_TOKEN: TOKEN }), port: 0 };
  ({ server } = await start(config));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => server.close());

const call = (method, path, body, token = TOKEN) => fetch(base + path, {
  method,
  headers: { 'content-type': 'application/json', ...(token ? { 'x-sidecar-token': token } : {}) },
  body: body === undefined ? undefined : JSON.stringify(body),
});

test('binds to loopback only', () => {
  assert.equal(server.address().address, '127.0.0.1');
});

test('every route needs the sidecar token', async () => {
  for (const [method, path] of [['GET', '/health'], ['GET', '/names/tree'], ['POST', '/names/agent']]) {
    assert.equal((await call(method, path, undefined, null)).status, 401);
    assert.equal((await call(method, path, undefined, 'wrong')).status, 401);
  }
});

test('refuses to start without a token', async () => {
  await assert.rejects(start({ ...loadConfig({ DRY_RUN: '1' }), port: 0 }), /ENS_SIDECAR_TOKEN/);
});

test('health reports dry-run mode', async () => {
  const res = await call('GET', '/health');
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.mode, 'dry_run');
  assert.equal(body.ok, true);
  assert.equal(body.root, 'agentslist-app.eth');
});

test('full flow over HTTP', async () => {
  assert.equal((await call('GET', '/names/tree')).status, 404);
  assert.equal((await call('POST', '/names/root/setup', {})).status, 200);
  const agent = await (await call('POST', '/names/agent', {
    agent_public_id: 'AGT-0000-0001-X', label: 'helper', records: { context: 'hi' },
  })).json();
  assert.equal(agent.name, 'helper.agentslist-app.eth');
  const expiry = Math.floor(Date.now() / 1000) + 3600;
  const job = await (await call('POST', '/names/job', {
    parent: agent.name, label: 'eng-1', expiry, records: { status: 'funded' },
    grantee: '0x00000000000000000000000000000000000a11ce',
  })).json();
  assert.equal(job.grants.length, 2);
  const sub = await call('POST', '/names/subjob', { parent: job.name, label: 'eng-2', expiry, records: { status: 'funded' } });
  assert.equal(sub.status, 200);
  const tree = await (await call('GET', '/names/tree?root=' + agent.name)).json();
  assert.equal(tree.tree.children[0].children[0].name, `eng-2.${job.name}`);
  const rev = await (await call('POST', '/names/revoke', { name: job.name })).json();
  assert.equal(rev.status, 'revoked');
});

test('bad input is a 4xx with a code', async () => {
  const res = await call('POST', '/names/agent', { label: 'BAD', agent_public_id: 'AGT-1' });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).code, 'INVALID_LABEL');
  const junk = await fetch(base + '/names/agent', { method: 'POST', headers: { 'x-sidecar-token': TOKEN }, body: '[' });
  assert.equal(junk.status, 400);
  assert.equal((await call('GET', '/nope')).status, 404);
});
