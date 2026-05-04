import json
import os
import sys
import time
import random
from datetime import datetime
from pathlib import Path

BLOCK_INTERVAL_MS = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path(__file__).parent.parent
CONFIG_DIR = ROOT / "config"
ARTIFACTS_DIR = ROOT / "artifacts" / "contracts"
LOGS_DIR = ROOT / "logs"
LOG_FILE = LOGS_DIR / "simulation.log"

DEPLOYED_ADDRESSES_FILE = CONFIG_DIR / "deployed_addresses.json"
ACTORS_FILE = CONFIG_DIR / "actors.json"

LOGS_DIR.mkdir(exist_ok=True)

_COLORS = {
    "INFO":   "\033[37m",
    "USER":   "\033[96m",
    "BOT":    "\033[91m",
    "ORACLE": "\033[93m",
    "POOL":   "\033[92m",
    "CLAIM":  "\033[95m",
    "ERROR":  "\033[1;31m",
    "SYSTEM": "\033[1;34m",
    "SETUP":  "\033[90m",
    "TRADE":  "\033[96m",
    "ATTACK": "\033[91m",
}
_RESET = "\033[0m"


def log(msg: str, tag: str = "INFO", to_file: bool = True) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    color = _COLORS.get(tag.upper(), "")
    print(f"{color}[{ts}][{tag:>6s}] {msg}{_RESET}")
    if to_file:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}][{tag:>6s}] {msg}\n")


def sim_log(msg: str, tag: str = " SWAP") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}][{tag:>6s}] {msg}\n")


def wait_for_deploy(timeout: int = 120) -> None:
    required_keys = {"MEVToken", "OracleRegistry", "MEVInsurance", "MockAMM"}
    deadline = time.time() + timeout
    while True:
        if DEPLOYED_ADDRESSES_FILE.exists():
            try:
                with open(DEPLOYED_ADDRESSES_FILE) as f:
                    data = json.load(f)
                if required_keys.issubset(data.keys()):
                    try:
                        from web3 import Web3
                        w3_check = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))
                        oracle_addr = data["OracleRegistry"]
                        code = w3_check.eth.get_code(Web3.to_checksum_address(oracle_addr))
                        if len(code) > 2:
                            return
                    except Exception:
                        pass
            except Exception:
                pass
        if time.time() > deadline:
            raise TimeoutError(f"Timeout {timeout}s: deployed_addresses.json not ready")
        log("Waiting for deploy...", "SYSTEM")
        time.sleep(3)


def get_web3(retries: int = 5) -> Web3:
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


def load_config() -> dict:
    if not DEPLOYED_ADDRESSES_FILE.exists():
        raise FileNotFoundError(
            "deployed_addresses.json not found.\n"
            "Run: npx hardhat run scripts/deploy_all.js --network localhost"
        )
    with open(DEPLOYED_ADDRESSES_FILE) as f:
        return json.load(f)


def load_actors() -> dict:
    if not ACTORS_FILE.exists():
        return {}
    with open(ACTORS_FILE) as f:
        return json.load(f)


def save_actors(data: dict) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(ACTORS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log(f"Saved actors.json ({len(data)} entries)", "SYSTEM")


def load_abi(contract_name: str) -> list:
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
    abi = load_abi(name)
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=abi
    )


def get_all_contracts(w3: Web3) -> dict:
    cfg = load_config()
    contracts = {}
    for name, addr in cfg.items():
        try:
            contracts[name] = get_contract(w3, name, addr)
        except FileNotFoundError:
            log(f"ABI missing for {name} — skipping", "SYSTEM")
    return contracts


_nonce_cache: dict = {}

