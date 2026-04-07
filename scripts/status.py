"""
status.py — Live dashboard for MEV Insurance pool state.

Usage:
    python scripts/status.py          # show once
    python scripts/status.py --watch  # refresh every 5s
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import get_web3, get_all_contracts, load_actors, from_wei, log

# ── Box Drawing ──

def _top(w):    return "╔" + "═" * w + "╗"
def _bot(w):    return "╚" + "═" * w + "╝"
def _mid(w):    return "╠" + "═" * w + "╣"
def _row(s, w): return "║ " + s.ljust(w - 2) + " ║"

def _kv(label, value, lw=38, vw=12):
    return f"{label:<{lw}s} {value:>{vw}s}"

def _sep(w): return "─" * (w - 2)


# ── Sections ──

def print_pool(contracts, w=62):
    insurance = contracts["MEVInsurance"]
    token     = contracts["MEVToken"]

    pool_bal     = token.functions.balanceOf(insurance.address).call()
    total_swaps  = insurance.functions.getInsuredSwapsCount().call()
    total_claims = insurance.functions.getClaimsCount().call()

    try:
        calc  = contracts["PremiumCalculator"]
        patt  = calc.functions.patt().call()
        t_app = insurance.functions.thetaApprove().call()
        t_rej = insurance.functions.thetaReject().call()
    except Exception:
        patt, t_app, t_rej = 0, 60, 80

    rows = [
        _kv("Pool MEVI balance:", f"{from_wei(pool_bal):,.2f}"),
        _sep(w),
        _kv("Total insured swaps:", str(total_swaps)),
        _kv("Total claims filed:", str(total_claims)),
        _sep(w),
        _kv("Patt (attack prob, bps):", str(patt)),
        _kv("θ_approve / θ_reject:", f"{t_app} / {t_rej}"),
    ]

    print(_top(w))
    print(_row("POOL STATUS".center(w - 2), w))
    print(_mid(w))
    for r in rows:
        print(_row(r, w))
    print(_bot(w))
    print()


def print_user(contracts, user_addr, w=62):
    insurance = contracts["MEVInsurance"]
    token     = contracts["MEVToken"]

    try:
        profile = insurance.functions.getUserProfile(user_addr).call()
        balance = token.functions.balanceOf(user_addr).call()
        policy  = insurance.functions.policies(user_addr).call()

        tier_names = {0: "Bronze", 1: "Silver", 2: "Gold", 3: "Platinum"}
        tier   = tier_names.get(profile[0], "?")
        active = "YES" if policy[6] else "NO (run RINNOVA)"

        rows = [
            _kv("Address:", user_addr[:20] + "…"),
            _kv("Balance:", f"{from_wei(balance):,.2f} MEVI"),
            _kv("Tier:", tier),
            _kv("Policy active:", active),
            _sep(w),
            _kv("Total swaps:", str(profile[2])),
            _kv("Total claims:", str(profile[3])),
            _kv("  Approved:", str(profile[4])),
            _kv("  Rejected:", str(profile[5])),
            _kv("Avg fraud score:", str(profile[6])),
            _kv("Blacklisted:", "YES" if profile[7] else "No"),
        ]
    except Exception as e:
        rows = [_kv("Error reading profile:", str(e)[:40])]

    print(_top(w))
    print(_row("YOUR PROFILE".center(w - 2), w))
    print(_mid(w))
    for r in rows:
        print(_row(r, w))
    print(_bot(w))
    print()


def print_oracles(contracts, oracle_addrs, w=74):
    registry = contracts["OracleRegistry"]

    STATUS = {0: "Inactive", 1: "Active", 2: "Suspended",
              3: "Watchlist", 4: "Withdrawing", 5: "Withdrawn", 6: "Expelled"}

    print(_top(w))
    print(_row("ORACLE STATUS".center(w - 2), w))
    print(_mid(w))

    header = f"{'Oracle':<10s} {'Address':<16s} {'Status':<12s} {'Stake':>10s} {'DevScore':>10s}"
    print(_row(header, w))
    print(_row(_sep(w), w))

    for i, addr in enumerate(oracle_addrs):
        try:
            info   = registry.functions.oracles(addr).call()
            stake  = info[2]
            status = STATUS.get(info[1], f"?({info[1]})")
            dev    = info[5] if len(info) > 5 else 0
        except Exception:
            stake, status, dev = 0, "Error", 0

        row = (f"{'Oracle-'+str(i+1):<10s} {addr[:14]+'…':<16s} "
               f"{status:<12s} {from_wei(stake):>8.2f}   {dev:>10d}")
        print(_row(row, w))

    print(_bot(w))
    print()


def print_recent_claims(contracts, n=5, w=78):
    insurance = contracts["MEVInsurance"]

    count = insurance.functions.getClaimsCount().call()
    if count == 0:
        return

    VERDICT = {0: "Pending", 1: "Approved", 2: "Rejected", 3: "CAPTCHA", 4: "Invalid"}

    print(_top(w))
    print(_row(f"RECENT CLAIMS (last {min(n, count)})".center(w - 2), w))
    print(_mid(w))

    header = f"{'ID':>4s} {'Claimant':<16s} {'Loss':>10s} {'Payout':>10s} {'Verdict':<10s}"
    print(_row(header, w))
    print(_row(_sep(w), w))

    start = max(0, count - n)
    for cid in range(start, count):
        try:
            c = insurance.functions.claims(cid).call()
            # fields: trader, swapId, lossClaimed, payout, verdict, ...
            claimant = c[0][:14] + "…"
            loss     = from_wei(c[2])
            payout   = from_wei(c[3])
            verdict  = VERDICT.get(c[4], f"?({c[4]})")
            row = f"{cid:>4d} {claimant:<16s} {loss:>10.2f} {payout:>10.2f} {verdict:<10s}"
            print(_row(row, w))
        except Exception:
            print(_row(f"{cid:>4d} (error reading claim)", w))

    print(_bot(w))
    print()


def show_dashboard(w3, contracts, actors):
    user_addr    = actors.get("user", w3.eth.accounts[1] if len(w3.eth.accounts) > 1 else None)
    oracle_addrs = actors.get("oracles", [])

    block = w3.eth.get_block("latest")
    print(f"\n{'═'*66}")
    print(f"  MEV Insurance Dashboard  |  Block #{block['number']}  |  ts={block['timestamp']}")
    print(f"{'═'*66}\n")

    print_pool(contracts)

    if user_addr:
        print_user(contracts, user_addr)

    if oracle_addrs:
        print_oracles(contracts, oracle_addrs)

    print_recent_claims(contracts)


def main():
    watch = "--watch" in sys.argv
    interval = 5

    w3        = get_web3()
    contracts = get_all_contracts(w3)
    actors    = load_actors()

    if watch:
        log(f"Watch mode — refreshing every {interval}s. Ctrl+C to stop.", "SYSTEM")
        try:
            while True:
                os.system("clear")
                show_dashboard(w3, contracts, actors)
                time.sleep(interval)
        except KeyboardInterrupt:
            log("Stopped.", "SYSTEM")
    else:
        show_dashboard(w3, contracts, actors)


if __name__ == "__main__":
    main()
