// Name lifecycle on ENSv2 Sepolia.
//
//   <root>.eth                       platform-owned; subregistry = platform UserRegistry
//   <agent>.<root>.eth               own PermissionedResolver (ENSIP-25/26 records)
//                                    + own UserRegistry that holds the agent's jobs
//   <job>.<agent>.<root>.eth         own PermissionedResolver, expiry = SOW deadline,
//                                    owner = platform with no roles (non-transferable);
//                                    the hired agent may set ONLY `status` and `deliverable`
//   <sub>.<job>.<agent>.<root>.eth   wildcard records on the job's resolver (no registry),
//                                    so they expire and revoke together with the job
//
// Resolver roles are per resolver instance, not per name, which is why every
// job gets its own resolver: a key-scoped grant then reaches that job only.
// Never write default (root-name) records on a resolver: once a job expires
// the Universal Resolver falls back to the closest ancestor resolver.
//
// Every multi-step operation records its progress on the node, so a retry
// after a failure resumes instead of re-deploying (CREATE2 salts would clash).
import { encodeFunctionData, getAddress, isAddress, isAddressEqual, toHex, zeroAddress, zeroHash } from 'viem';

import { ethRegistrarAbi, mockUsdcAbi, registryAbi, resolverAbi } from './abis.mjs';
import { checkAddresses } from './check.mjs';
import {
  ADDRESSES, AGENT_WRITABLE_JOB_KEYS, ALL_ROLES, ALLOWED_RECORDS, COMMIT_WAIT_SECONDS,
  DEFAULT_AGENT_TTL_SECONDS, DEFAULT_ROOT_DURATION_SECONDS, DEFAULT_ROOT_LABEL, MOCK_USDC_MINT,
  RECORD_KEYS, RESOLVER_ROLES,
} from './constants.mjs';
import { badRequest, notFound, SidecarError } from './errors.mjs';
import {
  assertLabel, keyResource, labelOf, parentOf, proxySalt, setTextCalls,
  setterCalldata, toTextRecords, tokenId,
} from './names.mjs';
import { NameStore } from './store.mjs';

const KINDS = ['root', 'agent', 'job', 'subjob'];
const MAX_UINT64 = 2n ** 64n - 1n;
const MIN_ROOT_DURATION_DAYS = 28;

function serial() {
  let tail = Promise.resolve();
  return (fn) => {
    const run = tail.then(fn);
    tail = run.catch(() => {});
    return run;
  };
}

const conflict = (message, code) => new SidecarError(409, code, message);

export class NamesService {
  constructor({ exec, store = new NameStore(), rootLabel = DEFAULT_ROOT_LABEL, now, random } = {}) {
    this.exec = exec;
    this.store = store;
    this.rootLabel = assertLabel(rootLabel, 'ENS_ROOT_LABEL');
    this.rootName = `${rootLabel}.eth`;
    this.now = now || (() => Math.floor(Date.now() / 1000));
    this.random = random || (() => toHex(crypto.getRandomValues(new Uint8Array(32))));
    this.lock = serial();
    this.addressCheck = null;
  }

  async init() {
    this.addressCheck = await checkAddresses(this.exec);
    return this.addressCheck;
  }

  kindOf(name) {
    if (name !== this.rootName && !name.endsWith(`.${this.rootName}`)) {
      throw badRequest(`${name} is not under ${this.rootName}`, 'OUTSIDE_ROOT');
    }
    const kind = KINDS[name.split('.').length - this.rootName.split('.').length];
    if (!kind) throw badRequest(`${name} is nested too deep`, 'INVALID_NAME');
    return kind;
  }

  health() {
    const root = this.store.get(this.rootName);
    return {
      ok: Boolean(this.addressCheck?.ok),
      mode: this.exec.mode,
      operator: this.exec.operator,
      root: this.rootName,
      root_status: root?.status || 'missing',
      address_check: this.addressCheck,
    };
  }

  // ── Root ────────────────────────────────────────────────────────────────

