#!/usr/bin/env python3
"""
MEV Insurance Protocol — Simulation Orchestrator

Single entry point that launches traders, bots, and oracles,
runs a day-by-day simulation loop, and produces detailed logs.

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/launch.py --traders 3 --bots 1 --oracles 7 --days 5
"""
import argparse
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, get_account, send_tx,
    to_wei, from_wei, increase_time, log, set_log_file, close_log_file,
)
from actors.trader_actor import TraderActor
from actors.oracle_actor import OracleActor
from actors.bot_actor import BotActor
from dashboard import (
    print_daily_summary, print_pool_status, print_user_table,
    print_oracle_table, print_bot_table, print_final_report,
)

# ── Constants ──
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
PATT = 0.15          # Base sandwich attack probability (PDF scenario)
FRAUD_CLAIM_RATE = 0.10  # 10% of non-sandwich claims are fraudulent attempts
ONE_DAY = 86400      # seconds
MAX_DAILY_SWAPS_BRONZE = 3

# ── Account allocation ──
# accounts[0] = deployer
# accounts[1..N] = traders
# accounts[N+1..N+M] = oracles
# accounts[N+M+1..N+M+K] = bots


def parse_args():
    parser = argparse.ArgumentParser(
        description="MEV Insurance Protocol — Simulation Orchestrator",
    )
    parser.add_argument("--network", default="localhost", choices=["localhost", "sepolia"])
    parser.add_argument("--traders", type=int, default=3, help="Number of traders")
    parser.add_argument("--bots", type=int, default=1, help="Number of MEV bots")
    parser.add_argument("--oracles", type=int, default=7, help="Number of oracles")
    parser.add_argument("--days", type=int, default=5, help="Simulated days")
    parser.add_argument("--swap-interval", type=int, default=3, help="Max swaps per trader per day")
    parser.add_argument("--attack-rate", type=float, default=PATT, help="Sandwich attack probability")
    parser.add_argument("--claim-rate", type=float, default=0.6, help="Claim filing probability after swap")
    parser.add_argument("--log-file", default=None, help="Optional log file path")
    parser.add_argument("--dashboard", action="store_true", help="Show weekly pool dashboards")
    return parser.parse_args()


def print_protocol_params(contracts):
    """Print on-chain protocol parameters."""
    insurance = contracts["MEVInsurance"]
    calc = contracts["PremiumCalculator"]

    log("╔══════════════════════════════════════════════════════════╗", "DASH")
    log("║           PROTOCOL PARAMETERS (on-chain)                ║", "DASH")
    log("╠══════════════════════════════════════════════════════════╣", "DASH")

    params = []
    try:
        params.append(f"  Patt (attack prob):     {calc.functions.patt().call()} bps")
        params.append(f"  Pmin (min premium):     {calc.functions.pmin().call()} bps")
    except Exception:
        params.append("  PremiumCalculator: could not read")

    try:
        params.append(f"  θ_approve:              {insurance.functions.thetaApprove().call()}")
        params.append(f"  θ_reject:               {insurance.functions.thetaReject().call()}")
        params.append(f"  nOracle:                {insurance.functions.nOracle().call()}")
        params.append(f"  Dispersione threshold:  {insurance.functions.dispersioneThreshold().call()}")
        params.append(f"  Oracle timeout:         {insurance.functions.oracleTimeout().call()}s")
        params.append(f"  Policy duration:        {insurance.functions.policyDuration().call()}s")
        params.append(f"  Gas refund amount:      {from_wei(insurance.functions.gasRefundAmount().call()):.4f} MEVI")
    except Exception:
        params.append("  MEVInsurance: could not read some params")

    for p in params:
        log(f"║{p:<58s}║", "DASH")
    log("╚══════════════════════════════════════════════════════════╝", "DASH")
    print()


# ── Setup Phase ──

