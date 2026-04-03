"""
Dashboard - Formatted terminal output for MEV Insurance simulation.

Uses box-drawing characters for clean display of pool status,
user tables, oracle tables, and daily/final reports.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import from_wei, log


# ── Box Drawing Helpers ──

def _box_top(width):
    return "╔" + "═" * width + "╗"

def _box_bottom(width):
    return "╚" + "═" * width + "╝"

def _box_mid(width):
    return "╠" + "═" * width + "╣"

def _box_row(text, width):
    return "║ " + text.ljust(width - 2) + " ║"

def _box_row_pair(label, value, width):
    content = f"{label:<30s} {value}"
    return _box_row(content, width)


def _print_box(title, rows, width=60):
    """Print a box with title and rows."""
    print(_box_top(width))
    print(_box_row(title.center(width - 2), width))
    print(_box_mid(width))
    for row in rows:
        print(_box_row(row, width))
    print(_box_bottom(width))
    print()


# ── Daily Summary ──

def print_daily_summary(day, day_stats):
    """
    Print end-of-day summary.
    day_stats: dict with keys swaps, claims, approved, rejected, captcha,
               invalid_pattern, premium_in, payout_out, pool_balance, secondary_reviews
    """
    width = 62
    title = f"DAY {day} SUMMARY"
    rows = [
        f"{'Swaps executed:':<35s} {day_stats.get('swaps', 0):>6d}",
        f"{'Claims submitted:':<35s} {day_stats.get('claims', 0):>6d}",
        f"{'  Approved (incl. CAPTCHA):':<35s} {day_stats.get('approved', 0):>6d}",
        f"{'  Rejected:':<35s} {day_stats.get('rejected', 0):>6d}",
        f"{'  CAPTCHA required:':<35s} {day_stats.get('captcha', 0):>6d}",
        f"{'  Invalid pattern:':<35s} {day_stats.get('invalid_pattern', 0):>6d}",
        f"{'  Secondary reviews:':<35s} {day_stats.get('secondary_reviews', 0):>6d}",
        f"{'─' * (width - 2)}",
        f"{'Premiums collected:':<35s} {from_wei(day_stats.get('premium_in', 0)):>10.4f} MEVI",
        f"{'Payouts issued:':<35s} {from_wei(day_stats.get('payout_out', 0)):>10.4f} MEVI",
        f"{'Daily P&L:':<35s} {from_wei(day_stats.get('premium_in', 0) - day_stats.get('payout_out', 0)):>+10.4f} MEVI",
        f"{'─' * (width - 2)}",
        f"{'Pool balance:':<35s} {from_wei(day_stats.get('pool_balance', 0)):>10.2f} MEVI",
    ]
    _print_box(title, rows, width)


# ── Pool Status ──

def print_pool_status(w3, contracts, cumulative_stats):
    """
    Print detailed pool status dashboard.
    cumulative_stats: dict with total_premium, total_payout, total_claims, etc.
    """
    insurance = contracts["MEVInsurance"]
    token = contracts["MEVToken"]

    pool_balance = token.functions.balanceOf(insurance.address).call()
    total_claims = insurance.functions.getClaimsCount().call()
    total_swaps = insurance.functions.getInsuredSwapsCount().call()

    # Read protocol parameters
    try:
        patt = contracts["PremiumCalculator"].functions.patt().call()
        theta_approve = insurance.functions.thetaApprove().call()
        theta_reject = insurance.functions.thetaReject().call()
    except Exception:
        patt = 0
        theta_approve = 60
        theta_reject = 80

    total_prem = cumulative_stats.get("total_premium", 0)
    total_pay = cumulative_stats.get("total_payout", 0)
    net = total_prem - total_pay

    width = 62
    title = "POOL STATUS"
    rows = [
        f"{'Pool MEVI balance:':<35s} {from_wei(pool_balance):>14.2f}",
        f"{'Total premiums collected:':<35s} {from_wei(total_prem):>14.4f}",
        f"{'Total payouts issued:':<35s} {from_wei(total_pay):>14.4f}",
        f"{'Net P&L:':<35s} {from_wei(net):>+14.4f}",
        f"{'─' * (width - 2)}",
        f"{'Total insured swaps:':<35s} {total_swaps:>14d}",
        f"{'Total claims filed:':<35s} {total_claims:>14d}",
        f"{'─' * (width - 2)}",
        f"{'Patt (attack probability):':<35s} {patt:>14d} bps",
        f"{'θ_approve:':<35s} {theta_approve:>14d}",
        f"{'θ_reject:':<35s} {theta_reject:>14d}",
    ]
    _print_box(title, rows, width)


# ── User Table ──

def print_user_table(traders):
    """
    Print a table of all traders with their profile info.
    traders: list of TraderActor instances.
    """
    width = 90
    print(_box_top(width))
    print(_box_row("TRADER OVERVIEW".center(width - 2), width))
    print(_box_mid(width))

    header = (
        f"{'ID':<12s} {'Tier':<9s} {'Swaps':>6s} {'Claims':>7s} "
        f"{'Approv':>7s} {'Reject':>7s} {'AvgFS':>6s} {'Balance':>14s} {'BL':>3s}"
    )
    print(_box_row(header, width))
    print(_box_row("─" * (width - 2), width))

    for t in traders:
        p = t.get_profile()
        row = (
            f"{t.trader_id:<12s} {p['tier']:<9s} {p['total_swaps']:>6d} {p['total_claims']:>7d} "
            f"{p['approved_claims']:>7d} {p['rejected_claims']:>7d} {p['avg_fraud_score']:>6d} "
            f"{from_wei(p['balance']):>11.2f}   {'Y' if p['is_blacklisted'] else 'N':>3s}"
        )
        print(_box_row(row, width))

    print(_box_bottom(width))
    print()


# ── Oracle Table ──

def print_oracle_table(oracles):
    """
    Print a table of all oracles with their status.
    oracles: list of OracleActor instances.
    """
    status_names = {0: "Inactive", 1: "Active", 2: "Suspended", 3: "Watchlist",
                    4: "Withdrawing", 5: "Withdrawn", 6: "Expelled"}

    width = 80
    print(_box_top(width))
    print(_box_row("ORACLE OVERVIEW".center(width - 2), width))
    print(_box_mid(width))

    header = (
        f"{'ID':<12s} {'Status':<12s} {'Stake':>10s} "
        f"{'Evaluated':>10s} {'DevScore':>10s}"
    )
    print(_box_row(header, width))
    print(_box_row("─" * (width - 2), width))

    for o in oracles:
        info = o.get_info()
        status_str = status_names.get(info["status"], f"?({info['status']})")
        row = (
            f"{o.oracle_id:<12s} {status_str:<12s} {from_wei(info['stake']):>8.4f}  "
            f"{o.stats['claims_evaluated']:>10d} {info['deviation_score']:>10d}"
        )
        print(_box_row(row, width))

    print(_box_bottom(width))
    print()


# ── Bot Table ──

def print_bot_table(bots):
    """
    Print a table of all bots with attack stats.
    bots: list of BotActor instances.
    """
    width = 80
    print(_box_top(width))
    print(_box_row("MEV BOT OVERVIEW".center(width - 2), width))
    print(_box_mid(width))

    header = (
        f"{'ID':<10s} {'Attempted':>10s} {'Success':>10s} {'Failed':>8s} "
        f"{'Profit':>12s} {'Blacklisted':>12s}"
    )
    print(_box_row(header, width))
    print(_box_row("─" * (width - 2), width))

    for b in bots:
        s = b.get_stats()
        row = (
            f"{b.bot_id:<10s} {s['attacks_attempted']:>10d} {s['attacks_successful']:>10d} "
            f"{s['attacks_failed']:>8d} {from_wei(s['total_profit']):>9.2f}   "
            f"{'YES' if s['is_blacklisted'] else 'No':>12s}"
        )
        print(_box_row(row, width))

    print(_box_bottom(width))
    print()


# ── Final Report ──

def print_final_report(cumulative_stats, traders, oracles, bots, days_simulated):
    """
    Print comprehensive final report.
    """
    width = 66
    total_prem = cumulative_stats.get("total_premium", 0)
    total_pay = cumulative_stats.get("total_payout", 0)
    net = total_prem - total_pay

    print()
    print("=" * (width + 2))
    title = "MEV INSURANCE SIMULATION — FINAL REPORT"
    rows = [
        f"{'Days simulated:':<40s} {days_simulated:>8d}",
        f"{'Traders:':<40s} {len(traders):>8d}",
        f"{'Oracles:':<40s} {len(oracles):>8d}",
        f"{'Bots:':<40s} {len(bots):>8d}",
        f"{'─' * (width - 2)}",
        f"{'Total swaps:':<40s} {cumulative_stats.get('total_swaps', 0):>8d}",
        f"{'Total claims:':<40s} {cumulative_stats.get('total_claims', 0):>8d}",
        f"{'  Approved:':<40s} {cumulative_stats.get('total_approved', 0):>8d}",
        f"{'  Rejected:':<40s} {cumulative_stats.get('total_rejected', 0):>8d}",
        f"{'  CAPTCHA required:':<40s} {cumulative_stats.get('total_captcha', 0):>8d}",
        f"{'  Invalid pattern:':<40s} {cumulative_stats.get('total_invalid_pattern', 0):>8d}",
        f"{'  Secondary reviews:':<40s} {cumulative_stats.get('total_secondary_reviews', 0):>8d}",
        f"{'─' * (width - 2)}",
        f"{'Total premiums collected:':<40s} {from_wei(total_prem):>12.4f} MEVI",
        f"{'Total payouts issued:':<40s} {from_wei(total_pay):>12.4f} MEVI",
        f"{'Pool net P&L:':<40s} {from_wei(net):>+12.4f} MEVI",
        f"{'Pool balance:':<40s} {from_wei(cumulative_stats.get('pool_balance', 0)):>12.2f} MEVI",
    ]

    # Coverage ratio
    total_loss = cumulative_stats.get("total_loss_claimed", 0)
    if total_loss > 0 and total_pay > 0:
        cov_ratio = total_pay / total_loss * 100
        rows.append(f"{'Coverage ratio (payout/loss):':<40s} {cov_ratio:>11.1f}%")

    # Bot stats
    total_attacks = sum(b.stats["attacks_successful"] for b in bots)
    total_profit = sum(b.stats["total_profit"] for b in bots)
    if total_attacks > 0:
        rows.append(f"{'─' * (width - 2)}")
        rows.append(f"{'Sandwich attacks:':<40s} {total_attacks:>8d}")
        rows.append(f"{'Bot total profit:':<40s} {from_wei(total_profit):>12.2f} MEVI")

    _print_box(title, rows, width)

    # Print sub-tables
    print_user_table(traders)
    print_oracle_table(oracles)
    if bots:
        print_bot_table(bots)
