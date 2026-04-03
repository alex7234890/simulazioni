"""
Shared utilities for MEV Insurance simulation scripts.

Supports both localhost (Hardhat) and Sepolia testnet.
Network-specific logic is contained entirely in this module.
"""
import json
import os
import sys
import time
import secrets
from web3 import Web3

# ── Constants ──
MAX_RETRIES = 3
GAS_LIMIT = 5_000_000
RETRY_DELAY = 2  # seconds between retries

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "deployed_addresses.json")
ARTIFACTS_DIR = os.path.join(PROJECT_ROOT, "artifacts", "contracts")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# ── Network Configuration ──
NETWORKS = {
    "localhost": {
        "rpc": "http://127.0.0.1:8545",
        "use_private_keys": False,
    },
    "sepolia": {
        "rpc": "https://sepolia.infura.io/v3/{INFURA_KEY}",
        "use_private_keys": True,
    },
}


# ── ANSI Colors ──
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    WHITE = "\033[97m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    RED_BOLD = "\033[1;91m"

    LEVEL_MAP = {
        "INFO": WHITE,
        "TRADE": GREEN,
        "ATTACK": RED,
        "ORACLE": BLUE,
        "CLAIM": CYAN,
        "POOL": YELLOW,
        "SETUP": MAGENTA,
        "ERROR": RED_BOLD,
        "TIME": YELLOW,
        "DASH": WHITE + BOLD,
    }


# ── Global log file handle ──
_log_file = None


def set_log_file(path):
    """Set a file path for logging output."""
    global _log_file
    _log_file = open(path, "a", encoding="utf-8")


def close_log_file():
    """Close the log file handle if open."""
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None


def log(message, level="INFO"):
    """Log with timestamp, color, and optional file output."""
    ts = time.strftime("%H:%M:%S")
    color = Colors.LEVEL_MAP.get(level, Colors.WHITE)
    tag = f"[{level:>6s}]" if level != "INFO" else "[  INFO]"
    line = f"[{ts}] {tag} {message}"
    print(f"{color}{line}{Colors.RESET}")
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


# ── Web3 Connection ──
def get_web3(network="localhost"):
    """Connect to the blockchain with retry logic."""
    cfg = NETWORKS.get(network)
    if not cfg:
        raise ValueError(f"Unknown network: {network}. Use: {list(NETWORKS.keys())}")

    rpc_url = cfg["rpc"]
    if network == "sepolia":
        infura_key = os.environ.get("INFURA_KEY", "")
        rpc_url = rpc_url.replace("{INFURA_KEY}", infura_key)

    for attempt in range(MAX_RETRIES):
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
        except Exception as e:
            log(f"Connection attempt {attempt + 1} failed: {e}", "ERROR")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)

    raise ConnectionError(f"Cannot connect to {rpc_url}. Is the node running?")


# ── Contract Loading ──
def load_config():
    """Load deployed contract addresses from config."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_abi(contract_name):
    """Load ABI from Hardhat artifacts."""
    abi_path = os.path.join(ARTIFACTS_DIR, f"{contract_name}.sol", f"{contract_name}.json")
    with open(abi_path) as f:
        return json.load(f)["abi"]


def get_contract(w3, name, address):
    """Get a web3 contract instance."""
    abi = load_abi(name)
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


def get_all_contracts(w3):
    """Load all deployed contracts."""
    cfg = load_config()
    contracts = {}
    for name, addr in cfg.items():
        contracts[name] = get_contract(w3, name, addr)
    return contracts


# ── Account Management ──
def _load_env():
    """Load .env file variables."""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


def get_account(w3, index, network="localhost"):
    """
    Get account address by index.
    localhost: uses w3.eth.accounts[index]
    sepolia: loads private key from .env (PRIVATE_KEY_0, PRIVATE_KEY_1, etc.)
    Returns (address, private_key_or_None)
    """
    if network == "localhost":
        accounts = w3.eth.accounts
        if index >= len(accounts):
            raise IndexError(f"Account index {index} out of range (have {len(accounts)})")
        return accounts[index], None
    else:
        _load_env()
        key = os.environ.get(f"PRIVATE_KEY_{index}")
        if not key:
            raise ValueError(f"PRIVATE_KEY_{index} not found in .env")
        acct = w3.eth.account.from_key(key)
        return acct.address, key


def get_accounts_batch(w3, count, network="localhost"):
    """Get multiple accounts. Returns list of (address, private_key_or_None)."""
    return [get_account(w3, i, network) for i in range(count)]


# ── Transaction Sending ──
def send_tx(w3, contract_fn, sender, value=0, network="localhost", private_key=None):
    """
    Send a transaction. On localhost, sends directly.
    On Sepolia, signs with private key.
    Handles nonce management and retries.
    """
    for attempt in range(MAX_RETRIES):
        try:
            nonce = w3.eth.get_transaction_count(sender)
            tx_params = {
                "from": sender,
                "value": value,
                "gas": GAS_LIMIT,
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
            }

            if network == "localhost":
                tx = contract_fn.build_transaction(tx_params)
                tx_hash = w3.eth.send_transaction(tx)
            else:
                if not private_key:
                    raise ValueError("Private key required for non-localhost networks")
                tx = contract_fn.build_transaction(tx_params)
                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 0:
                raise Exception("Transaction reverted")
            return receipt

        except Exception as e:
            err_msg = str(e)
            # Don't retry on contract reverts — they'll fail again
            if "revert" in err_msg.lower() or "require" in err_msg.lower():
                raise
            if attempt < MAX_RETRIES - 1:
                log(f"TX retry {attempt + 1}: {err_msg}", "ERROR")
                time.sleep(RETRY_DELAY)
            else:
                raise


def send_eth(w3, sender, to, value, network="localhost", private_key=None):
    """Send ETH directly (for funding oracle stakes etc.)."""
    nonce = w3.eth.get_transaction_count(sender)
    tx = {
        "from": sender,
        "to": to,
        "value": value,
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
        "nonce": nonce,
    }
    if network == "localhost":
        tx_hash = w3.eth.send_transaction(tx)
    else:
        if not private_key:
            raise ValueError("Private key required")
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)


# ── Crypto Helpers ──
def keccak256_commit(fraud_score, pattern_valid, salt):
    """Replicate keccak256(abi.encodePacked(uint8, bool, bytes32)) from Solidity."""
    return Web3.solidity_keccak(
        ["uint8", "bool", "bytes32"],
        [fraud_score, pattern_valid, salt],
    )


def generate_salt():
    """Generate a random 32-byte salt."""
    return secrets.token_bytes(32)


# ── Unit Conversion ──
def to_wei(amount):
    """Convert ether amount to wei."""
    return Web3.to_wei(amount, "ether")


def from_wei(amount):
    """Convert wei to ether (float). Handles negative values."""
    if amount < 0:
        return -float(Web3.from_wei(abs(amount), "ether"))
    return float(Web3.from_wei(amount, "ether"))


# ── Time Manipulation ──
def increase_time(w3, seconds, network="localhost"):
    """
    Advance blockchain time. Only works on localhost (Hardhat).
    On testnet, logs a warning and does nothing.
    """
    if network != "localhost":
        log("Testnet: cannot manipulate time — skipping", "TIME")
        return
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])
