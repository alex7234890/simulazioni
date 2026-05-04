import utils
from web3 import Web3
import sys
import warnings
import time
import random
import argparse
import os
import json
from pathlib import Path
warnings.filterwarnings("ignore", message=".*MismatchedABI.*")

# Attesa dopo swap prima di controllare attack_log (2 blocchi + margine)
_ATTACK_WAIT_SEC = max(int(os.environ.get("BLOCK_INTERVAL_MS", "3000")) / 1000 * 2 + 2.0, 8.0)

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument('account_pos', nargs='?', type=int, default=None)
_parser.add_argument('--account', type=int, default=None)
_known, _ = _parser.parse_known_args()

_account_idx = (_known.account if _known.account is not None
                else (_known.account_pos if _known.account_pos is not None else 1))

w3 = utils.get_web3()
contracts = utils.get_all_contracts(w3)

try:
    trader = w3.eth.accounts[_account_idx]
except Exception:
    print("Indice non valido, uso l'account #1")
    trader = w3.eth.accounts[1]

deployer = w3.eth.accounts[0]

ins = contracts["MEVInsurance"]
mevi = contracts["MEVToken"]
usdc = contracts["MockUSDC"]
amm = contracts["MockAMM"]

_ROOT = Path(__file__).parent.parent
_ATTACK_LOG = _ROOT / "config" / "attack_log.json"

_NOMI_COV = ["LOW", "MEDIUM", "HIGH"]

_swap_log = []


