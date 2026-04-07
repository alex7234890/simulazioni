"""
user_cli.py — Interactive CLI for the MEV Insurance protocol.

You ARE the trader. Commands:
  SWAP  <amount>         Execute an insured swap
  CLAIM <swap_id> <loss> Submit a claim (oracles vote automatically)
  STIMA <amount>         Estimate premium for a swap
  PROFILO                Show your profile and policy
  POOL                   Show pool status
  GIORNO [N]             Advance blockchain time by N days (default 1)
  RINNOVA                Renew your insurance policy
  HELP                   Show this help
  ESCI                   Exit

Usage:
    python scripts/user_cli.py
"""
import sys
import os
import time
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, get_all_contracts, load_actors,
    send_tx, to_wei, from_wei, log, advance_day,
    generate_salt, keccak256_commit, compute_fraud_score,
)

# ── Coverage level (High = 2) ──
COVERAGE_HIGH = 2
ORACLE_COMMIT_WAIT = 0.3   # seconds between oracle commits (cosmetic)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _banner():
    print("\033[1;34m")
    print("╔══════════════════════════════════════════════╗")
    print("║     MEV INSURANCE — Interactive Terminal     ║")
    print("╚══════════════════════════════════════════════╝")
    print("\033[0m")
    print("Type HELP for available commands.\n")


def _fmt_addr(addr):
    return addr[:10] + "…" + addr[-4:]


def _tier_name(tid):
    return {0: "Bronze", 1: "Silver", 2: "Gold", 3: "Platinum"}.get(tid, "?")


# ─────────────────────────────────────────────
# Oracle automation (commit → reveal)
# ─────────────────────────────────────────────

def _oracle_vote(w3, contracts, oracle_addrs, claim_id, is_real_attack):
    """
    All 7 oracles commit then reveal for claim_id.
    fraud_score computed from on-chain user profile.
    Returns list of fraud scores submitted.
    """
    insurance = contracts["MEVInsurance"]
    registry  = contracts["OracleRegistry"]

    # Read claimant profile for score computation
    try:
        claim    = insurance.functions.claims(claim_id).call()
        claimant = claim[0]
        profile  = insurance.functions.getUserProfile(claimant).call()
        tier_id      = profile[0]
        total_claims = profile[3]
        total_swaps  = profile[2]
    except Exception:
        tier_id, total_claims, total_swaps = 0, 0, 1

    commits = []  # (oracle_addr, fraud_score, pattern_valid, salt)

    # ── Phase 1: Commit ──
    log(f"Oracles committing on claim #{claim_id}…", "ORACLE")
    for addr in oracle_addrs:
        score, pattern = compute_fraud_score(tier_id, total_claims, total_swaps)
        # If it IS a real attack, bias score downward (lower = more likely legit)
        if is_real_attack:
            score = max(0, score - random.randint(10, 25))
            pattern = True
        salt = generate_salt()
        commit_hash = keccak256_commit(score, pattern, salt)

        try:
            send_tx(w3, insurance.functions.submitOracleCommit(claim_id, commit_hash), addr)
            commits.append((addr, score, pattern, salt))
            time.sleep(ORACLE_COMMIT_WAIT)
        except Exception as e:
            log(f"Oracle {_fmt_addr(addr)} commit failed: {e}", "ERROR")

    if not commits:
        log("No oracle commits succeeded — claim cannot be resolved", "ERROR")
        return []

    # ── Phase 2: Reveal ──
    log(f"Oracles revealing on claim #{claim_id}…", "ORACLE")
    scores = []
    for addr, score, pattern, salt in commits:
        try:
            send_tx(w3,
                insurance.functions.submitOracleReveal(claim_id, score, pattern, salt),
                addr,
            )
            scores.append(score)
            log(f"  {_fmt_addr(addr)} → score={score}, pattern={'Y' if pattern else 'N'}", "ORACLE")
            time.sleep(ORACLE_COMMIT_WAIT)
        except Exception as e:
            log(f"Oracle {_fmt_addr(addr)} reveal failed: {e}", "ERROR")

    return scores


