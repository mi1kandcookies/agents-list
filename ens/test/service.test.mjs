import assert from 'node:assert/strict';
import { test } from 'node:test';

import { decodeFunctionData, getAddress, zeroAddress } from 'viem';

import { resolverAbi } from '../lib/abis.mjs';
import { ADDRESSES, COMMIT_WAIT_SECONDS, RESOLVER_ROLES } from '../lib/constants.mjs';
import { DryRunExecutor } from '../lib/executor.mjs';
import { dnsEncode, keyResource, tokenId } from '../lib/names.mjs';
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

const HASH = '0x' + 'ab'.repeat(32);
const decodeAll = (mc) => mc.args[0].map((data) => decodeFunctionData({ abi: resolverAbi, data }));

test('an agent name resolves to its payout and is linked under the root', async () => {
  const { exec, service } = await makeService();
  const root = await service.setupRoot();
  const agent = await service.createAgent({
    agent_public_id: 'AGT-SH8W-D5VP-*', label: 'keelhaul-audit',
    records: { context: 'Audits', payout: HIRED, manifest_hash: HASH, web: 'https://app.example/agents/1' },
  });
  // The initializer writes the text records and the ETH address record.
  const deploy = calls(exec, 'deployProxy').filter((c) => c.args[0] === ADDRESSES.permissionedResolverImpl)[1];
  const init = decodeFunctionData({ abi: resolverAbi, data: deploy.args[2] });
  const inner = init.args[1].map((data) => decodeFunctionData({ abi: resolverAbi, data }));
  assert.deepEqual(inner.find((c) => c.functionName === 'setAddress').args.slice(1), [60n, HIRED]);
  assert.equal(agent.text['manifest-hash'], HASH);
  assert.equal(agent.text['agent-endpoint[web]'], 'https://app.example/agents/1');
  // Its registry points back at the root registry, so the tree is canonical.
  const link = calls(exec, 'setParent').find((c) => c.address === agent.registry);
  assert.deepEqual(link.args, [root.registry, 'keelhaul-audit']);
});

test('an agent may edit only its own endpoint records', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { payout: HIRED }, grantee: HIRED };
  const agent = await service.createAgent(body);
  const [mc] = calls(exec, 'multicall');
  assert.equal(mc.address, agent.resolver);
  const granted = decodeAll(mc).map((c) => [c.functionName, c.args[1],
    decodeFunctionData({ abi: resolverAbi, data: c.args[0] }).args[1]]);
  assert.deepEqual(granted, [
    ['grantSetterRoles', getAddress(HIRED), 'agent-endpoint[mcp]'],
    ['grantSetterRoles', getAddress(HIRED), 'agent-endpoint[a2a]'],
  ]);
  // Publishing again changes nothing on-chain.
  await service.createAgent(body);
  assert.equal(calls(exec, 'multicall').length, 1);
  // A new address takes over the grants; the old one loses them in the same
  // call, and the endpoint records it could write are reset.
  const next = '0x00000000000000000000000000000000000b0b00';
  await service.createAgent({ ...body, records: { payout: next }, grantee: next });
  assert.deepEqual(decodeAll(calls(exec, 'multicall')[1]).map((c) => [c.functionName, c.args[1]]), [
    ['setText', 'x402-payto'], ['setText', 'agent-endpoint[mcp]'], ['setText', 'agent-endpoint[a2a]'],
    ['setAddress', 60n], ['revokeRoles', RESOLVER_ROLES.SET_TEXT], ['revokeRoles', RESOLVER_ROLES.SET_TEXT],
    ['grantSetterRoles', getAddress(next)], ['grantSetterRoles', getAddress(next)],
  ]);
  // Revoking the agent revokes the live grants, then unregisters.
  await service.revoke({ name: agent.name });
  assert.deepEqual(calls(exec, 'revokeRoles').map((c) => [c.args[0], c.args[2]]), [
    [keyResource('agent-endpoint[mcp]'), getAddress(next)], [keyResource('agent-endpoint[a2a]'), getAddress(next)],
  ]);
  assert.equal(calls(exec, 'unregister').length, 1);
});

