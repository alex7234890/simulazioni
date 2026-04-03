#!/usr/bin/env python3
"""
Oracle Simulator - Standalone oracle node for MEV Insurance Protocol.

Performs real on-chain verification of sandwich attack claims:
  1. Fetches txHash1/txHash2/txHash3 from the claim
  2. Verifies all 3 transactions exist on-chain
  3. Verifies frontrun + backrun have same sender (bot)
  4. Verifies all 3 tx are in the same block
  5. Verifies ordering: frontrun.index < victim.index < backrun.index
  6. Verifies all 3 interact with the same contract (pool)
  7. Calculates fraud score based on user profile + claim analysis

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
            "patterns_valid": 0,
            "patterns_invalid": 0,
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

    # ─── Sandwich Pattern Verification (PDF §3.2) ───

    def _fetch_tx(self, tx_hash):
        """Fetch a transaction by hash. Returns None if not found."""
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
            return tx
        except Exception:
            return None

    def verify_sandwich_pattern(self, claim_id):
        """
        Verify the sandwich attack pattern from on-chain transaction data.
        Returns (pattern_valid, verification_details).

        Checks:
          1. All 3 tx hashes exist on-chain
          2. Frontrun (tx1) and backrun (tx3) have the same sender (the bot)
          3. All 3 are in the same block
          4. Ordering: tx1.index < tx2.index < tx3.index
          5. All 3 interact with the same contract (pool/AMM)
        """
        details = {
            "tx_exist": False,
            "same_bot_sender": False,
            "same_block": False,
            "correct_order": False,
            "same_pool": False,
        }

        try:
            claim_details = self.insurance.functions.getClaimDetails(claim_id).call()
            tx_hash1 = claim_details[1]
            tx_hash2 = claim_details[2]
            tx_hash3 = claim_details[3]
        # Fetch claim details (txHash1, txHash2, txHash3, botAddress)
        try:
            claim_details = self.insurance.functions.getClaimDetails(claim_id).call()
            # Returns: (user, txHash1, txHash2, txHash3, swapValue, loss, botAddress, secondaryReview)
            tx_hash1 = claim_details[1]  # frontrun
            tx_hash2 = claim_details[2]  # victim
            tx_hash3 = claim_details[3]  # backrun
            claimed_bot = claim_details[6]
        except Exception as e:
            log(f"    Cannot read claim details: {e}")
            return False, details

        # Check for zero hashes (no real tx provided)
        zero_hash = b'\x00' * 32
        if tx_hash1 == zero_hash or tx_hash2 == zero_hash or tx_hash3 == zero_hash:
            log(f"    Zero tx hash detected - no real transactions")
            return False, details

        # 1. Fetch all 3 transactions
        tx1 = self._fetch_tx(tx_hash1)
        tx2 = self._fetch_tx(tx_hash2)
        tx3 = self._fetch_tx(tx_hash3)

        if tx1 and tx2 and tx3:
            details["tx_exist"] = True
            log(f"    All 3 transactions found on-chain")
        else:
            missing = []
            if not tx1: missing.append("frontrun")
            if not tx2: missing.append("victim")
            if not tx3: missing.append("backrun")
            log(f"    Missing transactions: {', '.join(missing)}")
            return self._heuristic_pattern_check(claim_id, claimed_bot)

        if tx1["from"] == tx3["from"]:
            details["same_bot_sender"] = True
            log(f"    Frontrun & backrun same sender: {tx1['from'][:10]}...")
            # In simulation, tx hashes are synthetic (keccak of strings)
            # so they won't exist on-chain. Fall back to heuristic check.
            return self._heuristic_pattern_check(claim_id, claimed_bot)

        # 2. Verify frontrun and backrun have the same sender (the bot)
        if tx1["from"] == tx3["from"]:
            details["same_bot_sender"] = True
            log(f"    Frontrun & backrun same sender: {tx1['from'][:10]}...")
            # Cross-check with claimed bot address
            if claimed_bot != "0x0000000000000000000000000000000000000000":
                if tx1["from"].lower() != claimed_bot.lower():
                    log(f"    WARNING: Bot sender mismatch with claimed bot")
        else:
            log(f"    Frontrun sender ({tx1['from'][:10]}) != Backrun sender ({tx3['from'][:10]})")

        # 3. All in the same block
        if tx1["blockNumber"] == tx2["blockNumber"] == tx3["blockNumber"]:
            details["same_block"] = True
            log(f"    All in block #{tx1['blockNumber']}")
        else:
            log(f"    Different blocks: {tx1['blockNumber']}, {tx2['blockNumber']}, {tx3['blockNumber']}")

        # 4. Correct ordering: frontrun.index < victim.index < backrun.index
        if (tx1["transactionIndex"] < tx2["transactionIndex"] < tx3["transactionIndex"]):
            details["correct_order"] = True
            log(f"    Correct order: idx {tx1['transactionIndex']} < {tx2['transactionIndex']} < {tx3['transactionIndex']}")
        else:
            log(f"    Wrong order: {tx1['transactionIndex']}, {tx2['transactionIndex']}, {tx3['transactionIndex']}")

        # 5. All interact with the same contract (pool)
        if tx1["to"] and tx2["to"] and tx3["to"]:
            if tx1["to"] == tx2["to"] == tx3["to"]:
                details["same_pool"] = True
                log(f"    Same pool: {tx1['to'][:10]}...")
            elif tx1["to"] == tx3["to"]:
                details["same_pool"] = True
                log(f"    Frontrun & backrun same pool, victim via different entry")

        checks_passed = sum(details.values())
        pattern_valid = checks_passed >= 4
                # Frontrun and backrun same pool, victim might go through router
                details["same_pool"] = True
                log(f"    Frontrun & backrun same pool, victim via different entry")

        # Pattern is valid if all critical checks pass
        checks_passed = sum(details.values())
        pattern_valid = checks_passed >= 4  # At least 4/5 checks
        log(f"    Pattern verification: {checks_passed}/5 checks passed -> {'VALID' if pattern_valid else 'INVALID'}")

        return pattern_valid, details

    def _heuristic_pattern_check(self, claim_id, claimed_bot):
        """
        Fallback heuristic when tx hashes are synthetic (simulation mode).
        """
        details = {"heuristic": True}

        has_bot = claimed_bot != "0x0000000000000000000000000000000000000000"
        Checks on-chain state for consistency signals.
        """
        details = {"heuristic": True}

        # Check if a bot address was provided
        has_bot = claimed_bot != "0x0000000000000000000000000000000000000000"

        # Check if bot is known (has previous attack history)
        bot_known = False
        if has_bot:
            try:
                attack_count = self.insurance.functions.botAttackCount(claimed_bot).call()
                bot_known = attack_count > 0
            except Exception:
                pass

        # Check claim loss vs swap value ratio
        try:
            claim_details = self.insurance.functions.getClaimDetails(claim_id).call()
            swap_value = claim_details[4]
            loss = claim_details[5]
            if swap_value > 0:
                loss_ratio = loss / swap_value
                # Sandwich typically extracts 2-20% of swap value
                reasonable_loss = 0.01 <= loss_ratio <= 0.25
            else:
                reasonable_loss = False
        except Exception:
            reasonable_loss = True

        base_validity = 0.70
            reasonable_loss = True  # Benefit of the doubt

        # Heuristic: valid if bot provided AND loss is reasonable
        # With per-oracle variance
        base_validity = 0.70  # 70% base
        if has_bot:
            base_validity += 0.15
        if bot_known:
            base_validity += 0.10
        if not reasonable_loss:
            base_validity -= 0.30

        pattern_valid = random.random() < base_validity
        log(f"    Heuristic pattern check: bot={'yes' if has_bot else 'no'}, "
            f"known={'yes' if bot_known else 'no'}, "
            f"loss_ok={'yes' if reasonable_loss else 'no'} -> "
            f"{'VALID' if pattern_valid else 'INVALID'}")

        return pattern_valid, details

    # ─── Fraud Score Analysis ───

    def analyze_claim(self, claim_id):
        """
        Perform fraud analysis for a given claim.
        Returns (fraud_score, pattern_valid).
        """
        pattern_valid, pattern_details = self.verify_sandwich_pattern(claim_id)

        # Step 1: Verify sandwich pattern (real or heuristic)
        pattern_valid, pattern_details = self.verify_sandwich_pattern(claim_id)

        # Step 2: Calculate fraud score based on user profile
        try:
            claim_info = self.insurance.functions.getClaimInfo(claim_id).call()
            claimant = claim_info[0]
            loss = claim_info[4]
        except Exception:
            claimant = None
            loss = 0

        base_score = random.randint(10, 60)

        if claimant:
            try:
                profile = self.insurance.functions.getUserProfile(claimant).call()
                tier = profile[0]
                total_swaps = profile[2]
                total_claims = profile[3]

                tier_adjustment = {0: 15, 1: 5, 2: -5, 3: -15}
                base_score += tier_adjustment.get(tier, 0)

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

        # Loss amount analysis
        if loss > to_wei(200):
            base_score += 10
        if loss > to_wei(400):
            base_score += 10

        if pattern_details.get("tx_exist"):
            checks = sum(v for k, v in pattern_details.items() if isinstance(v, bool))
            if checks >= 4:
                base_score -= 10
            elif checks <= 2:
                base_score += 15

        # If pattern verification was strong (real tx found), adjust score
        if pattern_details.get("tx_exist"):
            checks = sum(v for k, v in pattern_details.items() if isinstance(v, bool))
            if checks >= 4:
                base_score -= 10  # Strong evidence reduces fraud suspicion
            elif checks <= 2:
                base_score += 15  # Weak evidence increases fraud suspicion

        # Per-oracle variance
        variance = random.randint(-8, 8)
        fraud_score = max(0, min(130, base_score + variance))

        if pattern_valid:
            self.stats["patterns_valid"] += 1
        else:
            self.stats["patterns_invalid"] += 1

        return fraud_score, pattern_valid

    # ─── Claim Processing ───

    def process_claim(self, claim_id):
        """Full commit-reveal cycle for a single claim."""
        try:
            assigned = self.insurance.functions.getClaimOracles(claim_id).call()
        except Exception:
            return False

        if self.address not in assigned:
            return False

        log(f"  Oracle #{self.oracle_idx} assigned to claim #{claim_id}")

        # Analyze (includes pattern verification)
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
        log(f"    Claims evaluated:   {self.stats['claims_evaluated']}")
        log(f"    Commits:            {self.stats['commits']}")
        log(f"    Reveals:            {self.stats['reveals']}")
        log(f"    Patterns valid:     {self.stats['patterns_valid']}")
        log(f"    Patterns invalid:   {self.stats['patterns_invalid']}")
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
