#!/usr/bin/env python3
"""
Oracle Simulator - Standalone oracle node for MEV Insurance Protocol.

Monitors pending claims, performs fraud analysis, and submits
commit-reveal verdicts for assigned claims.

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/oracle.py [--oracle-idx 0] [--cycles 50]
"""
import argparse
import random
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, get_accounts,
    send_tx, keccak256_commit, generate_salt, to_wei, from_wei,
    increase_time, log
)

ORACLE_START_IDX = 2  # Hardhat accounts[2..8]


class OracleNode:
    """Simulates a single oracle node participating in claim evaluation."""

    def __init__(self, w3, contracts, accounts, oracle_idx=0):
        self.w3 = w3
        self.contracts = contracts
        self.accounts = accounts
        self.oracle_idx = oracle_idx
        self.address = accounts[ORACLE_START_IDX + oracle_idx]
        self.insurance = contracts["MEVInsurance"]
        self.registry = contracts["OracleRegistry"]
        self.token = contracts["MEVToken"]
        self.stats = {
            "claims_evaluated": 0,
            "commits": 0,
            "reveals": 0,
            "rewards_earned": 0,
            "penalties": 0,
        }

    def ensure_registered(self):
        """Register and activate oracle if not already done."""
        is_reg = self.registry.functions.isOracle(self.address).call()
        if not is_reg:
            min_stake = self.registry.functions.getMinimumStake().call()
            send_tx(self.w3, self.registry.functions.registerOracle(), self.address, value=min_stake)
            send_tx(self.w3, self.registry.functions.activateOracle(), self.address)
            log(f"Oracle #{self.oracle_idx} ({self.address[:10]}...) registered & activated")
        else:
            log(f"Oracle #{self.oracle_idx} ({self.address[:10]}...) already registered")

    def analyze_claim(self, claim_id):
        """
        Perform off-chain fraud analysis for a given claim.
        Returns (fraud_score, pattern_valid).
        """
        try:
            claim_info = self.insurance.functions.getClaimInfo(claim_id).call()
            claimant = claim_info[0]
            loss = claim_info[4]
        except Exception:
            claimant = None
            loss = 0

        # Base analysis
        base_score = random.randint(10, 60)

        # Check user profile for risk signals
        if claimant:
            try:
                profile = self.insurance.functions.getUserProfile(claimant).call()
                tier = profile[0]  # 0=Bronze, 1=Silver, 2=Gold, 3=Platinum
                total_swaps = profile[2]
                total_claims = profile[3]

                # Higher tier = lower base risk
                tier_adjustment = {0: 15, 1: 5, 2: -5, 3: -15}
                base_score += tier_adjustment.get(tier, 0)

                # High claim rate is suspicious
                if total_swaps > 0:
                    claim_rate = total_claims / total_swaps
                    if claim_rate > 0.5:
                        base_score += 25
                    elif claim_rate > 0.3:
                        base_score += 15
                    elif claim_rate > 0.15:
                        base_score += 5
            except Exception:
                pass

        # Loss amount analysis (very high losses are suspicious)
        if loss > to_wei(200):
            base_score += 10
        if loss > to_wei(400):
            base_score += 10

        # Pattern validity (90% of the time patterns are valid)
        pattern_valid = random.random() < 0.90

        # Add per-oracle variance (each oracle sees slightly different data)
        variance = random.randint(-8, 8)
        fraud_score = max(0, min(130, base_score + variance))

        return fraud_score, pattern_valid

    def process_claim(self, claim_id):
        """Full commit-reveal cycle for a single claim."""
        # Check if this oracle is assigned
        try:
            assigned = self.insurance.functions.getClaimOracles(claim_id).call()
        except Exception:
            return False

        if self.address not in assigned:
            return False

        log(f"  Oracle #{self.oracle_idx} assigned to claim #{claim_id}")

        # Analyze
        fraud_score, pattern_valid = self.analyze_claim(claim_id)
        salt = generate_salt()
        commit_hash = keccak256_commit(fraud_score, pattern_valid, salt)

        # Commit
        try:
            send_tx(self.w3, self.insurance.functions.commitVerdict(claim_id, commit_hash), self.address)
            self.stats["commits"] += 1
            log(f"  Oracle #{self.oracle_idx} committed (score={fraud_score}, valid={pattern_valid})")
        except Exception as e:
            log(f"  Oracle #{self.oracle_idx} commit failed: {e}")
            return False

        # Reveal
        try:
            send_tx(self.w3, self.insurance.functions.revealVerdict(
                claim_id, fraud_score, pattern_valid, salt
            ), self.address)
            self.stats["reveals"] += 1
            self.stats["claims_evaluated"] += 1
            log(f"  Oracle #{self.oracle_idx} revealed")
        except Exception as e:
            log(f"  Oracle #{self.oracle_idx} reveal failed: {e}")
            return False

        return True

    def check_balance(self):
        """Check oracle stake and ETH balance."""
        try:
            info = self.registry.functions.getOracleInfo(self.address).call()
            stake = info[0]
            status = info[1]
            status_names = {0: "Inactive", 1: "Active", 2: "Suspended", 3: "Watchlist"}
            eth_bal = self.w3.eth.get_balance(self.address)
            log(f"  Oracle #{self.oracle_idx}: stake={from_wei(stake):.4f} ETH, "
                f"status={status_names.get(status, status)}, ETH={from_wei(eth_bal):.4f}")
        except Exception as e:
            log(f"  Balance check failed: {e}")

    def print_stats(self):
        """Print oracle performance stats."""
        log(f"\n  Oracle #{self.oracle_idx} Stats:")
        log(f"    Claims evaluated: {self.stats['claims_evaluated']}")
        log(f"    Commits:          {self.stats['commits']}")
        log(f"    Reveals:          {self.stats['reveals']}")
        self.check_balance()


def poll_and_process(oracle, insurance, poll_cycles, poll_interval):
    """Poll for new claims and process them."""
    last_processed = 0

    for cycle in range(poll_cycles):
        try:
            total_claims = insurance.functions.getClaimsCount().call()
        except Exception:
            total_claims = 0

        if total_claims > last_processed:
            for cid in range(last_processed, total_claims):
                oracle.process_claim(cid)
            last_processed = total_claims
        else:
            log(f"  [Cycle {cycle+1}/{poll_cycles}] No new claims (total={total_claims})")

        if cycle < poll_cycles - 1:
            time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Oracle Node Simulator")
    parser.add_argument("--oracle-idx", type=int, default=0, help="Oracle index (0-6)")
    parser.add_argument("--cycles", type=int, default=50, help="Polling cycles")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval (seconds)")
    args = parser.parse_args()

    log(f"Oracle Node Simulator - Oracle #{args.oracle_idx}")
    log("=" * 50)

    w3 = get_web3()
    contracts = get_all_contracts(w3)
    accounts = get_accounts(w3)

    if ORACLE_START_IDX + args.oracle_idx >= len(accounts):
        log(f"ERROR: Oracle index {args.oracle_idx} out of range")
        sys.exit(1)

    oracle = OracleNode(w3, contracts, accounts, args.oracle_idx)
    oracle.ensure_registered()
    oracle.check_balance()

    log(f"\nPolling for claims ({args.cycles} cycles, {args.interval}s interval)...")
    poll_and_process(oracle, contracts["MEVInsurance"], args.cycles, args.interval)

    oracle.print_stats()
    log("\nOracle node shutting down.")


if __name__ == "__main__":
    main()
