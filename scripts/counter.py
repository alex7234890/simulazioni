import re
from collections import defaultdict

def analyze_traders(log_text):
    traders = defaultdict(lambda: {
        "prot": None,
        "total_swaps": 0,
        "attacked_swaps": 0,
        "total_loss": 0.0
    })

    current_trader = None

    trader_pattern = re.compile(
        r"TRADER\s+(0x[a-fA-F0-9]+)\s+\[prot:\s*(.*?)\]"
    )

    swap_pattern = re.compile(
        r"\|\s+att:\s+(SI|NO)(?:\s+perdita:\s+([\d\.]+))?"
    )

    for line in log_text.splitlines():

        # Nuovo trader
        trader_match = trader_pattern.search(line)
        if trader_match:
            current_trader = trader_match.group(1)
            prot = trader_match.group(2)

            traders[current_trader]["prot"] = prot
            continue

        # Swap
        if current_trader:
            swap_match = swap_pattern.search(line)

            if swap_match:
                traders[current_trader]["total_swaps"] += 1

                attacked = swap_match.group(1)
                loss = swap_match.group(2)

                if attacked == "SI":
                    traders[current_trader]["attacked_swaps"] += 1

                    if loss:
                        traders[current_trader]["total_loss"] += float(loss)

    # Output risultati
    global_swaps = 0
    global_attacks = 0
    global_loss = 0.0

    print("\n=== RISULTATI PER TRADER ===\n")

    for trader, data in traders.items():

        total = data["total_swaps"]
        attacked = data["attacked_swaps"]
        loss = data["total_loss"]

        attack_rate = (
            attacked / total * 100
            if total > 0 else 0
        )

        avg_loss = (
            loss / attacked
            if attacked > 0 else 0
        )

        print(f"Trader: {trader}")
        print(f" Protezione: {data['prot']}")
        print(f" Swap totali: {total}")
        print(f" Swap attaccati: {attacked}")
        print(f" Attack rate: {attack_rate:.2f}%")
        print(f" Perdita totale: {loss:.4f}")
        print(f" Perdita media per attacco: {avg_loss:.4f}")
        print()

        global_swaps += total
        global_attacks += attacked
        global_loss += loss

    print("\n=== TOTALI GLOBALI ===\n")

    global_attack_rate = (
        global_attacks / global_swaps * 100
        if global_swaps > 0 else 0
    )

    avg_global_loss = (
        global_loss / global_attacks
        if global_attacks > 0 else 0
    )

    print(f"Swap totali: {global_swaps}")
    print(f"Swap attaccati: {global_attacks}")
    print(f"Attack rate globale: {global_attack_rate:.2f}%")
    print(f"Perdita totale: {global_loss:.4f}")
    print(f"Perdita media per attacco: {avg_global_loss:.4f}")


# ============================
# USO
# ============================

if __name__ == "__main__":

    print("Incolla il log trader, poi CTRL+D (Linux/Mac) o CTRL+Z (Windows):\n")

    import sys
    log_text = sys.stdin.read()

    analyze_traders(log_text)