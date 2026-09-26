"""chain/escrow.py: simulated guards, on-chain transaction building against a
fake JSON-RPC provider, and receipt polling. Nothing touches a network."""
import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.providers.base import BaseProvider

from chain.client import ABI, OnChain
from chain.config import CIRCLE_USDC_SEPOLIA
from chain.escrow import USDC_TRANSFER_ABI, EscrowError, EscrowService, ref_nonce
from chain.usdc import fallback_domain, recover_authorization_signer

PAYEE = "0x" + "c" * 40


class FakeRPC(BaseProvider):
    """Answers the handful of JSON-RPC calls a send makes; records raw txs."""

    def __init__(self):
        super().__init__()
        self.sent: list[bytes] = []
        self.receipts: dict[str, dict | None] = {}
        self.down = False

    def is_connected(self, show_traceback=False):
        return True

    def make_request(self, method, params):
        if self.down:
            raise ConnectionError("rpc down")
        if method == "eth_sendRawTransaction":
            raw = params[0]
            self.sent.append(bytes.fromhex(raw[2:] if isinstance(raw, str) else raw.hex()))
            result = Web3.keccak(self.sent[-1]).to_0x_hex()
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0])
        elif method == "eth_call":  # token metadata read → use the fallback domain
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "no node"}}
        else:
            result = {
                "eth_chainId": hex(11155111),
                "eth_getTransactionCount": "0x7",
                "eth_getBlockByNumber": {"number": "0x10", "baseFeePerGas": hex(10**9)},
                "eth_maxPriorityFeePerGas": hex(10**9),
                "eth_estimateGas": hex(80_000),
                "eth_getBalance": hex(10**18),
            }[method]
        return {"jsonrpc": "2.0", "id": 1, "result": result}


def _receipt(tx_hash: str, status: int) -> dict:
    zero32 = "0x" + "0" * 64
    return {"transactionHash": tx_hash, "transactionIndex": "0x0", "blockHash": zero32,
            "blockNumber": "0x11", "from": "0x" + "1" * 40, "to": CIRCLE_USDC_SEPOLIA,
            "cumulativeGasUsed": "0x5208", "gasUsed": "0x5208", "effectiveGasPrice": "0x1",
            "contractAddress": None, "logs": [], "logsBloom": "0x" + "0" * 512,
            "status": hex(status), "type": "0x2"}


@pytest.fixture()
def onchain_escrow(monkeypatch):
    monkeypatch.delenv("USDC_ADDRESS", raising=False)
    escrow_acct, facilitator, vault = Account.create(), Account.create(), Account.create()
    oc = OnChain("http://127.0.0.1:9", facilitator.key.hex(), None)
    rpc = FakeRPC()
    oc.w3 = Web3(rpc)
    svc = EscrowService(escrow_key=escrow_acct.key.hex(), facilitator_key=facilitator.key.hex(),
                        vault_key=vault.key.hex(), onchain=oc)
    return svc, rpc, {"escrow": escrow_acct, "facilitator": facilitator, "vault": vault}


def _decode(raw: bytes, abi):
    tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    contract = Web3().eth.contract(abi=abi)
    fn, args = contract.decode_function_input(tx["data"])
    return tx, fn.fn_name, args, Account.recover_transaction(raw)


# ── simulated ─────────────────────────────────────────────────────────────
def test_mode_is_simulated_unless_all_three_keys_are_set(monkeypatch):
    for name in ("ESCROW_PRIVATE_KEY", "FACILITATOR_PRIVATE_KEY", "BUYER_VAULT_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert EscrowService.from_env().mode == "simulated"
    monkeypatch.setenv("ESCROW_PRIVATE_KEY", Account.create().key.hex())
    monkeypatch.setenv("FACILITATOR_PRIVATE_KEY", Account.create().key.hex())
    assert EscrowService.from_env().mode == "simulated"   # vault key still missing
    monkeypatch.setenv("BUYER_VAULT_PRIVATE_KEY", Account.create().key.hex())
    assert EscrowService.from_env().mode == "onchain"     # constructing never touches the RPC


def test_simulated_calls_are_labeled_and_never_linked():
    svc = EscrowService()
    tx = svc.fund_from_vault(amount_micro=25_000_000, ref="ENG-1:fund")
    assert tx.status == "simulated" and tx.tx_hash.startswith("sim-") and tx.explorer is None
    assert svc.receipt_status(tx.tx_hash) == "confirmed"


def test_simulated_double_fund_and_double_release_are_rejected():
    svc = EscrowService()
    svc.fund_from_vault(amount_micro=100, ref="ENG-1:fund")
    with pytest.raises(EscrowError) as exc:
        svc.fund_from_vault(amount_micro=100, ref="ENG-1:fund")
    assert exc.value.code == "ALREADY_FUNDED"
    svc.release(to=PAYEE, amount_micro=100, ref="ENG-1:m0:release@0")
    with pytest.raises(EscrowError) as exc:
        svc.release(to=PAYEE, amount_micro=100, ref="ENG-1:m0:release@0")
    assert exc.value.code == "ALREADY_RELEASED"
    # a different milestone (or the next partial release) is a different ref
    svc.release(to=PAYEE, amount_micro=100, ref="ENG-1:m1:release@0")


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "100"])
def test_amounts_must_be_positive_integer_micro_usdc(amount):
    with pytest.raises(EscrowError) as exc:
        EscrowService().release(to=PAYEE, amount_micro=amount)
    assert exc.value.code == "INVALID_AMOUNT"


