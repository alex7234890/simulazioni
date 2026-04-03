"""
Bot Actor - Simulates a MEV extraction bot.

Performs sandwich attacks (frontrun + backrun) on the MockAMM
when instructed by the orchestrator. Tracks profits and
blacklist status.
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import send_tx, to_wei, from_wei, log


class BotActor:
    """
    Simulates a MEV bot that performs sandwich attacks.

    The bot doesn't act autonomously — the orchestrator (launch.py)
    decides when to trigger an attack based on attack_rate probability.
    """

    def __init__(self, w3, contracts, address, bot_id,
                 network="localhost", private_key=None):
        self.w3 = w3
        self.contracts = contracts
        self.address = address
        self.bot_id = bot_id              # e.g. "Bot-1"
        self.network = network
        self.private_key = private_key
        self.token = contracts["MEVToken"]
        self.usdc = contracts["MockUSDC"]
        self.amm = contracts["MockAMM"]
        self.bot_contract = contracts["SandwichBot"]
        self.insurance = contracts["MEVInsurance"]

        # Stats
        self.stats = {
            "attacks_attempted": 0,
            "attacks_successful": 0,
            "attacks_failed": 0,
            "total_profit": 0,
        }

    def _send(self, fn, value=0):
        """Send transaction from this bot."""
        return send_tx(
            self.w3, fn, self.address,
            value=value, network=self.network, private_key=self.private_key,
        )

    # ── Setup ──

    def setup(self, deployer, deployer_key=None):
        """Fund the bot with MEVI and USDC for trading."""
        mevi_bal = self.token.functions.balanceOf(self.address).call()
        if mevi_bal < to_wei(10000):
            send_tx(
                self.w3, self.token.functions.transfer(self.address, to_wei(50000)),
                deployer, network=self.network, private_key=deployer_key,
            )
            log(f"{self.bot_id} funded with 50,000 MEVI", "SETUP")

        usdc_bal = self.usdc.functions.balanceOf(self.address).call()
        if usdc_bal < to_wei(10000):
            send_tx(
                self.w3, self.usdc.functions.transfer(self.address, to_wei(50000)),
                deployer, network=self.network, private_key=deployer_key,
            )
            log(f"{self.bot_id} funded with 50,000 USDC", "SETUP")

        # Fund the SandwichBot contract too
        bot_addr = self.bot_contract.address
        bot_mevi = self.token.functions.balanceOf(bot_addr).call()
        if bot_mevi < to_wei(5000):
            send_tx(
                self.w3, self.token.functions.transfer(bot_addr, to_wei(10000)),
                deployer, network=self.network, private_key=deployer_key,
            )
            send_tx(
                self.w3, self.usdc.functions.transfer(bot_addr, to_wei(10000)),
                deployer, network=self.network, private_key=deployer_key,
            )
            log(f"{self.bot_id} SandwichBot contract funded (10k MEVI + 10k USDC)", "SETUP")

        log(f"{self.bot_id} setup complete", "SETUP")

    # ── Attack Logic ──

    def should_attack(self, swap_value, attack_rate=0.15):
        """Decide probabilistically whether to attack this swap."""
        return random.random() < attack_rate

    def execute_sandwich(self, swap_value):
        """
        Execute a sandwich attack: frontrun + backrun on the AMM.
        Returns (success, estimated_profit).
        """
        self.stats["attacks_attempted"] += 1

        # Frontrun: 10-25% of victim swap value
        frontrun_pct = random.uniform(0.10, 0.25)
        frontrun_amount = max(1, int(swap_value * frontrun_pct))
        frontrun_wei = to_wei(frontrun_amount)

        amm_addr = self.amm.address
        token_addr = self.token.address
        usdc_addr = self.usdc.address

        # Frontrun
        try:
            self._send(self.bot_contract.functions.executeFrontrun(
                amm_addr, token_addr, frontrun_wei,
            ))
        except Exception as e:
            log(f"{self.bot_id} | Frontrun failed: {e}", "ERROR")
            self.stats["attacks_failed"] += 1
            return False, 0

        # Backrun
        try:
            self._send(self.bot_contract.functions.executeBackrun(
                amm_addr, usdc_addr, frontrun_wei,
            ))
        except Exception as e:
            log(f"{self.bot_id} | Backrun failed: {e}", "ERROR")
            self.stats["attacks_failed"] += 1
            return False, 0

        # Estimate profit (simplified: 1-5% of victim swap)
        profit = int(swap_value * random.uniform(0.01, 0.05))
        self.stats["attacks_successful"] += 1
        self.stats["total_profit"] += to_wei(profit)

        log(
            f"{self.bot_id} | Sandwich on {swap_value} MEVI: "
            f"frontrun={frontrun_amount} MEVI, profit~{profit} MEVI",
            "ATTACK",
        )
        return True, profit

    # ── Info ──

    def get_stats(self):
        """Get bot stats including on-chain blacklist status."""
        try:
            is_blacklisted = self.insurance.functions.botBlacklisted(self.address).call()
            attack_count = self.insurance.functions.botAttackCount(self.address).call()
            total_damage = self.insurance.functions.botTotalDamage(self.address).call()
        except Exception:
            is_blacklisted = False
            attack_count = 0
            total_damage = 0

        return {
            "attacks_attempted": self.stats["attacks_attempted"],
            "attacks_successful": self.stats["attacks_successful"],
            "attacks_failed": self.stats["attacks_failed"],
            "total_profit": self.stats["total_profit"],
            "is_blacklisted": is_blacklisted,
            "onchain_attack_count": attack_count,
            "onchain_total_damage": total_damage,
        }
