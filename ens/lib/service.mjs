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
// An agent likewise gets setText rights on its own resolver for the endpoint
// keys it runs (mcp, a2a), never for payout or the manifest hash.
// Never write default (root-name) records on a resolver: once a job expires
// the Universal Resolver falls back to the closest ancestor resolver.
//
// Every multi-step operation records its progress on the node, so a retry
// after a failure resumes instead of re-deploying (CREATE2 salts would clash).
// A name this operator already holds on-chain but has no state for (issued
// from another host, or the state file was lost) is adopted, not redeployed.
import { encodeFunctionData, getAddress, isAddress, isAddressEqual, toHex, zeroAddress, zeroHash } from 'viem';

import { ethRegistrarAbi, mockUsdcAbi, registryAbi, resolverAbi } from './abis.mjs';
import { checkAddresses } from './check.mjs';
import {
  ADDRESSES, AGENT_WRITABLE_AGENT_KEYS, AGENT_WRITABLE_JOB_KEYS, ALL_ROLES, ALLOWED_RECORDS,
  COMMIT_WAIT_SECONDS, DEFAULT_AGENT_TTL_SECONDS, DEFAULT_ROOT_DURATION_SECONDS, DEFAULT_ROOT_LABEL,
  MOCK_USDC_MINT, NAME_STATUS, RECORD_KEYS, RESOLVER_ROLES,
} from './constants.mjs';
import { badRequest, notFound, SidecarError } from './errors.mjs';
import {
  assertLabel, grantCalls, keyResource, labelOf, parentOf, proxySalt, setAddressCall, setTextCalls,
  setterCalldata, toTextRecords, tokenId,
} from './names.mjs';
import { NameStore } from './store.mjs';