def _load_attack_log() -> dict:
    try:
        if _ATTACK_LOG.exists():
            with open(_ATTACK_LOG, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _estimate_mevi_out(usdc_wei: int) -> float:
    try:
        ra = amm.functions.reserveA().call()
        rb = amm.functions.reserveB().call()
        if ra == 0 or rb == 0 or usdc_wei == 0:
            return 0.0
        return (ra * usdc_wei) / (rb + usdc_wei) / 1e18
    except Exception:
        return 0.0


def _wait_and_check_attack(tx_hash_hex: str) -> dict | None:
    time.sleep(_ATTACK_WAIT_SEC)
    key = tx_hash_hex.lower()
    return _load_attack_log().get(key)


def _do_claim_from_log(swap_id, victim_tx_hex: str, attack_entry: dict,
                       loss_mevi: float, usdc_amount: float):
    frontrun_hex = attack_entry.get("frontrun_tx", "")
    backrun_hex  = attack_entry.get("backrun_tx", "")
    bot_str      = attack_entry.get("bot", "")

    if not frontrun_hex or not backrun_hex or not bot_str:
        print("  ⚠️  Dati claim incompleti nell'attack_log — claim saltato")
        return 0, 0.0, "dati claim incompleti"

    def _to_bytes32(h: str) -> bytes:
        h = h[2:] if h.startswith("0x") else h
        return bytes.fromhex(h.zfill(64))

    try:
        tx1 = _to_bytes32(frontrun_hex)
        tx2 = _to_bytes32(victim_tx_hex)
        tx3 = _to_bytes32(backrun_hex)
        loss_capped = min(loss_mevi, usdc_amount)
        loss_wei = utils.to_wei(loss_capped)
        bot_addr = Web3.to_checksum_address(bot_str)

        receipt = utils.send_tx(
            w3,
            ins.functions.submitClaim(swap_id, tx1, tx2, tx3, loss_wei, bot_addr),
            trader
        )
        claim_id = None
        for log_e in ins.events.ClaimSubmitted().process_receipt(receipt):
            claim_id = log_e['args']['claimId']
            oracles = log_e['args']['assignedOracles']
            print(f"\n  ✅ CLAIM #{claim_id} SOTTOMESSO — {len(oracles)} oracle assegnati")

        if claim_id is None:
            return 0, 0.0, "claim ID non trovato nel receipt"

        status, payout, reason = _attendi_risultato(claim_id)
        return status, payout, reason

    except Exception as e:
        print(f"\n  ❌ Errore claim: {e}\n")
        return 0, 0.0, f"eccezione: {str(e)[:80]}"


def _attendi_risultato(claim_id, timeout_sec=600):
    print(f"  ⏳ In attesa di valutazione degli oracle per il claim #{claim_id}...")
    mevi_prima = mevi.functions.balanceOf(trader).call()

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        info = ins.functions.getClaimInfo(claim_id).call()
        status = info[1]
        if status > 1:
            break
        time.sleep(1)
    else:
        # Controllo finale: il claim potrebbe essersi risolto nell'ultimo secondo
        info = ins.functions.getClaimInfo(claim_id).call()
        status = info[1]
        if status <= 1:
            print(f"  ⚠️  Timeout {timeout_sec}s: claim #{claim_id} ancora in OracleReview.")
            utils.sim_log(f"[CLAIM #{claim_id}] TIMEOUT oracle: non finalizzato dopo {timeout_sec}s", tag="CLAIM")
            return 1, 0.0, f"TIMEOUT oracle (>{timeout_sec}s)"

    mevi_dopo = mevi.functions.balanceOf(trader).call()
    payout_mevi = max(0.0, utils.from_wei(mevi_dopo - mevi_prima))

    profile = ins.functions.getUserProfile(trader).call()
    fraud_score = info[2]
    loss = utils.from_wei(info[4])
    dispersione = info[5]
    tiers = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]
    tier_name = tiers[profile[0]]

    base_tier_scores = {0: 50, 1: 30, 2: 15, 3: 0}
    base_score = base_tier_scores.get(profile[0], 50)
    total_swaps = profile[2]
    total_claims = profile[3]
    effective_swaps = total_swaps + 5 if total_swaps <= 3 else total_swaps
    claim_rate_pct = (total_claims / effective_swaps * 100) if effective_swaps > 0 else 0
    if claim_rate_pct >= 30: score_claim = 30
    elif claim_rate_pct >= 20: score_claim = 25
    elif claim_rate_pct >= 10: score_claim = 20
    elif claim_rate_pct >= 6: score_claim = 15
    else: score_claim = 0
    score_network = max(0, fraud_score - base_score - score_claim)

    status_labels = {2: "⏳ CAPTCHA RICHIESTO", 3: "✅ APPROVATO",
                     4: "❌ RIGETTATO", 5: "🚫 PATTERN INVALIDO"}
    esito = status_labels.get(status, f"Sconosciuto ({status})")

    print(f"\n  ╔══════════════════════════════════════════════════════════╗")
    print(f"  ║             📢 RESOCONTO FINALE CLAIM #{claim_id:<3d}          ║")
    print(f"  ╠══════════════════════════════════════════════════════════╣")
    print(f"  ║ ESITO: {esito:<49s} ║")
    print(f"  ║ FraudScore: {fraud_score:>3d}/130 | Dispersione: {dispersione:<21d} ║")
    print(f"  ║ Tier Attuale: {tier_name:<46s} ║")
    print(f"  ╠──────────────────────────────────────────────────────────╣")
    print(f"  ║ COMPOSIZIONE FRAUDSCORE:                                 ║")
    print(f"  ║  • Base Tier ({tier_name:>8s}): {base_score:>+4d}                           ║")
    print(f"  ║  • Claim Rate ({claim_rate_pct:>5.1f}%):  {score_claim:>+4d}  ({total_claims}cl/{effective_swaps}sw)       ║")
    print(f"  ║  • Network Score:      {score_network:>+4d}  (BFS distanza bot)         ║")
    print(f"  ╠──────────────────────────────────────────────────────────╣")
    if status == 3:
        print(f"  ║ 💰 PAYOUT EROGATO: {payout_mevi:>10.4f} MEVI                    ║")
    elif status == 4:
        if profile[7]:
            debito = utils.from_wei(profile[8])
            print(f"  ║ 🚫 AVVISO: UTENTE IN BLACKLIST                          ║")
            print(f"  ║    Debito accumulato: {debito:>10.4f} MEVI                   ║")
        else:
            print(f"  ║ FraudScore supera soglia di rigetto (θreject)            ║")
    elif status == 2:
        print(f"  ║ ⏳ CAPTCHA richiesto! Invio risposta automatica...        ║")
    elif status == 5:
        print(f"  ║ Pattern invalidato da ≥70% degli oracle.                 ║")
    print(f"  ╚══════════════════════════════════════════════════════════╝\n")

    if status == 2:
        captcha_reason = _auto_captcha_claim(claim_id)
        mevi_final = mevi.functions.balanceOf(trader).call()
        payout_mevi = max(0.0, utils.from_wei(mevi_final - mevi_prima))
        info2 = ins.functions.getClaimInfo(claim_id).call()
        final_status = info2[1]
        if final_status == 3:
            return final_status, payout_mevi, ""
        reason = captcha_reason if captcha_reason else f"CAPTCHA non risolto (status={final_status})"
        utils.sim_log(f"[CLAIM #{claim_id}] {reason}", tag="CLAIM")
        return final_status, payout_mevi, reason

    if status == 4:
        reason = "RIGETTATO (FraudScore alto)"
        utils.sim_log(f"[CLAIM #{claim_id}] {reason} score={fraud_score}/130 tier={tier_name}", tag="CLAIM")
        return status, 0.0, reason
    if status == 5:
        reason = "PATTERN INVALIDO (>=70% oracle)"
        utils.sim_log(f"[CLAIM #{claim_id}] {reason}", tag="CLAIM")
        return status, 0.0, reason

    return status, payout_mevi, ""