test('records the platform stops sending are cleared', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  await service.createAgent({
    agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED, manifest_hash: HASH },
  });
  const after = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x' } });
  assert.deepEqual(decodeAll(calls(exec, 'multicall')[0]).map((c) => [c.functionName, c.args.slice(1)]), [
    ['setText', ['x402-payto', '']], ['setText', ['manifest-hash', '']], ['setAddress', [60n, '0x']],
  ]);
  assert.deepEqual(after.text, { 'agent-context': 'x' });
});

test('names issued from another host are adopted, not redeployed', async () => {
  const { exec, service } = await makeService();
  const root = await service.setupRoot();
  const agent = await service.createAgent({
    agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'Audits', payout: HIRED },
  });
  // Same chain, empty state file.
  const other = new NamesService({ exec, rootLabel: 'agentslist-app', now: () => NOW });
  await other.init();
  const before = exec.calls.length;
  const adoptedRoot = await other.setupRoot();
  assert.equal(exec.calls.length, before); // nothing to send
  assert.equal(adoptedRoot.registry, root.registry);
  const adopted = await other.createAgent({
    agent_public_id: 'AGT-9999-0001-Z', label: 'a1', records: { context: 'Audits v2', payout: HIRED }, grantee: HIRED,
  });
  const sent = exec.calls.slice(before);
  assert.deepEqual(sent.map((c) => c.functionName), ['multicall']); // no deploy, no register, parent already set
  assert.equal(adopted.adopted, true);
  assert.equal(adopted.resolver, agent.resolver);
  // What is already on-chain (the payout and its address record) is not rewritten.
  assert.deepEqual(decodeAll(sent[0]).map((c) => [c.functionName, c.args[1]]), [
    ['setText', 'agent-context'],
    ['grantSetterRoles', getAddress(HIRED)], ['grantSetterRoles', getAddress(HIRED)],
  ]);
  assert.equal(adopted.text['agent-context'], 'Audits v2');
});

test('adoption finds the payee grants and a missing address record', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED } };
  const agent = await service.createAgent({ ...body, grantee: HIRED });
  exec.addrs.clear(); // as if issued before names carried the ETH address record
  const other = new NamesService({ exec, rootLabel: 'agentslist-app', now: () => NOW });
  await other.init();
  await other.setupRoot();
  const before = exec.calls.length;
  await other.createAgent({ ...body, grantee: null });
  const [mc] = exec.calls.slice(before);
  assert.equal(mc.address, agent.resolver);
  // The payout text already on-chain is kept; the missing address record is
  // repaired; the grants found for the payee are revoked and their endpoints reset.
  assert.deepEqual(decodeAll(mc).map((c) => [c.functionName, c.args[1], c.args[2]]), [
    ['setText', 'agent-endpoint[mcp]', ''], ['setText', 'agent-endpoint[a2a]', ''],
    ['setAddress', 60n, HIRED],
    ['revokeRoles', RESOLVER_ROLES.SET_TEXT, getAddress(HIRED)], ['revokeRoles', RESOLVER_ROLES.SET_TEXT, getAddress(HIRED)],
  ]);
  assert.equal(await exec.read({ address: agent.resolver, functionName: 'hasRoles',
    args: [keyResource('agent-endpoint[mcp]'), RESOLVER_ROLES.SET_TEXT, HIRED] }), false);
});

// A write that lands on-chain but whose receipt never reaches the service.
function loseReceiptOnce(exec, fn, address) {
  const write = exec.write.bind(exec);
  let lost = false;
  exec.write = async (req) => {
    const result = await write(req);
    if (!lost && req.functionName === fn && (!address || req.address === address)) {
      lost = true;
      throw new Error('receipt timed out');
    }
    return result;
  };
}

test('a root registration whose receipt was lost resumes without redeploying', async () => {
  const { exec, service } = await makeService();
  loseReceiptOnce(exec, 'register', ADDRESSES.ethRegistrar);
  await assert.rejects(service.setupRoot(), /receipt timed out/);
  const root = await service.setupRoot();
  assert.equal(root.status, 'active');
  assert.equal(calls(exec, 'deployProxy').length, 2);
  const [sub] = calls(exec, 'setSubregistry');
  assert.equal(sub.args[1], root.registry); // the registry deployed before the failure
});