def _finalize_claim(w3, contracts, deployer, claim_id):
    """Call finalizeClaim as deployer/owner. Returns verdict string."""
    insurance = contracts["MEVInsurance"]
    VERDICT   = {0: "Pending", 1: "✅ APPROVED", 2: "❌ REJECTED",
                 3: "⚠️  CAPTCHA required", 4: "❌ Invalid pattern"}
    try:
        send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer)
        claim   = insurance.functions.claims(claim_id).call()
        verdict = VERDICT.get(claim[4], f"?({claim[4]})")
        payout  = from_wei(claim[3])
        return verdict, payout
    except Exception as e:
        return f"Finalize error: {e}", 0.0


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

def cmd_swap(w3, contracts, user_addr, args):
    if not args:
        print("Usage: SWAP <amount_mevi>")
        return None

    try:
        amount = float(args[0])
    except ValueError:
        print("Amount must be a number.")
        return None

    insurance = contracts["MEVInsurance"]
    token     = contracts["MEVToken"]

    # Check balance
    bal = from_wei(token.functions.balanceOf(user_addr).call())
    log(f"Your balance: {bal:.2f} MEVI", "USER")

    try:
        send_tx(w3, insurance.functions.insuredSwap(to_wei(amount)), user_addr)
        swap_id  = insurance.functions.getInsuredSwapsCount().call() - 1
        swap_info = insurance.functions.insuredSwaps(swap_id).call()
        premium  = from_wei(swap_info[2])
        log(f"Swap #{swap_id} executed: {amount} MEVI | premium paid: {premium:.4f} MEVI", "USER")
        return swap_id
    except Exception as e:
        err = str(e)
        if "Daily swap limit" in err:
            log("Daily swap limit reached. Run GIORNO to advance to the next day.", "ERROR")
        elif "Policy expired" in err:
            log("Policy expired. Run RINNOVA to renew.", "ERROR")
        elif "Swap value exceeds" in err:
            log(f"Swap amount {amount} MEVI exceeds your policy maximum.", "ERROR")
        else:
            log(f"Swap failed: {err}", "ERROR")
        return None


def cmd_claim(w3, contracts, user_addr, deployer, oracle_addrs, args):
    if len(args) < 2:
        print("Usage: CLAIM <swap_id> <loss_mevi>")
        print("  Example: CLAIM 0 50")
        return

    try:
        swap_id = int(args[0])
        loss    = float(args[1])
    except ValueError:
        print("swap_id must be integer, loss must be a number.")
        return

    insurance = contracts["MEVInsurance"]

    # Validate swap exists and belongs to user
    try:
        swap = insurance.functions.insuredSwaps(swap_id).call()
        if swap[0].lower() != user_addr.lower():
            log(f"Swap #{swap_id} does not belong to you.", "ERROR")
            return
    except Exception:
        log(f"Swap #{swap_id} not found.", "ERROR")
        return

    # Ask if it was a real MEV attack (affects oracle scoring)
    print(f"\nWas swap #{swap_id} actually attacked by a MEV bot? [y/N] ", end="", flush=True)
    ans = input().strip().lower()
    is_real = ans in ("y", "yes", "si", "s")

    # Dummy tx hashes for the claim
    tx1 = w3.keccak(text=f"front_{swap_id}_{random.randint(0,999999)}")
    tx2 = w3.keccak(text=f"victim_{swap_id}_{random.randint(0,999999)}")
    tx3 = w3.keccak(text=f"back_{swap_id}_{random.randint(0,999999)}")
    bot_zero = "0x" + "0" * 40

    try:
        send_tx(w3,
            insurance.functions.submitClaim(
                swap_id, tx1, tx2, tx3, to_wei(loss), bot_zero,
            ),
            user_addr,
        )
        claim_id = insurance.functions.getClaimsCount().call() - 1
        log(f"Claim #{claim_id} submitted (loss={loss:.2f} MEVI)", "CLAIM")
    except Exception as e:
        log(f"Claim submission failed: {e}", "ERROR")
        return

    # Oracle voting
    scores = _oracle_vote(w3, contracts, oracle_addrs, claim_id, is_real)
    if not scores:
        return

    avg = sum(scores) / len(scores)
    log(f"Oracle scores: {scores}", "ORACLE")
    log(f"Average fraud score: {avg:.1f}", "ORACLE")

    # Finalize
    verdict, payout = _finalize_claim(w3, contracts, deployer, claim_id)
    log(f"Verdict: {verdict}", "CLAIM")
    if payout > 0:
        log(f"Payout received: {payout:.4f} MEVI", "CLAIM")


