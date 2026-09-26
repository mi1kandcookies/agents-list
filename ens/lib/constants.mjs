// ENSv2 (Sepolia) addresses, role bits and the text-record key map.
//
// Addresses come only from the ENS docs Deployments table ("Sepolia ENSv2"),
// which is the deployment the canonical Universal Resolver walks. Other
// published Sepolia v2 deployments are not reachable from that root, so names
// created through them do not resolve. The sidecar re-checks these at startup
// (lib/check.mjs) because the beta contracts are still being redeployed.

export const SEPOLIA_CHAIN_ID = 11155111;

export const ADDRESSES = Object.freeze({
  universalResolver: '0xeEeEEEeE14D718C2B47D9923Deab1335E144EeEe',
  rootRegistry: '0x9703dbd26dab89504490994138cf2c575251a9ce',
  ethRegistry: '0x657ea849311d3d5823348dded7c2aaafb3ede09e',
  ethRegistrar: '0xabe76f6c8dfced81aa5a2bb8034202a7136b94ca',
  mockUsdc: '0x16f95d91dba7da3aca778ec053df0ff6c6a8aa8e',
  verifiableFactory: '0x9e726eb570beb6bceb495ab8cda7df517d4e841c',
  permissionedResolverImpl: '0x14f09fd05d4585759e54844dc9b00147131cf243',
  userRegistryImpl: '0xa80338aaa8d23831cea25e858d1774534abb0263',
  erc8004IdentityRegistry: '0x8004A818BFB912233c491871b3d84c89A494BD9e',
});

// Every regular and admin role (64 nybbles of 0x1).
export const ALL_ROLES = BigInt('0x' + '1'.repeat(64));

// PermissionedResolver roles (admin role = role << 128).
export const RESOLVER_ROLES = Object.freeze({
  SET_TEXT: 1n << 4n,
});

// ETHRegistrar timing: commit, wait at least MIN_COMMITMENT_AGE, then register
// (the commitment expires after 24 h). A few seconds of slack cover clock skew.
export const MIN_COMMITMENT_AGE_SECONDS = 60;
export const COMMIT_WAIT_SECONDS = MIN_COMMITMENT_AGE_SECONDS + 5;

export const MOCK_USDC_MINT = 100_000_000n; // 100 MockUSDC (6 decimals)
export const DEFAULT_ROOT_LABEL = 'agentslist-app';
export const DEFAULT_ROOT_DURATION_SECONDS = 365n * 24n * 3600n;
export const DEFAULT_AGENT_TTL_SECONDS = 365n * 24n * 3600n;

// ERC-7930 interoperable address: version 0001 (2 bytes), chain type 0000
// (2 bytes, EVM), chain reference length (1 byte), chain reference, address
// length (1 byte), address, all lowercase hex. For the ERC-8004
// IdentityRegistry on Sepolia: 0x0001 0000 03 aa36a7 14 8004a818…
export function erc7930Address(chainId, address) {
  let ref = BigInt(chainId).toString(16);
  if (ref.length % 2) ref = '0' + ref;
  const refLen = (ref.length / 2).toString(16).padStart(2, '0');
  return `0x00010000${refLen}${ref}14${address.toLowerCase().replace(/^0x/, '')}`;
}

export const ERC8004_REGISTRY_7930 = erc7930Address(SEPOLIA_CHAIN_ID, ADDRESSES.erc8004IdentityRegistry);

// Logical record name -> ENS text key. Callers only ever send logical names;
// the on-chain key syntax (ENSIP-25/26, both Draft) lives here and nowhere else.
export const RECORD_KEYS = Object.freeze({
  context: 'agent-context',          // ENSIP-26: free text / Markdown / JSON
  mcp: 'agent-endpoint[mcp]',        // ENSIP-26: must be a URL
  sow_hash: 'sow-hash',
  escrow: 'escrow',
  mandate: 'mandate',
  status: 'status',
  deliverable: 'deliverable',
});

// ENSIP-25: agent-registration[<ERC-7930 registry>][<agentId>] = "1".
export const REGISTRATION_RECORD = 'erc8004_agent_id';
export function registrationKey(agentId, registry = ERC8004_REGISTRY_7930) {
  return `agent-registration[${registry}][${agentId}]`;
}

// Keys the hired agent may write on its own job resolver, and nothing else.
export const AGENT_WRITABLE_JOB_KEYS = Object.freeze(['status', 'deliverable']);

// Which logical records each kind of name accepts.
export const ALLOWED_RECORDS = Object.freeze({
  agent: ['context', 'mcp', REGISTRATION_RECORD],
  job: ['sow_hash', 'escrow', 'mandate', 'status', 'deliverable'],
  subjob: ['sow_hash', 'escrow', 'mandate', 'status', 'deliverable'],
});
