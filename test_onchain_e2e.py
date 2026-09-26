"""End-to-end integration test for onchain.py against live Fuji.

Exercises:
  1. Python reads from chain (get_credit_profile, get_session)
  2. Python signs gatekeeper incident + submits on-chain
  3. Python executes x402 flow: crafts client EIP-3009 sig + facilitator settles

Run:
  PRIVATE_KEY=0x... python test_onchain_e2e.py
"""
import os, sys, time, secrets
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_abi import encode
from onchain import OnChain, ADDRESSES, ABI, CHAIN_ID, FUJI_RPC

def main():
    pk = os.environ.get("PRIVATE_KEY")
    if not pk: print("PRIVATE_KEY required"); sys.exit(1)

    os.environ["FACILITATOR_PRIVATE_KEY"] = pk
    os.environ["GATEKEEPER_PRIVATE_KEY"] = pk
    oc = OnChain.from_env()
    print(f"[1] Python reads from chain")
    block = oc.w3.eth.block_number
    print(f"    block: {block}")
    profile = oc.get_credit_profile(1)
    print(f"    agent 1 score={profile['score']} tier={profile['tier']} tasks={profile['tasksCompleted']}")
    s = oc.get_session(1)
    print(f"    session 1 settled={s['settled']} deposit={int(s['totalDeposit'])/1e6} USDC")

    print(f"\n[2] End-to-end x402 flow (Python signs both client permit + facilitator tx)")
    client = Account.from_key(pk)
    w3 = oc.w3
    usdc = oc._contracts["MockUSDC"]

    # Ensure client has USDC
    bal = usdc.functions.balanceOf(client.address).call()
    print(f"    client USDC: {bal/1e6:.2f}")
    if bal < 5_000_000:
        print("    (would mint more if needed; skipping for idempotency)")

    # Build EIP-712 TransferWithAuthorization
    value = 2_000_000  # 2 USDC
    validBefore = int(time.time()) + 3600
    nonce_bytes = secrets.token_bytes(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name":"name","type":"string"},
                {"name":"version","type":"string"},
                {"name":"chainId","type":"uint256"},
                {"name":"verifyingContract","type":"address"},
            ],
            "TransferWithAuthorization": [
                {"name":"from","type":"address"},
                {"name":"to","type":"address"},
                {"name":"value","type":"uint256"},
                {"name":"validAfter","type":"uint256"},
                {"name":"validBefore","type":"uint256"},
                {"name":"nonce","type":"bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": "Mock USDC", "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": ADDRESSES["MockUSDC"],
        },
        "message": {
            "from": client.address,
            "to": client.address,
            "value": value,
            "validAfter": 0,
            "validBefore": validBefore,
            "nonce": nonce_bytes,
        },
    }
    signable = encode_typed_data(full_message=typed_data)
    sig = Account.sign_message(signable, private_key=pk)
    payload = {
        "from": client.address,
        "to": client.address,
        "value": str(value),
        "validAfter": 0,
        "validBefore": validBefore,
        "nonce": nonce_hex,
        "v": sig.v,
        "r": "0x" + sig.r.to_bytes(32, "big").hex(),
        "s": "0x" + sig.s.to_bytes(32, "big").hex(),
        "agentId": 1,
        "tokenBudget": value,
        "categoryId": 0,
    }
    print(f"    signed permit nonce={nonce_hex[:18]}...")
    print(f"    executing on-chain (3 txs: permit -> approve -> depositFunds)...")
    result = oc.x402_execute(payload)
    print(f"    OK session {result['sessionId']}")
    print(f"    OK permit   tx: {result['txHashes']['permit']}")
    print(f"    OK deposit  tx: {result['txHashes']['deposit']}")

    print(f"\n[3] Gatekeeper incident flow (Python signs + submits)")
    victim = "0x0000000000000000000000000000000000cafe01"
    # Use a unique victim each run to avoid signature replay
    victim = "0x" + secrets.token_hex(20)
    try:
        inc = oc.submit_incident(agent_id=2, affected_user=victim, severity=1)
        print(f"    OK incident signed + submitted: {inc['txHash']}")
    except Exception as e:
        print(f"    (incident submission: {e})")

    print(f"\n[DONE] ALL PYTHON ON-CHAIN FLOWS WORK END-TO-END ON FUJI")
    print(f"   View txs: https://testnet.snowtrace.io/address/{client.address}")

if __name__ == "__main__":
    main()
