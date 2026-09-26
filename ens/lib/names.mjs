// Name encoding, salts and record translation (logical record -> ENS text key).
import { encodeAbiParameters, encodeFunctionData, isAddress, keccak256, stringToHex, toHex } from 'viem';
import { labelhash, namehash, packetToBytes } from 'viem/ens';

import { resolverAbi } from './abis.mjs';
import {
  ALLOWED_RECORDS, ENDPOINT_RECORDS, ETH_COIN_TYPE, RECORD_KEYS, REGISTRATION_RECORD, registrationKey,
} from './constants.mjs';
import { badRequest } from './errors.mjs';

const LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const MAX_VALUE_LENGTH = 4096;

export function assertLabel(label, field = 'label') {
  if (typeof label !== 'string' || !LABEL_RE.test(label)) {
    throw badRequest(`${field} must be 1-63 chars of a-z, 0-9 and inner hyphens`, 'INVALID_LABEL');
  }
  return label;
}

export const labelOf = (name) => name.split('.')[0];
export const parentOf = (name) => name.split('.').slice(1).join('.');
export const tokenId = (label) => BigInt(labelhash(label));

// Resolver setters take the DNS wire-format name, not the namehash.
export const dnsEncode = (name) => toHex(packetToBytes(name));

// Per-key role resource on PermissionedResolver: keccak256 of the full key.
export const keyResource = (key) => BigInt(keccak256(stringToHex(key)));

// CREATE2 salt for VerifiableFactory proxies. "UserRegistry" follows the
// documented convention so standard tooling can discover the registries.
export function proxySalt(kind, name, version = 0) {
  return BigInt(keccak256(encodeAbiParameters(
    [{ type: 'bytes32' }, { type: 'bytes32' }, { type: 'uint256' }],
    [keccak256(stringToHex(kind)), namehash(name), BigInt(version)],
  )));
}

// Translate {logicalName: value} into [{field, key, value, input}] ENS text
// records (`input` is the caller's value; `value` is what goes on-chain).
export function toTextRecords(kind, records = {}) {
  if (records === null || typeof records !== 'object' || Array.isArray(records)) {
    throw badRequest('records must be an object');
  }
  const allowed = ALLOWED_RECORDS[kind];
  const out = [];
  for (const [field, raw] of Object.entries(records)) {
    if (raw === null || raw === undefined || raw === '') continue;
    if (!allowed.includes(field)) throw badRequest(`record ${field} is not allowed on a ${kind} name`, 'INVALID_RECORD');
    const value = String(raw);
    if (value.length > MAX_VALUE_LENGTH) throw badRequest(`record ${field} is too long`, 'INVALID_RECORD');
    if (field === REGISTRATION_RECORD) {
      if (!/^\d+$/.test(value)) throw badRequest('erc8004_agent_id must be a decimal token id', 'INVALID_RECORD');
      out.push({ field, key: registrationKey(value), value: '1', input: value });
      continue;
    }
    if (ENDPOINT_RECORDS.includes(field) && !/^(https|ipfs):\/\/\S+$/.test(value)) {
      throw badRequest(`${field} endpoint must be an https:// or ipfs:// URL`, 'INVALID_RECORD');
    }
    if (field === 'payout' && !isAddress(value)) {
      throw badRequest('payout must be a 20-byte address', 'INVALID_RECORD');
    }
    if (field === 'manifest_hash' && !/^0x[0-9a-fA-F]{64}$/.test(value)) {
      throw badRequest('manifest_hash must be a 0x-prefixed 32-byte hex hash', 'INVALID_RECORD');
    }
    out.push({ field, key: RECORD_KEYS[field], value, input: value });
  }
  return out;
}

export function setTextCalls(name, entries) {
  const dns = dnsEncode(name);
  return entries.map(({ key, value }) => encodeFunctionData({
    abi: resolverAbi, functionName: 'setText', args: [dns, key, value],
  }));
}

// The ETH address record, so the name resolves to the payee in any ENS
// client. No address clears it.
export const setAddressCall = (name, address) => encodeFunctionData({
  abi: resolverAbi, functionName: 'setAddress', args: [dnsEncode(name), ETH_COIN_TYPE, address || '0x'],
});

// Setter calldata for grantSetterRoles: only the selector and the key matter.
export const setterCalldata = (key) => encodeFunctionData({
  abi: resolverAbi, functionName: 'setText', args: ['0x', key, ''],
});

// grantSetterRoles calls, for a resolver multicall.
export const grantCalls = (keys, account) => keys.map((key) => encodeFunctionData({
  abi: resolverAbi, functionName: 'grantSetterRoles', args: [setterCalldata(key), account],
}));