def test_release_requires_an_address():
    with pytest.raises(EscrowError) as exc:
        EscrowService().release(to="alice", amount_micro=1)
    assert exc.value.code == "INVALID_ADDRESS"


# ── on-chain (fake RPC) ───────────────────────────────────────────────────
def test_onchain_fund_signs_eip3009_from_vault_to_escrow(onchain_escrow):
    svc, rpc, keys = onchain_escrow
    tx = svc.fund_from_vault(amount_micro=25_000_000, ref="ENG-1:fund")
    assert tx.status == "pending" and tx.explorer == f"https://sepolia.etherscan.io/tx/{tx.tx_hash}"
    [raw] = rpc.sent
    sent, fn, args, sender = _decode(raw, ABI["USDC"])
    assert fn == "transferWithAuthorization"
    assert sender == keys["facilitator"].address                  # facilitator pays gas
    assert HexBytes(sent["to"]) == HexBytes(CIRCLE_USDC_SEPOLIA) and sent["chainId"] == 11155111
    assert args["from"] == keys["vault"].address and args["to"] == keys["escrow"].address
    assert args["value"] == 25_000_000
    assert "0x" + args["nonce"].hex() == ref_nonce("ENG-1:fund")    # chain-enforced idempotency
    permit = {"from": args["from"], "to": args["to"], "value": args["value"],
              "validAfter": args["validAfter"], "validBefore": args["validBefore"],
              "nonce": "0x" + args["nonce"].hex(), "v": args["v"],
              "r": "0x" + args["r"].hex(), "s": "0x" + args["s"].hex()}
    domain = fallback_domain(CIRCLE_USDC_SEPOLIA, 11155111)
    assert recover_authorization_signer(permit, domain) == keys["vault"].address


def test_onchain_release_transfers_from_escrow_to_payee(onchain_escrow):
    svc, rpc, keys = onchain_escrow
    svc.release(to=PAYEE, amount_micro=10_000_000, ref="ENG-1:m0:release@0")
    sent, fn, args, sender = _decode(rpc.sent[0], USDC_TRANSFER_ABI)
    assert fn == "transfer" and sender == keys["escrow"].address
    assert HexBytes(sent["to"]) == HexBytes(CIRCLE_USDC_SEPOLIA)
    assert args["to"].lower() == PAYEE and args["value"] == 10_000_000


def test_onchain_permit_must_pay_the_escrow(onchain_escrow):
    svc, rpc, _ = onchain_escrow
    with pytest.raises(EscrowError) as exc:
        svc.fund_from_permit({"to": PAYEE, "value": "1"})
    assert exc.value.code == "WRONG_RECIPIENT" and not rpc.sent


def test_failed_send_frees_the_ref(onchain_escrow):
    svc, rpc, _ = onchain_escrow
    rpc.down = True
    with pytest.raises(ConnectionError):
        svc.release(to=PAYEE, amount_micro=1, ref="r1")
    rpc.down = False
    svc.release(to=PAYEE, amount_micro=1, ref="r1")   # not ALREADY_RELEASED


def test_receipt_poll_pending_then_confirmed_or_failed(onchain_escrow):
    svc, rpc, _ = onchain_escrow
    tx = svc.release(to=PAYEE, amount_micro=1)
    assert svc.receipt_status(tx.tx_hash) == "pending"
    rpc.receipts[tx.tx_hash] = _receipt(tx.tx_hash, 1)
    assert svc.receipt_status(tx.tx_hash) == "confirmed"
    rpc.receipts[tx.tx_hash] = _receipt(tx.tx_hash, 0)
    assert svc.receipt_status(tx.tx_hash) == "failed"
