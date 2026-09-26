#!/bin/sh
# Load the local test-wallet roles from macOS Keychain and run one command.
# Private keys never enter the repository, a .env file, or command output.
set -eu

if [ "$#" -eq 0 ]; then
  echo "usage: scripts/keychain_env.sh command [args ...]" >&2
  exit 2
fi

keychain_get() {
  /usr/bin/security find-generic-password \
    -a agents-list -s "agents-list.$1" -w 2>/dev/null
}

load_role() {
  variable=$1
  role=$2
  value=$(keychain_get "$role" || true)
  if [ -z "$value" ]; then
    echo "missing Keychain entry: agents-list.$role" >&2
    exit 2
  fi
  export "$variable=$value"
}

load_role ENS_OPERATOR_PRIVATE_KEY ens-operator
load_role FACILITATOR_PRIVATE_KEY facilitator
load_role ESCROW_PRIVATE_KEY escrow
load_role BUYER_VAULT_PRIVATE_KEY buyer-vault
load_role X402_PAYER_PRIVATE_KEY x402-payer

load_optional() {
  variable=$1
  role=$2
  value=$(keychain_get "$role" || true)
  if [ -n "$value" ]; then
    export "$variable=$value"
  fi
}

# Optional integrations: absence keeps the normal fail-closed behavior.
load_optional INTERCEPTA_API_KEY intercepta-api-key
load_optional MOCK_USDC_DEPLOYER_PRIVATE_KEY mock-deployer

# One shared sidecar token is generated and stored separately from wallet keys.
sidecar_token=$(keychain_get sidecar-token || true)
if [ -z "$sidecar_token" ]; then
  echo "missing Keychain entry: agents-list.sidecar-token" >&2
  exit 2
fi
export ENS_SIDECAR_TOKEN="$sidecar_token"

exec "$@"