def menu():
    print("\n" + "="*45)
    print("      🛡️  MEV INSURANCE CONSOLE 🛡️")
    print("="*45)
    print(" 1. info()              - Stato wallet e Tier")
    print(" 2. preventivo(val)     - Tabella comparativa Premi")
    print(" 3. swap_nudo(val)      - Swap standard (RISCHIOSO)")
    print(" 4. swap_protetto(val, liv) - Swap protetto (liv: 0,1,2)")
    print(" 5. set_fondi(u, m)     - Ricevi USDC e MEVI")
    print(" 6. skip_giorni(n)      - Viaggia nel futuro di n giorni")
    print(" 7. storico()           - Elenco swap")
    print(" 8. richiedi_platinum(max_swap) - Richiedi upgrade Platinum")
    print(" 9. rispondi_captcha()  - Rispondi alla sfida CAPTCHA")
    print("10. captcha_claim(id)   - Rispondi CAPTCHA di un claim")
    print("11. invia_eth(idx, eth) - Invia ETH nativo")
    print("12. fondi_pool_eth(eth) - Versa ETH nel pool oracle")
    print("="*45)
    print(" INFO LIVELLI: 0 = LOW (50%), 1 = MED (70%), 2 = HIGH (100%)")


def info():
    u_bal = utils.from_wei(usdc.functions.balanceOf(trader).call())
    m_bal = utils.from_wei(mevi.functions.balanceOf(trader).call())
    p = ins.functions.getUserProfile(trader).call()
    tiers = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]
    tier_name = tiers[p[0]]
    print(f"\n--- 👤 PROFILO TRADER ---")
    print(f"Wallet: {u_bal} USDC | {m_bal} MEVI")
    print(f"Livello: {tier_name} | Swaps: {p[2]}")
    print(f"Stato: {'🔴 BLACKLIST' if p[7] else '✅ ATTIVO'}")
    print("-" * 25)


def preventivo(u):
    val = utils.to_wei(u)
    livelli = {0: "LOW    (Rimborso 50%) ", 1: "MEDIUM (Rimborso 70%) ",
               2: "HIGH   (Rimborso 100%)"}
    print(f"\n--- 📊 TABELLA PREMI PER {u} USDC ---")
    for id_liv, nome in livelli.items():
        premio = ins.functions.getPremiumEstimate(val, id_liv).call()
        print(f"{nome} | Costo: {utils.from_wei(premio):.4f} MEVI")
    print("-" * 45)


