// Transaction executors. Both expose the same surface:
//   read({address, abi, functionName, args})         -> value
//   write({address, abi, functionName, args})        -> {hash}
//   deployProxy({implementation, salt, data})        -> {hash, address}
//   readText(name, key)                              -> string | null
//   readAddress(name)                                -> address | null (ETH)
//   sleep(seconds), chainId()
// DryRunExecutor never opens a network connection: it returns deterministic
// fake hashes/addresses and answers the handful of reads the service makes.
import {
  createPublicClient, createWalletClient, decodeFunctionData, encodeAbiParameters, encodeFunctionData, getAddress, http,
  isAddressEqual, keccak256, parseEventLogs, slice, stringToHex, zeroAddress,
} from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { sepolia } from 'viem/chains';
import { labelhash } from 'viem/ens';

import { factoryAbi, resolverAbi } from './abis.mjs';
import { ADDRESSES, ETH_COIN_TYPE, SEPOLIA_CHAIN_ID } from './constants.mjs';
import { SidecarError } from './errors.mjs';
import { dnsEncode, keyResource } from './names.mjs';

const stringify = (value) => JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));

// Serialize writes so nonces never race; a failed job doesn't block the queue.
function serial() {
  let tail = Promise.resolve();
  return (fn) => {
    const run = tail.then(fn);
    tail = run.catch(() => {});
    return run;
  };
}

export const DRY_RUN_OPERATOR = '0x00000000000000000000000000000000000d0e5e';

const MAX_EXPIRY = 2n ** 64n - 1n;
const entryKey = (registry, id) => `${registry.toLowerCase()}:${BigInt(id).toString(16)}`;

export class DryRunExecutor {
  constructor({ operator = DRY_RUN_OPERATOR, reads = {} } = {}) {
    this.mode = 'dry_run';
    this.operator = getAddress(operator);
    this.calls = [];
    this.slept = 0;
    this.overrides = reads; // {functionName: (args, address) => value}
    this.seq = 0;
    this.taken = new Set();
    // Registry entries and parents written so far, so reads reflect them.
    this.entries = new Map();
    this.parents = new Map();
    this.deployed = new Set();
    // Resolver records and key grants written so far (any resolver).
    this.texts = new Map();
    this.addrs = new Map();
    this.roles = new Set();
  }

  async chainId() { return SEPOLIA_CHAIN_ID; }

  async write({ address, functionName, args = [] }) {
    // Same as on-chain: a live registration can't be registered again.
    if (functionName === 'register' && !isAddressEqual(address, ADDRESSES.ethRegistrar) && this._entry(address, args[0])) {
      throw new Error(`dry run: ${args[0]} is already registered`);
    }
    this.seq += 1;
    const hash = keccak256(stringToHex(`dryrun:${this.seq}:${stringify([address, functionName, args])}`));
    this.calls.push({ address, functionName, args, hash });
    this._apply(address, functionName, args);
    return { hash };
  }

  _apply(address, functionName, args) {
    if (functionName === 'register' && isAddressEqual(address, ADDRESSES.ethRegistrar)) {
      // register(label, owner, secret, subregistry, resolver, duration, token, referrer)
      this.taken.add(args[0]);
      this.entries.set(entryKey(ADDRESSES.ethRegistry, labelhash(args[0])),
        { owner: args[1], subregistry: args[3], resolver: args[4], expiry: MAX_EXPIRY });
    } else if (functionName === 'register') {
      // register(label, owner, registry, resolver, roleBitmap, expiry)
      this.entries.set(entryKey(address, labelhash(args[0])),
        { owner: args[1], subregistry: args[2], resolver: args[3], expiry: args[5] });
    } else if (functionName === 'setSubregistry') {
      const entry = this.entries.get(entryKey(address, args[0]));
      if (entry) entry.subregistry = args[1];
    } else if (functionName === 'unregister') {
      this.entries.delete(entryKey(address, args[0]));
    } else if (functionName === 'setParent') {
      this.parents.set(address.toLowerCase(), [args[0], args[1]]);
    } else if (functionName === 'multicall') {
      this._applyResolver(address, args[0]);
    } else if (['setText', 'setAddress', 'grantSetterRoles', 'revokeRoles'].includes(functionName)) {
      this._applyResolver(address, [encodeFunctionData({ abi: resolverAbi, functionName, args })]);
    }
  }

  _applyResolver(resolver, calls) {
    const at = resolver.toLowerCase();
    for (const data of calls) {
      const { functionName, args } = decodeFunctionData({ abi: resolverAbi, data });
      if (functionName === 'setText') {
        this.texts.set(`${args[0]}|${args[1]}`, args[2]);
      } else if (functionName === 'setAddress' && args[1] === ETH_COIN_TYPE) {
        this.addrs.set(args[0], args[2] === '0x' ? null : getAddress(args[2]));
      } else if (functionName === 'grantSetterRoles') {
        const key = decodeFunctionData({ abi: resolverAbi, data: args[0] }).args[1];
        this.roles.add(`${at}|${keyResource(key)}|${args[1].toLowerCase()}`);
      } else if (functionName === 'revokeRoles') {
        this.roles.delete(`${at}|${args[0]}|${args[2].toLowerCase()}`);
      }
    }
  }

