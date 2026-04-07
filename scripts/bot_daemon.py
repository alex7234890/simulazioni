"""
bot_daemon.py — Background MEV bot that watches the chain and attacks swaps.

Run in a separate terminal while user_cli.py is active:
    python scripts/bot_daemon.py

Options:
    --rate   <float>   Attack probability per swap (default: 0.20)
    --poll   <float>   Polling interval in seconds (default: 2.0)
    --once             Run one attack cycle and exit

The bot watches for new insured swaps and probabilistically executes
sandwich attacks (frontrun + backrun) via the SandwichBot contract.
"""
import sys
import os
import time
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, load_actors,
    send_tx, to_wei, from_wei, log,
)


def _fmt_addr(a): return a[:10] + "…"


class BotDaemon:
    def __init__(self, w3, contracts, bot_addr, attack_rate=0.20):
        self.w3           = w3
        self.contracts    = contracts
        self.bot_addr     = bot_addr
        self.attack_rate  = attack_rate
        self.token        = contracts["MEVToken"]
        self.usdc         = contracts.get("MockUSDC")
        self.amm          = contracts.get("MockAMM")
        self.sandwich     = contracts.get("SandwichBot")
        self.insurance    = contracts["MEVInsurance"]
        self.last_swap_id = -1

        # Stats
        self.attempted  = 0
        self.successful = 0
        self.profit_wei = 0

    def _ensure_funded(self, deployer):
        """Fund bot and SandwichBot contract if low."""
        THRESHOLD = to_wei(5_000)
        TOPUP     = to_wei(20_000)

        bal = self.token.functions.balanceOf(self.bot_addr).call()
        if bal < THRESHOLD:
            try:
                send_tx(self.w3, self.token.functions.transfer(self.bot_addr, TOPUP), deployer)
                log(f"Bot topped up with {from_wei(TOPUP):.0f} MEVI", "BOT")
            except Exception:
                pass

        if self.sandwich:
            sb_bal = self.token.functions.balanceOf(self.sandwich.address).call()
            if sb_bal < THRESHOLD:
                try:
                    send_tx(self.w3, self.token.functions.transfer(self.sandwich.address, TOPUP), deployer)
                    if self.usdc:
                        send_tx(self.w3, self.usdc.functions.transfer(self.sandwich.address, TOPUP), deployer)
                    log(f"SandwichBot contract topped up", "BOT")
                except Exception:
                    pass

    def _get_latest_swap_id(self):
        try:
            return self.insurance.functions.getInsuredSwapsCount().call() - 1
        except Exception:
            return -1

    def _execute_sandwich(self, swap_id):
        """Execute frontrun + backrun on a swap. Returns (success, profit)."""
        self.attempted += 1

        try:
            swap     = self.insurance.functions.insuredSwaps(swap_id).call()
            amount   = swap[1]  # swapValue field (wei)
        except Exception:
            return False, 0

        frontrun_pct    = random.uniform(0.10, 0.25)
        frontrun_wei    = max(1, int(amount * frontrun_pct))
        amm_addr        = self.amm.address
        token_addr      = self.token.address
        usdc_addr       = self.usdc.address if self.usdc else None

        # Frontrun
        try:
            send_tx(self.w3,
                self.sandwich.functions.executeFrontrun(amm_addr, token_addr, frontrun_wei),
                self.bot_addr,
            )
        except Exception as e:
            log(f"Frontrun failed on swap #{swap_id}: {e}", "BOT")
            return False, 0

        # Backrun
        try:
            if usdc_addr:
                send_tx(self.w3,
                    self.sandwich.functions.executeBackrun(amm_addr, usdc_addr, frontrun_wei),
                    self.bot_addr,
                )
        except Exception as e:
            log(f"Backrun failed on swap #{swap_id}: {e}", "BOT")
            return False, 0

        profit = int(from_wei(amount) * random.uniform(0.01, 0.05))
        profit_wei = to_wei(profit)
        self.successful  += 1
        self.profit_wei  += profit_wei

        log(
            f"Sandwich on swap #{swap_id}: "
            f"frontrun={from_wei(frontrun_wei):.1f} MEVI, "
            f"profit~{profit} MEVI (total {from_wei(self.profit_wei):.1f})",
            "BOT",
        )
        return True, profit

    def scan_and_attack(self):
        """Check for new swaps and probabilistically attack."""
        latest = self._get_latest_swap_id()
        if latest <= self.last_swap_id:
            return 0

        attacked = 0
        for swap_id in range(self.last_swap_id + 1, latest + 1):
            if random.random() < self.attack_rate:
                if self.sandwich and self.amm:
                    self._execute_sandwich(swap_id)
                    attacked += 1
                else:
                    log(f"SandwichBot/AMM contract not available — skipping attack", "BOT")

        self.last_swap_id = latest
        return attacked

    def print_stats(self):
        rate = (self.successful / self.attempted * 100) if self.attempted else 0
        log(
            f"Stats: attempted={self.attempted}, "
            f"successful={self.successful} ({rate:.0f}%), "
            f"profit={from_wei(self.profit_wei):.2f} MEVI",
            "BOT",
        )


def main():
    parser = argparse.ArgumentParser(description="MEV Bot Daemon")
    parser.add_argument("--rate",  type=float, default=0.20, help="Attack rate (0-1)")
    parser.add_argument("--poll",  type=float, default=2.0,  help="Poll interval (seconds)")
    parser.add_argument("--once",  action="store_true",       help="Run once and exit")
    args = parser.parse_args()

    log("=" * 50, "BOT")
    log(f"MEV Bot starting | rate={args.rate:.0%} | poll={args.poll}s", "BOT")
    log("=" * 50, "BOT")

    w3        = get_web3()
    contracts = get_all_contracts(w3)
    actors    = load_actors()

    accounts  = w3.eth.accounts
    deployer  = actors.get("deployer", accounts[0])
    bot_addr  = actors.get("bot", accounts[19] if len(accounts) > 19 else accounts[-1])

    log(f"Bot address: {_fmt_addr(bot_addr)}", "BOT")

    daemon = BotDaemon(w3, contracts, bot_addr, attack_rate=args.rate)

    # Initial funding check
    daemon._ensure_funded(deployer)

    if args.once:
        daemon.scan_and_attack()
        daemon.print_stats()
        return

    log("Watching for swaps… (Ctrl+C to stop)", "BOT")
    cycle = 0
    try:
        while True:
            daemon.scan_and_attack()
            cycle += 1
            if cycle % 30 == 0:   # print stats every ~60s
                daemon.print_stats()
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print()
        log("Bot stopped.", "BOT")
        daemon.print_stats()


if __name__ == "__main__":
    main()
