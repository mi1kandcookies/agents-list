// Transaction executors. Both expose the same surface:
//   read({address, abi, functionName, args})         -> value
//   write({address, abi, functionName, args})        -> {hash}
//   deployProxy({implementation, salt, data})        -> {hash, address}
//   readText(name, key)                              -> string | null
//   sleep(seconds), chainId()
// DryRunExecutor never opens a network connection: it returns deterministic
// fake hashes/addresses and answers the handful of reads the service makes.
import {
  createPublicClient, createWalletClient, encodeAbiParameters, getAddress, http,
  keccak256, parseEventLogs, slice, stringToHex, zeroAddress,
} from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { sepolia } from 'viem/chains';

import { factoryAbi } from './abis.mjs';
import { ADDRESSES, SEPOLIA_CHAIN_ID } from './constants.mjs';
import { SidecarError } from './errors.mjs';

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

export class DryRunExecutor {
  constructor({ operator = DRY_RUN_OPERATOR, reads = {} } = {}) {
    this.mode = 'dry_run';
    this.operator = getAddress(operator);
    this.calls = [];
    this.slept = 0;
    this.overrides = reads; // {functionName: (args, address) => value}
    this.seq = 0;
    this.taken = new Set();
  }

  async chainId() { return SEPOLIA_CHAIN_ID; }

  async write({ address, functionName, args = [] }) {
    this.seq += 1;
    const hash = keccak256(stringToHex(`dryrun:${this.seq}:${stringify([address, functionName, args])}`));
    this.calls.push({ address, functionName, args, hash });
    if (functionName === 'register' && address === ADDRESSES.ethRegistrar) this.taken.add(args[0]);
    return { hash };
  }

  async deployProxy({ implementation, salt, data }) {
    const { hash } = await this.write({
      address: ADDRESSES.verifiableFactory, functionName: 'deployProxy', args: [implementation, salt, data],
    });
    const digest = keccak256(encodeAbiParameters(
      [{ type: 'address' }, { type: 'uint256' }, { type: 'address' }], [this.operator, salt, implementation],
    ));
    return { hash, address: getAddress(slice(digest, 12)) };
  }

  async read({ address, functionName, args = [] }) {
    if (this.overrides[functionName]) return this.overrides[functionName](args, address);
    switch (functionName) {
      case 'ROOT_REGISTRY': return getAddress(ADDRESSES.rootRegistry);
      case 'ETH_REGISTRY': return getAddress(ADDRESSES.ethRegistry);
      case 'getSubregistry': return args[0] === 'eth' ? getAddress(ADDRESSES.ethRegistry) : zeroAddress;
      case 'getResolver': return zeroAddress;
      case 'isAvailable': return !this.taken.has(args[0]);
      case 'getRegisterPrice': return [8_000_021n, 0n];
      case 'makeCommitment': return keccak256(stringToHex(stringify(args)));
      default: throw new Error(`dry run has no stub for ${functionName}`);
    }
  }

  async readText() { return null; }

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

  sleep(seconds) { return new Promise((resolve) => setTimeout(resolve, seconds * 1000)); }
}