  _entry(address, label) { return this.entries.get(entryKey(address, labelhash(label))); }

  async deployProxy({ implementation, salt, data }) {
    const digest = keccak256(encodeAbiParameters(
      [{ type: 'address' }, { type: 'uint256' }, { type: 'address' }], [this.operator, salt, implementation],
    ));
    const address = getAddress(slice(digest, 12));
    // Same as on-chain: a CREATE2 salt can only be used once.
    if (this.deployed.has(address)) throw new Error(`dry run: a proxy is already deployed at ${address}`);
    const { hash } = await this.write({
      address: ADDRESSES.verifiableFactory, functionName: 'deployProxy', args: [implementation, salt, data],
    });
    this.deployed.add(address);
    if (isAddressEqual(implementation, ADDRESSES.permissionedResolverImpl)) {
      this._applyResolver(address, decodeFunctionData({ abi: resolverAbi, data }).args[1]);
    }
    return { hash, address };
  }

  async read({ address, functionName, args = [] }) {
    if (this.overrides[functionName]) return this.overrides[functionName](args, address);
    switch (functionName) {
      case 'ROOT_REGISTRY': return getAddress(ADDRESSES.rootRegistry);
      case 'ETH_REGISTRY': return getAddress(ADDRESSES.ethRegistry);
      case 'getSubregistry':
        if (args[0] === 'eth') return getAddress(ADDRESSES.ethRegistry);
        return this._entry(address, args[0])?.subregistry || zeroAddress;
      case 'getResolver': return this._entry(address, args[0])?.resolver || zeroAddress;
      case 'getState': {
        const entry = this.entries.get(entryKey(address, args[0]));
        return entry
          ? { status: 2, expiry: BigInt(entry.expiry), latestOwner: entry.owner, tokenId: args[0], resource: 0n }
          : { status: 0, expiry: 0n, latestOwner: zeroAddress, tokenId: args[0], resource: 0n };
      }
      case 'getParent': return this.parents.get(address.toLowerCase()) || [zeroAddress, ''];
      case 'hasRoles': return this.roles.has(`${address.toLowerCase()}|${args[0]}|${args[2].toLowerCase()}`);
      case 'isAvailable': return !this.taken.has(args[0]);
      case 'getRegisterPrice': return [8_000_021n, 0n];
      case 'makeCommitment': return keccak256(stringToHex(stringify(args)));
      default: throw new Error(`dry run has no stub for ${functionName}`);
    }
  }

  async readText(name, key) { return this.texts.get(`${dnsEncode(name)}|${key}`) || null; }

  async readAddress(name) { return this.addrs.get(dnsEncode(name)) || null; }

  async sleep(seconds) { this.slept += seconds; }
}

export class LiveExecutor {
  constructor({ privateKey, rpcUrl }) {
    if (!/^0x[0-9a-fA-F]{64}$/.test(privateKey || '')) {
      throw new Error('ENS_OPERATOR_PRIVATE_KEY must be a 0x-prefixed 32-byte hex key');
    }
    if (!rpcUrl) throw new Error('SEPOLIA_RPC_URL is required when DRY_RUN is off');
    this.mode = 'live';
    this.account = privateKeyToAccount(privateKey);
    this.operator = this.account.address;
    const transport = http(rpcUrl, { timeout: 30_000 });
    this.public = createPublicClient({ chain: sepolia, transport });
    this.wallet = createWalletClient({ account: this.account, chain: sepolia, transport });
    this.queue = serial();
  }

  chainId() { return this.public.getChainId(); }

  read({ address, abi, functionName, args = [] }) {
    return this.public.readContract({ address, abi, functionName, args });
  }

  write(req) {
    return this.queue(async () => {
      // Simulate first so reverts surface with a decoded reason and no gas spent.
      const { request } = await this.public.simulateContract({ ...req, account: this.account });
      const hash = await this.wallet.writeContract(request);
      const receipt = await this.public.waitForTransactionReceipt({ hash, timeout: 180_000 });
      if (receipt.status !== 'success') {
        throw new SidecarError(502, 'TX_REVERTED', `${req.functionName} reverted in ${hash}`);
      }
      return { hash, receipt };
    });
  }

  async deployProxy({ implementation, salt, data }) {
    const { hash, receipt } = await this.write({
      address: ADDRESSES.verifiableFactory, abi: factoryAbi, functionName: 'deployProxy',
      args: [implementation, salt, data],
    });
    const log = parseEventLogs({ abi: factoryAbi, logs: receipt.logs, eventName: 'ProxyDeployed' })
      .find((l) => l.args.sender.toLowerCase() === this.operator.toLowerCase());
    if (!log) throw new SidecarError(502, 'DEPLOY_FAILED', `no ProxyDeployed event in ${hash}`);
    return { hash, address: getAddress(log.args.proxyAddress) };
  }

  // Read through the canonical Universal Resolver, the same path any ENS
  // client takes, so this confirms the name actually resolves.
  readText(name, key) {
    return this.public.getEnsText({ name, key, universalResolverAddress: ADDRESSES.universalResolver });
  }

  readAddress(name) {
    return this.public.getEnsAddress({ name, universalResolverAddress: ADDRESSES.universalResolver });
  }

  sleep(seconds) { return new Promise((resolve) => setTimeout(resolve, seconds * 1000)); }
}
