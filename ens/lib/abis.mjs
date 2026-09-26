// Minimal ABI fragments for the live ENSv2 Sepolia contracts. Every selector
// here was checked against deployed bytecode. Note the resolver setters take
// the DNS-encoded name (bytes), not a bytes32 node.
import { parseAbi } from 'viem';

export const universalResolverAbi = parseAbi([
  'function ROOT_REGISTRY() view returns (address)',
]);

export const registryAbi = parseAbi([
  'function initialize((address account, uint256 roleBitmap)[] grants)',
  'function register(string label, address owner, address registry, address resolver, uint256 roleBitmap, uint64 expiry) returns (uint256 tokenId)',
  'function unregister(uint256 anyId)',
  'function setSubregistry(uint256 anyId, address registry)',
  'function setParent(address parent, string label)',
  'function getSubregistry(string label) view returns (address)',
  'function getResolver(string label) view returns (address)',
  'function getState(uint256 anyId) view returns ((uint8 status, uint64 expiry, address latestOwner, uint256 tokenId, uint256 resource))',
]);

export const ethRegistrarAbi = parseAbi([
  'function ETH_REGISTRY() view returns (address)',
  'function isAvailable(string label) view returns (bool)',
  'function getRegisterPrice(string label, uint64 duration, address paymentToken) view returns (uint256 base, uint256 premium)',
  'function makeCommitment(string label, address owner, bytes32 secret, address subregistry, address resolver, uint64 duration, bytes32 referrer) pure returns (bytes32)',
  'function commit(bytes32 commitment)',
  'function register(string label, address owner, bytes32 secret, address subregistry, address resolver, uint64 duration, address paymentToken, bytes32 referrer) returns (uint256 tokenId)',
]);

export const mockUsdcAbi = parseAbi([
  'function mint(address to, uint256 amount)',
  'function approve(address spender, uint256 amount) returns (bool)',
]);

export const factoryAbi = parseAbi([
  'function deployProxy(address implementation, uint256 salt, bytes data) returns (address)',
  'event ProxyDeployed(address indexed sender, address indexed proxyAddress, uint256 salt, address implementation)',
]);

export const resolverAbi = parseAbi([
  'function initialize((address account, uint256 roleBitmap)[] grants, bytes[] calls)',
  'function setText(bytes name, string key, string value)',
  'function multicall(bytes[] calls) returns (bytes[])',
  'function grantSetterRoles(bytes setter, address account) returns (bool)',
  'function revokeRoles(uint256 resource, uint256 roleBitmap, address account) returns (bool)',
  'function hasRoles(uint256 resource, uint256 roleBitmap, address account) view returns (bool)',
]);