def swap_nudo(u):
    val = utils.to_wei(u)
    stima_mevi = _estimate_mevi_out(val)

    mevi_prima = mevi.functions.balanceOf(trader).call()
    utils.send_tx(w3, usdc.functions.approve(amm.address, val), trader)
    receipt = utils.send_tx(w3, amm.functions.swap(usdc.address, val), trader)
    mevi_dopo = mevi.functions.balanceOf(trader).call()
    mevi_ricevuti = utils.from_wei(mevi_dopo - mevi_prima)
    victim_tx_hex = receipt['transactionHash'].hex()

    print(f"  ✅ Swap nudo {u} USDC | stima: {stima_mevi:.4f} MEVI | ricevuti: {mevi_ricevuti:.4f} MEVI")

    attack = _wait_and_check_attack(victim_tx_hex)
    if attack:
        loss = attack.get("loss_sandwich_mevi", 0.0)
        if loss == 0.0:
            loss = max(0.0, stima_mevi - mevi_ricevuti)
        print(f"  🚨 ATTACCO rilevato dal bot! Perdita: {loss:.4f} MEVI | non assicurato")
        utils.sim_log(
            f"TRADER {trader} | {u:.2f} USDC | prot: NESSUNA | "
            f"stima: {stima_mevi:.4f} MEVI | ricevuti: {mevi_ricevuti:.4f} MEVI | "
            f"ATTACCO: SI | perdita: {loss:.4f} MEVI | rimborso: NON ASSICURATO"
        )
    else:
        utils.sim_log(
            f"TRADER {trader} | {u:.2f} USDC | prot: NESSUNA | "
            f"stima: {stima_mevi:.4f} MEVI | ricevuti: {mevi_ricevuti:.4f} MEVI | "
            f"ATTACCO: NO"
        )

    _swap_log.append({
        'idx': len(_swap_log), 'tipo': 'nudo', 'usdc': u,
        'stima': stima_mevi, 'ricevuti': mevi_ricevuti,
        'tx': victim_tx_hex, 'attacked': attack is not None,
    })


def swap_protetto(u, livello=1):
    val = utils.to_wei(u)
    cov_name = _NOMI_COV[livello] if livello < 3 else str(livello)

    stima_mevi = _estimate_mevi_out(val)

    premio = ins.functions.getPremiumEstimate(val, livello).call()
    utils.send_tx(w3, mevi.functions.approve(ins.address, premio), trader)

    receipt_ins = utils.send_tx(
        w3,
        ins.functions.insuredSwap(val, livello),
        trader
    )

    swap_id = None
    for log_e in ins.events.SwapInsured().process_receipt(receipt_ins):
        swap_id = log_e['args']['swapId']

    premio_mevi = utils.from_wei(premio)

    mevi_prima = mevi.functions.balanceOf(trader).call()

    utils.send_tx(
        w3,
        usdc.functions.approve(amm.address, val),
        trader
    )

    receipt_swap = utils.send_tx(
        w3,
        amm.functions.swap(usdc.address, val),
        trader
    )

    mevi_dopo = mevi.functions.balanceOf(trader).call()
    mevi_ricevuti = utils.from_wei(mevi_dopo - mevi_prima)

    victim_tx_hex = receipt_swap['transactionHash'].hex()

    print(
        f"  ✅ Swap protetto ({cov_name}) | "
        f"premio: {premio_mevi:.4f} MEVI | "
        f"stima: {stima_mevi:.4f} MEVI | "
        f"ricevuti: {mevi_ricevuti:.4f} MEVI"
    )

    attack = _wait_and_check_attack(victim_tx_hex)

    entry = {
        'idx': len(_swap_log),
        'tipo': 'protetto',
        'usdc': u,
        'swap_id': swap_id,
        'livello': livello,
        'stima': stima_mevi,
        'ricevuti': mevi_ricevuti,
        'premio': premio_mevi,
        'tx': victim_tx_hex,
        'attacked': attack is not None,
        'claimed': False,
        'payout': 0.0,
    }

    _swap_log.append(entry)

    if attack:
        loss = attack.get("loss_sandwich_mevi", 0.0)
        if loss == 0.0:
            loss = max(0.0, stima_mevi - mevi_ricevuti)

        print(
            f"\n  🚨 ATTACCO rilevato! "
            f"Perdita: {loss:.4f} MEVI — auto-claim..."
        )

        status, payout, reason = _do_claim_from_log(
            swap_id,
            victim_tx_hex,
            attack,
            loss,
            u
        )

        entry['claimed'] = True
        entry['payout'] = payout

        if status == 3:
            log_line = (
                f"TRADER {trader} | "
                f"{u:.2f} USDC | "
                f"prot: {cov_name} | "
                f"premio: {premio_mevi:.4f} MEVI | "
                f"stima: {stima_mevi:.4f} MEVI | "
                f"ricevuti: {mevi_ricevuti:.4f} MEVI | "
                f"ATTACCO: SI | "
                f"perdita: {loss:.4f} MEVI | "
                f"rimborso: {payout:.4f} MEVI"
            )
        else:
            motivo = f" | motivo: {reason}" if reason else ""
            log_line = (
                f"TRADER {trader} | "
                f"{u:.2f} USDC | "
                f"prot: {cov_name} | "
                f"premio: {premio_mevi:.4f} MEVI | "
                f"stima: {stima_mevi:.4f} MEVI | "
                f"ricevuti: {mevi_ricevuti:.4f} MEVI | "
                f"ATTACCO: SI | "
                f"perdita: {loss:.4f} MEVI | "
                f"rimborso: NO{motivo}"
            )
    else:
        print(f"  ✅ Nessun attacco nel log.")
        log_line = (
            f"TRADER {trader} | "
            f"{u:.2f} USDC | "
            f"prot: {cov_name} | "
            f"premio: {premio_mevi:.4f} MEVI | "
            f"stima: {stima_mevi:.4f} MEVI | "
            f"ricevuti: {mevi_ricevuti:.4f} MEVI | "
            f"ATTACCO: NO"
        )

    utils.sim_log(log_line)


