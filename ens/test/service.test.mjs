import assert from 'node:assert/strict';
import { test } from 'node:test';

import { decodeFunctionData, getAddress, zeroAddress } from 'viem';

import { resolverAbi } from '../lib/abis.mjs';
import { ADDRESSES, COMMIT_WAIT_SECONDS, RESOLVER_ROLES } from '../lib/constants.mjs';
import { DryRunExecutor } from '../lib/executor.mjs';
import { keyResource, tokenId } from '../lib/names.mjs';
import { NamesService } from '../lib/service.mjs';

const NOW = 1_800_000_000;
const HIRED = '0x00000000000000000000000000000000000a11ce';

async function makeService(opts = {}) {
  const exec = new DryRunExecutor(opts.exec);
  const service = new NamesService({ exec, rootLabel: 'agentslist-app', now: () => NOW, random: () => '0x' + '11'.repeat(32) });
  await service.init();
  return { exec, service };
}

async function provision(service) {
  const root = await service.setupRoot();
  const agent = await service.createAgent({
    agent_public_id: 'AGT-0000-0001-X', label: 'test-agent',
    records: { context: 'Writes tests', mcp: 'https://agent.example/mcp',
      payout: HIRED, erc8004_agent_id: '42' },
  });
  const job = await service.createJob({
    parent: agent.name, label: 'eng-aaaa', expiry: NOW + 86400,
    records: { sow_hash: '0xabc', escrow: '0xdef', mandate: 'MND-1', status: 'funded' }, grantee: HIRED,
  });
  const sub = await service.createSubjob({
    parent: job.name, label: 'eng-bbbb', expiry: NOW + 999_999, records: { status: 'funded' },
  });
  return { root, agent, job, sub };
}

const calls = (exec, fn) => exec.calls.filter((c) => c.functionName === fn);

test('address check passes against the canonical deployment', async () => {
  const { service } = await makeService();
  assert.equal(service.health().ok, true);
  assert.equal(service.health().mode, 'dry_run');
});

test('address check mismatch disables writes', async () => {
  const { service } = await makeService({ exec: { reads: { ETH_REGISTRY: () => '0x' + '12'.repeat(20) } } });
  assert.equal(service.health().ok, false);
  await assert.rejects(service.setupRoot(), (err) => err.status === 503 && err.code === 'ADDRESS_CHECK_FAILED');
});

test('root setup: mint, approve, commit, wait >= 60s, register, setSubregistry', async () => {
  const { exec, service } = await makeService();
  const root = await service.setupRoot();
  assert.equal(root.name, 'agentslist-app.eth');
  assert.equal(root.status, 'active');
  assert.deepEqual(root.txs.map((t) => t.step), [
    'deploy:resolver', 'deploy:registry', 'mint', 'approve', 'commit', 'register', 'setSubregistry', 'setParent',
  ]);
  assert.ok(exec.slept >= 60 && exec.slept === COMMIT_WAIT_SECONDS);
  const [reg] = calls(exec, 'register');
  assert.equal(reg.address, ADDRESSES.ethRegistrar);
  assert.equal(reg.args[0], 'agentslist-app');
  assert.equal(reg.args[6], ADDRESSES.mockUsdc);
  const [sub] = calls(exec, 'setSubregistry');
  assert.deepEqual(sub.args, [tokenId('agentslist-app'), root.registry]);
  assert.ok(!('secret' in root));
  // Idempotent: a second call does nothing.
  const again = await service.setupRoot();
  assert.equal(again.existing, true);
  assert.equal(calls(exec, 'register').length, 1);
});

test('agent, job, sub-job, revoke', async () => {
  const { exec, service } = await makeService();
  const { root, agent, job, sub } = await provision(service);

  // Agent name in the platform registry, platform-owned, no owner roles.
  assert.equal(agent.name, 'test-agent.agentslist-app.eth');
  const agentReg = calls(exec, 'register').find((c) => c.args[0] === 'test-agent');
  assert.equal(agentReg.address, root.registry);
  assert.equal(agentReg.args[4], 0n);
  assert.deepEqual(agent.text, {
    'agent-context': 'Writes tests',
    'agent-endpoint[mcp]': 'https://agent.example/mcp',
    'x402-payto': HIRED,
    'agent-registration[0x0001000003aa36a7148004a818bfb912233c491871b3d84c89a494bd9e][42]': '1',
  });
  assert.equal(agent.records.erc8004_agent_id, '42');

  // Job: in the agent's registry, own resolver, expiry = deadline, roleBitmap 0.
  assert.equal(job.name, 'eng-aaaa.test-agent.agentslist-app.eth');
  const jobReg = calls(exec, 'register').find((c) => c.args[0] === 'eng-aaaa');
  assert.equal(jobReg.address, agent.registry);
  assert.deepEqual(jobReg.args.slice(2), [zeroAddress, job.resolver, 0n, BigInt(NOW + 86400)]);
  assert.notEqual(job.resolver, agent.resolver);

  // The hired agent gets setText rights for status and deliverable only.
  const grants = calls(exec, 'grantSetterRoles');
  assert.equal(grants.length, 2);
  for (const g of grants) {
    assert.equal(g.address, job.resolver);
    assert.equal(g.args[1], getAddress(HIRED));
  }
  const keys = grants.map((g) => decodeFunctionData({ abi: resolverAbi, data: g.args[0] }).args[1]);
  assert.deepEqual(keys, ['status', 'deliverable']);

  // Sub-job: wildcard records on the job resolver, capped at the job expiry.
  assert.equal(sub.name, 'eng-bbbb.eng-aaaa.test-agent.agentslist-app.eth');
  assert.equal(sub.resolver, job.resolver);
  assert.equal(sub.expiry, NOW + 86400);
  const [mc] = calls(exec, 'multicall');
  assert.equal(mc.address, job.resolver);

  // Tree nests all four levels.
  const { tree } = await service.tree();
  assert.equal(tree.children[0].children[0].children[0].name, sub.name);

  // Revoke the job: roles revoked per key, then unregistered; subtree revoked.
  const revoked = await service.revoke({ name: job.name });
  assert.equal(revoked.status, 'revoked');
  const rr = calls(exec, 'revokeRoles');
  assert.deepEqual(rr.map((c) => c.args), [
    [keyResource('status'), RESOLVER_ROLES.SET_TEXT, getAddress(HIRED)],
    [keyResource('deliverable'), RESOLVER_ROLES.SET_TEXT, getAddress(HIRED)],
  ]);
  const [unreg] = calls(exec, 'unregister');
  assert.deepEqual([unreg.address, unreg.args[0]], [agent.registry, tokenId('eng-aaaa')]);
  const after = await service.tree();
  assert.equal(after.tree.children[0].children[0].children[0].status, 'revoked');
  assert.equal((await service.revoke({ name: job.name })).existing, true);
});

