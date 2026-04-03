#!/usr/bin/env python3
"""
Trader Simulator - Standalone trader for MEV Insurance Protocol.

Executes insured swaps, detects MEV extraction, submits claims,
and monitors claim outcomes.

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/trader.py [--swaps 20] [--claim-rate 0.6]
"""
import argparse
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, get_accounts,
    send_tx, to_wei, from_wei, log
)

TRADER_IDX = 1      # accounts[1]
BOT_IDX = 9         # accounts[9]


class Trader:
    """Simulates a trader who buys insurance and executes swaps."""

    def __init__(self, w3, contracts, accounts):
        self.w3 = w3
        self.contracts = contracts
        self.accounts = accounts
        self.address = accounts[TRADER_IDX]
        self.bot_address = accounts[BOT_IDX]
        self.insurance = contracts["MEVInsurance"]
        self.token = contracts["MEVToken"]
        self.deployer = accounts[0]
        self.stats = {
            "swaps": 0,
            "claims_submitted": 0,
            "claims_approved": 0,
            "claims_rejected": 0,
            "total_premium": 0,
            "total_payout": 0,
            "total_loss_claimed": 0,
        }

    def setup(self):
        """Fund trader, register, buy policy."""
        log("=== Trader Setup ===")

        # Fund with MEVI if needed
        bal = self.token.functions.balanceOf(self.address).call()
        if bal < to_wei(10000):
            send_tx(self.w3, self.token.functions.transfer(self.address, to_wei(50000)), self.deployer)
            log(f"Funded trader with 50k MEVI")

        # Register
        is_reg = self.insurance.functions.registeredUsers(self.address).call()
        if not is_reg:
            send_tx(self.w3, self.insurance.functions.registerUser(), self.address)
            log("Trader registered")

        # Buy policy
        policy = self.insurance.functions.policies(self.address).call()
        if not policy[6]:  # active
            send_tx(self.w3, self.token.functions.approve(self.insurance.address, to_wei(100000)), self.address)
            send_tx(self.w3, self.insurance.functions.buyPolicy(2), self.address)  # High coverage
            log("Bought High coverage policy")
        else:
            send_tx(self.w3, self.token.functions.approve(self.insurance.address, to_wei(100000)), self.address)
            log("Policy already active, approval refreshed")

        self.check_balance()

    def execute_swap(self, swap_value=None):
        """Execute an insured swap and return swap_id."""
        if swap_value is None:
            swap_value = random.randint(50, 500)

        swap_value_wei = to_wei(swap_value)

        try:
            send_tx(self.w3, self.insurance.functions.insuredSwap(swap_value_wei), self.address)
            swap_id = self.insurance.functions.getInsuredSwapsCount().call() - 1
            swap_info = self.insurance.functions.insuredSwaps(swap_id).call()
            premium = swap_info[2]
            self.stats["swaps"] += 1
            self.stats["total_premium"] += premium
            log(f"  Swap #{swap_id}: value={swap_value} MEVI, premium={from_wei(premium):.4f} MEVI")
            return swap_id
        except Exception as e:
            log(f"  Swap failed: {e}")
            return None

    def detect_mev(self):
        """Simulate MEV detection. Returns (is_sandwich, loss_pct)."""
        is_sandwich = random.random() < 0.35
        if is_sandwich:
            loss_pct = random.uniform(0.05, 0.20)
        else:
            loss_pct = random.uniform(0.02, 0.08)
        return is_sandwich, loss_pct

    def submit_claim(self, swap_id, loss_wei, is_sandwich):
        """Submit an insurance claim for a swap."""
        tx1 = self.w3.keccak(text=f"frontrun_{swap_id}_{random.randint(0,999999)}")
        tx2 = self.w3.keccak(text=f"victim_{swap_id}_{random.randint(0,999999)}")
        tx3 = self.w3.keccak(text=f"backrun_{swap_id}_{random.randint(0,999999)}")
        bot_addr = self.bot_address if is_sandwich else "0x0000000000000000000000000000000000000000"

        try:
            send_tx(self.w3, self.insurance.functions.submitClaim(
                swap_id, tx1, tx2, tx3, loss_wei, bot_addr
            ), self.address)
            claim_id = self.insurance.functions.getClaimsCount().call() - 1
            self.stats["claims_submitted"] += 1
            self.stats["total_loss_claimed"] += loss_wei
            log(f"  Claim #{claim_id} submitted (loss={from_wei(loss_wei):.2f} MEVI, sandwich={is_sandwich})")
            return claim_id
        except Exception as e:
            log(f"  Claim submission failed: {e}")
            return None

    def check_claim_status(self, claim_id):
        """Check and log the status of a claim."""
        try:
            info = self.insurance.functions.getClaimInfo(claim_id).call()
            status = info[1]
            status_names = {0: "Pending", 1: "OracleReview", 2: "CAPTCHARequired",
                            3: "Approved", 4: "Rejected", 5: "InvalidPattern"}
            status_name = status_names.get(status, f"Unknown({status})")
            log(f"  Claim #{claim_id} status: {status_name}")
            return status
        except Exception as e:
            log(f"  Status check failed: {e}")
            return None

    def check_balance(self):
        """Log current MEVI balance."""
        bal = self.token.functions.balanceOf(self.address).call()
        log(f"  Trader MEVI balance: {from_wei(bal):.2f}")

    def check_profile(self):
        """Log user profile info."""
        try:
            profile = self.insurance.functions.getUserProfile(self.address).call()
            tier_names = {0: "Bronze", 1: "Silver", 2: "Gold", 3: "Platinum"}
            log(f"  Tier: {tier_names.get(profile[0], 'Unknown')}")
            log(f"  Total swaps: {profile[2]}, Total claims: {profile[3]}")
            log(f"  Avg fraud score: {profile[6]}")
        except Exception:
            pass

    def run_swap_cycle(self, claim_rate=0.6):
        """
        Execute one swap cycle: swap -> maybe detect MEV -> maybe claim.
        Returns (swap_id, claim_id or None).
        """
        swap_id = self.execute_swap()
        if swap_id is None:
            return None, None

        # Detect MEV
        is_sandwich, loss_pct = self.detect_mev()
        swap_info = self.insurance.functions.insuredSwaps(swap_id).call()
        swap_value = swap_info[1]  # swapValue
        loss = int(swap_value * loss_pct)
        if loss < to_wei(1):
            loss = to_wei(1)

        # Decide whether to file claim
        should_claim = random.random() < claim_rate
        if not should_claim:
            log(f"  No claim filed for swap #{swap_id}")
            return swap_id, None

        if is_sandwich:
            log(f"  SANDWICH detected! Loss: {from_wei(loss):.2f} MEVI ({loss_pct*100:.1f}%)")
        else:
            log(f"  Possible MEV. Estimated loss: {from_wei(loss):.2f} MEVI ({loss_pct*100:.1f}%)")

        claim_id = self.submit_claim(swap_id, loss, is_sandwich)
        return swap_id, claim_id

    def print_stats(self):
        """Print trader session stats."""
        log("\n" + "=" * 50)
        log("       TRADER SESSION REPORT")
        log("=" * 50)
        log(f"  Swaps executed:      {self.stats['swaps']}")
        log(f"  Claims submitted:    {self.stats['claims_submitted']}")
        log(f"  Total premiums:      {from_wei(self.stats['total_premium']):.4f} MEVI")
        log(f"  Total losses:        {from_wei(self.stats['total_loss_claimed']):.2f} MEVI")
        self.check_balance()
        self.check_profile()
        log("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Trader Simulator")
    parser.add_argument("--swaps", type=int, default=20, help="Number of swap cycles")
    parser.add_argument("--claim-rate", type=float, default=0.6, help="Probability of filing a claim (0-1)")
    args = parser.parse_args()

    log("Trader Simulator")
    log("=" * 50)

    w3 = get_web3()
    contracts = get_all_contracts(w3)
    accounts = get_accounts(w3)

    trader = Trader(w3, contracts, accounts)
    trader.setup()

    log(f"\nRunning {args.swaps} swap cycles (claim rate: {args.claim_rate*100:.0f}%)...\n")

    for i in range(args.swaps):
        log(f"--- Cycle {i+1}/{args.swaps} ---")
        swap_id, claim_id = trader.run_swap_cycle(args.claim_rate)
        if claim_id is not None:
            log(f"  (Claim #{claim_id} awaiting oracle evaluation)")
        print()

    trader.print_stats()
    log("\nTrader session complete. Claims are pending oracle review.")
    log("Run oracle.py to process pending claims.")


if __name__ == "__main__":
    main()
