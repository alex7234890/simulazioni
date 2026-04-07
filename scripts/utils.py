"""
Shared utilities for MEV Insurance simulation scripts.

Provides: web3 connection, contract loading, transaction helpers,
time advancement, dual logging (stdout + file), and commit-reveal helpers.
"""
import json
import os
import sys
import time
import random
import secrets
from datetime import datetime
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ── Paths ──

ROOT = Path(__file__).parent.parent
CONFIG_DIR = ROOT / "config"
ARTIFACTS_DIR = ROOT / "artifacts" / "contracts"
LOGS_DIR = ROOT / "logs"
LOG_FILE = LOGS_DIR / "simulation.log"

DEPLOYED_ADDRESSES_FILE = CONFIG_DIR / "deployed_addresses.json"
ACTORS_FILE = CONFIG_DIR / "actors.json"

LOGS_DIR.mkdir(exist_ok=True)

# ── ANSI Colors ──

_COLORS = {
    "INFO":   "\033[37m",      # white
    "USER":   "\033[96m",      # cyan
    "BOT":    "\033[91m",      # red
    "ORACLE": "\033[93m",      # yellow
    "POOL":   "\033[92m",      # green
    "CLAIM":  "\033[95m",      # magenta
    "ERROR":  "\033[1;31m",    # bold red
    "SYSTEM": "\033[1;34m",    # bold blue
    "SETUP":  "\033[90m",      # dark grey
    "TRADE":  "\033[96m",      # cyan
    "ATTACK": "\033[91m",      # red
}
_RESET = "\033[0m"


def log(msg: str, tag: str = "INFO") -> None:
    """Dual log: colored stdout + plain file."""
    ts = datetime.now().strftime("%H:%M:%S")
    color = _COLORS.get(tag.upper(), "")
    print(f"{color}[{ts}][{tag:>6s}] {msg}{_RESET}")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}][{tag:>6s}] {msg}\n")


# ── Web3 Connection ──

def get_web3(retries: int = 5) -> Web3:
    """Connect to localhost Hardhat node with retry."""
    url = "http://127.0.0.1:8545"
    for attempt in range(retries):
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                log(f"Connected to {url} (chainId={w3.eth.chain_id})", "SYSTEM")
                return w3
        except Exception:
            pass
        delay = 2 ** attempt
        log(f"Connection failed, retrying in {delay}s…", "SYSTEM")
        time.sleep(delay)
    raise ConnectionError(f"Cannot connect to Hardhat node at {url}")


# ── Config / Actors ──

def load_config() -> dict:
    """Load deployed contract addresses."""
    if not DEPLOYED_ADDRESSES_FILE.exists():
        raise FileNotFoundError(
            "deployed_addresses.json not found.\n"
            "Run: npx hardhat run scripts/deploy_all.js --network localhost"
        )
    with open(DEPLOYED_ADDRESSES_FILE) as f:
        return json.load(f)

        time.sleep(RETRY_DELAY)

def load_actors() -> dict:
    """Load actors.json (oracle/bot addresses). Returns {} if missing."""
    if not ACTORS_FILE.exists():
        return {}
    with open(ACTORS_FILE) as f:
        return json.load(f)


def save_actors(data: dict) -> None:
    """Persist actors.json."""
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(ACTORS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log(f"Saved actors.json ({len(data)} entries)", "SYSTEM")


# ── ABI / Contract Loading ──

def load_abi(contract_name: str) -> list:
    """Load ABI from Hardhat artifacts."""
    candidates = list(ARTIFACTS_DIR.rglob(f"{contract_name}.json"))
    candidates = [p for p in candidates if not p.name.endswith(".dbg.json")]
    if not candidates:
        raise FileNotFoundError(
            f"ABI not found for '{contract_name}' in {ARTIFACTS_DIR}\n"
            "Run: npx hardhat compile"
        )
    with open(candidates[0]) as f:
        return json.load(f)["abi"]


def get_contract(w3: Web3, name: str, address: str):
    """Return a web3 contract instance."""
    abi = load_abi(name)

    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=abi
    )


