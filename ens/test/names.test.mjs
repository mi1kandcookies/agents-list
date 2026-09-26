import assert from 'node:assert/strict';
import { test } from 'node:test';

import { decodeFunctionData, toHex } from 'viem';

import { resolverAbi } from '../lib/abis.mjs';
import { ERC8004_REGISTRY_7930, RECORD_KEYS, erc7930Address, registrationKey } from '../lib/constants.mjs';
import { assertLabel, dnsEncode, keyResource, setAddressCall, setterCalldata, toTextRecords } from '../lib/names.mjs';

test('ERC-7930 interoperable addresses', () => {
  // Worked examples from ERC-7930 and ENSIP-25 (mainnet).
  assert.equal(erc7930Address(1, '0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045'),
    '0x00010000010114d8da6bf26964af9d7eed9e03e53415d37aa96045');
  assert.equal(erc7930Address(1, '0x8004A169FB4a3325136EB29fA0ceB6D2e539a432'),
    '0x000100000101148004a169fb4a3325136eb29fa0ceb6d2e539a432');
  // Sepolia: chain ref aa36a7 (3 bytes), ChainReferenceLength is ONE byte.
  assert.equal(ERC8004_REGISTRY_7930, '0x0001000003aa36a7148004a818bfb912233c491871b3d84c89a494bd9e');
});

test('per-key role resources match keccak256(key)', () => {
  assert.equal(toHex(keyResource(RECORD_KEYS.context), { size: 32 }),
    '0x159232e963e48c2b29936ac60add1c65bfcf046bffaad1a3660c3464a47fcac4');
  assert.equal(toHex(keyResource(RECORD_KEYS.mcp), { size: 32 }),
    '0x9141a145971a76bf4cd4304394de19852f0d825776df748d2684a853d6ddb84c');
  assert.equal(registrationKey('42'), `agent-registration[${ERC8004_REGISTRY_7930}][42]`);
});

test('names are DNS wire-encoded for resolver setters', () => {
  assert.equal(dnsEncode('ab.eth'), '0x02616203657468' + '00');
});

test('setter calldata for key-scoped grants carries only the key', () => {
  const { functionName, args } = decodeFunctionData({ abi: resolverAbi, data: setterCalldata('status') });
  assert.equal(functionName, 'setText');
  assert.deepEqual(args, ['0x', 'status', '']);
});

test('logical records map to ENSIP-25/26 keys', () => {
  const out = toTextRecords('agent', {
    context: 'Writes tests', mcp: 'https://agent.example/mcp',
    payout: '0x00000000000000000000000000000000000a11ce', erc8004_agent_id: '7',
  });
  assert.deepEqual(out.map((e) => [e.key, e.value]), [
    ['agent-context', 'Writes tests'],
    ['agent-endpoint[mcp]', 'https://agent.example/mcp'],
    ['x402-payto', '0x00000000000000000000000000000000000a11ce'],
    [`agent-registration[${ERC8004_REGISTRY_7930}][7]`, '1'],
  ]);
});

test('record validation', () => {
  assert.throws(() => toTextRecords('agent', { status: 'x' }), /not allowed/);
  assert.throws(() => toTextRecords('agent', { mcp: 'http://insecure' }), /https/);
  assert.throws(() => toTextRecords('agent', { payout: 'not-an-address' }), /address/);
  assert.throws(() => toTextRecords('agent', { erc8004_agent_id: 'abc' }), /decimal/);
  assert.throws(() => toTextRecords('job', []), /object/);
  assert.deepEqual(toTextRecords('job', { status: '', mandate: null }), []);
});

test('endpoint, manifest and payout records', () => {
  const out = toTextRecords('agent', {
    a2a: 'https://agent.example/a2a', web: 'https://app.example/agents/1', manifest_hash: '0x' + 'ab'.repeat(32),
  });
  assert.deepEqual(out.map((e) => e.key), ['agent-endpoint[a2a]', 'agent-endpoint[web]', 'manifest-hash']);
  assert.throws(() => toTextRecords('agent', { web: 'http://localhost:8090' }), /https/);
  assert.throws(() => toTextRecords('agent', { manifest_hash: '0x1234' }), /32-byte/);
  const addr = decodeFunctionData({ abi: resolverAbi, data: setAddressCall('a.eth', '0x00000000000000000000000000000000000a11ce') });
  assert.deepEqual([addr.functionName, addr.args[1], addr.args[2]],
    ['setAddress', 60n, '0x00000000000000000000000000000000000a11ce']);
  assert.equal(decodeFunctionData({ abi: resolverAbi, data: setAddressCall('a.eth', null) }).args[2], '0x');
});

test('labels are strict', () => {
  assert.equal(assertLabel('eng-01abc'), 'eng-01abc');
  for (const bad of ['', 'UPPER', '-x', 'x-', 'a.b', 'a'.repeat(64), 7]) {
    assert.throws(() => assertLabel(bad), /label/);
  }
});
