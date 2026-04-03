#!/usr/bin/env python3
"""
MEV Bot Simulator - Simulates sandwich attacks on the MockAMM.

Monitors trader swaps and executes front-run/back-run pairs to
extract value. Tracks profits and blacklist status.

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/mev_bot.py [--attacks 10]
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

BOT_IDX = 9  # accounts[9]


class MEVBot:
    """Simulates a MEV extraction bot performing sandwich attacks."""

    def __init__(self, w3, contracts, accounts):
        self.w3 = w3
        self.contracts = contracts
        self.accounts = accounts
        self.address = accounts[BOT_IDX]
        self.deployer = accounts[0]
        self.token = contracts["MEVToken"]
        self.usdc = contracts["MockUSDC"]
        self.amm = contracts["MockAMM"]
        self.bot_contract = contracts["SandwichBot"]
        self.insurance = contracts["MEVInsurance"]
        self.stats = {
            "attacks_attempted": 0,
            "attacks_successful": 0,
            "attacks_failed": 0,
            "total_profit": 0,
            "total_cost": 0,
        }

    def setup(self):
        """Fund the bot with tokens for trading."""
        log("=== MEV Bot Setup ===")

        # Fund bot account with MEVI and USDC
        mevi_bal = self.token.functions.balanceOf(self.address).call()
        if mevi_bal < to_wei(10000):
            send_tx(self.w3, self.token.functions.transfer(self.address, to_wei(50000)), self.deployer)
            log("Bot funded with 50k MEVI")

        usdc_bal = self.usdc.functions.balanceOf(self.address).call()
        if usdc_bal < to_wei(10000):
            send_tx(self.w3, self.usdc.functions.transfer(self.address, to_wei(50000)), self.deployer)
            log("Bot funded with 50k USDC")

        # Fund the SandwichBot contract with tokens for on-chain execution
        bot_addr = self.bot_contract.address
        bot_mevi = self.token.functions.balanceOf(bot_addr).call()
        if bot_mevi < to_wei(5000):
            send_tx(self.w3, self.token.functions.transfer(bot_addr, to_wei(10000)), self.deployer)
            send_tx(self.w3, self.usdc.functions.transfer(bot_addr, to_wei(10000)), self.deployer)
            log("SandwichBot contract funded with 10k MEVI + 10k USDC")

        self.check_balance()

    def check_balance(self):
        """Display bot balances."""
        mevi = self.token.functions.balanceOf(self.address).call()
        usdc = self.usdc.functions.balanceOf(self.address).call()
        eth = self.w3.eth.get_balance(self.address)
        log(f"  Bot balances: {from_wei(mevi):.2f} MEVI, {from_wei(usdc):.2f} USDC, {from_wei(eth):.4f} ETH")

    def check_blacklist_status(self):
        """Check if bot is blacklisted in insurance contract."""
        try:
            is_blacklisted = self.insurance.functions.botBlacklisted(self.address).call()
            attack_count = self.insurance.functions.botAttackCount(self.address).call()
            total_damage = self.insurance.functions.botTotalDamage(self.address).call()
            log(f"  Blacklist status: {'BLACKLISTED' if is_blacklisted else 'Clean'}")
            log(f"  Attack count: {attack_count}, Total damage: {from_wei(total_damage):.2f} MEVI")
            return is_blacklisted
        except Exception:
            log("  Could not check blacklist status")
            return False

    def execute_sandwich(self, victim_swap_value=None):
        """
        Execute a sandwich attack: frontrun -> (victim swap) -> backrun.
        Uses the SandwichBot contract for on-chain execution.
        """
        if victim_swap_value is None:
            victim_swap_value = random.randint(50, 300)

        # Front-run amount: 10-30% of victim swap
        frontrun_pct = random.uniform(0.10, 0.30)
        frontrun_amount = int(victim_swap_value * frontrun_pct)
        if frontrun_amount < 1:
            frontrun_amount = 1
        frontrun_wei = to_wei(frontrun_amount)

        self.stats["attacks_attempted"] += 1
        log(f"  Sandwich attack: victim={victim_swap_value} MEVI, frontrun={frontrun_amount} MEVI")

        amm_addr = self.amm.address
        token_addr = self.token.address

        # Execute frontrun via SandwichBot contract
        try:
            send_tx(self.w3, self.bot_contract.functions.executeFrontrun(
                amm_addr, token_addr, frontrun_wei
            ), self.address)
            log(f"  Frontrun executed: {frontrun_amount} MEVI")
        except Exception as e:
            log(f"  Frontrun failed: {e}")
            self.stats["attacks_failed"] += 1
            return False

        # Execute backrun
        backrun_wei = frontrun_wei
        usdc_addr = self.usdc.address
        try:
            send_tx(self.w3, self.bot_contract.functions.executeBackrun(
                amm_addr, usdc_addr, backrun_wei
            ), self.address)
            log(f"  Backrun executed: {frontrun_amount} USDC")
        except Exception as e:
            log(f"  Backrun failed: {e}")
            self.stats["attacks_failed"] += 1
            return False

        # Calculate profit (simplified)
        profit = int(victim_swap_value * random.uniform(0.01, 0.05))
        self.stats["attacks_successful"] += 1
        self.stats["total_profit"] += to_wei(profit)
        self.stats["total_cost"] += to_wei(frontrun_amount * 2)
        log(f"  Sandwich profit: ~{profit} MEVI")
        return True

    def execute_direct_swap_attack(self, swap_value=None):
        """
        Simplified attack: directly swap on AMM to manipulate price.
        """
        if swap_value is None:
            swap_value = random.randint(20, 200)

        swap_wei = to_wei(swap_value)
        self.stats["attacks_attempted"] += 1

        try:
            send_tx(self.w3, self.token.functions.approve(self.amm.address, swap_wei), self.address)
            send_tx(self.w3, self.amm.functions.swap(self.token.address, swap_wei), self.address)
            self.stats["attacks_successful"] += 1
            profit = int(swap_value * random.uniform(0.01, 0.03))
            self.stats["total_profit"] += to_wei(profit)
            log(f"  Direct swap: {swap_value} MEVI -> AMM (profit ~{profit} MEVI)")
            return True
        except Exception as e:
            log(f"  Direct swap failed: {e}")
            self.stats["attacks_failed"] += 1
            return False

    def print_stats(self):
        """Print bot performance stats."""
        log("\n" + "=" * 50)
        log("       MEV BOT SESSION REPORT")
        log("=" * 50)
        log(f"  Attacks attempted:  {self.stats['attacks_attempted']}")
        log(f"  Successful:         {self.stats['attacks_successful']}")
        log(f"  Failed:             {self.stats['attacks_failed']}")
        log(f"  Total profit:       {from_wei(self.stats['total_profit']):.2f} MEVI")
        self.check_balance()
        self.check_blacklist_status()
        log("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="MEV Bot Simulator")
    parser.add_argument("--attacks", type=int, default=10, help="Number of attack cycles")
    parser.add_argument("--mode", choices=["sandwich", "direct", "mixed"], default="mixed",
                        help="Attack mode")
    args = parser.parse_args()

    log("MEV Bot Simulator")
    log("=" * 50)

    w3 = get_web3()
    contracts = get_all_contracts(w3)
    accounts = get_accounts(w3)

    bot = MEVBot(w3, contracts, accounts)
    bot.setup()

    log(f"\nRunning {args.attacks} attack cycles (mode: {args.mode})...\n")

    for i in range(args.attacks):
        log(f"--- Attack {i+1}/{args.attacks} ---")

        if args.mode == "sandwich":
            bot.execute_sandwich()
        elif args.mode == "direct":
            bot.execute_direct_swap_attack()
        else:
            if random.random() < 0.60:
                bot.execute_sandwich()
            else:
                bot.execute_direct_swap_attack()
        print()

    bot.print_stats()
    log("\nMEV Bot session complete.")


if __name__ == "__main__":
    main()
