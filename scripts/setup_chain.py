"""
setup_chain.py — One-time chain initialization for MEV Insurance simulation.

Run this ONCE after deploying contracts:
    python scripts/setup_chain.py

What it does:
  1. Loads deployed contracts from config/deployed_addresses.json
  2. Registers 7 oracles (accounts 6-12) in OracleRegistry
  3. Stakes MEVI for each oracle and activates them
  4. Funds bot account (account 19) with MEVI + USDC
  5. Registers the user account (account 1) in MEVInsurance
  6. Saves oracle/bot/user addresses to config/actors.json
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, load_actors, save_actors,
    send_tx, to_wei, from_wei, log, advance_day
)

# ── Account indices ──
DEPLOYER_IDX   = 0
USER_IDX       = 1        # The human player
ORACLE_START   = 6        # Oracles: accounts 6-12
ORACLE_COUNT   = 7
BOT_IDX        = 19       # MEV bot

ORACLE_STAKE   = to_wei(1000)   # 1000 MEVI stake per oracle
BOT_FUND_MEVI  = to_wei(50_000)
BOT_FUND_USDC  = to_wei(50_000)
USER_FUND_MEVI = to_wei(100_000)


def setup_oracles(w3, contracts, deployer, oracle_addrs):
    """Register, fund, stake, and activate all oracles."""
    token    = contracts["MEVToken"]
    registry = contracts["OracleRegistry"]

    log(f"Setting up {len(oracle_addrs)} oracles…", "SETUP")

    for i, addr in enumerate(oracle_addrs):
        oracle_id = f"Oracle-{i+1}"

        # Check if already registered
        try:
            info = registry.functions.oracles(addr).call()
            status = info[1]  # status field
            if status != 0:   # 0 = Inactive (not registered)
                log(f"{oracle_id} already registered (status={status})", "SETUP")
                continue
        except Exception:
            pass

        # Fund oracle with MEVI for staking
        bal = token.functions.balanceOf(addr).call()
        if bal < ORACLE_STAKE:
            send_tx(w3, token.functions.transfer(addr, ORACLE_STAKE * 2), deployer)
            log(f"{oracle_id} funded with {from_wei(ORACLE_STAKE * 2):.0f} MEVI", "SETUP")

        # Register oracle
        try:
            send_tx(w3, registry.functions.registerOracle(), addr)
            log(f"{oracle_id} registered", "SETUP")
        except Exception as e:
            if "already" in str(e).lower():
                log(f"{oracle_id} already registered", "SETUP")
            else:
                log(f"{oracle_id} registration failed: {e}", "ERROR")
                continue

        # Approve token spending for stake
        send_tx(w3, token.functions.approve(registry.address, ORACLE_STAKE * 2), addr)

        # Stake
        try:
            send_tx(w3, registry.functions.stakeTokens(ORACLE_STAKE), addr)
            log(f"{oracle_id} staked {from_wei(ORACLE_STAKE):.0f} MEVI", "SETUP")
        except Exception as e:
            log(f"{oracle_id} stake failed: {e}", "ERROR")
            continue

        # Activate (owner call)
        try:
            send_tx(w3, registry.functions.activateOracle(addr), deployer)
            log(f"{oracle_id} activated", "SETUP")
        except Exception as e:
            log(f"{oracle_id} activation failed: {e}", "ERROR")

    log("Oracle setup complete", "SETUP")


def setup_bot(w3, contracts, deployer, bot_addr):
    """Fund the bot account with MEVI and USDC."""
    token = contracts["MEVToken"]
    usdc  = contracts["MockUSDC"]

    mevi_bal = token.functions.balanceOf(bot_addr).call()
    if mevi_bal < BOT_FUND_MEVI // 2:
        send_tx(w3, token.functions.transfer(bot_addr, BOT_FUND_MEVI), deployer)
        log(f"Bot funded with {from_wei(BOT_FUND_MEVI):.0f} MEVI", "SETUP")

    usdc_bal = usdc.functions.balanceOf(bot_addr).call()
    if usdc_bal < BOT_FUND_USDC // 2:
        send_tx(w3, usdc.functions.transfer(bot_addr, BOT_FUND_USDC), deployer)
        log(f"Bot funded with {from_wei(BOT_FUND_USDC):.0f} USDC", "SETUP")

    log(f"Bot ({bot_addr[:10]}…) setup complete", "SETUP")


def setup_user(w3, contracts, deployer, user_addr):
    """Fund and register the user in MEVInsurance."""
    token     = contracts["MEVToken"]
    insurance = contracts["MEVInsurance"]

    # Fund
    bal = token.functions.balanceOf(user_addr).call()
    if bal < USER_FUND_MEVI // 2:
        send_tx(w3, token.functions.transfer(user_addr, USER_FUND_MEVI), deployer)
        log(f"User funded with {from_wei(USER_FUND_MEVI):.0f} MEVI", "SETUP")

    # Register
    try:
        is_reg = insurance.functions.registeredUsers(user_addr).call()
        if not is_reg:
            send_tx(w3, insurance.functions.registerUser(), user_addr)
            log(f"User ({user_addr[:10]}…) registered in MEVInsurance", "SETUP")
        else:
            log(f"User already registered", "SETUP")
    except Exception as e:
        log(f"User registration failed: {e}", "ERROR")

    # Approve large allowance
    send_tx(w3, token.functions.approve(insurance.address, to_wei(1_000_000)), user_addr)
    log(f"User approved 1M MEVI spending for insurance contract", "SETUP")


def main():
    log("=" * 55, "SYSTEM")
    log("MEV Insurance — Chain Setup", "SYSTEM")
    log("=" * 55, "SYSTEM")

    w3 = get_web3()
    accounts = w3.eth.accounts

    if len(accounts) < 20:
        log(f"Need at least 20 Hardhat accounts, got {len(accounts)}", "ERROR")
        sys.exit(1)

    deployer     = accounts[DEPLOYER_IDX]
    user_addr    = accounts[USER_IDX]
    oracle_addrs = accounts[ORACLE_START : ORACLE_START + ORACLE_COUNT]
    bot_addr     = accounts[BOT_IDX]

    log(f"Deployer : {deployer}", "SETUP")
    log(f"User     : {user_addr}", "SETUP")
    log(f"Oracles  : {oracle_addrs[0][:10]}… – {oracle_addrs[-1][:10]}…", "SETUP")
    log(f"Bot      : {bot_addr}", "SETUP")

    contracts = get_all_contracts(w3)
    required = ["MEVToken", "MockUSDC", "MEVInsurance", "OracleRegistry"]
    for name in required:
        if name not in contracts:
            log(f"Contract '{name}' not found — run deploy_all.js first", "ERROR")
            sys.exit(1)

    # ── Setup steps ──
    setup_oracles(w3, contracts, deployer, oracle_addrs)
    setup_bot(w3, contracts, deployer, bot_addr)
    setup_user(w3, contracts, deployer, user_addr)

    # ── Save actors.json ──
    actors = {
        "deployer": deployer,
        "user": user_addr,
        "oracles": oracle_addrs,
        "bot": bot_addr,
    }
    save_actors(actors)

    log("", "SYSTEM")
    log("Setup complete! You can now run:", "SYSTEM")
    log("  python scripts/user_cli.py       (interactive CLI)", "SYSTEM")
    log("  python scripts/bot_daemon.py     (background bot)", "SYSTEM")
    log("  python scripts/status.py         (pool dashboard)", "SYSTEM")
    log("  python scripts/advance_day.py    (skip one day)", "SYSTEM")


if __name__ == "__main__":
    main()
