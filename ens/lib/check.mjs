// Startup self-check: the configured addresses must be the ones the canonical
// Universal Resolver actually walks (UR -> root registry -> "eth" registry,
// and the registrar must write into that same registry). If not, the sidecar
// refuses writes: names created elsewhere would never resolve.
import { isAddressEqual } from 'viem';

import { ethRegistrarAbi, registryAbi, universalResolverAbi } from './abis.mjs';
import { ADDRESSES, SEPOLIA_CHAIN_ID } from './constants.mjs';

export async function checkAddresses(exec, addresses = ADDRESSES) {
  const problems = [];
  const expect = (what, got, want) => {
    if (!got || !isAddressEqual(got, want)) problems.push(`${what} is ${got}, expected ${want}`);
  };
  try {
    const chainId = await exec.chainId();
    if (chainId !== SEPOLIA_CHAIN_ID) problems.push(`chain id is ${chainId}, expected ${SEPOLIA_CHAIN_ID}`);
    const root = await exec.read({
      address: addresses.universalResolver, abi: universalResolverAbi, functionName: 'ROOT_REGISTRY',
    });
    expect('UniversalResolver.ROOT_REGISTRY()', root, addresses.rootRegistry);
    const eth = await exec.read({
      address: root, abi: registryAbi, functionName: 'getSubregistry', args: ['eth'],
    });
    expect('RootRegistry.getSubregistry("eth")', eth, addresses.ethRegistry);
    const registrarTarget = await exec.read({
      address: addresses.ethRegistrar, abi: ethRegistrarAbi, functionName: 'ETH_REGISTRY',
    });
    expect('ETHRegistrar.ETH_REGISTRY()', registrarTarget, addresses.ethRegistry);
  } catch (err) {
    problems.push(`address check failed: ${err.shortMessage || err.message}`);
  }
  return { ok: problems.length === 0, problems, checked_at: new Date().toISOString() };
}
