"""
Shared utilities for MEV Insurance simulation scripts.
"""
import json
import os
import time
import secrets
from web3 import Web3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "deployed_addresses.json")
ARTIFACTS_DIR = os.path.join(PROJECT_ROOT, "artifacts", "contracts")
RPC_URL = "http://127.0.0.1:8545"


def get_web3():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to {RPC_URL}. Is `npx hardhat node` running?")
    return w3


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_abi(contract_name):
    abi_path = os.path.join(ARTIFACTS_DIR, f"{contract_name}.sol", f"{contract_name}.json")
    with open(abi_path) as f:
        return json.load(f)["abi"]


def get_contract(w3, name, address):
    abi = load_abi(name)
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


def get_all_contracts(w3):
    cfg = load_config()
    contracts = {}
    for name, addr in cfg.items():
        contracts[name] = get_contract(w3, name, addr)
    return contracts


def get_accounts(w3):
    return w3.eth.accounts


def send_tx(w3, contract_fn, sender, value=0, gas=3000000):
    tx = contract_fn.build_transaction({
        "from": sender,
        "value": value,
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(sender),
    })
    tx_hash = w3.eth.send_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt


def keccak256_commit(fraud_score, pattern_valid, salt):
    """Replicate keccak256(abi.encodePacked(uint8, bool, bytes32)) from Solidity."""
    return Web3.solidity_keccak(
        ["uint8", "bool", "bytes32"],
        [fraud_score, pattern_valid, salt]
    )


def keccak256_patt_commit(patt_estimate, dataset_hash, salt):
    """Replicate keccak256(abi.encodePacked(uint16, bytes32, bytes32))."""
    return Web3.solidity_keccak(
        ["uint16", "bytes32", "bytes32"],
        [patt_estimate, dataset_hash, salt]
    )


def generate_salt():
    return secrets.token_bytes(32)


def to_wei(amount):
    return Web3.to_wei(amount, "ether")


def from_wei(amount):
    return Web3.from_wei(amount, "ether")


def increase_time(w3, seconds):
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


def log(message):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {message}")
