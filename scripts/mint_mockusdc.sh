#!/usr/bin/env sh
set -eu

: "${RPC_URL:?set RPC_URL to the Ethereum Sepolia RPC endpoint}"
: "${MOCK_USDC_ADDRESS:?set MOCK_USDC_ADDRESS to the deployed MockUSDC3009 address}"
: "${MOCK_USDC_MINTER_PRIVATE_KEY:?set MOCK_USDC_MINTER_PRIVATE_KEY in a local secret store}"
: "${MINT_RECIPIENT:?set MINT_RECIPIENT to a public wallet address}"
: "${MINT_AMOUNT_ATOMIC:?set MINT_AMOUNT_ATOMIC in micro-USDC (for example 1000000000)}"

case "$MINT_RECIPIENT" in
  0x????????????????????????????????????????) ;;
  *) echo "MINT_RECIPIENT must be a 20-byte 0x address" >&2; exit 2 ;;
esac

# MockUSDC3009 is intentionally unrestricted and for Sepolia development only.
# Keep the minter key in Keychain memory; never put it in a tracked env file.
cast send "$MOCK_USDC_ADDRESS" \
  "mint(address,uint256)" "$MINT_RECIPIENT" "$MINT_AMOUNT_ATOMIC" \
  --rpc-url "$RPC_URL" \
  --private-key "$MOCK_USDC_MINTER_PRIVATE_KEY"