test('an active agent name can add a changed payout record', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const first = await service.createAgent({
    agent_public_id: 'AGT-0000-0001-X', label: 'test-agent', records: { context: 'Writes tests' },
  });
  const second = await service.createAgent({
    agent_public_id: 'AGT-0000-0001-X', label: 'test-agent',
    records: { context: 'Writes tests', payout: HIRED },
  });
  assert.equal(second.existing, true);
  assert.equal(second.text['x402-payto'], HIRED);
  const writes = calls(exec, 'multicall');
  assert.equal(writes.length, 1);
  assert.notEqual(first.resolver, undefined);
});

test('dry-run hashes are deterministic', async () => {
  const a = await makeService();
  const b = await makeService();
  const ta = await provision(a.service);
  const tb = await provision(b.service);
  assert.deepEqual(ta.job.txs, tb.job.txs);
  assert.match(ta.job.txs[0].hash, /^0x[0-9a-f]{64}$/);
});

test('validation errors', async () => {
  const { service } = await makeService();
  await assert.rejects(service.createAgent({ agent_public_id: 'AGT-1', label: 'x' }), /PARENT_NOT_READY|not set up/);
  await service.setupRoot();
  const agent = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  await assert.rejects(service.createAgent({ agent_public_id: 'AGT-0000-0002-Y', label: 'a1' }),
    (e) => e.code === 'NAME_TAKEN');
  await assert.rejects(service.createJob({ parent: agent.name, label: 'j', expiry: NOW - 1 }),
    (e) => e.code === 'INVALID_EXPIRY');
  await assert.rejects(service.createJob({ parent: 'x.other.eth', label: 'j', expiry: NOW + 1 }),
    (e) => e.code === 'OUTSIDE_ROOT');
  await assert.rejects(service.createJob({ parent: agent.name, label: 'j', expiry: NOW + 9, grantee: 'nope' }),
    /grantee/);
  await assert.rejects(service.createSubjob({ parent: agent.name, label: 's', expiry: NOW + 9, records: { status: 'x' } }),
    /job name/);
  await assert.rejects(service.revoke({ name: 'agentslist-app.eth' }), /root/);
  await assert.rejects(service.revoke({ name: 'Bad_Label.agentslist-app.eth' }), (e) => e.code === 'INVALID_LABEL');
});

test('root setup resumes after a failed register without re-committing', async () => {
  let t = NOW;
  const exec = new DryRunExecutor();
  const service = new NamesService({ exec, now: () => t, random: () => '0x' + '22'.repeat(32) });
  await service.init();
  const write = exec.write.bind(exec);
  let fail = true;
  exec.write = async (req) => {
    if (fail && req.functionName === 'register') throw new Error('rpc down');
    return write(req);
  };
  await assert.rejects(service.setupRoot(), /rpc down/);
  t += 600;
  fail = false;
  const slept = exec.slept;
  const root = await service.setupRoot();
  assert.equal(root.status, 'active');
  assert.equal(calls(exec, 'commit').length, 1);
  assert.equal(exec.slept, slept); // already past the 60 s window
  assert.equal(calls(exec, 'deployProxy').length, 2);
});

test('a failed step resumes on retry without redeploying', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const agent = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  const write = exec.write.bind(exec);
  let fail = true;
  exec.write = async (req) => {
    if (fail && req.functionName === 'register') throw new Error('rpc down');
    return write(req);
  };
  await assert.rejects(service.createJob({ parent: agent.name, label: 'j1', expiry: NOW + 60 }), /rpc down/);
  fail = false;
  const job = await service.createJob({ parent: agent.name, label: 'j1', expiry: NOW + 60 });
  assert.equal(job.status, 'active');
  assert.equal(job.txs.filter((t) => t.step === 'deploy:resolver').length, 1);
});

test('a revoked name can be issued again with fresh proxies', async () => {
  const { service } = await makeService();
  await service.setupRoot();
  const first = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  await service.revoke({ name: first.name });
  const second = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  assert.equal(second.version, 1);
  assert.notEqual(second.resolver, first.resolver);
});