def setup_actors(w3, contracts, args):
    """Create and initialize all actors. Returns (traders, oracles, bots, deployer, deployer_key)."""
    network = args.network
    deployer_addr, deployer_key = get_account(w3, 0, network)

    # On localhost, disable oracle activation delay
    if network == "localhost":
        try:
            registry = contracts["OracleRegistry"]
            send_tx(w3, registry.functions.setTActivation(0), deployer_addr,
                    network=network, private_key=deployer_key)
            log("OracleRegistry.tActivation set to 0 for local testing", "SETUP")
        except Exception:
            pass  # might already be 0

    # Allocate accounts: [0]=deployer, [1..T]=traders, [T+1..T+O]=oracles, [T+O+1..T+O+B]=bots
    n_traders = args.traders
    n_oracles = args.oracles
    n_bots = args.bots
    total_needed = 1 + n_traders + n_oracles + n_bots

    log(f"Need {total_needed} accounts (1 deployer + {n_traders} traders + {n_oracles} oracles + {n_bots} bots)", "SETUP")

    # Create traders
    traders = []
    for i in range(n_traders):
        idx = 1 + i
        addr, pk = get_account(w3, idx, network)
        t = TraderActor(w3, contracts, addr, deployer_addr, f"Trader-{i+1}",
                        network=network, private_key=pk, deployer_key=deployer_key)
        traders.append(t)

    # Create oracles
    oracles = []
    for i in range(n_oracles):
        idx = 1 + n_traders + i
        addr, pk = get_account(w3, idx, network)
        o = OracleActor(w3, contracts, addr, f"Oracle-{i+1}",
                        network=network, private_key=pk)
        oracles.append(o)

    # Create bots
    bots = []
    for i in range(n_bots):
        idx = 1 + n_traders + n_oracles + i
        addr, pk = get_account(w3, idx, network)
        b = BotActor(w3, contracts, addr, f"Bot-{i+1}",
                     network=network, private_key=pk)
        bots.append(b)

    # Setup all actors
    log("── Setting up oracles ──", "SETUP")
    for o in oracles:
        o.setup(deployer_addr, deployer_key)

    log("── Setting up traders ──", "SETUP")
    for t in traders:
        t.setup()

    log("── Setting up bots ──", "SETUP")
    for b in bots:
        b.setup(deployer_addr, deployer_key)

    print()
    return traders, oracles, bots, deployer_addr, deployer_key


# ── Claim Processing ──

def process_claim_oracles(claim_id, oracles, insurance, is_real_attack):
    """
    All assigned oracles commit+reveal for a claim.
    Returns list of (fraud_score, pattern_valid) for oracles that participated.
    """
    try:
        assigned = insurance.functions.getClaimOracles(claim_id).call()
    except Exception:
        return []

    results = []
    # Map addresses to oracle actors
    oracle_map = {o.address: o for o in oracles}

    # Commit phase (all oracles commit first)
    commit_data = {}
    for addr in assigned:
        oracle = oracle_map.get(addr)
        if not oracle:
            continue
        fraud_score, pattern_valid, breakdown = oracle.analyze_claim(claim_id, is_real_attack)
        log(
            f"{oracle.oracle_id} | Claim #{claim_id}: FraudScore={fraud_score} "
            f"(tier={breakdown['tier']} + rate={breakdown['rate']} "
            f"+ net={breakdown['net']} + var={breakdown['var']:+d}), "
            f"pattern={'Valid' if pattern_valid else 'Invalid'}",
            "ORACLE",
        )
        if oracle.commit_verdict(claim_id, fraud_score, pattern_valid):
            commit_data[addr] = (oracle, fraud_score, pattern_valid)

    # Reveal phase (all oracles reveal after all commits)
    for addr, (oracle, fraud_score, pattern_valid) in commit_data.items():
        if oracle.reveal_verdict(claim_id):
            results.append((fraud_score, pattern_valid))

    return results