def cmd_estimate(w3, contracts, user_addr, args):
    if not args:
        print("Usage: STIMA <amount_mevi>")
        return

    try:
        amount = float(args[0])
    except ValueError:
        print("Amount must be a number.")
        return

    try:
        calc    = contracts["PremiumCalculator"]
        premium = calc.functions.calculatePremium(
            to_wei(amount), user_addr, COVERAGE_HIGH
        ).call()
        log(f"Estimated premium for {amount} MEVI swap: {from_wei(premium):.4f} MEVI", "USER")
    except Exception:
        # Fallback: try getPremiumEstimate on insurance
        try:
            insurance = contracts["MEVInsurance"]
            premium   = insurance.functions.getPremiumEstimate(
                to_wei(amount), COVERAGE_HIGH
            ).call()
            log(f"Estimated premium for {amount} MEVI swap: {from_wei(premium):.4f} MEVI", "USER")
        except Exception as e:
            log(f"Could not estimate premium: {e}", "ERROR")


def cmd_profile(w3, contracts, user_addr):
    insurance = contracts["MEVInsurance"]
    token     = contracts["MEVToken"]

    try:
        profile = insurance.functions.getUserProfile(user_addr).call()
        policy  = insurance.functions.policies(user_addr).call()
        balance = token.functions.balanceOf(user_addr).call()

        w = 52
        print("╔" + "═"*w + "╗")
        print("║" + " YOUR PROFILE ".center(w) + "║")
        print("╠" + "═"*w + "╣")
        lines = [
            f"{'Address:':<28s} {_fmt_addr(user_addr)}",
            f"{'Balance:':<28s} {from_wei(balance):,.2f} MEVI",
            f"{'Tier:':<28s} {_tier_name(profile[0])}",
            f"{'Policy active:':<28s} {'YES' if policy[6] else 'NO — run RINNOVA'}",
            "─" * (w - 2),
            f"{'Total swaps:':<28s} {profile[2]}",
            f"{'Total claims:':<28s} {profile[3]}",
            f"{'  Approved:':<28s} {profile[4]}",
            f"{'  Rejected:':<28s} {profile[5]}",
            f"{'Avg fraud score:':<28s} {profile[6]}",
            f"{'Blacklisted:':<28s} {'YES ⚠' if profile[7] else 'No'}",
        ]
        for line in lines:
            print("║ " + line.ljust(w - 2) + " ║")
        print("╚" + "═"*w + "╝")
        print()
    except Exception as e:
        log(f"Could not read profile: {e}", "ERROR")


def cmd_pool(w3, contracts):
    insurance = contracts["MEVInsurance"]
    token     = contracts["MEVToken"]

    try:
        pool_bal     = from_wei(token.functions.balanceOf(insurance.address).call())
        total_swaps  = insurance.functions.getInsuredSwapsCount().call()
        total_claims = insurance.functions.getClaimsCount().call()

        w = 52
        print("╔" + "═"*w + "╗")
        print("║" + " POOL STATUS ".center(w) + "║")
        print("╠" + "═"*w + "╣")
        lines = [
            f"{'Pool balance:':<30s} {pool_bal:>12.2f} MEVI",
            f"{'Total insured swaps:':<30s} {total_swaps:>12d}",
            f"{'Total claims:':<30s} {total_claims:>12d}",
        ]
        for line in lines:
            print("║ " + line.ljust(w - 2) + " ║")
        print("╚" + "═"*w + "╝")
        print()
    except Exception as e:
        log(f"Could not read pool: {e}", "ERROR")