def storico():
    if not _swap_log:
        print("  Nessuno swap registrato.")
        return
    print(f"\n{'═'*72}")
    print(f"  # │ Tipo     │ USDC   │ Stima MEVI  │ Ricevuti    │ Protezione │ Stato")
    print(f"{'─'*72}")
    for e in _swap_log:
        tipo = e['tipo']
        cov = _NOMI_COV[e['livello']] if tipo == 'protetto' else "—"
        if e['attacked']:
            stato = f"ATTCK payout={e.get('payout',0):.4f}" if tipo == 'protetto' else "ATTCK no-ins"
        else:
            stato = "OK"
        print(f"  {e['idx']} │ {tipo:<8s} │ {e['usdc']:>6} │ "
              f"{e['stima']:>11.4f} │ {e['ricevuti']:>11.4f} │ {cov:>10s} │ {stato}")
    print(f"{'═'*72}")


def set_fondi(u, m):
    utils.send_tx(w3, usdc.functions.transfer(trader, utils.to_wei(u)), deployer)
    utils.send_tx(w3, mevi.functions.transfer(trader, utils.to_wei(m)), deployer)
    print(f"  ✅ Accreditati {u} USDC e {m} MEVI.")


def skip_giorni(n=1):
    secondi = n * 86400
    w3.provider.make_request("evm_increaseTime", [secondi])
    w3.provider.make_request("evm_mine", [])
    print(f"  ✅ Avanzati {n} giorno/i!")


_REVERSE_LEET = {'4': 'a', '3': 'e', '1': 'i', '0': 'o', '5': 's', '7': 't'}

def _decode_leetspeak(challenge):
    return ''.join(_REVERSE_LEET.get(ch, ch) for ch in challenge)


def _auto_captcha_claim(claim_id) -> str:
    print(f"\n  🤖 AUTO-CAPTCHA: attendo sfida per claim #{claim_id}...")
    for _ in range(90):  # 90s: oracle pubblica inline ma potrebbe esserci lag
        cc = ins.functions.getClaimCaptcha(claim_id).call()
        if cc[5]:
            break
        time.sleep(1)
    else:
        msg = f"CAPTCHA: sfida non pubblicata entro 90s (claim #{claim_id})"
        print(f"  ⚠️  {msg}")
        return msg
    cc = ins.functions.getClaimCaptcha(claim_id).call()
    if cc[4]:
        print(f"  ✅ CAPTCHA già risolto.")
        return ""
    if cc[6]:
        print(f"  ⚠️  Risposta già inviata, attendo verdetto...")
        ok = _attendi_captcha_claim(claim_id)
        return "" if ok else "CAPTCHA: timeout verdetto oracle"
    challenge_text = cc[1]
    risposta = _decode_leetspeak(challenge_text)
    print(f"  🧩 Sfida: '{challenge_text}' → Risposta: '{risposta}'")
    try:
        utils.send_tx(w3, ins.functions.submitClaimCaptchaAnswer(claim_id, risposta), trader)
        print(f"  ✅ Risposta inviata: '{risposta}'")
        ok = _attendi_captcha_claim(claim_id)
        return "" if ok else "CAPTCHA: timeout verdetto oracle"
    except Exception as e:
        msg = f"CAPTCHA: errore risposta ({str(e)[:60]})"
        print(f"  ❌ {msg}")
        return msg


