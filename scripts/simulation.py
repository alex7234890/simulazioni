#!/usr/bin/env python3
"""
MEV Insurance Protocol - End-to-End Simulation (Sequential)

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/simulation.py
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, load_config, get_all_contracts, get_accounts,
    send_tx, keccak256_commit, generate_salt, to_wei, from_wei,
    increase_time, log
)

# ── Config ──
N_CYCLES = 20
N_ORACLES = 7           # accounts[2..8]
TRADER_ACCOUNT_IDX = 1  # accounts[1]
BOT_ACCOUNT_IDX = 9     # accounts[9]
ORACLE_START_IDX = 2    # accounts[2..8]

# Stats
stats = {
    "swaps": 0, "claims_submitted": 0, "claims_approved": 0,
    "claims_rejected": 0, "claims_captcha": 0, "claims_invalid_pattern": 0,
    "total_loss": 0, "total_payout": 0, "total_premium": 0,
    "bot_profit": 0, "oracle_rewards": 0, "secondary_reviews": 0,
}


def setup(w3, contracts, accounts):
    """Initial setup: fund trader/bot, register oracles."""
    token = contracts["MEVToken"]
    usdc = contracts["MockUSDC"]
    registry = contracts["OracleRegistry"]
    insurance = contracts["MEVInsurance"]
    deployer = accounts[0]
    trader = accounts[TRADER_ACCOUNT_IDX]
    bot_addr = accounts[BOT_ACCOUNT_IDX]

    log("=== Setup Phase ===")

    # Fund trader with MEVI
    trader_bal = token.functions.balanceOf(trader).call()
    if trader_bal < to_wei(10000):
        send_tx(w3, token.functions.transfer(trader, to_wei(50000)), deployer)
        log(f"Trader {trader[:10]}... funded with 50k MEVI")

    # Fund bot with MEVI + USDC
    bot_bal = token.functions.balanceOf(bot_addr).call()
    if bot_bal < to_wei(10000):
        send_tx(w3, token.functions.transfer(bot_addr, to_wei(50000)), deployer)
        send_tx(w3, usdc.functions.transfer(bot_addr, to_wei(50000)), deployer)
        log(f"Bot {bot_addr[:10]}... funded with 50k MEVI + 50k USDC")

    # Register and activate oracles
    for i in range(N_ORACLES):
        oracle = accounts[ORACLE_START_IDX + i]
        is_registered = registry.functions.isOracle(oracle).call()
        if not is_registered:
            min_stake = registry.functions.getMinimumStake().call()
            send_tx(w3, registry.functions.registerOracle(), oracle, value=min_stake)
            send_tx(w3, registry.functions.activateOracle(), oracle)
            log(f"Oracle {i} ({oracle[:10]}...) registered and activated")
        else:
            log(f"Oracle {i} ({oracle[:10]}...) already registered")

    # Register trader if needed
    is_registered = insurance.functions.registeredUsers(trader).call()
    if not is_registered:
        send_tx(w3, insurance.functions.registerUser(), trader)
        log(f"Trader registered")

    # Buy policy if needed
    policy = insurance.functions.policies(trader).call()
    if not policy[6]:  # active field
        send_tx(w3, token.functions.approve(insurance.address, to_wei(100000)), trader)
        send_tx(w3, insurance.functions.buyPolicy(2), trader)  # High coverage
        log("Trader bought High coverage policy")
    else:
        # Ensure enough approval
        send_tx(w3, token.functions.approve(insurance.address, to_wei(100000)), trader)

    log("Setup complete!\n")


def simulate_oracle_analysis(user_addr, insurance, w3):
    """Simulate off-chain fraud analysis. Returns (fraud_score, pattern_valid)."""
    # Simplified scoring:
    # - Pattern valid 90% of the time
    pattern_valid = random.random() < 0.90

    # - Fraud score components (simplified)
    tier_score = 50   # Bronze default
    claim_rate_score = random.randint(0, 30)
    network_score = random.randint(0, 15)

    try:
        profile = insurance.functions.getUserProfile(user_addr).call()
        tier = profile[0]
        tier_map = {0: 50, 1: 30, 2: 15, 3: 0}
        tier_score = tier_map.get(tier, 50)
        total_swaps = profile[2]
        total_claims = profile[3]
        if total_swaps > 0:
            claim_rate = total_claims / total_swaps
            if claim_rate > 0.5:
                claim_rate_score = 40
            elif claim_rate > 0.3:
                claim_rate_score = 25
            elif claim_rate > 0.1:
                claim_rate_score = 15
            else:
                claim_rate_score = 5
    except Exception:
        pass

    fraud_score = min(130, tier_score + claim_rate_score + network_score)
    # Add some randomness per oracle
    fraud_score = max(0, min(130, fraud_score + random.randint(-10, 10)))
    return fraud_score, pattern_valid


def run_cycle(w3, contracts, accounts, cycle_num):
    """Run one claim cycle: swap -> (sandwich) -> claim -> oracle eval -> finalize."""
    token = contracts["MEVToken"]
    insurance = contracts["MEVInsurance"]
    amm = contracts["MockAMM"]
    bot_contract = contracts["SandwichBot"]
    trader = accounts[TRADER_ACCOUNT_IDX]
    bot_addr = accounts[BOT_ACCOUNT_IDX]
    deployer = accounts[0]

    log(f"─── Cycle {cycle_num + 1}/{N_CYCLES} ───")

    # 1. Insured swap
    swap_value = random.randint(50, 500)
    swap_value_wei = to_wei(swap_value)
    try:
        receipt = send_tx(w3, insurance.functions.insuredSwap(swap_value_wei), trader)
        swap_id = insurance.functions.getInsuredSwapsCount().call() - 1
        stats["swaps"] += 1

        # Get premium paid from InsuredSwap struct
        swap_info = insurance.functions.insuredSwaps(swap_id).call()
        premium_paid = swap_info[2]  # premiumPaid
        stats["total_premium"] += premium_paid
        log(f"  Swap #{swap_id}: value={swap_value} MEVI, premium={from_wei(premium_paid):.4f} MEVI")
    except Exception as e:
        log(f"  insuredSwap failed: {e}")
        return

    # 2. Simulate sandwich attack (30% chance)
    is_sandwich = random.random() < 0.30
    loss_pct = random.uniform(0.05, 0.20) if is_sandwich else random.uniform(0.02, 0.08)
    loss = int(swap_value * loss_pct)
    if loss < 1:
        loss = 1
    loss_wei = to_wei(loss)

    if is_sandwich:
        log(f"  SANDWICH ATTACK! Loss: {loss} MEVI ({loss_pct*100:.1f}%)")
    else:
        log(f"  Normal claim. Estimated loss: {loss} MEVI ({loss_pct*100:.1f}%)")

    # 3. Submit claim
    tx1 = w3.keccak(text=f"frontrun_{cycle_num}_{random.randint(0,999999)}")
    tx2 = w3.keccak(text=f"victim_{cycle_num}_{random.randint(0,999999)}")
    tx3 = w3.keccak(text=f"backrun_{cycle_num}_{random.randint(0,999999)}")
    bot_claim_addr = bot_addr if is_sandwich else "0x0000000000000000000000000000000000000000"

    try:
        send_tx(w3, insurance.functions.submitClaim(
            swap_id, tx1, tx2, tx3, loss_wei, bot_claim_addr
        ), trader)
        claim_id = insurance.functions.getClaimsCount().call() - 1
        stats["claims_submitted"] += 1
        stats["total_loss"] += loss_wei
        log(f"  Claim #{claim_id} submitted")
    except Exception as e:
        log(f"  submitClaim failed: {e}")
        return

    # 4. Get assigned oracles
    assigned = insurance.functions.getClaimOracles(claim_id).call()
    log(f"  Assigned {len(assigned)} oracles")

    # 5. Oracle commit-reveal
    oracle_data = []
    for addr in assigned:
        fraud_score, pattern_valid = simulate_oracle_analysis(trader, insurance, w3)
        salt = generate_salt()
        commit_hash = keccak256_commit(fraud_score, pattern_valid, salt)
        oracle_data.append({
            "address": addr,
            "fraud_score": fraud_score,
            "pattern_valid": pattern_valid,
            "salt": salt,
            "commit_hash": commit_hash,
        })

    # Find signers for assigned oracles
    oracle_accounts = {accounts[ORACLE_START_IDX + i]: accounts[ORACLE_START_IDX + i]
                       for i in range(N_ORACLES)}

    # Commit phase
    for od in oracle_data:
        signer = od["address"]
        if signer not in oracle_accounts:
            continue
        try:
            send_tx(w3, insurance.functions.commitVerdict(claim_id, od["commit_hash"]), signer)
        except Exception as e:
            log(f"  Commit failed for {signer[:10]}: {e}")

    # Reveal phase
    scores = []
    for od in oracle_data:
        signer = od["address"]
        if signer not in oracle_accounts:
            continue
        try:
            send_tx(w3, insurance.functions.revealVerdict(
                claim_id, od["fraud_score"], od["pattern_valid"], od["salt"]
            ), signer)
            scores.append(od["fraud_score"])
        except Exception as e:
            log(f"  Reveal failed for {signer[:10]}: {e}")

    if not scores:
        log("  No oracle reveals - skipping finalization")
        return

    median_score = sorted(scores)[len(scores) // 2]
    patterns_valid = sum(1 for od in oracle_data if od["pattern_valid"])
    log(f"  Oracle scores: {scores}, median={median_score}, valid_patterns={patterns_valid}/{len(scores)}")

    # 6. Finalize
    try:
        receipt = send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer)
    except Exception as e:
        log(f"  finalizeClaim failed: {e}")
        return

    # Check result
    claim_info = insurance.functions.getClaimInfo(claim_id).call()
    status = claim_info[1]  # ClaimStatus
    status_names = {0: "Pending", 1: "OracleReview", 2: "CAPTCHARequired",
                    3: "Approved", 4: "Rejected", 5: "InvalidPattern"}
    status_name = status_names.get(status, f"Unknown({status})")

    # If secondary review was triggered (status back to OracleReview), do second round
    if status == 1:  # OracleReview - secondary review
        stats["secondary_reviews"] += 1
        log(f"  SECONDARY REVIEW triggered (high dispersione)")
        # Re-do oracle commit-reveal with new oracles
        assigned2 = insurance.functions.getClaimOracles(claim_id).call()
        oracle_data2 = []
        for addr in assigned2:
            fs = median_score + random.randint(-5, 5)
            fs = max(0, min(130, fs))
            salt = generate_salt()
            ch = keccak256_commit(fs, True, salt)
            oracle_data2.append({"address": addr, "fraud_score": fs,
                                 "pattern_valid": True, "salt": salt, "commit_hash": ch})

        for od in oracle_data2:
            signer = od["address"]
            try:
                send_tx(w3, insurance.functions.commitVerdict(claim_id, od["commit_hash"]), signer)
            except Exception:
                pass
        for od in oracle_data2:
            signer = od["address"]
            try:
                send_tx(w3, insurance.functions.revealVerdict(
                    claim_id, od["fraud_score"], od["pattern_valid"], od["salt"]
                ), signer)
            except Exception:
                pass

        try:
            send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer)
        except Exception as e:
            log(f"  Second finalize failed: {e}")
            return

        claim_info = insurance.functions.getClaimInfo(claim_id).call()
        status = claim_info[1]
        status_name = status_names.get(status, f"Unknown({status})")

    # 7. Handle outcome
    if status == 3:  # Approved
        stats["claims_approved"] += 1
        payout = claim_info[4] * insurance.functions.coveragePercentBps(2).call() // 10000  # High=100%
        stats["total_payout"] += payout
        log(f"  -> APPROVED! Payout: {from_wei(payout):.2f} MEVI")

    elif status == 2:  # CAPTCHARequired
        stats["claims_captcha"] += 1
        # Auto-resolve CAPTCHA (approve 80% of the time)
        approve = random.random() < 0.80
        try:
            bal_before = token.functions.balanceOf(trader).call()
            send_tx(w3, insurance.functions.resolveCAPTCHA(claim_id, approve), deployer)
            bal_after = token.functions.balanceOf(trader).call()
            payout = bal_after - bal_before
            if approve:
                stats["claims_approved"] += 1
                stats["total_payout"] += payout
                log(f"  -> CAPTCHA resolved: APPROVED (payout: {from_wei(payout):.2f} MEVI)")
            else:
                stats["claims_rejected"] += 1
                log(f"  -> CAPTCHA resolved: REJECTED")
        except Exception as e:
            log(f"  CAPTCHA resolution failed: {e}")

    elif status == 4:  # Rejected
        stats["claims_rejected"] += 1
        log(f"  -> REJECTED (fraud score too high)")

    elif status == 5:  # InvalidPattern
        stats["claims_invalid_pattern"] += 1
        log(f"  -> INVALID PATTERN (>70% oracles flagged)")

    else:
        log(f"  -> Status: {status_name}")


def print_final_stats(w3, contracts, accounts):
    """Print end-of-simulation statistics."""
    token = contracts["MEVToken"]
    insurance = contracts["MEVInsurance"]
    trader = accounts[TRADER_ACCOUNT_IDX]

    log("\n" + "=" * 60)
    log("         MEV INSURANCE SIMULATION - FINAL REPORT")
    log("=" * 60)

    pool_balance = token.functions.balanceOf(insurance.address).call()
    trader_balance = token.functions.balanceOf(trader).call()

    try:
        profile = insurance.functions.getUserProfile(trader).call()
        tier_names = {0: "Bronze", 1: "Silver", 2: "Gold", 3: "Platinum"}
        tier = tier_names.get(profile[0], "Unknown")
        avg_fraud = profile[6]
    except Exception:
        tier = "Unknown"
        avg_fraud = 0

    log(f"  Cycles completed:       {N_CYCLES}")
    log(f"  Swaps insured:          {stats['swaps']}")
    log(f"  Claims submitted:       {stats['claims_submitted']}")
    log(f"  Claims approved:        {stats['claims_approved']}")
    log(f"  Claims rejected:        {stats['claims_rejected']}")
    log(f"  Claims CAPTCHA:         {stats['claims_captcha']}")
    log(f"  Invalid pattern:        {stats['claims_invalid_pattern']}")
    log(f"  Secondary reviews:      {stats['secondary_reviews']}")
    log(f"  ──────────────────────────────────────")
    log(f"  Total premiums:         {from_wei(stats['total_premium']):.4f} MEVI")
    log(f"  Total losses claimed:   {from_wei(stats['total_loss']):.2f} MEVI")
    log(f"  Total payouts:          {from_wei(stats['total_payout']):.2f} MEVI")
    log(f"  Pool balance:           {from_wei(pool_balance):.2f} MEVI")
    log(f"  Trader balance:         {from_wei(trader_balance):.2f} MEVI")
    log(f"  Trader tier:            {tier}")
    log(f"  Trader avg fraud score: {avg_fraud}")

    net = stats['total_premium'] - stats['total_payout']
    log(f"  Pool P&L:               {from_wei(net):+.4f} MEVI")
    if stats['total_loss'] > 0:
        coverage_ratio = stats['total_payout'] / stats['total_loss'] * 100
        log(f"  Coverage ratio:         {coverage_ratio:.1f}%")

    log("=" * 60)


def main():
    log("MEV Insurance Protocol - Simulation Starting")

    # Connect
    w3 = get_web3()
    log(f"Connected to chain (block #{w3.eth.block_number})")

    # Load contracts
    contracts = get_all_contracts(w3)
    accounts = get_accounts(w3)
    log(f"Loaded {len(contracts)} contracts, {len(accounts)} accounts")

    if len(accounts) < 10:
        log("ERROR: Need at least 10 Hardhat accounts!")
        sys.exit(1)

    # Setup
    setup(w3, contracts, accounts)

    # Run cycles
    for i in range(N_CYCLES):
        try:
            run_cycle(w3, contracts, accounts, i)
        except Exception as e:
            log(f"Cycle {i+1} error: {e}")
        print()

    # Final stats
    print_final_stats(w3, contracts, accounts)


if __name__ == "__main__":
    main()