  setupRoot(body = {}) {
    return this.lock(async () => {
      this._ready();
      const days = body.duration_days ?? 365;
      if (!Number.isInteger(days) || days < MIN_ROOT_DURATION_DAYS) {
        throw badRequest(`duration_days must be an integer >= ${MIN_ROOT_DURATION_DAYS}`);
      }
      const duration = body.duration_days ? BigInt(days) * 86400n : DEFAULT_ROOT_DURATION_SECONDS;
      const label = this.rootLabel;
      const op = this.exec.operator;
      const node = this._open(this.rootName, { kind: 'root', parent: 'eth' });
      if (node.status === 'active') return this._view(node, { existing: true });

      if (!node.resolver) node.resolver = await this._deployResolver(node, []);
      if (!node.registry) node.registry = await this._deployRegistry(node);

      if (!node.registered) {
        const available = await this.exec.read({
          address: ADDRESSES.ethRegistrar, abi: ethRegistrarAbi, functionName: 'isAvailable', args: [label],
        });
        if (available) {
          await this._write(node, 'mint', ADDRESSES.mockUsdc, mockUsdcAbi, 'mint', [op, MOCK_USDC_MINT]);
          const [base, premium] = await this.exec.read({
            address: ADDRESSES.ethRegistrar, abi: ethRegistrarAbi, functionName: 'getRegisterPrice',
            args: [label, duration, ADDRESSES.mockUsdc],
          });
          await this._write(node, 'approve', ADDRESSES.mockUsdc, mockUsdcAbi, 'approve',
            [ADDRESSES.ethRegistrar, base + premium]);
          node.secret = node.secret || this.random();
          const commitment = await this.exec.read({
            address: ADDRESSES.ethRegistrar, abi: ethRegistrarAbi, functionName: 'makeCommitment',
            args: [label, op, node.secret, zeroAddress, node.resolver, duration, zeroHash],
          });
          await this._write(node, 'commit', ADDRESSES.ethRegistrar, ethRegistrarAbi, 'commit', [commitment]);
          await this.exec.sleep(COMMIT_WAIT_SECONDS);
          await this._write(node, 'register', ADDRESSES.ethRegistrar, ethRegistrarAbi, 'register',
            [label, op, node.secret, zeroAddress, node.resolver, duration, ADDRESSES.mockUsdc, zeroHash]);
          node.expiry = this.now() + Number(duration);
        } else {
          const state = await this.exec.read({
            address: ADDRESSES.ethRegistry, abi: registryAbi, functionName: 'getState', args: [tokenId(label)],
          });
          if (!isAddressEqual(state.latestOwner, op)) {
            throw conflict(`${this.rootName} is registered to another account`, 'NAME_TAKEN');
          }
          node.expiry = Number(state.expiry);
        }
        node.registered = true;
        node.owner = op;
        this.store.save();
      }
      if (!node.subregistry_set) {
        await this._write(node, 'setSubregistry', ADDRESSES.ethRegistry, registryAbi, 'setSubregistry',
          [tokenId(label), node.registry]);
        node.subregistry_set = true;
      }
      if (!node.parent_set) {
        await this._write(node, 'setParent', node.registry, registryAbi, 'setParent', [ADDRESSES.ethRegistry, label]);
        node.parent_set = true;
      }
      return this._activate(node);
    });
  }

  // ── Agent ───────────────────────────────────────────────────────────────

  createAgent(body = {}) {
    return this.lock(async () => {
      this._ready();
      const label = assertLabel(body.label);
      const publicId = String(body.agent_public_id || '');
      if (!/^[A-Za-z0-9-]{4,32}$/.test(publicId)) throw badRequest('agent_public_id is required');
      const entries = toTextRecords('agent', body.records || {});
      const name = `${label}.${this.rootName}`;
      const existing = this.store.get(name);
      if (existing && existing.status !== 'revoked' && existing.agent_public_id !== publicId) {
        throw conflict(`${name} belongs to another agent`, 'NAME_TAKEN');
      }
      const node = this._open(name, { kind: 'agent', parent: this.rootName, agent_public_id: publicId, entries });
      if (node.status === 'active') return this._view(node, { existing: true });

      const parentRegistry = await this._registryOf(this.rootName);
      if (!node.resolver) node.resolver = await this._deployResolver(node, entries);
      if (!node.registry) node.registry = await this._deployRegistry(node);
      if (!node.registered) {
        const rootExpiry = this.store.get(this.rootName)?.expiry;
        let expiry = this.now() + Number(DEFAULT_AGENT_TTL_SECONDS);
        if (rootExpiry && rootExpiry < expiry) expiry = rootExpiry;
        await this._write(node, 'register', parentRegistry, registryAbi, 'register',
          [label, this.exec.operator, node.registry, node.resolver, 0n, BigInt(expiry)]);
        node.registered = true;
        node.expiry = expiry;
        node.owner = this.exec.operator;
      }
      return this._activate(node);
    });
  }

  // ── Job ─────────────────────────────────────────────────────────────────

