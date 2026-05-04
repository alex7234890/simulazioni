import utils
import time
import random
import argparse
import json
import os
from pathlib import Path
from web3 import Web3
import sys

_ROOT = Path(__file__).parent.parent
_ATTACKED_FILE  = _ROOT / "config" / "attacked_txs.json"
_VICTIMS_FILE   = _ROOT / "config" / "victims.json"
_ATTACK_LOG     = _ROOT / "config" / "attack_log.json"
_NAKED_STATS    = _ROOT / "config" / "naked_stats.json"


def _decode_amm_amount(tx_input) -> int:
    try:
        data = bytes(tx_input)
        if len(data) < 68:
            return 0
        return int.from_bytes(data[36:68], 'big')
    except Exception:
        return 0


def _compute_sandwich_loss(amm, victim_amount_wei: int, bot_amount_usdc: float) -> tuple:
    try:
        reserve_mevi = amm.functions.reserveA().call()
        reserve_usdc = amm.functions.reserveB().call()
        if reserve_mevi == 0 or reserve_usdc == 0 or victim_amount_wei == 0:
            return 0.0, 0.0, 0

        bot_wei = int(bot_amount_usdc * 1e18)
        k = reserve_usdc * reserve_mevi

        out_no_attack = reserve_mevi - k // (reserve_usdc + victim_amount_wei)

        ri_after = reserve_usdc + bot_wei
        ro_after = k // ri_after
        k2 = ri_after * ro_after
        out_with_attack = ro_after - k2 // (ri_after + victim_amount_wei)

        loss_wei = max(0, out_no_attack - out_with_attack)
        loss_pct = (loss_wei / out_no_attack * 100.0) if out_no_attack > 0 else 0.0
        return float(loss_wei) / 1e18, loss_pct, out_no_attack
    except Exception:
        return 0.0, 0.0, 0


def _actual_victim_loss(w3, amm, victim_addr: str, frontrun_block: int,
                        out_no_attack_wei: int) -> float:
    if out_no_attack_wei <= 0:
        return 0.0
    try:
        end_block = w3.eth.block_number
        start_block = max(0, frontrun_block - 1)
        try:
            evs = amm.events.Swap().get_logs(from_block=start_block, to_block=end_block)
        except TypeError:
            evs = amm.events.Swap().get_logs(fromBlock=start_block, toBlock=end_block)
        victim_lower = victim_addr.lower()
        for ev in reversed(evs):
            if ev['args']['sender'].lower() == victim_lower:
                actual_out = ev['args']['amountOut']
                loss = max(0, out_no_attack_wei - actual_out)
                return float(loss) / 1e18
    except Exception:
        pass
    return 0.0


def _load_attack_log() -> dict:
    try:
        if _ATTACK_LOG.exists():
            with open(_ATTACK_LOG, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_attack_log(log: dict) -> None:
    try:
        _ATTACK_LOG.parent.mkdir(exist_ok=True)
        tmp = str(_ATTACK_LOG) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(log, f, indent=2)
        os.replace(tmp, str(_ATTACK_LOG))
    except Exception:
        pass


def _append_attack(tx_hash: str, victim: str, swap_amount_wei: int,
                   is_insured: bool, bot_profit_usdc: float, bot_addr: str,
                   loss_sandwich_mevi: float = 0.0, loss_pct: float = 0.0,
                   premium_mevi: float = 0.0,
                   frontrun_tx: str = "", backrun_tx: str = "") -> None:
    log = _load_attack_log()
    swap_usdc = swap_amount_wei / 1e18 if swap_amount_wei > 0 else 0
    log[tx_hash.lower()] = {
        "victim":               victim,
        "swap_amount_wei":      swap_amount_wei,
        "swap_amount_usdc":     round(swap_usdc, 4),
        "is_insured":           is_insured,
        "bot_profit_usdc":      round(bot_profit_usdc, 4),
        "loss_sandwich_mevi":   round(loss_sandwich_mevi, 6),
        "loss_pct":             round(loss_pct, 2),
        "premium_mevi":         round(premium_mevi, 6),
        "timestamp":            time.strftime("%H:%M:%S"),
        "bot":                  bot_addr,
        "frontrun_tx":          frontrun_tx,
        "backrun_tx":           backrun_tx,
    }
    _save_attack_log(log)


def _load_naked_stats() -> dict:
    try:
        if _NAKED_STATS.exists():
            with open(_NAKED_STATS, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"naked_seen": 0, "insured_seen": 0}


def _update_naked_stats(naked_delta: int = 0, insured_delta: int = 0) -> None:
    try:
        stats = _load_naked_stats()
        stats["naked_seen"]   += naked_delta
        stats["insured_seen"] += insured_delta
        tmp = str(_NAKED_STATS) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stats, f)
        os.replace(tmp, str(_NAKED_STATS))
    except Exception:
        pass