def captcha_claim(claim_id):
    cc = ins.functions.getClaimCaptcha(claim_id).call()
    if cc[4]:
        print("  ✅ CAPTCHA già risolto.")
        return
    if not cc[5]:
        print("  ⏳ In attesa che l'oracle pubblichi la sfida...")
        for _ in range(60):
            cc = ins.functions.getClaimCaptcha(claim_id).call()
            if cc[5]:
                break
            time.sleep(1)
        else:
            print("  ⚠️  Timeout.")
            return
    cc = ins.functions.getClaimCaptcha(claim_id).call()
    if cc[6]:
        print("  ⚠️  Hai già risposto. In attesa verdetto...")
        _attendi_captcha_claim(claim_id)
        return
    challenge_text = cc[1]
    print(f"\n  🧩 SFIDA CAPTCHA per claim #{claim_id}: {challenge_text}")
    risposta = input("  ✏️  Decodifica la frase: ").strip()
    if not risposta:
        print("  ❌ Annullato.")
        return
    try:
        utils.send_tx(w3, ins.functions.submitClaimCaptchaAnswer(claim_id, risposta), trader)
        print(f"  ✅ Risposta inviata: '{risposta}'")
        _attendi_captcha_claim(claim_id)
    except Exception as e:
        print(f"  ❌ Errore: {e}")


def _attendi_captcha_claim(claim_id) -> bool:
    for _ in range(240):  # 240s: oracle deve rilevare evento + votare 2-of-3
        cc = ins.functions.getClaimCaptcha(claim_id).call()
        if cc[4]:
            info_claim = ins.functions.getClaimInfo(claim_id).call()
            status = info_claim[1]
            if status == 3:
                loss = utils.from_wei(info_claim[4])
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  ✅ CLAIM #{claim_id} APPROVATO VIA CAPTCHA!   ║")
                print(f"  ║  Payout erogato: {loss:.4f} MEVI          ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            else:
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  ❌ CLAIM #{claim_id} RIGETTATO VIA CAPTCHA    ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            return True
        time.sleep(1)
    print(f"  ⚠️  Timeout verdetto CAPTCHA (240s): claim #{claim_id} non risolto.")
    return False


def fondi_pool_eth(importo_eth):
    val = Web3.to_wei(importo_eth, 'ether')
    saldo_eth = w3.eth.get_balance(trader)
    if saldo_eth < val:
        print(f"  ❌ Saldo ETH insufficiente: {Web3.from_wei(saldo_eth, 'ether'):.4f} ETH")
        return
    tx_hash = w3.eth.send_transaction({'from': trader, 'to': ins.address, 'value': val, 'gas': 30000})
    w3.eth.wait_for_transaction_receipt(tx_hash)
    pool_bal = ins.functions.getPoolEthBalance().call()
    print(f"  ✅ Versati {importo_eth} ETH al pool oracle.")
    print(f"  Pool ETH: {Web3.from_wei(pool_bal, 'ether'):.6f} ETH")


def invia_eth(destinatario_idx, importo_eth):
    dest = w3.eth.accounts[destinatario_idx]
    val = Web3.to_wei(importo_eth, 'ether')
    tx_hash = w3.eth.send_transaction({'from': trader, 'to': dest, 'value': val, 'gas': 21000})
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  💸 Inviati {importo_eth} ETH a Account #{destinatario_idx} ({dest[:16]}...)")
    print(f"  ⚠️  Transazione visibile dal BFS degli oracle!")


def richiedi_platinum(max_swap_usdc):
    profile = ins.functions.getUserProfile(trader).call()
    tiers = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]
    if profile[0] == 3:
        print("  ⚠️  Sei già Platinum!")
        return
    alpha_bps = ins.functions.alphaStakeBps().call()
    desired_max = utils.to_wei(max_swap_usdc)
    stake_needed = (desired_max * alpha_bps) // 10000
    stake_eth = Web3.from_wei(stake_needed, 'ether')
    print(f"\n  🏆 RICHIESTA UPGRADE PLATINUM")
    print(f"  Tier attuale: {tiers[profile[0]]}")
    print(f"  Max swap desiderato: {max_swap_usdc} MEVI | Stake: {stake_eth} ETH")
    try:
        utils.send_tx(w3, ins.functions.requestPlatinum(desired_max), trader, value=stake_needed)
        print(f"  ✅ Richiesta inviata! Stake: {stake_eth} ETH")
        print(f"  ⏳ Attendo sfida CAPTCHA...")
        _auto_captcha_platinum()
    except Exception as e:
        print(f"  ❌ Errore: {e}")