  createJob(body = {}) {
    return this.lock(async () => {
      this._ready();
      const label = assertLabel(body.label);
      const parent = String(body.parent || '');
      if (this.kindOf(parent) !== 'agent') throw badRequest('parent must be an agent name');
      const expiry = this._expiry(body.expiry);
      const grantee = this._address(body.grantee);
      const entries = toTextRecords('job', body.records || {});
      const name = `${label}.${parent}`;
      const node = this._open(name, { kind: 'job', parent, entries, expiry });
      if (node.status === 'active') return this._view(node, { existing: true });

      const agentRegistry = await this._registryOf(parent);
      if (!node.resolver) node.resolver = await this._deployResolver(node, entries);
      if (!node.registered) {
        // Owner = platform, roleBitmap 0: the name can't be transferred, and
        // only the platform (root roles on the registry) can unregister it.
        await this._write(node, 'register', agentRegistry, registryAbi, 'register',
          [label, this.exec.operator, zeroAddress, node.resolver, 0n, BigInt(expiry)]);
        node.registered = true;
        node.owner = this.exec.operator;
      }
      if (grantee) {
        node.grants = node.grants || [];
        for (const field of AGENT_WRITABLE_JOB_KEYS) {
          const key = RECORD_KEYS[field];
          if (node.grants.some((g) => g.key === key && isAddressEqual(g.account, grantee))) continue;
          await this._write(node, `grant:${key}`, node.resolver, resolverAbi, 'grantSetterRoles',
            [setterCalldata(key), grantee]);
          node.grants.push({ key, account: grantee });
          this.store.save();
        }
      }
      return this._activate(node);
    });
  }

  // ── Sub-job (wildcard records on the job resolver) ─────────────────────

  createSubjob(body = {}) {
    return this.lock(async () => {
      this._ready();
      const label = assertLabel(body.label);
      const parent = String(body.parent || '');
      if (this.kindOf(parent) !== 'job') throw badRequest('parent must be a job name');
      const entries = toTextRecords('subjob', body.records || {});
      if (!entries.length) throw badRequest('a sub-job needs at least one record to resolve');
      // Wildcard records live and die with the job, so their expiry is the job's.
      let expiry = this._expiry(body.expiry);
      const parentExpiry = this.store.get(parent)?.expiry;
      if (parentExpiry && parentExpiry < expiry) expiry = parentExpiry;
      const name = `${label}.${parent}`;
      const node = this._open(name, { kind: 'subjob', parent, entries, expiry });
      if (node.status === 'active') return this._view(node, { existing: true });

      node.resolver = node.resolver || await this._resolverOf(parent);
      await this._write(node, 'records', node.resolver, resolverAbi, 'multicall', [setTextCalls(name, entries)]);
      node.owner = this.exec.operator;
      return this._activate(node);
    });
  }

  // ── Revoke ──────────────────────────────────────────────────────────────

  revoke(body = {}) {
    return this.lock(async () => {
      this._ready();
      const name = String(body.name || '');
      const kind = this.kindOf(name);
      if (kind === 'root') throw badRequest('the root name cannot be revoked here');
      const node = this.store.get(name) || this.store.put(this._newNode(name, { kind, parent: parentOf(name) }));
      if (node.status === 'revoked') return this._view(node, { existing: true });

      if (kind === 'subjob') {
        // Clear every key a sub-job may carry; empty text == no record.
        const resolver = node.resolver || await this._resolverOf(node.parent);
        const keys = ALLOWED_RECORDS.subjob.map((f) => ({ key: RECORD_KEYS[f], value: '' }));
        await this._write(node, 'clear', resolver, resolverAbi, 'multicall', [setTextCalls(name, keys)]);
      } else {
        if (kind === 'job') {
          const resolver = node.resolver || await this._resolverOf(name);
          // After a lost state file the caller names the grantee explicitly.
          const grantee = this._address(body.grantee);
          const grants = node.grants?.length ? node.grants
            : grantee ? AGENT_WRITABLE_JOB_KEYS.map((f) => ({ key: RECORD_KEYS[f], account: grantee })) : [];
          for (const g of grants.filter((x) => !x.revoked)) {
            await this._write(node, `revoke:${g.key}`, resolver, resolverAbi, 'revokeRoles',
              [keyResource(g.key), RESOLVER_ROLES.SET_TEXT, g.account]);
            g.revoked = true;
          }
          node.grants = grants;
        }
        const parentRegistry = await this._registryOf(node.parent);
        await this._write(node, 'unregister', parentRegistry, registryAbi, 'unregister', [tokenId(labelOf(name))]);
      }
      node.status = 'revoked';
      node.revoked_at = this.now();
      for (const child of this.store.descendants(name)) {
        child.status = 'revoked';
        child.revoked_at = node.revoked_at;
      }
      this.store.save();
      return this._view(node);
    });
  }

  // ── Tree ────────────────────────────────────────────────────────────────

  async tree({ root, live = false } = {}) {
    const name = root || this.rootName;
    const tree = this.store.subtree(name);
    if (!tree) throw notFound(`${name} is not known to this sidecar`);
    const view = (n) => ({ ...this._view(n), children: n.children.map(view) });
    const out = view(tree);
    if (live && this.exec.mode === 'live') await this._readLive(out);
    return { mode: this.exec.mode, root: name, tree: out };
  }

