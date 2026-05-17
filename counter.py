import re
from collections import defaultdict


def analyze_traders(file_path):

    traders = defaultdict(lambda: {
        "total_swaps": 0,
        "total_usdc": 0.0
    })

    current_trader = None

    # Match intestazione trader
    trader_pattern = re.compile(
        r"TRADER\s+(0x[a-fA-F0-9]+)"
    )

    # Match quantità USDC
    swap_pattern = re.compile(
        r"([\d\.]+)\s+USDC\s+→"
    )

    # Legge il file
    with open(file_path, "r", encoding="utf-8") as f:

        for line in f:

            # Nuovo trader
            trader_match = trader_pattern.search(line)

            if trader_match:
                current_trader = trader_match.group(1)
                continue

            # Swap
            if current_trader:

                swap_match = swap_pattern.search(line)

                if swap_match:

                    usdc_amount = float(swap_match.group(1))

                    traders[current_trader]["total_swaps"] += 1
                    traders[current_trader]["total_usdc"] += usdc_amount

    # Output risultati
    print("\n=== TOTALI PER TRADER ===\n")

    for trader, data in traders.items():

        print(f"Trader: {trader}")
        print(f"Swap eseguiti: {data['total_swaps']}")
        print(f"Totale swappato: {data['total_usdc']:.2f} USDC")
        print()


# ============================
# AVVIO
# ============================

if __name__ == "__main__":

    file_path = input("Percorso file txt: ").strip()

    analyze_traders(file_path)