def _auto_captcha_platinum():
    print(f"\n  🤖 AUTO-CAPTCHA: attendo sfida Platinum...")
    for _ in range(60):
        req = ins.functions.getPlatinumRequest(trader).call()
        if req[7]:
            break
        time.sleep(1)
    else:
        print(f"  ⚠️  Timeout CAPTCHA. Usa rispondi_captcha() manualmente.")
        return
    req = ins.functions.getPlatinumRequest(trader).call()
    if req[6]:
        print(f"  ✅ Richiesta già risolta.")
        return
    if req[8]:
        print(f"  ⚠️  Risposta già inviata, attendo verdetto...")
        _attendi_verdetto_platinum()
        return
    challenge_text = req[3]
    risposta = _decode_leetspeak(challenge_text)
    print(f"  🧩 Sfida: '{challenge_text}' → Risposta: '{risposta}'")
    try:
        utils.send_tx(w3, ins.functions.submitCaptchaAnswer(risposta), trader)
        print(f"  ✅ Risposta inviata: '{risposta}'")
        _attendi_verdetto_platinum()
    except Exception as e:
        print(f"  ❌ Errore auto-captcha platinum: {e}")


def rispondi_captcha():
    req = ins.functions.getPlatinumRequest(trader).call()
    if req[6]:
        print("  🏆 Sei già Platinum!" if req[4] >= 2 else "  ❌ Richiesta già risolta (negata).")
        return
    if not req[7]:
        print("  ⚠️  Nessuna sfida CAPTCHA disponibile.")
        return
    if req[8]:
        print("  ⚠️  Hai già risposto. Attendo verdetto...")
        _attendi_verdetto_platinum()
        return
    challenge_text = req[3]
    print(f"\n  🧩 SFIDA: {challenge_text}")
    risposta = input("  ✏️  Decodifica la frase: ").strip()
    if not risposta:
        print("  ❌ Risposta vuota.")
        return
    try:
        utils.send_tx(w3, ins.functions.submitCaptchaAnswer(risposta), trader)
        print(f"  ✅ Risposta inviata: '{risposta}'")
        _attendi_verdetto_platinum()
    except Exception as e:
        print(f"  ❌ Errore: {e}")


