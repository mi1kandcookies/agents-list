// Agent's List - on-chain ABIs only.
//
// Addresses, chain metadata and the payment recipient come from /config.js
// (rendered from server env), which base.html loads before this file. The
// USDC EIP-712 domain is fetched from /api/x402/domain before signing.

window.AGENTSLIST_ABIS = {
  USDC: [
    'function balanceOf(address) view returns (uint256)',
    'function name() view returns (string)',
    'function version() view returns (string)',
    'function DOMAIN_SEPARATOR() view returns (bytes32)',
    'function transferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce,uint8 v,bytes32 r,bytes32 s)',
  ],
  IdentityRegistry: [
    'function ownerOf(uint256) view returns (address)',
    'function tokenURI(uint256) view returns (string)',
    'function balanceOf(address) view returns (uint256)',
  ],
};