  async _readLive(node) {
    if (node.status === 'active') {
      node.onchain = {};
      for (const key of Object.keys(node.text || {})) {
        try {
          node.onchain[key] = await this.exec.readText(node.name, key);
        } catch (err) {
          node.onchain[key] = { error: err.shortMessage || err.message };
        }
      }
    }
    for (const child of node.children) await this._readLive(child);
  }

  // ── Internals ───────────────────────────────────────────────────────────

  _ready() {
    if (!this.addressCheck?.ok) {
      const why = this.addressCheck?.problems?.join('; ') || 'not run';
      throw new SidecarError(503, 'ADDRESS_CHECK_FAILED', `refusing writes: ${why}`);
    }
  }

  _address(raw) {
    if (!raw) return null;
    if (!isAddress(String(raw))) throw badRequest('grantee must be an address');
    return getAddress(raw);
  }

  _expiry(raw) {
    const expiry = Number(raw);
    if (!Number.isSafeInteger(expiry) || expiry <= this.now() || BigInt(expiry) > MAX_UINT64) {
      throw badRequest('expiry must be a future unix timestamp in seconds', 'INVALID_EXPIRY');
    }
    return expiry;
  }

  _newNode(name, { kind, parent, agent_public_id, entries = [], expiry = null, version = 0 }) {
    return {
      name, kind, parent, label: labelOf(name), version, status: 'pending',
      agent_public_id: agent_public_id || null, owner: null, expiry,
      records: Object.fromEntries(entries.map((e) => [e.field, e.input])),
      text: Object.fromEntries(entries.map((e) => [e.key, e.value])),
      resolver: null, registry: null, grants: [], txs: [],
    };
  }

  // Reuse a pending node (resume) or start a fresh one; a revoked name gets
  // a new version so its CREATE2 salts don't collide with the old proxies.
  _open(name, opts) {
    const existing = this.store.get(name);
    if (existing && existing.status !== 'revoked') return existing;
    const version = existing ? existing.version + 1 : 0;
    return this.store.put(this._newNode(name, { ...opts, version }));
  }

  _activate(node) {
    node.status = 'active';
    this.store.save();
    return this._view(node);
  }

  _view(node, extra = {}) {
    const { secret, registered, subregistry_set, parent_set, children, ...rest } = node;
    return { ...rest, ...extra };
  }

  async _write(node, step, address, abi, functionName, args) {
    const { hash } = await this.exec.write({ address, abi, functionName, args });
    node.txs.push({ step, hash });
    this.store.save();
    return hash;
  }

  async _deployResolver(node, entries) {
    const data = encodeFunctionData({
      abi: resolverAbi, functionName: 'initialize',
      args: [[{ account: this.exec.operator, roleBitmap: ALL_ROLES }], setTextCalls(node.name, entries)],
    });
    return this._deploy(node, 'deploy:resolver', ADDRESSES.permissionedResolverImpl,
      proxySalt('PermissionedResolver', node.name, node.version), data);
  }

  async _deployRegistry(node) {
    const data = encodeFunctionData({
      abi: registryAbi, functionName: 'initialize',
      args: [[{ account: this.exec.operator, roleBitmap: ALL_ROLES }]],
    });
    return this._deploy(node, 'deploy:registry', ADDRESSES.userRegistryImpl,
      proxySalt('UserRegistry', node.name, node.version), data);
  }

  async _deploy(node, step, implementation, salt, data) {
    const { hash, address } = await this.exec.deployProxy({ implementation, salt, data });
    node.txs.push({ step, hash });
    this.store.save();
    return address;
  }

  // Registry that holds the children of `name` (from state, else read on-chain).
  async _registryOf(name) {
    const known = this.store.get(name);
    if (known?.registry && known.status === 'active') return known.registry;
    const holder = name === this.rootName ? ADDRESSES.ethRegistry : await this._registryOf(parentOf(name));
    const reg = await this.exec.read({
      address: holder, abi: registryAbi, functionName: 'getSubregistry', args: [labelOf(name)],
    });
    if (!reg || isAddressEqual(reg, zeroAddress)) throw conflict(`${name} is not set up yet`, 'PARENT_NOT_READY');
    return reg;
  }

  async _resolverOf(name) {
    const known = this.store.get(name);
    if (known?.resolver && known.status === 'active') return known.resolver;
    const holder = await this._registryOf(parentOf(name));
    const res = await this.exec.read({
      address: holder, abi: registryAbi, functionName: 'getResolver', args: [labelOf(name)],
    });
    if (!res || isAddressEqual(res, zeroAddress)) throw conflict(`${name} has no resolver`, 'PARENT_NOT_READY');
    return res;
  }
}