def finalize_and_handle(claim_id, trader, insurance, token, deployer, deployer_key,
                        w3, network, day_stats, cumulative_stats):
    """
    Finalize a claim and handle the outcome (CAPTCHA, approval, rejection).
    Updates stats dictionaries.
    """
    try:
        send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer,
                network=network, private_key=deployer_key)
    except Exception as e:
        log(f"finalizeClaim #{claim_id} failed: {e}", "ERROR")
        return

    # Read result
    try:
        claim_info = insurance.functions.getClaimInfo(claim_id).call()
    except Exception:
        return

    status = claim_info[1]
    median = claim_info[2]
    dispersione = claim_info[5]

    status_names = {0: "Pending", 1: "OracleReview", 2: "CAPTCHARequired",
                    3: "Approved", 4: "Rejected", 5: "InvalidPattern"}
    status_name = status_names.get(status, f"Unknown({status})")

    # Secondary review — status back to OracleReview
    if status == 1:
        day_stats["secondary_reviews"] = day_stats.get("secondary_reviews", 0) + 1
        cumulative_stats["total_secondary_reviews"] = cumulative_stats.get("total_secondary_reviews", 0) + 1
        log(f"Claim #{claim_id} → SECONDARY REVIEW (dispersione={dispersione} > threshold)", "CLAIM")
        # In a real run we'd need another oracle round here.
        # For simulation, skip secondary review claims (they need new oracle set).
        log(f"Claim #{claim_id} secondary review skipped in simulation (would need new oracle round)", "CLAIM")
        return

    log(f"Claim #{claim_id} finalized: {status_name} (median={median}, disp={dispersione})", "CLAIM")

    if status == 3:  # Approved
        day_stats["approved"] = day_stats.get("approved", 0) + 1
        cumulative_stats["total_approved"] = cumulative_stats.get("total_approved", 0) + 1
        # Read actual payout from balance change
        bal_before = token.functions.balanceOf(trader.address).call()
        payout = claim_info[4] * insurance.functions.coveragePercentBps(2).call() // 10000
        gas_refund = insurance.functions.gasRefundAmount().call()
        total_payout = payout + gas_refund
        day_stats["payout_out"] = day_stats.get("payout_out", 0) + total_payout
        cumulative_stats["total_payout"] = cumulative_stats.get("total_payout", 0) + total_payout
        trader.record_payout(total_payout)
        log(f"Claim #{claim_id} → APPROVED, payout={from_wei(total_payout):.4f} MEVI", "CLAIM")

    elif status == 2:  # CAPTCHARequired
        day_stats["captcha"] = day_stats.get("captcha", 0) + 1
        cumulative_stats["total_captcha"] = cumulative_stats.get("total_captcha", 0) + 1
        trader.record_captcha()

        # Auto-resolve CAPTCHA: 80% approve, 20% reject
        approve = random.random() < 0.80
        bal_before = token.functions.balanceOf(trader.address).call()
        try:
            send_tx(w3, insurance.functions.resolveCAPTCHA(claim_id, approve), deployer,
                    network=network, private_key=deployer_key)
        except Exception as e:
            log(f"CAPTCHA resolution failed for claim #{claim_id}: {e}", "ERROR")
            return

        bal_after = token.functions.balanceOf(trader.address).call()
        actual_payout = bal_after - bal_before

        if approve:
            day_stats["approved"] = day_stats.get("approved", 0) + 1
            cumulative_stats["total_approved"] = cumulative_stats.get("total_approved", 0) + 1
            day_stats["payout_out"] = day_stats.get("payout_out", 0) + actual_payout
            cumulative_stats["total_payout"] = cumulative_stats.get("total_payout", 0) + actual_payout
            trader.record_payout(actual_payout)
            log(f"Claim #{claim_id} CAPTCHA → APPROVED, payout={from_wei(actual_payout):.4f} MEVI", "CLAIM")
        else:
            day_stats["rejected"] = day_stats.get("rejected", 0) + 1
            cumulative_stats["total_rejected"] = cumulative_stats.get("total_rejected", 0) + 1
            trader.record_rejection()
            log(f"Claim #{claim_id} CAPTCHA → REJECTED", "CLAIM")

    elif status == 4:  # Rejected
        day_stats["rejected"] = day_stats.get("rejected", 0) + 1
        cumulative_stats["total_rejected"] = cumulative_stats.get("total_rejected", 0) + 1
        trader.record_rejection()
        log(f"Claim #{claim_id} → REJECTED (fraud score {median} >= θ_reject)", "CLAIM")

    elif status == 5:  # InvalidPattern
        day_stats["invalid_pattern"] = day_stats.get("invalid_pattern", 0) + 1
        cumulative_stats["total_invalid_pattern"] = cumulative_stats.get("total_invalid_pattern", 0) + 1
        trader.record_rejection()
        log(f"Claim #{claim_id} → INVALID PATTERN", "CLAIM")