test('an agent registration whose receipt was lost resumes without redeploying', async () => {
  const { exec, service } = await makeService();
  const root = await service.setupRoot();
  loseReceiptOnce(exec, 'register', root.registry);
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED } };
  await assert.rejects(service.createAgent(body), /receipt timed out/);
  const agent = await service.createAgent(body);
  assert.equal(agent.status, 'active');
  assert.equal(calls(exec, 'deployProxy').length, 4); // root 2 + agent 2
  assert.equal(calls(exec, 'multicall').length, 0); // its own resolver: records already there
  assert.equal(agent.text['x402-payto'], HIRED);
});

test('an on-chain agent name without a resolver is refused, not activated', async () => {
  const { exec, service } = await makeService();
  const root = await service.setupRoot();
  await exec.write({ address: root.registry, functionName: 'register',
    args: ['a1', exec.operator, zeroAddress, zeroAddress, 0n, BigInt(NOW + 9999)] });
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x' } };
  for (let i = 0; i < 2; i += 1) {
    await assert.rejects(service.createAgent(body), (e) => e.code === 'PARENT_NOT_READY');
  }
  assert.equal(calls(exec, 'deployProxy').length, 2); // root only
  assert.notEqual(service.store.get('a1.agentslist-app.eth').status, 'active');
});

test('grants follow the grantee field: absent keeps them, null revokes them', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x' } };
  await service.createAgent({ ...body, grantee: HIRED });
  await service.createAgent(body);
  assert.equal(calls(exec, 'revokeRoles').length + calls(exec, 'multicall').length, 1);
  await service.createAgent({ ...body, grantee: null });
  // Revoking also resets the endpoints the grantee could write.
  assert.deepEqual(decodeAll(calls(exec, 'multicall')[1]).map((c) => c.functionName),
    ['setText', 'setText', 'revokeRoles', 'revokeRoles']);
  const agent = await service.createAgent({ ...body, grantee: null });
  assert.equal(calls(exec, 'multicall').length, 2); // nothing left to revoke
  assert.ok(agent.grants.every((g) => g.revoked));
});

test('an endpoint the platform stops sending is cleared only while it is still the platform value', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1' };
  await service.createAgent({ ...body, records: { mcp: 'https://platform.example/mcp' } });
  exec.readText = async () => 'https://agent.example/mcp'; // the agent replaced it
  const kept = await service.createAgent({ ...body, records: { a2a: 'https://platform.example/a2a' } });
  assert.deepEqual(decodeAll(calls(exec, 'multicall')[0]).map((c) => c.args[1]), ['agent-endpoint[a2a]']);
  assert.equal(kept.text['agent-endpoint[mcp]'], undefined);
  exec.readText = async () => { throw new Error('rpc down'); }; // unknown: keep it, try again later
  const pending = await service.createAgent({ ...body, records: {} });
  assert.equal(calls(exec, 'multicall').length, 1);
  assert.equal(pending.text['agent-endpoint[a2a]'], 'https://platform.example/a2a');
  exec.readText = async () => 'https://platform.example/a2a'; // still the platform's
  await service.createAgent({ ...body, records: {} });
  assert.deepEqual(decodeAll(calls(exec, 'multicall')[1]).map((c) => c.args.slice(1)), [['agent-endpoint[a2a]', '']]);
});

test('a job registration whose receipt was lost resumes without registering twice', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const agent = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  loseReceiptOnce(exec, 'register', agent.registry);
  const body = { parent: agent.name, label: 'eng-aaaa', expiry: NOW + 86400, records: { status: 'funded' }, grantee: HIRED };
  await assert.rejects(service.createJob(body), /receipt timed out/);
  const job = await service.createJob(body);
  assert.equal(job.status, 'active');
  assert.equal(calls(exec, 'register').filter((c) => c.args[0] === 'eng-aaaa').length, 1);
  assert.equal(calls(exec, 'grantSetterRoles').length, 2);
});

async function adoptingHost(exec) {
  const other = new NamesService({ exec, rootLabel: 'agentslist-app', now: () => NOW });
  await other.init();
  await other.setupRoot();
  return other;
}

