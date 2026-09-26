#!/usr/bin/env sh
set -eu

: "${RPC_URL:?set RPC_URL to the Ethereum Sepolia RPC endpoint}"
: "${MOCK_USDC_DEPLOYER_PRIVATE_KEY:?set MOCK_USDC_DEPLOYER_PRIVATE_KEY in a local secret store}"

# This command broadcasts only when the operator explicitly invokes it. Keep
# the key in the environment (or load it with the local keychain helper), and
# never put it in a file tracked by git.
forge create contracts/MockUSDC3009.sol:MockUSDC3009 \
  --rpc-url "$RPC_URL" \
  --private-key "$MOCK_USDC_DEPLOYER_PRIVATE_KEY" \
  --broadcast