# ── Main Simulation Loop ──

def run_day(day, traders, oracles, bots, contracts, w3, deployer, deployer_key,
            network, args, cumulative_stats):
    """
    Simulate one day. Each trader executes up to swap_interval swaps.
    Returns day_stats dict.
    """
    insurance = contracts["MEVInsurance"]
    token = contracts["MEVToken"]
    bot_pool = bots if bots else []

    day_stats = {
        "swaps": 0, "claims": 0, "approved": 0, "rejected": 0,
        "captcha": 0, "invalid_pattern": 0, "secondary_reviews": 0,
        "premium_in": 0, "payout_out": 0, "pool_balance": 0,
    }

    log(f"{'═' * 60}", "INFO")
    log(f"DAY {day} — Starting simulation", "TIME")
    log(f"{'═' * 60}", "INFO")

    for trader in traders:
        # Ensure policy is active (renew if expired)
        trader.ensure_policy_active()

        for swap_num in range(args.swap_interval):
            # Random swap value 50-500 MEVI
            swap_value = random.randint(50, 500)

            # Execute insured swap
            swap_id = trader.execute_insured_swap(swap_value)
            if swap_id is None:
                continue  # daily limit or other error

            day_stats["swaps"] += 1
            cumulative_stats["total_swaps"] = cumulative_stats.get("total_swaps", 0) + 1

            # Record premium
            try:
                swap_info = insurance.functions.insuredSwaps(swap_id).call()
                premium = swap_info[2]
                day_stats["premium_in"] += premium
                cumulative_stats["total_premium"] = cumulative_stats.get("total_premium", 0) + premium
            except Exception:
                premium = 0

            # Bot attack decision
            is_sandwich = False
            loss_pct = random.uniform(0.02, 0.08)  # base loss for non-attack

            if bot_pool:
                bot = random.choice(bot_pool)
                if bot.should_attack(swap_value, args.attack_rate):
                    success, profit = bot.execute_sandwich(swap_value)
                    if success:
                        is_sandwich = True
                        loss_pct = random.uniform(0.05, 0.20)

            # Decide whether to file a claim
            should_claim = random.random() < args.claim_rate
            if not should_claim:
                continue

            # Calculate loss
            loss = max(1, int(swap_value * loss_pct))
            loss_wei = to_wei(loss)

            # Determine if this is a fraudulent claim (non-sandwich claim attempt)
            is_fraud_attempt = False
            if not is_sandwich and random.random() < FRAUD_CLAIM_RATE:
                is_fraud_attempt = True

            # Bot address for the claim
            bot_address = ZERO_ADDRESS
            if is_sandwich and bot_pool:
                bot_address = bot.address

            if is_sandwich:
                log(f"{trader.trader_id} | SANDWICH detected! Loss: {loss} MEVI ({loss_pct*100:.1f}%)", "ATTACK")
            elif is_fraud_attempt:
                log(f"{trader.trader_id} | Fraudulent claim attempt: {loss} MEVI", "CLAIM")

            # Submit claim
            claim_id = trader.submit_claim(swap_id, loss, bot_address)
            if claim_id is None:
                continue

            day_stats["claims"] += 1
            cumulative_stats["total_claims"] = cumulative_stats.get("total_claims", 0) + 1
            cumulative_stats["total_loss_claimed"] = cumulative_stats.get("total_loss_claimed", 0) + loss_wei

            # Oracle evaluation: is_real_attack determines scoring behavior
            is_real_attack = is_sandwich and not is_fraud_attempt
            results = process_claim_oracles(claim_id, oracles, insurance, is_real_attack)

            if not results:
                log(f"Claim #{claim_id} — no oracle reveals, skipping finalization", "ERROR")
                continue

            scores = [r[0] for r in results]
            median = sorted(scores)[len(scores) // 2]
            log(f"Claim #{claim_id} | Scores: {scores}, median={median}", "ORACLE")

            # Finalize
            finalize_and_handle(
                claim_id, trader, insurance, token, deployer, deployer_key,
                w3, network, day_stats, cumulative_stats,
            )

            # Pool balance log
            pool_bal = token.functions.balanceOf(insurance.address).call()
            log(
                f"Pool balance: {from_wei(pool_bal):.2f} MEVI | "
                f"Premium in: +{from_wei(premium):.4f} | "
                f"Day payout out: -{from_wei(day_stats['payout_out']):.4f}",
                "POOL",
            )

    # End of day: advance time by 1 day to reset daily swap limits
    increase_time(w3, ONE_DAY, network)
    log(f"Advanced blockchain time by 1 day", "TIME")

    # Record pool balance
    day_stats["pool_balance"] = token.functions.balanceOf(insurance.address).call()
    cumulative_stats["pool_balance"] = day_stats["pool_balance"]

    return day_stats


# ── Main ──

def main():
    args = parse_args()

    if args.log_file:
        set_log_file(args.log_file)

    log("╔══════════════════════════════════════════════════════════╗", "DASH")
    log("║     MEV INSURANCE PROTOCOL — SIMULATION STARTING        ║", "DASH")
    log("╚══════════════════════════════════════════════════════════╝", "DASH")
    log(f"Network: {args.network} | Traders: {args.traders} | "
        f"Bots: {args.bots} | Oracles: {args.oracles} | Days: {args.days}", "INFO")
    log(f"Attack rate: {args.attack_rate*100:.0f}% | Claim rate: {args.claim_rate*100:.0f}% | "
        f"Swaps/day/trader: {args.swap_interval}", "INFO")
    print()

    # Connect
    w3 = get_web3(args.network)
    log(f"Connected to chain (block #{w3.eth.block_number})", "INFO")

    # Load contracts
    contracts = get_all_contracts(w3)
    log(f"Loaded {len(contracts)} contracts", "INFO")
    print()

    # Print protocol parameters
    print_protocol_params(contracts)

    # Setup actors
    traders, oracles, bots, deployer, deployer_key = setup_actors(w3, contracts, args)

    # Cumulative stats
    cumulative_stats = {
        "total_swaps": 0, "total_claims": 0, "total_approved": 0,
        "total_rejected": 0, "total_captcha": 0, "total_invalid_pattern": 0,
        "total_secondary_reviews": 0, "total_premium": 0, "total_payout": 0,
        "total_loss_claimed": 0, "pool_balance": 0,
    }

    # ── Day loop ──
    for day in range(1, args.days + 1):
        try:
            day_stats = run_day(
                day, traders, oracles, bots, contracts, w3,
                deployer, deployer_key, args.network, args, cumulative_stats,
            )
            print()
            print_daily_summary(day, day_stats)
        except Exception as e:
            log(f"Day {day} error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            print()

        # Weekly dashboard
        if args.dashboard and day % 7 == 0:
            print_pool_status(w3, contracts, cumulative_stats)
            print_user_table(traders)
            print_oracle_table(oracles)

    # ── Final Report ──
    print_final_report(cumulative_stats, traders, oracles, bots, args.days)

    if args.log_file:
        close_log_file()
        log(f"Log saved to {args.log_file}", "INFO")

    log("Simulation complete.", "INFO")


if __name__ == "__main__":
    main()