const KINDS = ['root', 'agent', 'job', 'subjob'];
const MAX_UINT64 = 2n ** 64n - 1n;
const MIN_ROOT_DURATION_DAYS = 28;
const COMMITMENT_REUSE_SECONDS = 23 * 3600; // registrar max commitment age is 24 h
// Public agent ids end in a check symbol, which can be one of * ~ $ =.
const PUBLIC_ID_RE = /^[A-Za-z0-9*~$=-]{4,32}$/;
const AGENT_KEYS = AGENT_WRITABLE_AGENT_KEYS.map((f) => RECORD_KEYS[f]);
// Agent-name records only the platform writes.
const PLATFORM_KEYS = ['context', 'web', 'payout', 'manifest_hash'].map((f) => RECORD_KEYS[f]);
const sameAddress = (a, b) => (a && b ? isAddressEqual(a, b) : !a && !b);

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
    if (typeof name !== 'string' || !name) throw badRequest('name is required', 'INVALID_NAME');
    name.split('.').slice(0, -1).forEach((l) => assertLabel(l, 'name'));
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

      if (!node.registered) {
        const available = await this.exec.read({
          address: ADDRESSES.ethRegistrar, abi: ethRegistrarAbi, functionName: 'isAvailable', args: [label],
        });
        if (!available) {
          // Already registered: reuse it (and its subregistry) if it is ours.
          if (!await this._adopt(node, ADDRESSES.ethRegistry)) {
            throw conflict(`${this.rootName} is registered to another account`, 'NAME_TAKEN');
          }
        } else if (body.adopt_only) {
          // Callers that only expect an existing root never register one by accident.
          throw conflict(`${this.rootName} is not registered yet`, 'ROOT_NOT_REGISTERED');
        } else {
          if (!node.resolver) node.resolver = await this._deployResolver(node, []);
          if (!node.registry) node.registry = await this._deployRegistry(node);
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
          // A commitment stays valid for 24 h; on resume reuse it instead of
          // re-committing (the registrar rejects a still-live duplicate).
          if (!node.committed_at || this.now() - node.committed_at > COMMITMENT_REUSE_SECONDS) {
            await this._write(node, 'commit', ADDRESSES.ethRegistrar, ethRegistrarAbi, 'commit', [commitment]);
            node.committed_at = this.now();
            this.store.save();
          }
          const wait = node.committed_at + COMMIT_WAIT_SECONDS - this.now();
          if (wait > 0) await this.exec.sleep(wait);
          await this._write(node, 'register', ADDRESSES.ethRegistrar, ethRegistrarAbi, 'register',
            [label, op, node.secret, zeroAddress, node.resolver, duration, ADDRESSES.mockUsdc, zeroHash]);
          node.expiry = this.now() + Number(duration);
          node.registered = true;
          node.owner = op;
          this.store.save();
        }
      }
      if (!node.registry) node.registry = await this._deployRegistry(node);
      if (!node.subregistry_set) {
        await this._write(node, 'setSubregistry', ADDRESSES.ethRegistry, registryAbi, 'setSubregistry',
          [tokenId(label), node.registry]);
        node.subregistry_set = true;
      }
      if (!node.parent_set) await this._setParent(node, ADDRESSES.ethRegistry);
      return this._activate(node);
    });
  }

  // ── Agent ───────────────────────────────────────────────────────────────

  createAgent(body = {}) {
    return this.lock(async () => {
      this._ready();
      const label = assertLabel(body.label);
      const publicId = String(body.agent_public_id || '');
      if (!PUBLIC_ID_RE.test(publicId)) throw badRequest('agent_public_id is required');
      const entries = toTextRecords('agent', body.records || {});
      // grantee: the address that may edit the endpoint records. Absent leaves
      // the grants as they are; null revokes them.
      const manageGrants = Object.hasOwn(body, 'grantee');
      const grantee = this._address(body.grantee);
      const name = `${label}.${this.rootName}`;
      const existing = this.store.get(name);
      if (existing && existing.status !== 'revoked' && existing.agent_public_id
          && existing.agent_public_id !== publicId) {
        throw conflict(`${name} belongs to another agent`, 'NAME_TAKEN');
      }
      const node = this._open(name, { kind: 'agent', parent: this.rootName, agent_public_id: publicId, entries });
      node.agent_public_id = node.agent_public_id || publicId;
      const parentRegistry = await this._registryOf(this.rootName);
      if (node.status === 'active') {
        // State from builds that did not track the address record and grants
        // is read back from the chain once, so syncs clear what is there.
        if (!node.reconciled) {
          const accounts = [grantee, ...node.grants.filter((g) => !g.revoked).map((g) => g.account)];
          Object.assign(node, await this._readAgentState(node.name, node.resolver, entries, accounts, node.text),
            { reconciled: true });
          this.store.save();
        }
        if (!node.parent_set) await this._setParent(node, parentRegistry);
        await this._syncAgent(node, entries, grantee, manageGrants);
        return this._view(node, { existing: true });
      }

      // Registered already (from another host, with a lost state file, or by a
      // register whose receipt never arrived): take the on-chain entry over.
      if (!node.registered) {
        await this._adopt(node, parentRegistry, { requireResolver: true, entries, accounts: [grantee] });
      }
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
      } else if (!node.subregistry_set) {
        // Adopted without a registry for its jobs: attach the one just deployed.
        await this._write(node, 'setSubregistry', parentRegistry, registryAbi, 'setSubregistry',
          [tokenId(label), node.registry]);
        node.subregistry_set = true;
      }
      if (!node.parent_set) await this._setParent(node, parentRegistry);
      await this._syncAgent(node, entries, grantee, manageGrants);
      return this._activate(node);
    });
  }

  // One multicall on the agent's resolver brings it to the requested state:
  // new or changed records, records the platform no longer sends (cleared),
  // the ETH address record, and setText grants on the endpoint keys for the
  // agent's current address. With manageGrants, grants held by any other
  // address are revoked in the same call (all of them when there is no
  // grantee), and the endpoint records written with them are reset.
  async _syncAgent(node, entries, grantee, manageGrants) {
    const wanted = new Set(entries.map((e) => e.key));
    const changed = entries.filter((e) => node.text[e.key] !== e.value);
    const cleared = [];
    for (const key of Object.keys(node.text).filter((k) => !wanted.has(k))) {
      if (!AGENT_KEYS.includes(key)) {
        cleared.push({ key, value: '' });
        continue;
      }
      // An endpoint the agent may edit is retracted only while it still holds
      // the platform's value; once the agent replaced it, it is the agent's.
      // If the read fails, it stays tracked and the next sync tries again.
      let onchain;
      try {
        onchain = await this.exec.readText(node.name, key);
      } catch {
        continue;
      }
      if (onchain === node.text[key]) cleared.push({ key, value: '' });
      else delete node.text[key];
    }
    const live = node.grants.filter((g) => !g.revoked);
    const stale = manageGrants ? live.filter((g) => !grantee || !isAddressEqual(g.account, grantee)) : [];
    if (stale.length) {
      // Taking grants away also takes back what was written with them: each
      // endpoint key goes back to the platform's value, or to empty.
      for (const key of AGENT_KEYS) {
        const entry = entries.find((e) => e.key === key);
        if (entry) {
          if (!changed.includes(entry)) changed.push(entry);
        } else if (!cleared.some((c) => c.key === key)) {
          cleared.push({ key, value: '' });
        }
      }
    }
    const grants = grantee
      ? AGENT_KEYS.filter((key) => !live.some((g) => g.key === key && isAddressEqual(g.account, grantee)))
      : [];
    const payout = entries.find((e) => e.key === RECORD_KEYS.payout)?.value || null;
    const addressChanged = node.addr === undefined || !sameAddress(node.addr, payout);
    const calls = [
      ...setTextCalls(node.name, [...changed, ...cleared]),
      ...(addressChanged ? [setAddressCall(node.name, payout)] : []),
      ...stale.map((g) => encodeFunctionData({
        abi: resolverAbi, functionName: 'revokeRoles', args: [keyResource(g.key), RESOLVER_ROLES.SET_TEXT, g.account],
      })),
      ...grantCalls(grants, grantee),
    ];
    if (calls.length) await this._write(node, 'records', node.resolver, resolverAbi, 'multicall', [calls]);
    for (const e of changed) node.text[e.key] = e.value;
    for (const e of cleared) delete node.text[e.key];
    node.records = Object.fromEntries(entries.map((e) => [e.field, e.input]));
    node.addr = payout;
    for (const g of stale) g.revoked = true;
    node.grants.push(...grants.map((key) => ({ key, account: grantee })));
    this.store.save();
  }

  // Link a registry to its parent so tools that walk the tree upward
  // (canonical-name checks, explorers) find the names below it.
  async _setParent(node, parentRegistry) {
    await this._write(node, 'setParent', node.registry, registryAbi, 'setParent', [parentRegistry, node.label]);
    node.parent_set = true;
    this.store.save();
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
      // A register whose receipt never arrived: the name may be live already.
      if (!node.registered && node.resolver) await this._adopt(node, agentRegistry);
      if (!node.resolver) node.resolver = await this._deployResolver(node, entries);
      if (!node.registered) {
        // Owner = platform, roleBitmap 0: the name can't be transferred, and
        // only the platform (root roles on the registry) can unregister it.
        await this._write(node, 'register', agentRegistry, registryAbi, 'register',
          [label, this.exec.operator, zeroAddress, node.resolver, 0n, BigInt(expiry)]);
        node.registered = true;
        node.owner = this.exec.operator;
      }
      // Records an adopted job's resolver doesn't hold yet (it wasn't this sidecar's).
      const missing = entries.filter((e) => node.text[e.key] !== e.value);
      if (missing.length) {
        await this._write(node, 'records', node.resolver, resolverAbi, 'multicall', [setTextCalls(name, missing)]);
        for (const e of missing) node.text[e.key] = e.value;
        node.records = Object.fromEntries(entries.map((e) => [e.field, e.input]));
        this.store.save();
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
        // After a lost state file the caller names the grantee explicitly.
        const grantee = this._address(body.grantee);
        const keys = kind === 'job' ? AGENT_WRITABLE_JOB_KEYS.map((f) => RECORD_KEYS[f]) : AGENT_KEYS;
        const grants = node.grants?.length ? node.grants
          : grantee ? keys.map((key) => ({ key, account: grantee })) : [];
        for (const g of grants.filter((x) => !x.revoked)) {
          node.resolver = node.resolver || await this._resolverOf(name);
          await this._write(node, `revoke:${g.key}`, node.resolver, resolverAbi, 'revokeRoles',
            [keyResource(g.key), RESOLVER_ROLES.SET_TEXT, g.account]);
          g.revoked = true;
        }
        node.grants = grants;
        // The registry refuses to unregister an expired name, which no longer
        // resolves anyway: only a live registration needs unregistering.
        const parentRegistry = await this._registryOf(node.parent);
        const state = await this.exec.read({
          address: parentRegistry, abi: registryAbi, functionName: 'getState', args: [tokenId(labelOf(name))],
        });
        if (Number(state.status) !== NAME_STATUS.AVAILABLE) {
          await this._write(node, 'unregister', parentRegistry, registryAbi, 'unregister', [tokenId(labelOf(name))]);
        }
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
    return isAddressEqual(raw, zeroAddress) ? null : getAddress(raw); // nobody to grant to
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
    const { secret, committed_at, registered, subregistry_set, parent_set, reconciled, children, ...rest } = node;
    return { ...rest, ...extra };
  }

  async _write(node, step, address, abi, functionName, args) {
    const { hash } = await this.exec.write({ address, abi, functionName, args });
    node.txs.push({ step, hash });
    this.store.save();
    return hash;
  }

  async _deployResolver(node, entries) {
    const payout = entries.find((e) => e.key === RECORD_KEYS.payout)?.value || null;
    const calls = [
      ...setTextCalls(node.name, entries),
      ...(node.kind === 'agent' && payout ? [setAddressCall(node.name, payout)] : []),
    ];
    const data = encodeFunctionData({
      abi: resolverAbi, functionName: 'initialize',
      args: [[{ account: this.exec.operator, roleBitmap: ALL_ROLES }], calls],
    });
    const address = await this._deploy(node, 'deploy:resolver', ADDRESSES.permissionedResolverImpl,
      proxySalt('PermissionedResolver', node.name, node.version), data);
    // The initializer wrote exactly these records (a resumed node may have
    // been opened with different ones).
    node.text = Object.fromEntries(entries.map((e) => [e.key, e.value]));
    node.records = Object.fromEntries(entries.map((e) => [e.field, e.input]));
    if (node.kind === 'agent') Object.assign(node, { addr: payout, reconciled: true });
    this.store.save();
    return address;
  }

  // Take over a name this operator already holds on-chain but has no state
  // for, or whose register receipt never arrived: reuse its resolver and
  // subregistry (deploying again would hit the same CREATE2 salts), keeping
  // what this sidecar already deployed where the entry has none. For an
  // agent name on a resolver this sidecar didn't deploy, its records and
  // grants are read first. Everything is read before anything changes, so a
  // failed read leaves the node as it was. Returns false if not registered.
  async _adopt(node, holder, { requireResolver = false, entries = [], accounts = [] } = {}) {
    const read = (address, functionName, args = []) => this.exec.read({ address, abi: registryAbi, functionName, args });
    const state = await read(holder, 'getState', [tokenId(node.label)]);
    if (Number(state.status) !== NAME_STATUS.REGISTERED) return false;
    if (!isAddressEqual(state.latestOwner, this.exec.operator)) {
      throw conflict(`${node.name} is registered to another account`, 'NAME_TAKEN');
    }
    const nonZero = (address) => (isAddressEqual(address, zeroAddress) ? null : getAddress(address));
    const [resolver, registry] = (await Promise.all([
      read(holder, 'getResolver', [node.label]), read(holder, 'getSubregistry', [node.label]),
    ])).map(nonZero);
    if (requireResolver && !resolver) throw conflict(`${node.name} is registered without a resolver`, 'PARENT_NOT_READY');
    const known = resolver && node.resolver && isAddressEqual(resolver, node.resolver);
    const holdsRegistry = registry || node.registry;
    const [parent, parentLabel] = holdsRegistry ? await read(holdsRegistry, 'getParent') : [zeroAddress, ''];
    const onchain = !known && node.kind === 'agent' && resolver
      ? { ...await this._readAgentState(node.name, resolver, entries, accounts), reconciled: true } : null;
    if (!known) Object.assign(node, { records: {}, text: {}, grants: [] });
    Object.assign(node, {
      registered: true, adopted: true, owner: this.exec.operator, expiry: Number(state.expiry),
      resolver: resolver || node.resolver, registry: holdsRegistry, subregistry_set: Boolean(registry),
      parent_set: isAddressEqual(parent, holder) && parentLabel === node.label,
    }, onchain);
    this.store.save();
    return true;
  }

  // An agent name's on-chain state: the platform-owned records (plus any
  // tracked ones), the ETH address record, and the endpoint grants held by
  // the given accounts and by the payee the name names (grants always go to
  // the payee). An endpoint key the platform sends that already holds a value
  // counts as the platform's, so an agent's own edit is not overwritten. Read
  // errors propagate, so the state is never half-known.
  async _readAgentState(name, resolver, entries, accounts, tracked = {}) {
    const sent = Object.fromEntries(entries.map((e) => [e.key, e.value]));
    const keys = [...new Set([...PLATFORM_KEYS, ...Object.keys(sent), ...Object.keys(tracked)])];
    const [values, addr] = await Promise.all([
      Promise.all(keys.map((key) => this.exec.readText(name, key))),
      this.exec.readAddress(name),
    ]);
    const text = {};
    keys.forEach((key, i) => {
      if (!values[i]) return;
      if (!AGENT_KEYS.includes(key)) text[key] = values[i];
      else if (key in sent) text[key] = sent[key];
    });
    const holders = [...new Set([...accounts, text[RECORD_KEYS.payout]]
      .filter((a) => a && isAddress(a)).map((a) => getAddress(a)))];
    const checks = holders.flatMap((account) => AGENT_KEYS.map((key) => ({ key, account })));
    const held = await Promise.all(checks.map(({ key, account }) => this.exec.read({
      address: resolver, abi: resolverAbi, functionName: 'hasRoles',
      args: [keyResource(key), RESOLVER_ROLES.SET_TEXT, account],
    })));
    return { text, addr: addr ? getAddress(addr) : null, grants: checks.filter((_, i) => held[i]) };
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
