#!/usr/bin/env bash
# Add (or replace) an agent's public key: ./scripts/add-client.sh <name>
# Paste the key when prompted. The private key never leaves the agent's machine.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${1:-}"
[ -z "$NAME" ] && { echo "usage: $0 <agent-name>"; exit 1; }
mkdir -p keys
echo "Paste the Ed25519 PUBLIC key for '$NAME' (one line), then Enter:"
read -r PUBKEY
[ -z "$PUBKEY" ] && { echo "empty — nothing written."; exit 1; }
printf '%s\n' "$PUBKEY" > "keys/${NAME}.pub"
echo "saved to keys/${NAME}.pub — restart bridge.py to pick it up."
echo "Revoke with: rm keys/${NAME}.pub && restart bridge.py"