test('a failed read during adoption changes nothing, and the next try adopts', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED } };
  await service.createAgent({ ...body, grantee: HIRED });
  const other = await adoptingHost(exec);
  const readText = exec.readText.bind(exec);
  exec.readText = async () => { throw new Error('rpc down'); };
  await assert.rejects(other.createAgent(body), /rpc down/);
  const node = other.store.get('a1.agentslist-app.eth');
  assert.ok(!node.registered && !node.adopted);
  exec.readText = readText;
  const adopted = await other.createAgent({ ...body, grantee: HIRED });
  assert.equal(adopted.adopted, true);
  assert.equal(adopted.grants.length, 2); // found on-chain, not granted again
});

test('an adopted payout is cleared even when the address record disagrees', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED } };
  await service.createAgent(body);
  exec.addrs.set(dnsEncode('a1.agentslist-app.eth'), getAddress('0x' + '77'.repeat(20)));
  const other = await adoptingHost(exec);
  const before = exec.calls.length;
  await other.createAgent({ ...body, records: { context: 'x' } });
  assert.deepEqual(decodeAll(exec.calls[before]).map((c) => [c.functionName, c.args[1], c.args[2]]), [
    ['setText', 'x402-payto', ''], ['setAddress', 60n, '0x'],
  ]);
  assert.equal(await exec.readAddress('a1.agentslist-app.eth'), null);
});

test('state from an earlier build is reconciled with the chain once', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const body = { agent_public_id: 'AGT-0000-0001-X', label: 'a1', records: { context: 'x', payout: HIRED } };
  await service.createAgent({ ...body, grantee: HIRED });
  const node = service.store.get('a1.agentslist-app.eth');
  delete node.reconciled; // as saved by a build that tracked neither grants nor the address record
  delete node.addr;
  node.grants = [];
  const before = exec.calls.length;
  await service.createAgent({ ...body, grantee: null });
  assert.deepEqual(decodeAll(exec.calls[before]).map((c) => c.functionName),
    ['setText', 'setText', 'revokeRoles', 'revokeRoles']); // grants found and revoked; address record already right
  assert.equal(node.reconciled, true);
  await service.createAgent({ ...body, grantee: null });
  assert.equal(exec.calls.length, before + 1);
});

test('an adopted job on a resolver this sidecar did not deploy gets its records', async () => {
  const { exec, service } = await makeService();
  await service.setupRoot();
  const agent = await service.createAgent({ agent_public_id: 'AGT-0000-0001-X', label: 'a1' });
  loseReceiptOnce(exec, 'register', agent.registry);
  const body = { parent: agent.name, label: 'eng-aaaa', expiry: NOW + 86400, records: { status: 'funded' } };
  await assert.rejects(service.createJob(body), /receipt timed out/);
  const other = '0x' + '55'.repeat(20);
  exec.entries.get([...exec.entries.keys()].find((k) => k.startsWith(agent.registry.toLowerCase()))).resolver = other;
  const job = await service.createJob(body);
  assert.equal(job.resolver, getAddress(other));
  assert.equal(await exec.readText(job.name, 'status'), 'funded');
});

test('adopt-only root setup never registers a new root', async () => {
  const { exec, service } = await makeService();
  await assert.rejects(service.setupRoot({ adopt_only: true }), (e) => e.code === 'ROOT_NOT_REGISTERED');
  assert.equal(exec.calls.length, 0);
});

test('adoption refuses a name held by another account', async () => {
  const { service } = await makeService({ exec: { reads: {
    isAvailable: () => false,
    getState: () => ({ status: 2, expiry: 9n, latestOwner: '0x' + '99'.repeat(20), tokenId: 0n, resource: 0n }),
  } } });
  await assert.rejects(service.setupRoot(), (e) => e.code === 'NAME_TAKEN');
});

test('an expired job is revoked without unregistering', async () => {
  const { exec, service } = await makeService();
  const { job } = await provision(service);
  exec.overrides.getState = (args) => ({ status: 0, expiry: 1n, latestOwner: zeroAddress, tokenId: args[0], resource: 0n });
  const revoked = await service.revoke({ name: job.name });
  assert.equal(revoked.status, 'revoked');
  assert.equal(calls(exec, 'revokeRoles').length, 2);
  assert.equal(calls(exec, 'unregister').length, 0);
});