def cmd_renew(w3, contracts, user_addr):
    insurance = contracts["MEVInsurance"]
    try:
        send_tx(w3, insurance.functions.buyPolicy(COVERAGE_HIGH), user_addr)
        log("Policy renewed (High coverage).", "USER")
    except Exception as e:
        log(f"Policy renewal failed: {e}", "ERROR")


def cmd_giorno(w3, args):
    days = 1
    if args:
        try:
            days = int(args[0])
        except ValueError:
            pass
    advance_day(w3, days)
    block = w3.eth.get_block("latest")
    log(f"Now at block #{block['number']}, timestamp={block['timestamp']}", "SYSTEM")


def cmd_help():
    print("""
╔══════════════════════════════════════════════════════╗
║                  AVAILABLE COMMANDS                  ║
╠══════════════════════════════════════════════════════╣
║ SWAP  <amount>      Execute an insured swap           ║
║ CLAIM <id> <loss>   Submit claim (oracles vote auto)  ║
║ STIMA <amount>      Estimate premium for a swap       ║
║ PROFILO             Show your profile & policy        ║
║ POOL                Show pool balance & stats         ║
║ GIORNO [N]          Advance blockchain by N days      ║
║ RINNOVA             Renew your insurance policy       ║
║ HELP                Show this help                    ║
║ ESCI                Exit                              ║
╚══════════════════════════════════════════════════════╝
""")


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

def main():
    _banner()

    w3        = get_web3()
    contracts = get_all_contracts(w3)
    actors    = load_actors()

    accounts = w3.eth.accounts
    deployer     = actors.get("deployer", accounts[0])
    user_addr    = actors.get("user", accounts[1] if len(accounts) > 1 else accounts[0])
    oracle_addrs = actors.get("oracles", accounts[6:13] if len(accounts) >= 13 else [])

    if not oracle_addrs:
        log("No oracles found in actors.json — run setup_chain.py first", "ERROR")
        sys.exit(1)

    log(f"Logged in as: {_fmt_addr(user_addr)}", "USER")
    log(f"Oracles available: {len(oracle_addrs)}", "SYSTEM")

    # Ensure user is registered and has a policy
    insurance = contracts["MEVInsurance"]
    try:
        is_reg = insurance.functions.registeredUsers(user_addr).call()
        if not is_reg:
            log("Registering user…", "SETUP")
            send_tx(w3, insurance.functions.registerUser(), user_addr)

        policy = insurance.functions.policies(user_addr).call()
        if not policy[6]:
            log("No active policy — buying High coverage policy…", "SETUP")
            from utils import to_wei as _tw
            token = contracts["MEVToken"]
            send_tx(w3, token.functions.approve(insurance.address, _tw(1_000_000)), user_addr)
            send_tx(w3, insurance.functions.buyPolicy(COVERAGE_HIGH), user_addr)
            log("Policy activated.", "SETUP")
    except Exception as e:
        log(f"Setup check failed: {e}", "ERROR")

    print()

    while True:
        try:
            raw = input("\033[96mMEVI> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            log("Goodbye.", "SYSTEM")
            break

        if not raw:
            continue

        parts = raw.split()
        cmd   = parts[0].upper()
        args  = parts[1:]

        if cmd == "ESCI" or cmd == "EXIT" or cmd == "QUIT":
            log("Goodbye.", "SYSTEM")
            break
        elif cmd == "HELP":
            cmd_help()
        elif cmd == "SWAP":
            cmd_swap(w3, contracts, user_addr, args)
        elif cmd == "CLAIM":
            cmd_claim(w3, contracts, user_addr, deployer, oracle_addrs, args)
        elif cmd == "STIMA":
            cmd_estimate(w3, contracts, user_addr, args)
        elif cmd == "PROFILO":
            cmd_profile(w3, contracts, user_addr)
        elif cmd == "POOL":
            cmd_pool(w3, contracts)
        elif cmd == "GIORNO":
            cmd_giorno(w3, args)
        elif cmd == "RINNOVA":
            cmd_renew(w3, contracts, user_addr)
        else:
            print(f"Unknown command '{cmd}'. Type HELP for available commands.")


if __name__ == "__main__":
    main()