def _attendi_verdetto_platinum():
    for _ in range(120):
        req = ins.functions.getPlatinumRequest(trader).call()
        if req[6]:
            approve = req[4]
            reject  = req[5]
            profile = ins.functions.getUserProfile(trader).call()
            if approve >= 2:
                alpha_bps = ins.functions.alphaStakeBps().call()
                max_swap = utils.from_wei((req[1] * 10000) // alpha_bps)
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  🏆 UPGRADE A PLATINUM CONFERMATO!       ║")
                print(f"  ║  Voti: {approve} approve / {reject} reject           ║")
                print(f"  ║  Max swap assicurabile: {max_swap:.0f} MEVI   ║")
                print(f"  ║  Stake bloccato: {Web3.from_wei(req[1], 'ether'):.4f} ETH       ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            else:
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  ❌ UPGRADE NEGATO                       ║")
                print(f"  ║  Voti: {approve} approve / {reject} reject           ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            return
        time.sleep(1)
    print(f"  ⚠️  Timeout verdetto Platinum.")


def _auto_refund():
    u_bal = utils.from_wei(usdc.functions.balanceOf(trader).call())
    m_bal = utils.from_wei(mevi.functions.balanceOf(trader).call())
    refunded = False
    if u_bal < 500:
        utils.send_tx(w3, usdc.functions.transfer(trader, utils.to_wei(1000)), deployer)
        print(f"  [AUTO] Refund USDC: {u_bal:.2f} < 500 → +1000")
        refunded = True
    if m_bal < 500:
        utils.send_tx(w3, mevi.functions.transfer(trader, utils.to_wei(1000)), deployer)
        print(f"  [AUTO] Refund MEVI: {m_bal:.2f} < 500 → +1000")
        refunded = True
    return refunded


def run_auto_naked_mode(interval_sec, amount_min, amount_max, tx_count):
    print(f"  [AUTO] Trader NUDO | account={trader[:16]}... | "
          f"ogni {interval_sec}s | {amount_min}-{amount_max} USDC")
    done = 0
    while tx_count == 0 or done < tx_count:
        try:
            _auto_refund()
            amount = round(random.uniform(amount_min, amount_max), 2)
            print(f"  [AUTO] TX #{done+1}: swap_nudo({amount})")
            swap_nudo(amount)
            done += 1
        except KeyboardInterrupt:
            print(f"  [AUTO] Fermato dopo {done} TX.")
            break
        except Exception as e:
            err_str = str(e).lower()
            if "revert" in err_str or "status 0" in err_str:
                print(f"  [AUTO] Revert AMM — ricarico pool...")
            else:
                print(f"  [AUTO] Errore: {str(e)[:80]}. Salto TX.")
        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            print(f"  [AUTO] Fermato dopo {done} TX.")
            break


def run_auto_mode(interval_sec, amount_min, amount_max, coverage, tx_count):
    cov_name = _NOMI_COV[coverage] if coverage < 3 else str(coverage)
    print(f"  [AUTO] Trader PROTETTO | account={trader[:16]}... | "
          f"ogni {interval_sec}s | {amount_min}-{amount_max} USDC | copertura {cov_name}")

    done = 0
    blocked_day = None

    while tx_count == 0 or done < tx_count:
        if blocked_day is not None:
            try:
                current_day = w3.eth.get_block('latest')['timestamp'] // 86400
            except Exception:
                current_day = blocked_day
            if current_day <= blocked_day:
                print(f"  [AUTO] Tier limit — pausa fino al giorno {blocked_day + 1}")
                try:
                    time.sleep(interval_sec)
                except KeyboardInterrupt:
                    print(f"  [AUTO] Fermato dopo {done} TX.")
                    return
                continue
            else:
                print(f"  [AUTO] Nuovo giorno blockchain ({current_day}). Riprendo.")
                blocked_day = None

        try:
            _auto_refund()
            amount = round(random.uniform(amount_min, amount_max), 2)
            print(f"  [AUTO] TX #{done+1}: swap_protetto({amount}, {coverage})")
            swap_protetto(amount, coverage)
            done += 1

        except KeyboardInterrupt:
            print(f"  [AUTO] Fermato dopo {done} TX.")
            break
        except Exception as e:
            err_str = str(e).lower()
            if any(kw in err_str for kw in ["tier limit", "daily", "exceed", "blacklist",
                                             "execution reverted", "revert"]):
                try:
                    blocked_day = w3.eth.get_block('latest')['timestamp'] // 86400
                except Exception:
                    blocked_day = 0
                print(f"  [AUTO] Revert ('{str(e)[:80]}'). Pausa fino al giorno {blocked_day + 1}.")
            else:
                print(f"  [AUTO] Errore TX #{done+1}: {e}")

        if tx_count == 0 or done < tx_count:
            try:
                time.sleep(interval_sec)
            except KeyboardInterrupt:
                print(f"  [AUTO] Fermato dopo {done} TX.")
                break

    print(f"  [AUTO] Completato: {done} swap.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MEV Insurance Trader")
    parser.add_argument('account_pos', nargs='?', type=int, default=None)
    parser.add_argument('--account', type=int, default=None)
    parser.add_argument('--usdc', type=float, default=None)
    parser.add_argument('--mevi', type=float, default=None)
    parser.add_argument('--auto', action='store_true')
    parser.add_argument('--interval', type=float, default=60.0)
    parser.add_argument('--amount-min', type=float, default=50.0)
    parser.add_argument('--amount-max', type=float, default=150.0)
    parser.add_argument('--coverage', type=int, default=2, choices=[0, 1, 2])
    parser.add_argument('--count', type=int, default=0)
    parser.add_argument('--naked', action='store_true')
    args = parser.parse_args()

    if args.usdc is not None or args.mevi is not None:
        set_fondi(args.usdc or 0, args.mevi or 0)

    if args.auto and args.naked:
        run_auto_naked_mode(args.interval, args.amount_min, args.amount_max, args.count)
    elif args.auto:
        run_auto_mode(args.interval, args.amount_min, args.amount_max,
                      args.coverage, args.count)
    else:
        menu()