def _load_attacked() -> set:
    try:
        if _ATTACKED_FILE.exists():
            with open(_ATTACKED_FILE, "r") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def _save_attacked(attacked: set) -> None:
    try:
        _ATTACKED_FILE.parent.mkdir(exist_ok=True)
        tmp = str(_ATTACKED_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(attacked), f)
        os.replace(tmp, str(_ATTACKED_FILE))
    except Exception:
        pass

def _mark_attacked(tx_hash_hex: str) -> None:
    attacked = _load_attacked()
    attacked.add(tx_hash_hex)
    _save_attacked(attacked)

def _record_victim(tx_hash_hex: str, victim_addr: str) -> None:
    try:
        victims: dict = {}
        if _VICTIMS_FILE.exists():
            with open(_VICTIMS_FILE, "r") as f:
                victims = json.load(f)
        victims[tx_hash_hex.lower()] = victim_addr.lower()
        tmp = str(_VICTIMS_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(victims, f)
        os.replace(tmp, str(_VICTIMS_FILE))
    except Exception:
        pass

def setup_bot():
    print("\n" + "="*45)
    print("      🤖 CONFIGURAZIONE MEV-BOT 🤖")
    print("="*45)
    idx = int(input("Indice account Hardhat (default 1): ") or "1")
    u_amount = float(input("Quanti USDC dare al bot? (es. 10000): ") or "10000")
    m_amount = float(input("Quanti MEVI dare al bot? (es. 1000): ") or "1000")
    max_tx = float(input("Max USDC per ogni attacco (es. 100): ") or "100")
    attack_rate = float(input("Percentuale attacchi 0-100% (default 10): ") or "10") / 100.0
    return idx, u_amount, m_amount, max_tx, attack_rate

def bot_info(w3, bot_acc, contracts):
    usdc = contracts["MockUSDC"]
    mevi = contracts["MEVToken"]
    u_bal = utils.from_wei(usdc.functions.balanceOf(bot_acc).call())
    m_bal = utils.from_wei(mevi.functions.balanceOf(bot_acc).call())

    print(f"\n--- 💰 STATO FINANZE BOT ---")
    print(f"Wallet: {u_bal:.2f} USDC | {m_bal:.2f} MEVI")
    print("-" * 28)

def set_fondi_bot(w3, bot_acc, deployer, u_val, m_val, contracts):
    usdc = contracts["MockUSDC"]
    mevi = contracts["MEVToken"]
    amm = contracts["MockAMM"]

    print(f"[⚙️] Preparazione fondi e APPROVE...")
    utils.send_tx(w3, usdc.functions.transfer(bot_acc, utils.to_wei(u_val)), deployer)
    utils.send_tx(w3, mevi.functions.transfer(bot_acc, utils.to_wei(m_val)), deployer)

    allowance = w3.to_wei(1000000, 'ether')
    nonce = w3.eth.get_transaction_count(bot_acc)

    tx_usdc = usdc.functions.approve(amm.address, allowance).build_transaction({
        'from': bot_acc,
        'nonce': nonce,
        'gas': 100000,
        'gasPrice': w3.eth.gas_price
    })
    w3.eth.send_transaction(tx_usdc)

    tx_mevi = mevi.functions.approve(amm.address, allowance).build_transaction({
        'from': bot_acc,
        'nonce': nonce + 1,
        'gas': 100000,
        'gasPrice': w3.eth.gas_price
    })
    w3.eth.send_transaction(tx_mevi)

    print(f"[✅] Bot fundato e autorizzato correttamente!")

def execute_sandwich(w3, contracts, bot_acc, victim_tx, max_amount):
    amm = contracts["MockAMM"]
    usdc = contracts["MockUSDC"]
    mev_token = contracts["MEVToken"]

    v_gas_price = victim_tx.get('gasPrice') or victim_tx.get('maxFeePerGas')
    gas_front = int(v_gas_price * 1.2)
    gas_back = max(1, int(v_gas_price * 0.9))

    nonce = w3.eth.get_transaction_count(bot_acc)

    print(f"[*] Esecuzione Sandwich...")
    try:
        front_amount_wei = utils.to_wei(max_amount)

        # Calcola MEVI che il frontrun riceverà usando getAmountOut on-chain.
        # Necessario perché il prezzo MEVI/USDC drifa col pool — usare max_amount*0.99
        # causa backrun revert (si tenta di vendere più MEVI di quanti acquistati).
        mevi_out_wei = amm.functions.getAmountOut(usdc.address, front_amount_wei).call()
        if mevi_out_wei <= 0:
            print(f"[⚠️] getAmountOut = 0 — skip sandwich")
            return None, None
        backrun_wei = int(mevi_out_wei * 0.99)

        tx_f = amm.functions.swap(usdc.address, front_amount_wei).build_transaction({
            'from': bot_acc,
            'gasPrice': gas_front,
            'nonce': nonce,
            'gas': 300000
        })
        tx_b = amm.functions.swap(mev_token.address, backrun_wei).build_transaction({
            'from': bot_acc,
            'gasPrice': gas_back,
            'nonce': nonce + 1,
            'gas': 300000
        })

        hash_f = w3.eth.send_transaction(tx_f)
        hash_b = w3.eth.send_transaction(tx_b)

        print(f"[🚀] Raffiche inviate! Attendo che il blocco le catturi...")

        receipt_f = w3.eth.wait_for_transaction_receipt(hash_f)
        receipt_b = w3.eth.wait_for_transaction_receipt(hash_b)

        ok_f = receipt_f.get('status', 1) == 1
        ok_b = receipt_b.get('status', 1) == 1
        if ok_f and ok_b:
            print(f"[✅] Sandwich minato con successo!")
            return hash_f.hex(), hash_b.hex()
        else:
            print(f"[⚠️] Sandwich parziale: frontrun={'OK' if ok_f else 'REVERT'}, "
                  f"backrun={'OK' if ok_b else 'REVERT'}")
            return (hash_f.hex() if ok_f else ""), (hash_b.hex() if ok_b else "")

    except Exception as e:
        print(f"[❌] Errore esecuzione: {e}")
        return None, None

def _read_patt_from_chain(contracts) -> float:
    try:
        bps = contracts["PremiumCalculator"].functions.patt().call()
        return bps / 10000.0
    except Exception:
        return None


def monitor_mempool(bot_idx=None, u_start=None, m_start=None,
                    max_tx_usdc=None, attack_rate=0.05, interactive=True):
    utils.wait_for_deploy()
    w3 = utils.get_web3()
    contracts = utils.get_all_contracts(w3)
    deployer = w3.eth.accounts[0]
    amm = contracts["MockAMM"]
    usdc = contracts["MockUSDC"]

    if interactive:
        bot_idx, u_start, m_start, max_tx_usdc, attack_rate = setup_bot()

    bot_acc = w3.eth.accounts[bot_idx]

    def _load_known_bots() -> set:
        actors = utils.load_actors()
        bots = {a.lower() for a in actors.get("bots", [])}
        bots.add(deployer.lower())
        bots.add(bot_acc.lower())
        return bots

    known_bots: set = _load_known_bots()
    _bots_reload_counter = 0

    # Chiama set_fondi_bot solo se è necessario finanziare il bot.
    # L'orchestratore passa --usdc 0 --mevi 0 quando ha già pre-fondato.
    if u_start and u_start > 0 or m_start and m_start > 0:
        set_fondi_bot(w3, bot_acc, deployer, u_start, m_start, contracts)
    else:
        print(f"[ℹ️] Bot già pre-fondato dall'orchestratore — skip setup fondi.")
    bot_info(w3, bot_acc, contracts)

    ins_contract = contracts.get("MEVInsurance")
    try:
        import json as _json
        with open(_ROOT / "config" / "deployed_addresses.json") as _f:
            _addrs = _json.load(_f)
        insurance_addr = _addrs.get("MEVInsurance", "").lower()
    except Exception:
        insurance_addr = ""

    insured_recent: dict = {}
    _last_event_poll_block: int = w3.eth.block_number

    def _poll_insured_events(from_block: int) -> int:
        if ins_contract is None:
            return from_block
        try:
            to_block = w3.eth.block_number
            if to_block < from_block:
                return from_block
            try:
                evs = ins_contract.events.SwapInsured().get_logs(
                    from_block=from_block, to_block=to_block)
            except TypeError:
                evs = ins_contract.events.SwapInsured().get_logs(
                    fromBlock=from_block, toBlock=to_block)
            for ev in evs:
                user = ev["args"]["user"].lower()
                blk  = ev["blockNumber"]
                prev = insured_recent.get(user, {})
                if blk > prev.get("block", 0):
                    insured_recent[user] = {
                        "block":       blk,
                        "premium_wei": ev["args"].get("premiumPaid", 0),
                    }
            return to_block
        except Exception:
            return from_block

    def _classify_insured(sender: str, current_block: int) -> bool:
        info = insured_recent.get(sender.lower(), {})
        last = info.get("block", 0)
        return last > 0 and (current_block - last) <= 10

    # patt on-chain è il parametro di premio, NON cambia attack_rate del bot
    chain_patt = _read_patt_from_chain(contracts)
    if chain_patt is not None:
        print(f"[📡] patt premio on-chain: {int(chain_patt*10000)} bps ({chain_patt*100:.1f}%) "
              f"[attack_rate bot = {attack_rate*100:.0f}% — indipendente]")
    pct = int(attack_rate * 100)
    utils.log(f"MEV BOT ATTIVO su {bot_acc} | attack-rate={int(attack_rate*10000)}bps ({pct}%) | max_swap={max_tx_usdc} USDC", "BOT")
    print(f"\n[🚀] MEV BOT ATTIVO su {bot_acc}")
    print(f"[🔍] In ascolto della MEMPOOL... (attacco {pct}% degli swap visibili — naked+insured)")

    seen_hashes: set = set()
    last_clean_block = w3.eth.block_number
    # Contatori separati per gruppo: garantisce attack_rate identico su insured e naked
    attacked_ins = 0
    attacked_nak = 0
    eligible_ins = 0
    eligible_nak = 0
    skipped = 0

    while True:
        try:
            pending_txs = []
            try:
                pblock = w3.eth.get_block('pending', full_transactions=True)
                pending_txs = list(pblock.get('transactions', []))
            except Exception:
                try:
                    pf = w3.eth.filter('pending')
                    for h in pf.get_new_entries():
                        try:
                            t = w3.eth.get_transaction(h)
                            if t:
                                pending_txs.append(t)
                        except Exception:
                            pass
                except Exception:
                    pass

            cur_block = w3.eth.block_number
            if cur_block > last_clean_block + 30:
                seen_hashes.clear()
                last_clean_block = cur_block

            _bots_reload_counter += 1
            if _bots_reload_counter % 50 == 0:
                known_bots = _load_known_bots()

            if cur_block > _last_event_poll_block:
                _last_event_poll_block = _poll_insured_events(_last_event_poll_block)

            amm_addr = amm.address.lower()

            for tx in pending_txs:
                try:
                    tx_hex = tx['hash'].hex() if hasattr(tx['hash'], 'hex') else str(tx['hash'])

                    if tx_hex in seen_hashes:
                        continue

                    if tx.get('blockNumber') is not None:
                        seen_hashes.add(tx_hex)
                        continue

                    if not tx.get('to') or tx['to'].lower() != amm_addr:
                        continue

                    if tx['from'].lower() in known_bots:
                        continue

                    seen_hashes.add(tx_hex)

                    if tx_hex in _load_attacked():
                        utils.log(f"Skip {tx_hex[:16]}... (già attaccata)", "BOT")
                        continue

                    is_insured = _classify_insured(tx['from'], cur_block)
                    kind = "insured" if is_insured else "NAKED"

                    # Campionamento deterministico per gruppo: ogni gruppo riceve
                    # esattamente attack_rate% di attacchi indipendentemente dall'altro
                    if is_insured:
                        eligible_ins += 1
                        should_attack = attacked_ins < eligible_ins * attack_rate
                    else:
                        eligible_nak += 1
                        should_attack = attacked_nak < eligible_nak * attack_rate

                    if not should_attack:
                        skipped += 1
                        _update_naked_stats(
                            naked_delta=0 if is_insured else 1,
                            insured_delta=1 if is_insured else 0
                        )
                        attacked_tot = attacked_ins + attacked_nak
                        elig_tot = eligible_ins + eligible_nak
                        continue

                    u_prima = utils.from_wei(usdc.functions.balanceOf(bot_acc).call())
                    attacked_tot = attacked_ins + attacked_nak + 1
                    print(f"\n[!] BERSAGLIO [{kind}]: {tx_hex}")

                    _mark_attacked(tx_hex)
                    _record_victim(tx_hex, tx['from'])
                    swap_amount_wei = _decode_amm_amount(tx.get('input', b''))

                    loss_mevi_pred, loss_pct, out_no_attack_wei = _compute_sandwich_loss(
                        amm, swap_amount_wei, max_tx_usdc)

                    frontrun_block = w3.eth.block_number

                    hash_f_hex, hash_b_hex = execute_sandwich(
                        w3, contracts, bot_acc, tx, max_tx_usdc)

                    if is_insured:
                        attacked_ins += 1
                    else:
                        attacked_nak += 1

                    time.sleep(0.5)

                    loss_mevi = _actual_victim_loss(
                        w3, amm, tx['from'], frontrun_block, out_no_attack_wei)
                    if loss_mevi <= 0:
                        loss_mevi = loss_mevi_pred
                    loss_pct = (loss_mevi / (out_no_attack_wei / 1e18) * 100.0) if out_no_attack_wei > 0 else loss_pct

                    bot_info(w3, bot_acc, contracts)
                    u_dopo = utils.from_wei(usdc.functions.balanceOf(bot_acc).call())
                    profitto = u_dopo - u_prima

                    _victim_info = insured_recent.get(tx['from'].lower(), {})
                    _premium_mevi = _victim_info.get("premium_wei", 0) / 1e18
                    _append_attack(tx_hex, tx['from'], swap_amount_wei,
                                   is_insured, profitto, bot_acc,
                                   loss_sandwich_mevi=loss_mevi, loss_pct=loss_pct,
                                   premium_mevi=_premium_mevi,
                                   frontrun_tx=hash_f_hex or "",
                                   backrun_tx=hash_b_hex or "")
                    _update_naked_stats(
                        naked_delta=0 if is_insured else 1,
                        insured_delta=1 if is_insured else 0
                    )

                    attacked_tot = attacked_ins + attacked_nak
                    print(f"[💎] PROFITTO NETTO: {profitto:.4f} USDC")

                    if swap_amount_wei > 0:
                        swap_usdc = swap_amount_wei / 1e18
                        label = "INSURED" if is_insured else "NAKED"
                        emoji = "🟢" if is_insured else "🔴"
                        print(f"[{emoji}] {label} ATTACKED — perdita sandwich victim: "
                              f"{loss_mevi:.4f} MEVI ({loss_pct:.2f}% di {swap_usdc:.4f} USDC swappati)")
                        utils.log(
                            f"{label} ATTACK | victim={tx['from'][:16]} | "
                            f"swap={swap_usdc:.4f} USDC | loss_sandwich={loss_mevi:.4f} MEVI ({loss_pct:.2f}%)",
                            "ATTACK"
                        )

                    # Un solo attacco per ciclo
                    break

                except Exception:
                    continue

            time.sleep(0.1)

        except KeyboardInterrupt:
            utils.log(f"MEV Bot fermato. Attacchi: {attacked_ins + attacked_nak}, Skip: {skipped}", "BOT")
            break
        except Exception as e:
            if "not found" not in str(e).lower():
                print(f"[❌] Errore monitoraggio: {e}")
            continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MEV Sandwich Bot")
    parser.add_argument('--account', type=int, default=None,
                        help="Indice account Hardhat")
    parser.add_argument('--usdc', type=float, default=None,
                        help="USDC iniziali")
    parser.add_argument('--mevi', type=float, default=None,
                        help="MEVI iniziali")
    parser.add_argument('--max-swap', type=float, default=None,
                        help="Max USDC per attacco")
    parser.add_argument('--attack-rate', type=float, default=None,
                        help="Percentuale attacchi 0-100 (default: 5)")
    args = parser.parse_args()

    if all(x is not None for x in [args.account, args.usdc, args.mevi, args.max_swap]):
        rate = (args.attack_rate / 100.0) if args.attack_rate is not None else 0.05
        monitor_mempool(
            bot_idx=args.account,
            u_start=args.usdc,
            m_start=args.mevi,
            max_tx_usdc=args.max_swap,
            attack_rate=rate,
            interactive=False
        )
    else:
        monitor_mempool(interactive=True)
