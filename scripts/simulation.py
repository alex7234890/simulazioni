#!/usr/bin/env python3
"""
MEV Insurance Protocol - End-to-End Simulation (Sequential)

Usage:
  Terminal 1: npx hardhat node
  Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
  Terminal 3: python scripts/simulation.py
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import (
    get_web3, load_config, get_all_contracts, get_accounts,
    send_tx, keccak256_commit, generate_salt, to_wei, from_wei,
    increase_time, log
)

# ─── Account layout ────────────────────────────────────────────
# accounts[0]                         = deployer
# accounts[1 .. N_USERS]              = traders
# accounts[N_USERS+1 .. N_USERS+N_ORA]= oracles
# accounts[N_USERS+N_ORA+1]           = MEV bot
# Max needed: 2 + N_USERS + N_ORA  (≤ 20 Hardhat accounts)
# ────────────────────────────────────────────────────────────────

# Will be set after interactive config
CFG = {}

# Stats (reset each run)
stats = {
    "swaps": 0, "claims_submitted": 0, "claims_approved": 0,
    "claims_rejected": 0, "claims_captcha": 0, "claims_invalid_pattern": 0,
    "total_loss": 0, "total_payout": 0, "total_premium": 0,
    "secondary_reviews": 0, "daily_limit_hits": 0,
}


# ═══════════════════════════════════════════════════════════════
#  INTERACTIVE CONFIG
# ═══════════════════════════════════════════════════════════════

def ask_int(prompt, default, lo, hi):
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        val = int(raw) if raw else default
        if lo <= val <= hi:
            return val
        print(f"  → Inserisci un valore tra {lo} e {hi}")


def ask_float(prompt, default, lo, hi):
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        val = float(raw) if raw else default
        if lo <= val <= hi:
            return val
        print(f"  → Inserisci un valore tra {lo} e {hi}")


def interactive_config():
    print("\n" + "═" * 60)
    print("   MEV INSURANCE PROTOCOL — CONFIGURATORE SIMULAZIONE")
    print("═" * 60)
    print("\nParametri simulazione:\n")

    n_days        = ask_int("Giorni di simulazione       (1-60)",   30,  1, 60)
    avg_swap      = ask_int("Valore medio swap (MEVI)   (10-2000)", 200, 10, 2000)
    n_users       = ask_int("Numero utenti               (1-5)",     3,  1,  5)
    n_oracles     = ask_int("Numero oracle               (7-12)",    7,  7, 12)
    claim_prob    = ask_float("Probabilità claim/swap     (0.1-1.0)",0.6,0.1, 1.0)

    CFG.update({
        "n_days":    n_days,
        "avg_swap":  avg_swap,
        "n_users":   n_users,
        "n_oracles": n_oracles,
        "claim_prob": claim_prob,
        # account indices
        "user_start":   1,
        "oracle_start": n_users + 1,
        "bot_idx":      n_users + n_oracles + 1,
    })

    total_accts_needed = 2 + n_users + n_oracles
    print(f"\n  Account Hardhat necessari: {total_accts_needed}  (disponibili: 20)")
    print()


# ═══════════════════════════════════════════════════════════════
#  FORMULA DISPLAY
# ═══════════════════════════════════════════════════════════════

def bps(val):
    return f"{val} bps ({val/100:.2f}%)"

def show_formula_params(w3, contracts):
    calc   = contracts["PremiumCalculator"]
    ins    = contracts["MEVInsurance"]
    reg    = contracts["OracleRegistry"]

    # PremiumCalculator
    patt       = calc.functions.patt().call()
    lPercent   = calc.functions.lPercent().call()
    eFNR       = calc.functions.eFNR().call()
    mBase      = calc.functions.mBase().call()
    pmin       = calc.functions.pmin().call()
    srSafe     = calc.functions.srSafe().call()
    srCrit     = calc.functions.srCritical().call()
    dMmed      = calc.functions.deltaMmed().call()
    dMhigh     = calc.functions.deltaMhigh().call()

    # MEVInsurance
    tApprove   = ins.functions.thetaApprove().call()
    tReject    = ins.functions.thetaReject().call()
    dispThr    = ins.functions.dispersioneThreshold().call()
    nOra       = ins.functions.nOracle().call()
    sw_bronze  = ins.functions.maxDailySwaps(0).call()
    sw_silver  = ins.functions.maxDailySwaps(1).call()
    sw_gold    = ins.functions.maxDailySwaps(2).call()
    actFee     = ins.functions.activationFee().call()

    # OracleRegistry
    minStake   = reg.functions.getMinimumStake().call()

    print("─" * 60)
    print("  PARAMETRI CONTRATTO (lettura on-chain)")
    print("─" * 60)
    print(f"\n  Formula premium:")
    print(f"  P = max(V×[(Patt×L%) + (Tint×E/(1-E))/Vbase + C/Vbase]×(1+M)×Fcov, Pmin×V)\n")
    print(f"  Patt  (prob. attacco)      : {bps(patt)}")
    print(f"  L%    (perdita media)      : {bps(lPercent)}")
    print(f"  eFNR  (false neg. rate)    : {bps(eFNR)}")
    print(f"  mBase (margine base)       : {bps(mBase)}")
    print(f"  pmin  (premium minimo)     : {bps(pmin)}")
    print(f"  Fcov  Low/Med/High         : 70% / 90% / 100%")
    print(f"  SR safe/critico            : {srSafe/10000:.1f}x / {srCrit/10000:.1f}x")
    print(f"  mAdj  med/high             : +{dMmed/100:.0f}% / +{dMhigh/100:.0f}%")
    print()
    print(f"  θApprove (soglia approv.)  : {tApprove}  (Gold/Plat: score < {tApprove} → OK)")
    print(f"  θReject  (soglia rifiuto)  : {tReject}  (score ≥ {tReject} → blacklist)")
    print(f"  Dispersione threshold      : {dispThr}  (> {dispThr} → revisione secondaria)")
    print(f"  Oracle per claim           : {nOra}")
    print(f"  Max swap/giorno            : Bronze={sw_bronze}, Silver={sw_silver}, Gold={sw_gold}, Plat=∞")
    print(f"  Activation fee (policy)    : {from_wei(actFee):.0f} MEVI")
    print(f"  Oracle min stake           : {from_wei(minStake):.4f} ETH")
    print()
    print("─" * 60)

    n_days   = CFG["n_days"]
    avg_swap = CFG["avg_swap"]
    n_users  = CFG["n_users"]
    cp       = CFG["claim_prob"]

    print(f"\n  RIEPILOGO SIMULAZIONE:")
    print(f"  Giorni:           {n_days}")
    print(f"  Utenti:           {n_users}")
    print(f"  Oracle attivi:    {CFG['n_oracles']}")
    print(f"  Swap medio:       {avg_swap} MEVI")
    print(f"  Prob. claim:      {cp*100:.0f}%")
    max_daily = sw_bronze   # Bronze tier default
    est_swaps = n_days * n_users * max_daily
    est_claims = int(est_swaps * cp)
    print(f"  Swap stimati:     ~{est_swaps}  (Bronze, {max_daily}/giorno/utente)")
    print(f"  Claim stimati:    ~{est_claims}")
    print("═" * 60)
    input("\n  Premi INVIO per avviare la simulazione...\n")


# ═══════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════

def setup(w3, contracts, accounts):
    token    = contracts["MEVToken"]
    registry = contracts["OracleRegistry"]
    insurance= contracts["MEVInsurance"]
    deployer = accounts[0]
    n_users   = CFG["n_users"]
    n_oracles = CFG["n_oracles"]
    u_start   = CFG["user_start"]
    o_start   = CFG["oracle_start"]
    bot_idx   = CFG["bot_idx"]

    log("=== Setup Phase ===")

    # Fund users
    for i in range(n_users):
        user = accounts[u_start + i]
        if token.functions.balanceOf(user).call() < to_wei(5000):
            send_tx(w3, token.functions.transfer(user, to_wei(50000)), deployer)
            log(f"  User {i+1} ({user[:10]}...) funded 50k MEVI")

    # Fund bot
    bot = accounts[bot_idx]
    if token.functions.balanceOf(bot).call() < to_wei(5000):
        send_tx(w3, token.functions.transfer(bot, to_wei(50000)), deployer)
        log(f"  Bot ({bot[:10]}...) funded 50k MEVI")

    # Register oracles
    for i in range(n_oracles):
        oracle = accounts[o_start + i]
        if not registry.functions.isOracle(oracle).call():
            min_stake = registry.functions.getMinimumStake().call()
            send_tx(w3, registry.functions.registerOracle(), oracle, value=min_stake)
            send_tx(w3, registry.functions.activateOracle(), oracle)
            log(f"  Oracle {i+1} ({oracle[:10]}...) registrato")
        else:
            log(f"  Oracle {i+1} ({oracle[:10]}...) già registrato")

    # Register and buy policies for users
    for i in range(n_users):
        user = accounts[u_start + i]
        if not insurance.functions.registeredUsers(user).call():
            send_tx(w3, insurance.functions.registerUser(), user)
            log(f"  User {i+1} registrato")
        policy = insurance.functions.policies(user).call()
        if not policy[6]:  # active
            send_tx(w3, token.functions.approve(insurance.address, to_wei(200000)), user)
            send_tx(w3, insurance.functions.buyPolicy(2), user)  # High coverage
            log(f"  User {i+1} policy High acquistata")
        else:
            send_tx(w3, token.functions.approve(insurance.address, to_wei(200000)), user)

    log("Setup completato!\n")


# ═══════════════════════════════════════════════════════════════
#  ORACLE ANALYSIS — FIX dispersion
# ═══════════════════════════════════════════════════════════════

def compute_claim_base_score(user_addr, insurance):
    """
    Calcola un punteggio BASE deterministico dal profilo on-chain del claimant.
    Tutti e 7 gli oracle leggono gli stessi dati → stessa base.
    Solo il rumore per-oracle (±5) varia → dispersione max = 10 < soglia 20.
    """
    base = 40  # default Gold-like

    try:
        profile = insurance.functions.getUserProfile(user_addr).call()
        tier = profile[0]
        total_swaps = profile[2]
        total_claims = profile[3]

        # Punteggio per tier (Bronze = più sospetto)
        tier_base = {0: 48, 1: 38, 2: 28, 3: 18}
        base = tier_base.get(tier, 40)

        # Fattore claim rate
        if total_swaps > 0:
            cr = total_claims / total_swaps
            if   cr > 0.60: base += 18
            elif cr > 0.40: base += 10
            elif cr > 0.20: base +=  4
            # bassa claim rate = utente onesto
    except Exception:
        pass

    return base


def oracle_score(base_score):
    """Aggiunge rumore per-oracle ±5. Max dispersion tra 7 oracle = 10 < soglia 20."""
    noise = random.randint(-5, 5)
    return max(0, min(130, base_score + noise))


# ═══════════════════════════════════════════════════════════════
#  RUN CYCLE  (1 swap → claim → oracle commit/reveal → finalize)
# ═══════════════════════════════════════════════════════════════

def run_cycle(w3, contracts, accounts, user_idx, cycle_label):
    token    = contracts["MEVToken"]
    insurance= contracts["MEVInsurance"]
    deployer = accounts[0]
    trader   = accounts[CFG["user_start"] + user_idx]
    bot_addr = accounts[CFG["bot_idx"]]
    o_start  = CFG["oracle_start"]
    n_ora    = CFG["n_oracles"]
    cp       = CFG["claim_prob"]
    avg_swap = CFG["avg_swap"]

    log(f"  [{cycle_label}] User {user_idx+1}")

    # ── 1. Insured swap ──────────────────────────────────────
    swap_value = random.randint(int(avg_swap * 0.5), int(avg_swap * 1.5))
    try:
        send_tx(w3, insurance.functions.insuredSwap(to_wei(swap_value)), trader)
        swap_id = insurance.functions.getInsuredSwapsCount().call() - 1
        swap_info = insurance.functions.insuredSwaps(swap_id).call()
        premium = swap_info[2]
        stats["swaps"] += 1
        stats["total_premium"] += premium
        log(f"    Swap #{swap_id}: {swap_value} MEVI, premium={from_wei(premium):.4f}")
    except Exception as e:
        if "Daily swap limit" in str(e):
            stats["daily_limit_hits"] += 1
            log(f"    Daily swap limit raggiunto per User {user_idx+1} (ok, avanza giorno)")
        else:
            log(f"    insuredSwap fallito: {e}")
        return

    # ── 2. Decide se fare claim ──────────────────────────────
    if random.random() > cp:
        log(f"    Nessun claim per swap #{swap_id}")
        return

    is_sandwich = random.random() < 0.35
    loss_pct = random.uniform(0.05, 0.20) if is_sandwich else random.uniform(0.02, 0.08)
    loss_wei = max(to_wei(1), int(to_wei(swap_value) * loss_pct))

    tx1 = w3.keccak(text=f"fr_{swap_id}_{random.randint(0, 999999)}")
    tx2 = w3.keccak(text=f"vi_{swap_id}_{random.randint(0, 999999)}")
    tx3 = w3.keccak(text=f"br_{swap_id}_{random.randint(0, 999999)}")
    bot_claim = bot_addr if is_sandwich else "0x0000000000000000000000000000000000000000"

    try:
        send_tx(w3, insurance.functions.submitClaim(
            swap_id, tx1, tx2, tx3, loss_wei, bot_claim
        ), trader)
        claim_id = insurance.functions.getClaimsCount().call() - 1
        stats["claims_submitted"] += 1
        stats["total_loss"] += loss_wei
        tag = "SANDWICH" if is_sandwich else "normal"
        log(f"    Claim #{claim_id} ({tag}) loss={from_wei(loss_wei):.2f} MEVI")
    except Exception as e:
        log(f"    submitClaim fallito: {e}")
        return

    # ── 3. Oracle commit-reveal ──────────────────────────────
    assigned = insurance.functions.getClaimOracles(claim_id).call()
    base_score = compute_claim_base_score(trader, insurance)
    pattern_valid = random.random() < 0.90

    oracle_data = []
    for addr in assigned:
        fs   = oracle_score(base_score)
        salt = generate_salt()
        ch   = keccak256_commit(fs, pattern_valid, salt)
        oracle_data.append({"address": addr, "fs": fs, "pv": pattern_valid,
                            "salt": salt, "ch": ch})

    # Commit
    for od in oracle_data:
        try:
            send_tx(w3, insurance.functions.commitVerdict(claim_id, od["ch"]), od["address"])
        except Exception as e:
            log(f"    Commit fallito ({od['address'][:8]}): {e}")

    # Reveal
    scores = []
    for od in oracle_data:
        try:
            send_tx(w3, insurance.functions.revealVerdict(
                claim_id, od["fs"], od["pv"], od["salt"]
            ), od["address"])
            scores.append(od["fs"])
        except Exception as e:
            log(f"    Reveal fallito ({od['address'][:8]}): {e}")

    if not scores:
        log(f"    Nessun reveal — claim #{claim_id} abbandonato")
        return

    median = sorted(scores)[len(scores) // 2]
    disp   = max(scores) - min(scores)
    log(f"    Scores: {scores} | median={median} disp={disp}")

    # ── 4. Finalize (round 1) ────────────────────────────────
    try:
        send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer)
    except Exception as e:
        log(f"    finalizeClaim(1) fallito: {e}")
        return

    # ── 5. Secondary review? ─────────────────────────────────
    status = insurance.functions.getClaimInfo(claim_id).call()[1]
    if status == 1:  # OracleReview → secondary triggered
        stats["secondary_reviews"] += 1
        log(f"    → REVISIONE SECONDARIA (disp={disp})")

        assigned2 = insurance.functions.getClaimOracles(claim_id).call()
        # Secondo round: score ≈ median con rumore ±4 → disp max 8
        for addr in assigned2:
            fs2   = max(0, min(130, median + random.randint(-4, 4)))
            salt2 = generate_salt()
            ch2   = keccak256_commit(fs2, True, salt2)
            try:
                send_tx(w3, insurance.functions.commitVerdict(claim_id, ch2), addr)
                send_tx(w3, insurance.functions.revealVerdict(claim_id, fs2, True, salt2), addr)
            except Exception as e:
                log(f"    Seconda round fallita ({addr[:8]}): {e}")

        try:
            send_tx(w3, insurance.functions.finalizeClaim(claim_id), deployer)
        except Exception as e:
            log(f"    finalizeClaim(2) fallito: {e}")
            return

        status = insurance.functions.getClaimInfo(claim_id).call()[1]

    # ── 6. Gestione esito ────────────────────────────────────
    _handle_outcome(w3, contracts, accounts, claim_id, status, trader, deployer, loss_wei)


def _handle_outcome(w3, contracts, accounts, claim_id, status, trader, deployer, loss_wei):
    token    = contracts["MEVToken"]
    insurance= contracts["MEVInsurance"]

    STATUS = {0:"Pending", 1:"OracleReview", 2:"CAPTCHA",
              3:"Approved", 4:"Rejected", 5:"InvalidPattern"}
    name = STATUS.get(status, f"?({status})")

    if status == 3:  # Approved
        payout = insurance.functions.getClaimInfo(claim_id).call()[4]
        stats["claims_approved"] += 1
        stats["total_payout"] += payout
        log(f"    → APPROVATO  payout={from_wei(payout):.2f} MEVI")

    elif status == 2:  # CAPTCHA
        stats["claims_captcha"] += 1
        approve = random.random() < 0.80
        bal_pre = token.functions.balanceOf(trader).call()
        try:
            send_tx(w3, insurance.functions.resolveCAPTCHA(claim_id, approve), deployer)
            if approve:
                payout = token.functions.balanceOf(trader).call() - bal_pre
                stats["claims_approved"] += 1
                stats["total_payout"] += payout
                log(f"    → CAPTCHA: APPROVATO  payout={from_wei(payout):.2f} MEVI")
            else:
                stats["claims_rejected"] += 1
                log(f"    → CAPTCHA: RIFIUTATO")
        except Exception as e:
            log(f"    resolveCAPTCHA fallito: {e}")

    elif status == 4:  # Rejected
        stats["claims_rejected"] += 1
        log(f"    → RIFIUTATO (fraud score troppo alto)")

    elif status == 5:  # InvalidPattern
        stats["claims_invalid_pattern"] += 1
        log(f"    → PATTERN INVALIDO")

    else:
        log(f"    → Stato: {name}")


# ═══════════════════════════════════════════════════════════════
#  FINAL REPORT
# ═══════════════════════════════════════════════════════════════

def print_final_report(w3, contracts, accounts):
    token    = contracts["MEVToken"]
    insurance= contracts["MEVInsurance"]
    n_users  = CFG["n_users"]
    u_start  = CFG["user_start"]

    log("\n" + "═" * 60)
    log("      MEV INSURANCE — REPORT FINALE")
    log("═" * 60)

    pool_bal = token.functions.balanceOf(insurance.address).call()

    log(f"  Giorni simulati:       {CFG['n_days']}")
    log(f"  Swap eseguiti:         {stats['swaps']}")
    log(f"  Claim inviati:         {stats['claims_submitted']}")
    log(f"  ├─ Approvati:          {stats['claims_approved']}")
    log(f"  ├─ Rifiutati:          {stats['claims_rejected']}")
    log(f"  ├─ CAPTCHA:            {stats['claims_captcha']}")
    log(f"  └─ Pattern invalido:   {stats['claims_invalid_pattern']}")
    log(f"  Revisioni secondarie:  {stats['secondary_reviews']}")
    log(f"  Limit giornaliero:     {stats['daily_limit_hits']} volte raggiunto")
    log(f"  ─────────────────────────────────────")
    log(f"  Premium totali:        {from_wei(stats['total_premium']):>10.4f} MEVI")
    log(f"  Perdite dichiarate:    {from_wei(stats['total_loss']):>10.2f} MEVI")
    log(f"  Payout totali:         {from_wei(stats['total_payout']):>10.2f} MEVI")
    log(f"  Pool residuo:          {from_wei(pool_bal):>10.2f} MEVI")
    net = stats['total_premium'] - stats['total_payout']
    log(f"  P&L pool:              {from_wei(net):>+10.4f} MEVI")
    if stats['total_loss'] > 0:
        ratio = stats['total_payout'] / stats['total_loss'] * 100
        log(f"  Coverage ratio:        {ratio:>9.1f}%")

    log(f"\n  Utenti:")
    TIER = {0:"Bronze", 1:"Silver", 2:"Gold", 3:"Platinum"}
    for i in range(n_users):
        user = accounts[u_start + i]
        bal  = token.functions.balanceOf(user).call()
        try:
            p = insurance.functions.getUserProfile(user).call()
            tier = TIER.get(p[0], "?")
            log(f"    User {i+1}: {from_wei(bal):.2f} MEVI  tier={tier}  "
                f"swaps={p[2]}  claims={p[3]}  avgFraud={p[6]}")
        except Exception:
            log(f"    User {i+1}: {from_wei(bal):.2f} MEVI")

    log("═" * 60)


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    # 1. Config interattiva
    interactive_config()

    # 2. Connetti
    log("Connessione al nodo Hardhat...")
    w3        = get_web3()
    contracts = get_all_contracts(w3)
    accounts  = get_accounts(w3)

    needed = 2 + CFG["n_users"] + CFG["n_oracles"]
    if len(accounts) < needed:
        log(f"ERRORE: servono {needed} account, disponibili {len(accounts)}")
        sys.exit(1)

    log(f"Connesso (block #{w3.eth.block_number}), {len(accounts)} account disponibili")

    # 3. Mostra parametri formula
    show_formula_params(w3, contracts)

    # 4. Setup
    setup(w3, contracts, accounts)

    # 5. Simulazione: loop giornaliero
    n_days   = CFG["n_days"]
    n_users  = CFG["n_users"]
    # Bronze ha max 3 swap/giorno → 3 cicli per utente per giorno
    max_sw   = contracts["MEVInsurance"].functions.maxDailySwaps(0).call()  # Bronze
    swaps_pd = max(1, min(max_sw, 3))  # swap al giorno per utente

    log(f"Avvio simulazione: {n_days} giorni × {n_users} utenti × {swaps_pd} swap/giorno\n")

    for day in range(n_days):
        log(f"╔══ GIORNO {day+1}/{n_days} ══")

        for user_idx in range(n_users):
            for sw in range(swaps_pd):
                label = f"G{day+1}-U{user_idx+1}-S{sw+1}"
                try:
                    run_cycle(w3, contracts, accounts, user_idx, label)
                except Exception as e:
                    log(f"  [{label}] Errore ciclo: {e}")

        # ── FIX B: avanza il tempo di 1 giorno a fine giornata ──
        increase_time(w3, 86400)
        log(f"╚══ Fine giorno {day+1} — blockchain avanzata di 1 giorno\n")

    # 6. Report finale
    print_final_report(w3, contracts, accounts)


if __name__ == "__main__":
    main()