def get_all_contracts(w3: Web3) -> dict:
    """Load all deployed contracts. Returns dict keyed by contract name."""
    cfg = load_config()

    contracts = {}

    for name, addr in cfg.items():
        try:
            contracts[name] = get_contract(w3, name, addr)
        except FileNotFoundError:
            log(f"ABI missing for {name} — skipping", "SYSTEM")
    return contracts


# ── Transaction Helper ──

# Per-address nonce cache
_nonce_cache: dict = {}


def send_tx(w3: Web3, fn, sender: str, value: int = 0,
            gas: int = 500_000, retries: int = 3) -> dict:
    """
    Build and send a transaction using Hardhat's unlocked accounts.
    Returns the transaction receipt.
    """
    sender = Web3.to_checksum_address(sender)

    if sender not in _nonce_cache:
        _nonce_cache[sender] = w3.eth.get_transaction_count(sender)
    nonce = _nonce_cache[sender]

    tx_params = {
        "from": sender,
        "nonce": nonce,
        "gas": gas,
        "value": value,
    }

    for attempt in range(retries):
        try:
            tx_hash = fn.transact(tx_params)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt["status"] == 0:
                raise RuntimeError("Transaction reverted (status=0)")
            _nonce_cache[sender] = nonce + 1
            return receipt
        except Exception as e:
            err = str(e)
            if "nonce too low" in err or "already known" in err:
                _nonce_cache[sender] = w3.eth.get_transaction_count(sender)
                nonce = _nonce_cache[sender]
                tx_params["nonce"] = nonce
            elif "revert" in err.lower() or "require" in err.lower():
                raise
            elif attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)

    raise RuntimeError("send_tx: max retries exceeded")

    return secrets.token_bytes(32)

# ── Time Advancement ──

def advance_day(w3: Web3, days: int = 1) -> None:
    """Advance blockchain time by N days and mine a block."""
    seconds = 86_400 * days
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])
    log(f"Advanced blockchain by {days} day(s) ({seconds}s)", "SYSTEM")


# ── Unit Conversion ──

def to_wei(n) -> int:
    """Convert MEVI (float/int) to wei."""
    return int(float(n) * 10 ** 18)


def from_wei(n) -> float:
    """Convert wei to MEVI. Handles negative values."""
    if n < 0:
        return -(abs(n) / 10 ** 18)
    return float(n) / 10 ** 18


# ── Commit-Reveal ──

def generate_salt() -> int:
    """Generate a random 256-bit salt."""
    return random.randint(0, 2**256 - 1)


def keccak256_commit(fraud_score: int, pattern_valid: bool, salt: int) -> bytes:
    """
    Compute commit hash matching Solidity:
    keccak256(abi.encodePacked(uint256, bool, uint256))
    """
    from eth_abi import encode
    encoded = encode(["uint256", "bool", "uint256"], [fraud_score, pattern_valid, salt])
    return Web3.keccak(encoded)


# ── FraudScore Calculation (PDF §1.3.11) ──

def compute_fraud_score(tier_id: int, total_claims: int, total_swaps: int) -> tuple:
    """
    Compute FraudScore = ScoreTier + ScoreClaimRate + ScoreNetwork + variance.
    Returns (fraud_score: int, pattern_valid: bool).

    ScoreTier:      Bronze=50, Silver=30, Gold=15, Platinum=0
    ScoreClaimRate: >30%→30, >20%→25, >10%→20, ≤10%→15, <6%→0
    ScoreNetwork:   random(0, 15)
    Variance:       random(-3, +3)
    """
    score_tier = [50, 30, 15, 0][min(tier_id, 3)]

    claim_rate = (total_claims / total_swaps) if total_swaps > 0 else 0.0

    if claim_rate > 0.30:
        score_claim = 30
    elif claim_rate > 0.20:
        score_claim = 25
    elif claim_rate > 0.10:
        score_claim = 20
    else:
        score_claim = 15
    if claim_rate < 0.06:
        score_claim = 0

    score_network = random.randint(0, 15)
    variance = random.randint(-3, 3)

    fraud_score = max(0, min(130, score_tier + score_claim + score_network + variance))
    pattern_valid = random.random() < 0.7  # 70% chance of valid pattern for real attacks

    return fraud_score, pattern_valid
