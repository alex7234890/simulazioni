"""
advance_day.py — Advance blockchain time by one day.

Usage:
    python scripts/advance_day.py          # advance 1 day
    python scripts/advance_day.py 3        # advance 3 days
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import get_web3, advance_day, get_all_contracts, from_wei, log


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    w3 = get_web3()
    advance_day(w3, days)

    # Show new block info
    block = w3.eth.get_block("latest")
    log(f"Block #{block['number']} | timestamp={block['timestamp']}", "SYSTEM")

    # Optionally show pool balance if contracts are deployed
    try:
        contracts = get_all_contracts(w3)
        insurance = contracts.get("MEVInsurance")
        token     = contracts.get("MEVToken")
        if insurance and token:
            bal = token.functions.balanceOf(insurance.address).call()
            log(f"Pool balance: {from_wei(bal):.2f} MEVI", "POOL")
    except Exception:
        pass

    label = "day" if days == 1 else "days"
    log(f"Advanced {days} {label}. Run 'python scripts/status.py' to see current state.", "SYSTEM")


if __name__ == "__main__":
    main()