def send_tx(w3: Web3, fn, sender: str, value: int = 0,
            gas: int = 1_000_000, retries: int = 3) -> dict:
    sender = Web3.to_checksum_address(sender)

    if sender not in _nonce_cache:
        _nonce_cache[sender] = w3.eth.get_transaction_count(sender)

    pending_hash = None

    for attempt in range(retries):
        nonce = _nonce_cache[sender]
        tx_params = {"from": sender, "nonce": nonce, "gas": gas, "value": value}

        try:
            # If a previous attempt sent the tx but timed out, don't resend —
            # just wait for the original hash (it's still pending in the mempool).
            if pending_hash is not None:
                print(f"⏳ Waiting for pending tx {pending_hash.hex()[:16]}... (attempt {attempt+1})")
                receipt = w3.eth.wait_for_transaction_receipt(pending_hash, timeout=120)
                if receipt["status"] == 0:
                    _nonce_cache[sender] = nonce + 1
                    raise RuntimeError(f"Revert on-chain (status 0). Hash: {pending_hash.hex()}")
                _nonce_cache[sender] = nonce + 1
                return receipt

            tx_hash = fn.transact(tx_params)
            pending_hash = tx_hash
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] == 0:
                _nonce_cache[sender] = nonce + 1
                raise RuntimeError(f"Revert on-chain (status 0). Hash: {tx_hash.hex()}")
            _nonce_cache[sender] = nonce + 1
            return receipt

        except Exception as e:
            err = str(e).lower()

            if "nonce too low" in err or "already known" in err:
                real_nonce = w3.eth.get_transaction_count(sender)
                print(f"⚠️ Nonce mismatch: cache={nonce} chain={real_nonce}. Resyncing...")
                _nonce_cache[sender] = real_nonce
                pending_hash = None
                continue

            if "known transaction" in err:
                if pending_hash is not None:
                    print(f"⏳ Tx already in mempool, waiting for {pending_hash.hex()[:16]}...")
                    try:
                        receipt = w3.eth.wait_for_transaction_receipt(pending_hash, timeout=120)
                        if receipt["status"] == 0:
                            _nonce_cache[sender] = nonce + 1
                            raise RuntimeError(f"Revert on-chain (status 0). Hash: {pending_hash.hex()}")
                        _nonce_cache[sender] = nonce + 1
                        return receipt
                    except Exception:
                        pass
                real_nonce = w3.eth.get_transaction_count(sender)
                _nonce_cache[sender] = real_nonce
                pending_hash = None
                continue

            if "replacement transaction underpriced" in err or "underpriced" in err:
                real_nonce = w3.eth.get_transaction_count(sender)
                print(f"⚠️ Underpriced (nonce {nonce}): chain={real_nonce}. Resyncing...")
                _nonce_cache[sender] = real_nonce
                pending_hash = None
                continue

            if "revert" in err or "execution reverted" in err:
                # ERC20 custom errors (e.g. insufficient balance) → silent raise, let caller log
                if "erc20" not in err:
                    print(f"❌ Contract revert: {err}")
                raise

            if attempt < retries - 1:
                wait_time = 1.5 ** attempt
                print(f"🔄 Error: {err}. Retry in {wait_time:.1f}s...")
                time.sleep(wait_time)
                # Don't resync nonce if tx might still be pending
                if pending_hash is None:
                    _nonce_cache[sender] = w3.eth.get_transaction_count(sender)
            else:
                raise RuntimeError(f"send_tx: failed after {retries} attempts. Error: {err}")

    raise RuntimeError("send_tx: Unexpected loop exit")


def advance_day(w3: Web3, days: int = 1) -> None:
    seconds = 86_400 * days
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])
    log(f"Advanced blockchain by {days} day(s) ({seconds}s)", "SYSTEM")


def to_wei(n) -> int:
    return int(float(n) * 10 ** 18)


def from_wei(n) -> float:
    if n < 0:
        return -(abs(n) / 10 ** 18)
    return float(n) / 10 ** 18


def generate_salt() -> int:
    return random.randint(0, 2**256 - 1)


def keccak256_commit(fraud_score: int, pattern_valid: bool, salt: int) -> bytes:
    from eth_abi import encode
    encoded = encode(["uint256", "bool", "uint256"], [fraud_score, pattern_valid, salt])
    return Web3.keccak(encoded)


def compute_fraud_score(tier_id: int, total_claims: int, total_swaps: int) -> tuple:
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
