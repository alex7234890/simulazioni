#!/usr/bin/env python3
"""
MEV Bot Simulator - Simulates sandwich attacks on the MockAMM.
"""

import argparse
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    get_web3,
    get_all_contracts,
    get_accounts,
    send_tx,
    to_wei,
    from_wei,
    log
)

BOT_IDX = 9  # accounts[9]


class MEVBot:

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

        log("=== MEV Bot Setup ===")

        mevi_bal = self.token.functions.balanceOf(
            self.address
        ).call()

        if mevi_bal < to_wei(10000):

            send_tx(
                self.w3,
                self.token.functions.transfer(
                    self.address,
                    to_wei(50000)
                ),
                self.deployer
            )

            log("Bot funded with 50k MEVI")

        usdc_bal = self.usdc.functions.balanceOf(
            self.address
        ).call()

        if usdc_bal < to_wei(10000):

            send_tx(
                self.w3,
                self.usdc.functions.transfer(
                    self.address,
                    to_wei(50000)
                ),
                self.deployer
            )

            log("Bot funded with 50k USDC")

        bot_addr = self.bot_contract.address

        bot_mevi = self.token.functions.balanceOf(
            bot_addr
        ).call()

        if bot_mevi < to_wei(5000):

            send_tx(
                self.w3,
                self.token.functions.transfer(
                    bot_addr,
                    to_wei(10000)
                ),
                self.deployer
            )

            send_tx(
                self.w3,
                self.usdc.functions.transfer(
                    bot_addr,
                    to_wei(10000)
                ),
                self.deployer
            )

            log("SandwichBot contract funded")

        self.check_balance()

    def check_balance(self):

        mevi = self.token.functions.balanceOf(
            self.address
        ).call()

        usdc = self.usdc.functions.balanceOf(
            self.address
        ).call()

        eth = self.w3.eth.get_balance(
            self.address
        )

        log(
            f"  Bot balances: "
            f"{from_wei(mevi):.2f} MEVI, "
            f"{from_wei(usdc):.2f} USDC, "
            f"{from_wei(eth):.4f} ETH"
        )

    def check_blacklist_status(self):

        try:

            is_blacklisted = \
                self.insurance.functions.botBlacklisted(
                    self.address
                ).call()

            attack_count = \
                self.insurance.functions.botAttackCount(
                    self.address
                ).call()

            total_damage = \
                self.insurance.functions.botTotalDamage(
                    self.address
                ).call()

            log(
                f"  Blacklist: "
                f"{'BLACKLISTED' if is_blacklisted else 'Clean'}"
            )

            log(
                f"  Attacks: {attack_count}, "
                f"Damage: {from_wei(total_damage):.2f}"
            )

            return is_blacklisted

        except Exception:

            log("  Could not check blacklist")

            return False

    def execute_sandwich(
        self,
        victim_swap_value=None
    ):

        if victim_swap_value is None:

            victim_swap_value = \
                random.randint(50, 300)

        frontrun_pct = \
            random.uniform(0.10, 0.30)

        frontrun_amount = int(
            victim_swap_value
            * frontrun_pct
        )

        if frontrun_amount < 1:
            frontrun_amount = 1

        frontrun_wei = to_wei(
            frontrun_amount
        )

        self.stats["attacks_attempted"] += 1

        log(
            f"  Sandwich attack: "
            f"victim={victim_swap_value}"
        )

        amm_addr = self.amm.address
        token_addr = self.token.address

        try:

            send_tx(
                self.w3,
                self.bot_contract.functions.executeFrontrun(
                    amm_addr,
                    token_addr,
                    frontrun_wei
                ),
                self.address
            )

            log("  Frontrun executed")

        except Exception as e:

            log(f"  Frontrun failed: {e}")

            self.stats["attacks_failed"] += 1

            return False

        # SINGLE backrun definition (fixed)

        backrun_wei = frontrun_wei

        usdc_addr = self.usdc.address

        try:

            send_tx(
                self.w3,
                self.bot_contract.functions.executeBackrun(
                    amm_addr,
                    usdc_addr,
                    backrun_wei
                ),
                self.address
            )

            log("  Backrun executed")

        except Exception as e:

            log(f"  Backrun failed: {e}")

            self.stats["attacks_failed"] += 1

            return False

        profit = int(
            victim_swap_value
            * random.uniform(0.01, 0.05)
        )

        self.stats["attacks_successful"] += 1

        self.stats["total_profit"] += \
            to_wei(profit)

        # FIXED — single cost

        self.stats["total_cost"] += \
            to_wei(frontrun_amount * 2)

        log(f"  Profit ~{profit} MEVI")

        return True

    def execute_direct_swap_attack(
        self,
        swap_value=None
    ):

        if swap_value is None:

            swap_value = \
                random.randint(20, 200)

        swap_wei = to_wei(
            swap_value
        )

        self.stats["attacks_attempted"] += 1

        try:

            send_tx(
                self.w3,
                self.token.functions.approve(
                    self.amm.address,
                    swap_wei
                ),
                self.address
            )

            send_tx(
                self.w3,
                self.amm.functions.swap(
                    self.token.address,
                    swap_wei
                ),
                self.address
            )

            self.stats["attacks_successful"] += 1

            profit = int(
                swap_value
                * random.uniform(0.01, 0.03)
            )

            self.stats["total_profit"] += \
                to_wei(profit)

            log(
                f"  Direct swap {swap_value}"
            )

            return True

        except Exception as e:

            log(f"  Direct swap failed: {e}")

            self.stats["attacks_failed"] += 1

            return False

    def print_stats(self):

        log("\n" + "=" * 50)

        log("MEV BOT REPORT")

        log("=" * 50)

        log(
            f"Attempts: "
            f"{self.stats['attacks_attempted']}"
        )

        log(
            f"Success: "
            f"{self.stats['attacks_successful']}"
        )

        log(
            f"Failed: "
            f"{self.stats['attacks_failed']}"
        )

        log(
            f"Profit: "
            f"{from_wei(self.stats['total_profit']):.2f}"
        )

        self.check_balance()

        self.check_blacklist_status()

        log("=" * 50)


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--attacks",
        type=int,
        default=10
    )

    parser.add_argument(
        "--mode",
        choices=["sandwich", "direct", "mixed"],
        default="mixed"
    )

    args = parser.parse_args()

    log("MEV Bot Simulator")

    w3 = get_web3()

    contracts = get_all_contracts(w3)

    accounts = get_accounts(w3)

    bot = MEVBot(
        w3,
        contracts,
        accounts
    )

    bot.setup()

    for i in range(args.attacks):

        log(f"\n--- Attack {i+1} ---")

        if args.mode == "sandwich":

            bot.execute_sandwich()

        elif args.mode == "direct":

            bot.execute_direct_swap_attack()

        else:

            if random.random() < 0.60:
                bot.execute_sandwich()
            else:
                bot.execute_direct_swap_attack()

    bot.print_stats()


if __name__ == "__main__":
    main()
