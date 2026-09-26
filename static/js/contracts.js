// AgentHire - on-chain ABIs only.
//
// Addresses and chain metadata come from the backend at /config.js (rendered
// from server env by the Flask app). base.html loads /config.js BEFORE this
// file, so window.AGENTHIRE_CHAIN and window.AGENTHIRE_ADDRESSES are already
// populated when the script below runs.
//
// To point the UI at a different deployment, export the *_ADDRESS env vars
// documented in .env.example and restart Flask - no code changes needed.

// Minimal ABIs - only what the UI actually calls. Full ABIs are in the backend repo.
window.AGENTHIRE_ABIS = {
  MockUSDC: [
    'function balanceOf(address) view returns (uint256)',
    'function approve(address,uint256) returns (bool)',
    'function mint(address,uint256)',
    'function transferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce,uint8 v,bytes32 r,bytes32 s)',
    'function DOMAIN_SEPARATOR() view returns (bytes32)',
  ],
  AgentRegistry: [
    'function nextAgentId() view returns (uint256)',
    'function getAgent(uint256) view returns (tuple(uint256 agentId,address wallet,string name,string endpointURL,uint256 registeredAt,bool active,bool banned))',
    'function getListing(uint256) view returns (tuple(uint256 minPricePerToken,uint256 maxTokensPerSession,bool acceptingWork,uint256 nonce))',
    'function getAgentByWallet(address) view returns (uint256,tuple(uint256 agentId,address wallet,string name,string endpointURL,uint256 registeredAt,bool active,bool banned))',
    'function registerAgent(address,string,string) returns (uint256)',
  ],
  ReputationContract: [
    'function getCreditProfile(uint256) view returns (uint256 score,uint8 tier,uint256 tasksCompleted,uint256 incidentCount,uint64 lastDecayTs,uint256 projectedScore)',
    'function getCategoryTier(uint256,uint256) view returns (uint8)',
  ],
  StakingSlashing: [
    'function getStake(uint256) view returns (uint256,uint256,bool)',
    'function stake(uint256,uint256)',
    'function requestUnstake(uint256,uint256)',
    'function completeUnstake(uint256,address)',
    'function getUnstakeRequest(uint256) view returns (uint256 amount,uint256 availableAt)',
  ],
  EscrowPayment: [
    'function depositFunds(uint256 agentId,uint256 depositAmount,uint256 tokenBudget,uint256 categoryId,uint64 expiresAt) returns (uint256)',
    'function getSession(uint256) view returns (tuple(uint256 agentId,address user,uint256 totalDeposit,uint256 tokenBudget,uint256 pricePerToken,uint256 categoryId,uint64 expiresAt,bool settled,bool cancelled))',
    'function cancelSession(uint256)',
    'function nextSessionId() view returns (uint256)',
  ],
  AuctionMarket: [
    'function postBid(uint256 depositAmount,uint256 tokenBudget,uint256 maxPricePerToken,uint256 categoryId,uint8 minTier,uint64 expiresAt) returns (uint256)',
    'function getBid(uint256) view returns (tuple(address user,uint256 depositAmount,uint256 tokenBudget,uint256 maxPricePerToken,uint256 categoryId,uint8 minTier,uint64 expiresAt,uint256 claimedByAgentId,bool settled,bool cancelled))',
    'function cancelBid(uint256)',
  ],
};
