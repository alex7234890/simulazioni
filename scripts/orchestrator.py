"""
MEV Insurance Simulation Orchestrator
======================================
Configura e avvia tutti i componenti della simulazione in modo automatico.

Uso:  python scripts/orchestrator.py
"""

import os
import sys
import json
import time
import math
import random
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = ROOT / "scripts"
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"

sys.path.insert(0, str(SCRIPTS_DIR))

# ──────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────

_CYAN   = "\033[96m"
_YELLOW = "\033[93m"
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _c(text, color): return f"{color}{text}{_RESET}"

def banner():
    print(_c("""
╔══════════════════════════════════════════════════════════════╗
║         MEV INSURANCE — SIMULATION ORCHESTRATOR             ║
╚══════════════════════════════════════════════════════════════╝
""", _BOLD + _CYAN))

def section(title):
    print(f"\n{_BOLD}{_YELLOW}{'─'*60}{_RESET}")
    print(f"{_BOLD}{_YELLOW}  {title}{_RESET}")
    print(f"{_BOLD}{_YELLOW}{'─'*60}{_RESET}")

def ask(prompt, default=None, cast=str, choices=None):
    suffix = f" [{default}]" if default is not None else ""
    if choices:
        suffix += f" ({'/'.join(str(c) for c in choices)})"
    while True:
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return default
        if not raw:
            return default
        try:
            val = cast(raw)
            if choices and val not in choices:
                print(f"  ⚠️  Scelta non valida. Opzioni: {choices}")
                continue
            return val
        except (ValueError, TypeError):
            print(f"  ⚠️  Valore non valido, riprova.")

def ask_yn(prompt, default=True):
    suffix = "[S/n]" if default else "[s/N]"
    try:
        raw = input(f"  {prompt} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not raw:
        return default
    return raw in ('s', 'si', 'y', 'yes', '1')

def launch_window(title, cmd_parts, env_extra=None):
    """Lancia un comando in una nuova finestra CMD su Windows."""
    env_prefix = ""
    if env_extra:
        for k, v in env_extra.items():
            env_prefix += f"set {k}={v} && "
    cmd_str = " ".join(str(p) for p in cmd_parts)
    cwd = str(ROOT)
    full = f'start "{title}" cmd /k "cd /d {cwd} && {env_prefix}{cmd_str}"'
    os.system(full)

def wait_for_node(timeout=180):
    print(f"  ⏳ Attendo avvio nodo Hardhat...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545",
                                        request_kwargs={"timeout": 3}))
            if w3.is_connected():
                print(f" {_c('OK', _GREEN)}")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(2)
    print(f" {_c('TIMEOUT', _RED)}")
    return False

def wait_for_deploy(timeout=180):
    deployed_file = CONFIG_DIR / "deployed_addresses.json"
    required = {"MEVToken", "OracleRegistry", "MEVInsurance", "MockAMM"}
    print(f"  ⏳ Attendo deploy contratti...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if deployed_file.exists():
            try:
                with open(deployed_file) as f:
                    data = json.load(f)
                if required.issubset(data.keys()):
                    print(f" {_c('OK', _GREEN)}")
                    return True
            except Exception:
                pass
        print(".", end="", flush=True)
        time.sleep(3)
    print(f" {_c('TIMEOUT', _RED)}")
    return False

def connect_web3():
    import utils as u
    w3 = u.get_web3()
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

# ──────────────────────────────────────────────────────────────
#  PRESET SIMULAZIONE BASE
# ──────────────────────────────────────────────────────────────
#
#  Layout account Hardhat (25 disponibili):
#    0          → deployer
#    1, 2, 3    → bot MEV #0, #1, #2
#    4–8        → trader #0–#4
#    9–20       → oracle #0–#11
#
BASE_PRESET = {
    'block_ms': 1500,
    'traders': [
        {'account_idx': 4,  'usdc': 1000.0, 'mevi': 500.0, 'auto': True,
         'interval': 15, 'amount_min': 50.0, 'amount_max': 150.0, 'coverage': 2, 'tx_count': 0},
        {'account_idx': 5,  'usdc': 1000.0, 'mevi': 500.0, 'auto': True,
         'interval': 15, 'amount_min': 50.0, 'amount_max': 150.0, 'coverage': 2, 'tx_count': 0},
        {'account_idx': 6,  'usdc': 1000.0, 'mevi': 500.0, 'auto': True,
         'interval': 15, 'amount_min': 50.0, 'amount_max': 150.0, 'coverage': 2, 'tx_count': 0},
        {'account_idx': 7,  'usdc': 1000.0, 'mevi': 500.0, 'auto': True,
         'interval': 15, 'amount_min': 50.0, 'amount_max': 150.0, 'coverage': 2, 'tx_count': 0},
        {'account_idx': 8,  'usdc': 1000.0, 'mevi': 500.0, 'auto': True,
         'interval': 15, 'amount_min': 50.0, 'amount_max': 150.0, 'coverage': 2, 'tx_count': 0},
    ],
    'bots': [
        {'account_idx': 1, 'usdc': 10000.0, 'mevi': 1000.0, 'max_swap': 100.0, 'attack_rate': 0.05},
        {'account_idx': 2, 'usdc': 10000.0, 'mevi': 1000.0, 'max_swap': 100.0, 'attack_rate': 0.05},
        {'account_idx': 3, 'usdc': 10000.0, 'mevi': 1000.0, 'max_swap': 100.0, 'attack_rate': 0.05},
    ],
    'oracle_count':     12,
    'oracle_start_idx': 9,
    'oracle_base_stake': 0.1,
    # patt = 1000 bps = 10% → parametro formula premio (non controlla i bot; vedi attack_rate)
    'gov_params': None,   # riempito a runtime (serve GOV_DEFAULTS)
}

def get_base_preset():
    """Restituisce la configurazione del preset base con gov_params inizializzati."""
    cfg = dict(BASE_PRESET)
    gov = {k: v['val'] for k, v in GOV_DEFAULTS.items()}
    gov['patt'] = 1000          # 10% in bps
    cfg['gov_params'] = gov
    cfg['traders'] = [dict(t) for t in BASE_PRESET['traders']]
    cfg['bots']    = [dict(b) for b in BASE_PRESET['bots']]
    return cfg

def show_preset_summary(cfg):
    print(f"\n  {_c('── SIMULAZIONE BASE ──', _BOLD + _CYAN)}")
    print(f"  Block time : {cfg['block_ms']} ms")
    print(f"  Trader     : {len(cfg['traders'])} × 1000 USDC + 500 MEVI (AUTO, ogni 15s)")
    print(f"  Bot MEV    : {len(cfg['bots'])} × attack_rate 5%  max 100 USDC/attacco")
    print(f"  Oracle     : {cfg['oracle_count']}  (account {cfg['oracle_start_idx']}–"
          f"{cfg['oracle_start_idx'] + cfg['oracle_count'] - 1})")
    print(f"  patt       : 1000 bps (10%)  — tutti gli altri parametri: default")
    print(f"  Account layout:")
    print(f"    0=deployer  1-3=bot  4-8=trader  9-20=oracle")
    print()

# ──────────────────────────────────────────────────────────────
#  CONFIGURAZIONE GOVERNANCE (Tabella 8)
# ──────────────────────────────────────────────────────────────

GOV_DEFAULTS = {
    # MEVInsurance
    "nOracle":               {"val": 7,      "desc": "N oracle per claim",         "unit": ""},
    "thetaReject":           {"val": 80,     "desc": "θ_reject (soglia rigetto)",   "unit": ""},
    "thetaApprove":          {"val": 60,     "desc": "θ_approve (soglia approv.)",  "unit": ""},
    "oracleTimeout":         {"val": 259200, "desc": "Oracle timeout",              "unit": "s"},
    "patternInvalidBps":     {"val": 7000,   "desc": "Soglia pattern invalido",     "unit": "bps"},
    "dispersioneThreshold":  {"val": 20,     "desc": "Soglia dispersione",          "unit": ""},
    "alphaStakeBps":         {"val": 2000,   "desc": "αstake Platinum",             "unit": "bps"},
    "gasRefundAmount":       {"val": int(0.01e18), "desc": "Gas refund importo",    "unit": "wei"},
    "inactivityPenalty":     {"val": int(0.001e18),"desc": "Penale inattività",     "unit": "wei"},
    # PremiumCalculator
    "patt":                  {"val": 500,    "desc": "P_att (prob. attacco)",       "unit": "bps"},
    "lPercent":              {"val": 200,    "desc": "L% (perdita media)",          "unit": "bps"},
    "eFNR":                  {"val": 2000,   "desc": "E (false negative rate)",     "unit": "bps"},
    "mBase":                 {"val": 2000,   "desc": "M_base (margine base)",       "unit": "bps"},
    "pmin":                  {"val": 0,      "desc": "P_min (premio minimo, 0=off)","unit": "bps"},
    "srSafe":                {"val": 15000,  "desc": "SR_safe (solvency safe)",     "unit": "bps"},
    "srCritical":            {"val": 13000,  "desc": "SR_critical (solvency crit.)","unit": "bps"},
    "deltaMmed":             {"val": 500,    "desc": "ΔM_med (adj. medio)",         "unit": "bps"},
    "deltaMhigh":            {"val": 1000,   "desc": "ΔM_high (adj. alto)",         "unit": "bps"},
}

def collect_governance_params():
    """Mostra i parametri di governance e permette di modificarli."""
    section("PARAMETRI DI GOVERNANCE (Tabella 8)")
    params = {k: v["val"] for k, v in GOV_DEFAULTS.items()}

    print(f"\n  {'Parametro':<22} {'Default':>10}  {'Descrizione'}")
    print(f"  {'─'*22} {'─'*10}  {'─'*35}")
    for k, meta in GOV_DEFAULTS.items():
        unit = f" {meta['unit']}" if meta['unit'] else ""
        print(f"  {k:<22} {str(meta['val']):>10}{unit}  {meta['desc']}")

    if not ask_yn("\nVuoi modificare qualche parametro?", default=False):
        print(f"  {_c('Parametri di default confermati.', _GREEN)}")
        return params

    print("\n  (Premi INVIO per mantenere il default)")
    for k, meta in GOV_DEFAULTS.items():
        unit = f" {meta['unit']}" if meta['unit'] else ""
        new_val = ask(f"  {k} [{meta['desc']}]", default=meta['val'], cast=int)
        params[k] = new_val

    return params

def apply_governance_params(w3, contracts, deployer, params):
    """Applica i parametri di governance ai contratti on-chain."""
    import utils
    ins = contracts["MEVInsurance"]
    prem = contracts["PremiumCalculator"]

    # Switcha a automine per operazioni veloci
    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    setters_ins = {
        "nOracle":              ins.functions.setNOracle,
        "thetaReject":          ins.functions.setThetaReject,
        "thetaApprove":         ins.functions.setThetaApprove,
        "oracleTimeout":        ins.functions.setOracleTimeout,
        "patternInvalidBps":    ins.functions.setPatternInvalidThresholdBps,
        "dispersioneThreshold": ins.functions.setDispersioneThreshold,
        "alphaStakeBps":        ins.functions.setAlphaStakeBps,
        "gasRefundAmount":      ins.functions.setGasRefundAmount,
        "inactivityPenalty":    ins.functions.setInactivityPenalty,
    }
    setters_prem = {
        "patt":       prem.functions.setPatt,
        "lPercent":   prem.functions.setLPercent,
        "eFNR":       prem.functions.setEFNR,
        "mBase":      prem.functions.setMBase,
        "pmin":       prem.functions.setPmin,
        "srSafe":     prem.functions.setSRSafe,
        "srCritical": prem.functions.setSRCritical,
        "deltaMmed":  prem.functions.setDeltaMmed,
        "deltaMhigh": prem.functions.setDeltaMhigh,
    }

    for k, fn in {**setters_ins, **setters_prem}.items():
        if k in params:
            try:
                utils.send_tx(w3, fn(params[k]), deployer)
                print(f"  ✅ {k} = {params[k]}")
            except Exception as e:
                print(f"  ❌ {k}: {e}")

    block_ms = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))
    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [block_ms])

# ──────────────────────────────────────────────────────────────
#  SETUP ATTORI
# ──────────────────────────────────────────────────────────────

def setup_trader_on_chain(w3, contracts, deployer, trader_addr, usdc_amt, mevi_amt):
    import utils
    token  = contracts["MEVToken"]
    usdc_c = contracts["MockUSDC"]
    ins    = contracts["MEVInsurance"]
    amm    = contracts["MockAMM"]
    big    = w3.to_wei(1_000_000, 'ether')

    utils.send_tx(w3, usdc_c.functions.transfer(trader_addr, utils.to_wei(usdc_amt)), deployer)
    utils.send_tx(w3, token.functions.transfer(trader_addr, utils.to_wei(mevi_amt)), deployer)
    utils.send_tx(w3, token.functions.approve(amm.address, big), trader_addr)
    utils.send_tx(w3, usdc_c.functions.approve(amm.address, big), trader_addr)
    utils.send_tx(w3, token.functions.approve(ins.address, big), trader_addr)
    utils.send_tx(w3, usdc_c.functions.approve(ins.address, big), trader_addr)
    print(f"  ✅ Trader {trader_addr[:18]}... → {usdc_amt} USDC + {mevi_amt} MEVI")

def setup_bot_on_chain(w3, contracts, deployer, bot_addr, usdc_amt, mevi_amt):
    import utils
    token  = contracts["MEVToken"]
    usdc_c = contracts["MockUSDC"]
    amm    = contracts["MockAMM"]
    big    = w3.to_wei(1_000_000, 'ether')

    utils.send_tx(w3, usdc_c.functions.transfer(bot_addr, utils.to_wei(usdc_amt)), deployer)
    utils.send_tx(w3, token.functions.transfer(bot_addr, utils.to_wei(mevi_amt)), deployer)
    utils.send_tx(w3, token.functions.approve(amm.address, big), bot_addr)
    utils.send_tx(w3, usdc_c.functions.approve(amm.address, big), bot_addr)
    print(f"  ✅ Bot {bot_addr[:18]}... → {usdc_amt} USDC + {mevi_amt} MEVI")

# ──────────────────────────────────────────────────────────────
#  MENU INTERATTIVO
# ──────────────────────────────────────────────────────────────

def show_user_info(w3, contracts, addr):
    try:
        addr = Web3.to_checksum_address(addr)
    except Exception:
        print(f"  ❌ Indirizzo non valido: {addr}")
        return

    import utils
    ins  = contracts["MEVInsurance"]
    mevi = contracts["MEVToken"]
    usdc = contracts["MockUSDC"]

    u_bal = utils.from_wei(usdc.functions.balanceOf(addr).call())
    m_bal = utils.from_wei(mevi.functions.balanceOf(addr).call())
    eth   = Web3.from_wei(w3.eth.get_balance(addr), 'ether')

    try:
        p = ins.functions.getUserProfile(addr).call()
        tiers = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]
        tier  = tiers[p[0]] if p[0] < 4 else f"Tier({p[0]})"
        print(f"\n  ╔═══════════════════════════════════════════╗")
        print(f"  ║  👤 PROFILO UTENTE                        ║")
        print(f"  ║  Addr:   {addr[:30]}...  ║")
        print(f"  ║  USDC:   {u_bal:>12.4f}  MEVI: {m_bal:>12.4f}  ║")
        print(f"  ║  ETH:    {float(eth):>12.6f}                        ║")
        print(f"  ║  Tier:   {tier:<8s}  Swaps: {p[2]:<6d} Claims: {p[3]:<6d} ║")
        print(f"  ║  Stato:  {'🔴 BLACKLIST' if p[7] else '✅ ATTIVO':<35s}  ║")
        print(f"  ╚═══════════════════════════════════════════╝")
    except Exception as e:
        print(f"  ⚠️  Profilo non trovato ({e})")
        print(f"  Wallet: {u_bal:.4f} USDC | {m_bal:.4f} MEVI | {float(eth):.6f} ETH")

def manual_swap(w3, contracts, trader_addr, amount, level=2):
    """Esegue uno swap protetto manualmente dall'orchestratore."""
    import utils
    ins  = contracts["MEVInsurance"]
    mevi = contracts["MEVToken"]
    usdc = contracts["MockUSDC"]
    amm  = contracts["MockAMM"]

    val   = utils.to_wei(amount)
    nomi  = ["LOW", "MEDIUM", "HIGH"]
    try:
        premio = ins.functions.getPremiumEstimate(val, level).call()
        print(f"  Premio: {utils.from_wei(premio):.4f} MEVI")
        utils.send_tx(w3, mevi.functions.approve(ins.address, premio), trader_addr)
        receipt_ins = utils.send_tx(w3, ins.functions.insuredSwap(val, level), trader_addr)
        swap_id = None
        for ev in ins.events.SwapInsured().process_receipt(receipt_ins):
            swap_id = ev['args']['swapId']
        utils.send_tx(w3, usdc.functions.approve(amm.address, val), trader_addr)
        utils.send_tx(w3, amm.functions.swap(usdc.address, val), trader_addr)
        print(f"  ✅ Swap protetto ({nomi[level]}) {amount} USDC eseguito! swapId={swap_id}")
    except Exception as e:
        print(f"  ❌ Errore: {e}")

def skip_days(w3, n):
    w3.provider.make_request("evm_increaseTime", [n * 86400])
    w3.provider.make_request("evm_mine", [])
    print(f"  ✅ Avanzati {n} giorno/i nella blockchain.")

def start_auto_day_advance(w3, day_duration_min, stop_event):
    """
    Thread daemon: ogni `day_duration_min` minuti reali avanza la blockchain di 1 giorno.
    Si ferma quando stop_event è settato.
    """
    interval_s = day_duration_min * 60
    day_num = 0
    def _run():
        nonlocal day_num
        while not stop_event.wait(timeout=interval_s):
            day_num += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n  🕐 [{ts}] AUTO DAY ADVANCE → giorno +{day_num} (ogni {day_duration_min} min)")
            try:
                skip_days(w3, 1)
            except Exception as e:
                print(f"  ⚠️  auto_day_advance errore: {e}")
    t = threading.Thread(target=_run, daemon=True, name="auto-day-advance")
    t.start()
    return t

def start_pool_logger(w3, contracts, stop_event, interval_s=120):
    """Daemon: ogni interval_s scrive snapshot pool su simulation.log."""
    import utils
    token = contracts["MEVToken"]
    ins   = contracts["MEVInsurance"]

    def _run():
        stop_event.wait(timeout=interval_s)
        while not stop_event.is_set():
            try:
                mevi = utils.from_wei(token.functions.balanceOf(ins.address).call())
                eth  = float(Web3.from_wei(ins.functions.getPoolEthBalance().call(), 'ether'))
                utils.sim_log(
                    f"POOL UPDATE | mevi={mevi:.4f} MEVI | eth={eth:.6f} ETH",
                    tag=" POOL"
                )
            except Exception:
                pass
            stop_event.wait(timeout=interval_s)

    t = threading.Thread(target=_run, daemon=True, name="pool-logger")
    t.start()
    return t


def start_amm_rebalancer(w3, contracts, deployer, interval_s, stop_event):
    """
    Daemon thread: every interval_s seconds, restores MockAMM reserves to
    initial ratio by calling amm.rebalance(). Simulates LP rebalancing.
    Only triggers when drift exceeds 5% on either reserve.
    """
    import utils

    amm = contracts["MockAMM"]

    def _run():
        while not stop_event.wait(timeout=interval_s):
            try:
                reserve_a = amm.functions.reserveA().call()
                reserve_b = amm.functions.reserveB().call()
                init_a    = amm.functions.initReserveA().call()
                init_b    = amm.functions.initReserveB().call()

                if init_a == 0 or init_b == 0:
                    continue

                drift_a = abs(reserve_a - init_a) / init_a
                drift_b = abs(reserve_b - init_b) / init_b

                if drift_a > 0.05 or drift_b > 0.05:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n  🔄 [{ts}] AMM REBALANCE "
                          f"(drift MEVI={drift_a*100:.1f}% USDC={drift_b*100:.1f}%)")
                    utils.send_tx(w3, amm.functions.rebalance(), deployer)
                    print(f"  ✅ Pool riportato a {init_a/1e18:.0f} MEVI / {init_b/1e18:.0f} USDC")
            except Exception as e:
                if "not found" not in str(e).lower():
                    print(f"  ⚠️  AMM rebalancer error: {e}")

    t = threading.Thread(target=_run, daemon=True, name="amm-rebalancer")
    t.start()
    return t


def fund_eth_pool(w3, contracts, deployer, block_ms):
    """Invia ETH dal deployer al pool oracle di MEVInsurance."""
    import utils
    ins = contracts["MEVInsurance"]
    pool_before = Web3.from_wei(ins.functions.getPoolEthBalance().call(), 'ether')
    print(f"\n  Pool ETH corrente: {float(pool_before):.6f} ETH")
    amount_eth = ask("ETH da versare (dal deployer)", default=1.0, cast=float)
    if amount_eth <= 0:
        print(f"  Annullato.")
        return
    val_wei = Web3.to_wei(amount_eth, 'ether')
    deployer_bal = w3.eth.get_balance(deployer)
    if deployer_bal < val_wei:
        print(f"  ❌ Saldo ETH deployer insufficiente: {float(Web3.from_wei(deployer_bal, 'ether')):.4f} ETH")
        return
    try:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_setIntervalMining", [0])
        tx = {'from': deployer, 'to': ins.address, 'value': val_wei, 'gas': 30000}
        tx_hash = w3.eth.send_transaction(tx)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        pool_after = Web3.from_wei(ins.functions.getPoolEthBalance().call(), 'ether')
        print(f"  ✅ Versati {amount_eth} ETH al pool oracle.")
        print(f"  Pool ETH: {float(pool_before):.6f} → {float(pool_after):.6f} ETH")
    except Exception as e:
        print(f"  ❌ Errore: {e}")
    finally:
        w3.provider.make_request("evm_setAutomine", [False])
        w3.provider.make_request("evm_setIntervalMining", [block_ms])

def show_pool_state(w3, contracts):
    import utils
    ins  = contracts["MEVInsurance"]
    mevi = contracts["MEVToken"]
    oreg = contracts["OracleRegistry"]

    pool_mevi = utils.from_wei(mevi.functions.balanceOf(ins.address).call())
    pool_eth  = Web3.from_wei(ins.functions.getPoolEthBalance().call(), 'ether')
    active    = oreg.functions.activeOracleCount().call()
    block     = w3.eth.block_number

    print(f"\n  ┌─────────────────────────────────────────┐")
    print(f"  │  🏦 STATO POOL (blocco #{block:<6d})        │")
    print(f"  │  MEVI pool:   {pool_mevi:>14.4f} MEVI        │")
    print(f"  │  ETH pool:    {float(pool_eth):>14.6f} ETH         │")
    print(f"  │  Oracle att.: {active:>14d}               │")
    print(f"  └─────────────────────────────────────────┘")

_REVERSE_LEET = {'4': 'a', '3': 'e', '1': 'i', '0': 'o', '5': 's', '7': 't'}

def _decode_leet(text: str) -> str:
    return ''.join(_REVERSE_LEET.get(ch, ch) for ch in text)


def _auto_captcha_platinum(w3, contracts, trader_addr, block_ms):
    """Attende la sfida CAPTCHA Platinum e la risolve automaticamente."""
    import utils, time
    ins = contracts["MEVInsurance"]
    print(f"  🤖 Auto-CAPTCHA Platinum: attendo sfida...")
    # platinumRequests getter omits address[3] assignedOracles:
    # [0]=desiredMaxSwap [1]=stakeDeposited [2]=answerHash [3]=challengeText
    # [4]=approveVotes [5]=rejectVotes [6]=totalVotes [7]=resolved
    # [8]=challengeSet [9]=answerSubmitted [10]=timestamp
    for _ in range(90):
        req = ins.functions.platinumRequests(trader_addr).call()
        if req[8]:  # challengeSet
            break
        time.sleep(1)
    else:
        print(f"  ⚠️  Timeout: nessuna sfida CAPTCHA ricevuta (90s).")
        print(f"      Usa [22] per rispondere manualmente.")
        return

    req = ins.functions.platinumRequests(trader_addr).call()
    if req[7]:   # resolved
        print(f"  ✅ Richiesta già risolta.")
        return
    if req[9]:  # answerSubmitted
        print(f"  ⚠️  Risposta già inviata, attendo verdetto...")
    else:
        challenge_text = req[3]
        answer = _decode_leet(challenge_text)
        print(f"  🧩 Sfida: '{challenge_text}' → risposta: '{answer}'")
        try:
            w3.provider.make_request("evm_setAutomine", [True])
            w3.provider.make_request("evm_setIntervalMining", [0])
            utils.send_tx(w3, ins.functions.submitCaptchaAnswer(answer), trader_addr)
            w3.provider.make_request("evm_setAutomine", [False])
            w3.provider.make_request("evm_setIntervalMining", [block_ms])
            print(f"  ✅ Risposta '{answer}' inviata automaticamente!")
        except Exception as e:
            print(f"  ❌ Errore invio risposta: {e}")
            return

    # Attendi verdetto
    print(f"  ⏳ Attendo verdetto oracle (max 120s)...")
    for _ in range(120):
        req = ins.functions.platinumRequests(trader_addr).call()
        if req[7]:  # resolved
            approve = req[4]
            reject  = req[5]
            if approve >= 2:
                alpha  = ins.functions.alphaStakeBps().call()
                stake  = req[1]
                max_sw = utils.from_wei((stake * 10000) // alpha) if alpha else 0
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  🏆 UPGRADE A PLATINUM CONFERMATO!       ║")
                print(f"  ║  Voti: {approve} approve / {reject} reject         ║")
                print(f"  ║  Max swap assicurabile: {max_sw:.0f} MEVI   ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            else:
                print(f"\n  ╔══════════════════════════════════════════╗")
                print(f"  ║  ❌ UPGRADE NEGATO                       ║")
                print(f"  ║  Voti: {approve} approve / {reject} reject         ║")
                print(f"  ║  Stake restituito.                       ║")
                print(f"  ╚══════════════════════════════════════════╝\n")
            return
        time.sleep(1)
    print(f"  ⚠️  Timeout verdetto (120s). Riprova con [22].")


def request_platinum_manual(w3, contracts, trader_addr, max_swap_usdc, block_ms=3000):
    import utils
    ins = contracts["MEVInsurance"]
    alpha = ins.functions.alphaStakeBps().call()
    desired = utils.to_wei(max_swap_usdc)
    stake   = (desired * alpha) // 10000
    print(f"  Stake richiesto: {Web3.from_wei(stake, 'ether'):.6f} ETH")

    # ── Pre-check: evita revert prevedibili ──────────────────────────
    try:
        profile = ins.functions.getUserProfile(trader_addr).call()
        if profile[0] == 3:   # Tier.Platinum == 3
            print(f"  ⚠️  Account già Platinum — upgrade non necessario.")
            return
        if profile[7]:        # isBlacklisted
            print(f"  ❌ Account in blacklist — requestPlatinum non consentito.")
            return
        req = ins.functions.platinumRequests(trader_addr).call()
        # platinumRequests getter omits address[3] assignedOracles:
        # [0]=desiredMaxSwap [1]=stakeDeposited [2]=answerHash [3]=challengeText
        # [4]=approveVotes [5]=rejectVotes [6]=totalVotes [7]=resolved
        # [8]=challengeSet [9]=answerSubmitted [10]=timestamp
        challenge_set    = req[8]  if len(req) > 8  else False
        resolved         = req[7]  if len(req) > 7  else True
        stake_deposited  = req[1]  if len(req) > 1  else 0
        answer_submitted = req[9]  if len(req) > 9  else False
        if challenge_set and not resolved:
            print(f"  ⚠️  Richiesta Platinum già pendente (CAPTCHA pubblicato).")
            if not answer_submitted:
                print(f"      Usa [22] per rispondere al CAPTCHA.")
            else:
                print(f"      Risposta già inviata, attendi verdetto oracle.")
            return
        if stake_deposited > 0 and not resolved:
            print(f"  ⚠️  Richiesta Platinum già pendente (oracle stanno generando il CAPTCHA).")
            print(f"      Attendi qualche secondo, poi usa [22] per rispondere.")
            return
        eth_bal = Web3.from_wei(w3.eth.get_balance(trader_addr), 'ether')
        stake_eth = Web3.from_wei(stake, 'ether')
        if float(eth_bal) < float(stake_eth) + 0.01:
            print(f"  ❌ ETH insufficienti: {float(eth_bal):.4f} ETH disponibili, "
                  f"{float(stake_eth):.4f} ETH richiesti (+ gas).")
            return
    except Exception as pre_e:
        print(f"  ⚠️  Pre-check fallito ({pre_e}), provo comunque...")

    try:
        utils.send_tx(w3, ins.functions.requestPlatinum(desired), trader_addr, value=stake)
        print(f"  ✅ Richiesta Platinum inviata!")
        _auto_captcha_platinum(w3, contracts, trader_addr, block_ms)
    except Exception as e:
        err = str(e)
        if "already pending" in err.lower():
            print(f"  ❌ Richiesta già pendente per questo account.")
        elif "already platinum" in err.lower():
            print(f"  ❌ Account già Platinum.")
        elif "insufficient stake" in err.lower():
            print(f"  ❌ Stake insufficiente.")
        elif "blacklisted" in err.lower():
            print(f"  ❌ Account in blacklist.")
        else:
            print(f"  ❌ Errore: {e}")

_STATUS_NAMES = [
    "Inactive", "Pending", "Active", "Watchlisted",
    "Contested", "Slashed", "Expelled"
]
_STATUS_ICONS = {0: "💤", 1: "⏳", 2: "✅", 3: "⚠️", 4: "⚖️", 5: "🔻", 6: "🚫"}

def _oracle_status_name(code):
    return _STATUS_NAMES[code] if code < len(_STATUS_NAMES) else f"Unknown({code})"


def show_oracle_network(w3, contracts):
    """Mostra lo stato completo di tutti gli oracle registrati, inclusi guasti attivi."""
    oreg = contracts["OracleRegistry"]
    count = oreg.functions.getOracleCount().call()
    active_total = oreg.functions.activeOracleCount().call()
    min_stake = oreg.functions.getMinimumStake().call()

    # Carica guasti dal test state
    bugs = _load_test_state().get("bugs", {})

    print(f"\n  {'═'*82}")
    print(f"  ⚡  RETE ORACLE  —  {count} registrati, {active_total} attivi  "
          f"|  min stake: {Web3.from_wei(min_stake, 'ether'):.4f} ETH")
    print(f"  {'─'*82}")
    print(f"  {'#':>3}  {'Indirizzo':>18}  {'Status':>12}  "
          f"{'Stake':>10}  {'DevScore':>9}  {'Strikes':>7}  {'Claims':>7}  {'Guasto':>10}")
    print(f"  {'─'*3}  {'─'*18}  {'─'*12}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*7}  {'─'*10}")

    for i in range(count):
        addr = oreg.functions.oracleList(i).call()
        info = oreg.functions.getOracleInfo(addr).call()
        stake_eth   = float(Web3.from_wei(info[0], 'ether'))
        status_code = info[1]
        dev_score   = info[4]
        claims_eval = info[5]
        strikes     = info[6]
        icon  = _STATUS_ICONS.get(status_code, "?")
        label = _oracle_status_name(status_code)

        fault_dev = bugs.get(addr.lower())
        if fault_dev is not None:
            sign = "+" if fault_dev >= 0 else ""
            fault_str = _c(f"dev{sign}{fault_dev}", _RED)
        else:
            fault_str = _c("OK", _GREEN)

        color = _GREEN if status_code == 2 else \
                _YELLOW if status_code == 3 else \
                _RED    if status_code in (5, 6) else \
                _RESET
        print(f"  {_c(f'{i:>3}', color)}  {addr[:18]}  "
              f"{_c(f'{icon} {label:<10}', color)}  "
              f"{stake_eth:>9.4f}E  {dev_score:>9}  {strikes:>7}  {claims_eval:>7}  {fault_str}")

    watchlist = oreg.functions.getWatchlist().call()
    print(f"  {'─'*82}")
    print(f"  Watchlist ({len(watchlist)}): "
          + (", ".join(a[:14]+"..." for a in watchlist) if watchlist else "nessuno"))
    if bugs:
        print(f"  Guasti attivi: {len(bugs)}")
    print(f"  {'═'*82}")


def show_watchlist(w3, contracts):
    """Mostra la watchlist con dettaglio deviation score, ordinata per dev score decrescente."""
    oreg = contracts["OracleRegistry"]
    watchlist = oreg.functions.getWatchlist().call()
    if not watchlist:
        print(f"\n  ✅ Watchlist vuota — nessun oracle sorvegliato.")
        return
    # Raccolta dati + ordinamento per deviation score decrescente
    entries = []
    for addr in watchlist:
        info = oreg.functions.getOracleInfo(addr).call()
        stake_eth = float(Web3.from_wei(info[0], 'ether'))
        dev_score = info[4]
        strikes   = info[6]
        entries.append((dev_score, addr, stake_eth, strikes))
    entries.sort(key=lambda x: x[0], reverse=True)
    print(f"\n  ⚠️  WATCHLIST ({len(watchlist)} oracle) — ordinata per deviation score ↓")
    print(f"  {'─'*60}")
    print(f"  {'Rank':>4}  {'Indirizzo':>20}  {'DevScore':>9}  {'Stake':>10}  {'Strikes':>7}")
    print(f"  {'─'*4}  {'─'*20}  {'─'*9}  {'─'*10}  {'─'*7}")
    for rank, (dev_score, addr, stake_eth, strikes) in enumerate(entries, 1):
        print(f"  {rank:>4}  {_c(addr[:20]+'...', _YELLOW)}  "
              f"{dev_score:>9}  {stake_eth:>9.4f}E  {strikes:>7}")
    print(f"  {'─'*60}")


def submit_slash_report(w3, contracts, reporter_addr, accused_addr):
    """Sottomette un report di slashing contro un oracle."""
    import utils
    slashing = contracts.get("SlashingSystem")
    if slashing is None:
        print(f"  ❌ SlashingSystem non deployato.")
        return

    deposit = slashing.functions.reportDeposit().call()
    deposit_eth = float(Web3.from_wei(deposit, 'ether'))

    oreg = contracts["OracleRegistry"]
    try:
        info = oreg.functions.getOracleInfo(Web3.to_checksum_address(accused_addr)).call()
        status_code = info[1]
        stake_eth   = float(Web3.from_wei(info[0], 'ether'))
        print(f"\n  Oracle accusato: {accused_addr[:20]}...")
        print(f"  Status: {_oracle_status_name(status_code)}  |  Stake: {stake_eth:.4f} ETH")
        if status_code not in (2, 3):
            print(f"  ⚠️  Oracle non Active/Watchlisted — non può essere reportato.")
            return
    except Exception as e:
        print(f"  ❌ Lettura info oracle: {e}")
        return

    print(f"\n  Deposito richiesto: {_c(f'{deposit_eth:.4f} ETH', _YELLOW)}")
    proof_text = ask("Descrizione prova (testo libero)", default="comportamento anomalo")
    if not ask_yn(f"Inviare report? Costi: {deposit_eth:.4f} ETH", default=False):
        print(f"  Annullato.")
        return

    proof_bytes = proof_text.encode("utf-8")
    try:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_setIntervalMining", [0])
        utils.send_tx(
            w3,
            slashing.functions.submitReport(Web3.to_checksum_address(accused_addr), proof_bytes),
            reporter_addr,
            value=deposit
        )
        print(f"  ✅ Report inviato! La jury verrà selezionata automaticamente.")
        print(f"  ℹ️  Il daemon oracle (oraclec.py) processerà il report nella prossima finestra.")
    except Exception as e:
        print(f"  ❌ Errore invio report: {e}")
    finally:
        block_ms = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))
        w3.provider.make_request("evm_setAutomine", [False])
        w3.provider.make_request("evm_setIntervalMining", [block_ms])


def oracle_reintegrate(w3, contracts, oracle_addr):
    """Permette a un oracle slashato di reintegrarsi pagando 2x minStake."""
    import utils
    oreg = contracts["OracleRegistry"]
    oracle_addr = Web3.to_checksum_address(oracle_addr)

    try:
        info = oreg.functions.getOracleInfo(oracle_addr).call()
        status_code = info[1]
        current_stake = info[0]
    except Exception as e:
        print(f"  ❌ Lettura info oracle: {e}")
        return

    if status_code != 5:  # 5 = Slashed
        print(f"  ❌ Oracle non Slashed (status: {_oracle_status_name(status_code)}). "
              f"Solo oracle Slashed possono reintegrarsi.")
        return

    min_stake = oreg.functions.getMinimumStake().call()
    required_total = min_stake * 2
    already_have   = current_stake
    to_pay         = max(0, required_total - already_have)

    print(f"\n  Oracle: {oracle_addr[:20]}...")
    print(f"  Status:          Slashed")
    print(f"  Stake residuo:   {float(Web3.from_wei(already_have, 'ether')):.6f} ETH")
    print(f"  Min stake att.:  {float(Web3.from_wei(min_stake, 'ether')):.6f} ETH")
    print(f"  Richiesto (2x):  {float(Web3.from_wei(required_total, 'ether')):.6f} ETH")
    to_pay_eth = float(Web3.from_wei(to_pay, 'ether'))
    print(f"  {_c(f'Da pagare ora:   {to_pay_eth:.6f} ETH', _YELLOW)}")

    if not ask_yn(f"Procedere con il reintegro?", default=False):
        print(f"  Annullato.")
        return

    try:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_setIntervalMining", [0])
        utils.send_tx(
            w3,
            oreg.functions.reintegrateOracle(),
            oracle_addr,
            value=to_pay
        )
        print(f"  ✅ Reintegro avviato! L'oracle sarà operativo dopo il cooldown (7 giorni sim.).")
        print(f"  ℹ️  Usa [5] per avanzare il tempo se stai testando.")
    except Exception as e:
        print(f"  ❌ Errore reintegro: {e}")
    finally:
        block_ms = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))
        w3.provider.make_request("evm_setAutomine", [False])
        w3.provider.make_request("evm_setIntervalMining", [block_ms])


def oracle_reset_deviation(w3, contracts, oracle_addr):
    """Resetta il deviation score di un oracle (dopo tReset)."""
    import utils
    oreg = contracts["OracleRegistry"]
    oracle_addr = Web3.to_checksum_address(oracle_addr)

    try:
        info = oreg.functions.getOracleInfo(oracle_addr).call()
        status_code = info[1]
        dev_score   = info[4]
        strikes     = info[6]
    except Exception as e:
        print(f"  ❌ Lettura info: {e}")
        return

    print(f"\n  Oracle: {oracle_addr[:20]}...")
    print(f"  Status:    {_oracle_status_name(status_code)}")
    print(f"  DevScore:  {dev_score}  |  Strikes: {strikes}")

    if not ask_yn("Resettare deviation score?", default=False):
        return

    try:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_setIntervalMining", [0])
        utils.send_tx(w3, oreg.functions.resetDeviationScore(oracle_addr), oracle_addr)
        print(f"  ✅ Deviation score resettato.")
    except Exception as e:
        print(f"  ❌ Errore: {e}")
    finally:
        block_ms = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))
        w3.provider.make_request("evm_setAutomine", [False])
        w3.provider.make_request("evm_setIntervalMining", [block_ms])


TEST_STATE_FILE = ROOT / "oracle_test_state.json"

def _load_test_state() -> dict:
    if TEST_STATE_FILE.exists():
        try:
            with open(TEST_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("bugs", {})
            data.setdefault("jury_probs", {})
            return data
        except Exception:
            pass
    return {"bugs": {}, "jury_probs": {}}

def _save_test_state(data: dict) -> None:
    with open(TEST_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def oracle_register_new(w3, contracts):
    """Registra e attiva un nuovo oracle su OracleRegistry."""
    import utils
    oreg = contracts["OracleRegistry"]
    min_stake = oreg.functions.getMinimumStake().call()
    min_eth   = float(Web3.from_wei(min_stake, 'ether'))

    print(f"\n  Min stake richiesto: {min_eth:.4f} ETH")
    raw = ask("Account Hardhat idx", default=10, cast=int)
    stake_eth = ask(f"Stake ETH (min {min_eth:.4f})", default=round(min_eth, 4), cast=float)

    try:
        addr = w3.eth.accounts[raw]
    except IndexError:
        print(f"  ❌ Account idx {raw} non esiste.")
        return

    stake_wei = Web3.to_wei(stake_eth, 'ether')
    if stake_wei < min_stake:
        print(f"  ❌ Stake troppo basso (min {min_eth:.4f} ETH).")
        return

    # Check già registrato
    try:
        info = oreg.functions.getOracleInfo(addr).call()
        if info[1] > 0:
            print(f"  ⚠️  Oracle già registrato (status: {_oracle_status_name(info[1])}).")
            return
    except Exception:
        pass

    if not ask_yn(f"Registrare acc#{raw} ({addr[:18]}...) con stake {stake_eth:.4f} ETH?", default=True):
        return

    try:
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_setIntervalMining", [0])
        utils.send_tx(w3, oreg.functions.registerOracle(), addr, value=stake_wei)
        print(f"  ✅ Registrato! Attivazione in corso...")
        utils.send_tx(w3, oreg.functions.activateOracle(), addr)
        print(f"  ✅ Oracle attivo!")

        # Aggiorna actors.json
        actors = utils.load_actors()
        oracles = actors.setdefault("oracles", [])
        if addr not in oracles:
            oracles.append(addr)
            utils.save_actors(actors)
            print(f"  ✅ Aggiunto ad actors.json")

        print(f"  ⚠️  Riavvia oraclec.py perché partecipi al commit-reveal.")
    except Exception as e:
        print(f"  ❌ Errore: {e}")
    finally:
        block_ms = int(os.environ.get("BLOCK_INTERVAL_MS", "3000"))
        w3.provider.make_request("evm_setAutomine", [False])
        w3.provider.make_request("evm_setIntervalMining", [block_ms])


def oracle_inject_fault(w3, contracts):
    """Inietta un guasto (deviation costante) nel fraud_score di un oracle."""
    oreg = contracts["OracleRegistry"]
    count = oreg.functions.getOracleCount().call()
    if count == 0:
        print(f"  ❌ Nessun oracle registrato.")
        return

    # Mostra lista breve
    print(f"\n  Oracle registrati:")
    for i in range(count):
        addr = oreg.functions.oracleList(i).call()
        info = oreg.functions.getOracleInfo(addr).call()
        label = _oracle_status_name(info[1])
        print(f"    [{i:>2}] {addr[:20]}...  {label}")

    raw = ask("Oracle target (idx o 0x...)", default="0")
    try:
        # Indice → oracle dalla registry (non da w3.eth.accounts)
        oracle_addr = oreg.functions.oracleList(int(raw)).call() if raw.isdigit() else Web3.to_checksum_address(raw)
    except Exception:
        print(f"  ❌ Indirizzo non valido.")
        return

    deviation = ask("Scostamento costante (+x o -x, es. +25 o -30)", default=0, cast=int)
    if deviation == 0:
        print(f"  ℹ️  Scostamento 0 = nessun guasto (rimuove eventuale guasto esistente).")

    state = _load_test_state()
    key = oracle_addr.lower()
    if deviation == 0:
        state["bugs"].pop(key, None)
        _save_test_state(state)
        print(f"  ✅ Guasto rimosso per {oracle_addr[:20]}...")
    else:
        state["bugs"][key] = deviation
        _save_test_state(state)
        sign = "+" if deviation > 0 else ""
        print(f"  ✅ Guasto iniettato: oracle {oracle_addr[:20]}... → fraud_score {sign}{deviation}")
        print(f"  ℹ️  Effetto al prossimo ciclo commit di oraclec.py.")


def oracle_list_faults(w3, contracts):
    """Mostra tutti gli oracle con il loro guasto (o OK), permette reset per indice."""
    state = _load_test_state()
    bugs = state.get("bugs", {})
    oreg = contracts["OracleRegistry"]

    try:
        count = oreg.functions.getOracleCount().call()
    except Exception:
        count = 0

    print(f"\n  {'─'*60}")
    print(f"  GUASTI ORACLE  (🔴 = guasto attivo, ✅ = nessun guasto)")
    print(f"  {'─'*60}")
    print(f"  {'#':>3}  {'Indirizzo':>20}  {'Status':>12}  {'Guasto':>12}")
    print(f"  {'─'*3}  {'─'*20}  {'─'*12}  {'─'*12}")

    oracle_entries = []
    for i in range(count):
        try:
            addr = oreg.functions.oracleList(i).call()
            info = oreg.functions.getOracleInfo(addr).call()
            label = _oracle_status_name(info[1])
        except Exception:
            addr = "0x???"
            label = "?"
        key = addr.lower()
        dev = bugs.get(key)
        if dev is not None:
            sign = "+" if dev >= 0 else ""
            fault_str = _c(f"🔴 dev{sign}{dev}", _RED)
        else:
            fault_str = _c("✅ OK", _GREEN)
        print(f"  {i:>3}  {addr[:20]}  {label:>12}  {fault_str}")
        oracle_entries.append((i, addr, key))

    if bugs:
        print(f"  {'─'*60}")
        print(f"  Totale guasti: {len(bugs)}")
    print(f"  {'─'*60}")

    if not oracle_entries:
        return

    if ask_yn("Rimuovere un guasto?", default=False):
        raw = ask("Indice oracle (# dalla lista sopra) o indirizzo 0x...")
        if raw:
            try:
                idx = int(raw)
                _, _, key = oracle_entries[idx]
                addr_display = oracle_entries[idx][1][:20]
            except (ValueError, IndexError):
                key = raw.lower().strip()
                addr_display = raw[:20]
            if key in bugs:
                del bugs[key]
                _save_test_state(state)
                print(f"  ✅ Guasto rimosso per {addr_display}...")
            else:
                print(f"  ⚠️  Nessun guasto trovato per quell'oracle.")


DEFAULT_JURY_PROB = 0.55  # probabilità innocente di default (oraclec.py)

def oracle_manage_jury_probs(w3, contracts):
    """Mostra e modifica le probabilità di voto innocente per ogni oracle nella jury."""
    state = _load_test_state()
    jury_probs = state.setdefault("jury_probs", {})
    oreg = contracts["OracleRegistry"]

    # Default globale può essere sovrascritto in test state
    global_default = state.get("jury_default_prob", DEFAULT_JURY_PROB)

    def _refresh_list():
        try:
            count = oreg.functions.getOracleCount().call()
        except Exception:
            count = 0
        entries = []
        print(f"\n  {_c('── GESTIONE PROBABILITÀ JURY ──', _BOLD + _CYAN)}")
        print(f"  Default globale: {int(global_default * 100)}%  "
              f"(0=sempre slash | 1-100=% innocente)")
        print(f"  {'─'*60}")
        print(f"  {'#':>3}  {'Indirizzo':>20}  {'Prob innocente':>15}")
        print(f"  {'─'*3}  {'─'*20}  {'─'*15}")
        for i in range(count):
            try:
                addr = oreg.functions.oracleList(i).call()
                key = addr.lower()
                cur = jury_probs.get(key)
                if cur is None:
                    label = _c(f"default ({int(global_default * 100)}%)", _RESET)
                else:
                    label = _c(f"{int(cur * 100)}% (override)", _YELLOW)
                print(f"  {i:>3}  {addr[:20]}  {label}")
                entries.append((i, addr, key))
            except Exception:
                pass
        print(f"  {'─'*60}")
        return entries

    oracle_entries = _refresh_list()
    if not oracle_entries:
        print(f"  ⚠️  Nessun oracle registrato.")
        return

    print(f"  Comandi: numero = modifica oracle | 'd' = cambia default globale | 'r' = reset oracle | 'q' = esci")

    while True:
        raw = ask("Scelta (#oracle / d / r / q)", default="q")
        if not raw or raw.lower() == "q":
            break

        if raw.lower() == "d":
            # Cambia default globale
            val = ask("Nuovo default globale (0=sempre slash, 1-100=%)", default=int(global_default * 100), cast=int)
            val = max(0, min(100, val))
            global_default = val / 100.0 if val > 0 else 0.0
            state["jury_default_prob"] = global_default
            _save_test_state(state)
            print(f"  ✅ Default globale → {int(global_default * 100)}%")
            print(f"  ℹ️  Modifica il valore DEFAULT_PROB in oraclec.py per allinearlo.")
            oracle_entries = _refresh_list()
            continue

        if raw.lower() == "r":
            raw2 = ask("Indice oracle da resettare al default")
            try:
                idx2 = int(raw2)
                _, _, key2 = oracle_entries[idx2]
                addr2 = oracle_entries[idx2][1]
            except (ValueError, IndexError):
                key2 = raw2.lower().strip()
                addr2 = raw2
            if key2 in jury_probs:
                del jury_probs[key2]
                _save_test_state(state)
                print(f"  ✅ {addr2[:20]} → default ({int(global_default * 100)}%)")
            else:
                print(f"  ⚠️  Nessun override per quell'oracle.")
            oracle_entries = _refresh_list()
            continue

        try:
            idx = int(raw)
            _, addr, key = oracle_entries[idx]
        except (ValueError, IndexError):
            print(f"  ⚠️  Indice non valido (digita un numero dalla lista).")
            continue

        val = ask(f"Prob innocente per oracle #{idx} (0=sempre slash, 1-100=%)",
                  default=int((jury_probs.get(key, global_default)) * 100), cast=int)
        val = max(0, min(100, val))

        if val == 0:
            jury_probs[key] = 0.0
            label = "0% (sempre slash)"
        else:
            jury_probs[key] = val / 100.0
            label = f"{val}% innocente"

        _save_test_state(state)
        print(f"  ✅ Oracle #{idx} {addr[:20]}... → {label}")
        print(f"  ℹ️  attivo al prossimo voto.")
        oracle_entries = _refresh_list()


def interactive_menu(w3, contracts, traders_config, block_ms, duration_min=0, sim_config=None):
    """
    Menu interattivo principale mentre la simulazione è in corso.
    duration_min: durata in minuti (0 = infinita). Alla scadenza genera il report automaticamente.
    """
    import utils

    sim_end = (time.time() + duration_min * 60) if duration_min > 0 else None

    def _remaining() -> str:
        if sim_end is None:
            return "∞"
        rem = max(0, sim_end - time.time())
        return f"{int(rem // 60):02d}:{int(rem % 60):02d}"

    MENU = f"""
{_BOLD}{_CYAN}╔════════════════════════════════════════════════════╗
║       ORCHESTRATORE — MENU INTERATTIVO             ║
╠══════════════════════╦═════════════════════════════╣
║  UTENTE / POOL       ║  ORACLE                     ║
║  [1]  Info utente    ║  [14] Stato rete oracle     ║
║  [2]  Swap protetto  ║  [15] Watchlist dettaglio   ║
║  [3]  Swap nudo      ║  [16] Invia slash report    ║
║  [4]  Platinum req.  ║  [17] Reintegra oracle      ║
║  [5]  Skip giorni    ║  [18] Reset deviation score ║
║  [6]  Stato pool     ║  [19] Inietta guasto oracle ║
║  [7]  Report finale  ║  [20] Guasti attivi         ║
╠══════════════════════╬═════════════════════════════╣
║  [8]  Nuovo trader   ║  [9]  Rifinanzia acc.       ║
║  [10] Cambia patt    ║  [11] Modifica bot          ║
║  [12] Modifica trd.  ║  [13] Nuovo bot MEV         ║
║  [21] Prob. jury     ║  [22] Rispondi CAPTCHA Plat.║
║  [23] Fonda pool ETH ║  [0]  Mostra menu           ║
║  [q]  Esci           ║                             ║
╚══════════════════════╩═════════════════════════════╝{_RESET}"""

    print(MENU)
    if sim_end is not None:
        print(f"  {_c(f'⏱  Durata simulazione: {duration_min} min — report automatico alla scadenza.', _YELLOW)}")

    while True:
        # ── Controllo timer auto-report ──
        if sim_end is not None and time.time() >= sim_end:
            print(f"\n  {_c('⏰ Tempo scaduto! Generazione report automatica...', _YELLOW)}")
            return "report"

        try:
            prompt = (f"\n{_BOLD}ORCHESTRATORE [{_remaining()}] > {_RESET}"
                      if sim_end else f"\n{_BOLD}ORCHESTRATORE > {_RESET}")
            cmd = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "q"

        if cmd == "0" or cmd == "menu":
            print(MENU)

        elif cmd == "1":
            raw = ask("Indirizzo o indice account (es. 0x... oppure 2)", default="2")
            try:
                idx = int(raw)
                addr = w3.eth.accounts[idx]
            except (ValueError, IndexError):
                addr = raw
            show_user_info(w3, contracts, addr)

        elif cmd == "2":
            raw = ask("Indirizzo o indice trader", default="2")
            try:
                trader_addr = w3.eth.accounts[int(raw)]
            except (ValueError, IndexError):
                trader_addr = Web3.to_checksum_address(raw)
            amount = ask("Importo USDC", default=100.0, cast=float)
            level  = ask("Copertura (0=LOW, 1=MED, 2=HIGH)", default=2, cast=int)
            manual_swap(w3, contracts, trader_addr, amount, level)

        elif cmd == "3":
            raw = ask("Indirizzo o indice trader", default="2")
            try:
                trader_addr = w3.eth.accounts[int(raw)]
            except (ValueError, IndexError):
                trader_addr = Web3.to_checksum_address(raw)
            amount = ask("Importo USDC", default=100.0, cast=float)
            try:
                val = utils.to_wei(amount)
                usdc = contracts["MockUSDC"]
                amm  = contracts["MockAMM"]
                utils.send_tx(w3, usdc.functions.approve(amm.address, val), trader_addr)
                utils.send_tx(w3, amm.functions.swap(usdc.address, val), trader_addr)
                print(f"  ✅ Swap nudo {amount} USDC eseguito!")
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "4":
            raw = ask("Indirizzo o indice trader", default="2")
            try:
                trader_addr = w3.eth.accounts[int(raw)]
            except (ValueError, IndexError):
                trader_addr = Web3.to_checksum_address(raw)
            max_swap = ask("Max swap desiderato (MEVI)", default=10000.0, cast=float)
            request_platinum_manual(w3, contracts, trader_addr, max_swap, block_ms)

        elif cmd == "5":
            n = ask("Quanti giorni avanzare?", default=1, cast=int)
            skip_days(w3, n)

        elif cmd == "6":
            show_pool_state(w3, contracts)

        elif cmd == "7":
            if ask_yn("Generare report finale e terminare?", default=True):
                return "report"

        elif cmd == "8":
            # ── Aggiungi nuovo trader ──
            section_title = "AGGIUNGI NUOVO TRADER"
            print(f"\n  {_c(f'── {section_title} ──', _BOLD + _CYAN)}")
            raw = ask("Account Hardhat idx", default=9, cast=int)
            usdc  = ask("USDC iniziali", default=1000.0, cast=float)
            mevi  = ask("MEVI iniziali", default=500.0, cast=float)
            auto  = ask_yn("Modalità automatica?", default=True)
            try:
                addr = w3.eth.accounts[raw]
                w3.provider.make_request("evm_setAutomine", [True])
                w3.provider.make_request("evm_setIntervalMining", [0])
                setup_trader_on_chain(w3, contracts, w3.eth.accounts[0], addr, usdc, mevi)
                w3.provider.make_request("evm_setAutomine", [False])
                w3.provider.make_request("evm_setIntervalMining", [block_ms])
                t_cfg = {"account_idx": raw, "usdc": usdc, "mevi": mevi, "auto": auto}
                if auto:
                    t_cfg['interval']   = ask("Intervallo TX (secondi)", default=15, cast=float)
                    t_cfg['amount_min'] = ask("Importo minimo (USDC)", default=50.0, cast=float)
                    t_cfg['amount_max'] = ask("Importo massimo (USDC)", default=150.0, cast=float)
                    t_cfg['coverage']   = ask("Copertura (0=LOW 1=MED 2=HIGH)", default=2, cast=int)
                    t_cfg['tx_count']   = ask("Numero TX (0=infinito)", default=0, cast=int)
                    cmd_parts = [
                        "python", "scripts/trader.py",
                        "--account", raw,
                        "--auto",
                        "--interval",   t_cfg['interval'],
                        "--amount-min", t_cfg['amount_min'],
                        "--amount-max", t_cfg['amount_max'],
                        "--coverage",   t_cfg['coverage'],
                        "--count",      t_cfg['tx_count'],
                    ]
                else:
                    cmd_parts = ["python", "-i", "scripts/trader.py", "--account", raw]
                launch_window(f"TRADER #{raw}", cmd_parts, env_extra={"BLOCK_INTERVAL_MS": str(block_ms)})
                traders_config.append(t_cfg)
                print(f"  ✅ Trader acc#{raw} aggiunto e avviato!")
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "9":
            # ── Rifinanzia trader o bot ──
            print(f"\n  {_c('── RIFINANZIA UTENTE / BOT ──', _BOLD + _CYAN)}")
            raw = ask("Account Hardhat idx da rifinanziare", default=4, cast=int)
            usdc = ask("USDC da aggiungere (0 = nessuno)", default=0.0, cast=float)
            mevi = ask("MEVI da aggiungere (0 = nessuno)", default=0.0, cast=float)
            eth  = ask("ETH da aggiungere (0 = nessuno)", default=0.0, cast=float)
            try:
                deployer = w3.eth.accounts[0]
                addr = w3.eth.accounts[raw]
                w3.provider.make_request("evm_setAutomine", [True])
                w3.provider.make_request("evm_setIntervalMining", [0])
                if usdc > 0:
                    utils.send_tx(w3, contracts["MockUSDC"].functions.transfer(
                        addr, utils.to_wei(usdc)), deployer)
                    print(f"  ✅ Trasferiti {usdc} USDC")
                if mevi > 0:
                    utils.send_tx(w3, contracts["MEVToken"].functions.transfer(
                        addr, utils.to_wei(mevi)), deployer)
                    print(f"  ✅ Trasferiti {mevi} MEVI")
                if eth > 0:
                    val = w3.to_wei(eth, 'ether')
                    w3.eth.send_transaction({
                        'from': deployer, 'to': addr, 'value': val, 'gas': 21000,
                    })
                    print(f"  ✅ Trasferiti {eth} ETH")
                w3.provider.make_request("evm_setAutomine", [False])
                w3.provider.make_request("evm_setIntervalMining", [block_ms])
                print(f"  ✅ Rifinanziamento completato per acc#{raw} ({addr[:18]}...)")
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "10":
            # ── Cambia patt governance ──
            print(f"\n  {_c('── CAMBIA PATT (governance on-chain) ──', _BOLD + _CYAN)}")
            try:
                cur = contracts["PremiumCalculator"].functions.patt().call()
                print(f"  patt attuale (on-chain): {cur} bps ({cur/100:.1f}%)")
            except Exception:
                cur = 1000
            # Mostra attack_rate corrente dei bot in memoria
            bots = (sim_config or {}).get('bots', [])
            if bots:
                print(f"  Bot in esecuzione:")
                for b in bots:
                    ar_pct = int(b['attack_rate'] * 100)
                    ar_bps = int(b['attack_rate'] * 10000)
                    print(f"    acc#{b['account_idx']} → attack_rate={ar_bps} bps ({ar_pct}%)")
            new_patt = ask("Nuovo patt (bps, es. 500 = 5%)", default=cur, cast=int)
            if ask_yn(f"Impostare patt = {new_patt} bps ({new_patt/100:.1f}%)?", default=True):
                try:
                    deployer = w3.eth.accounts[0]
                    w3.provider.make_request("evm_setAutomine", [True])
                    w3.provider.make_request("evm_setIntervalMining", [0])
                    utils.send_tx(w3, contracts["PremiumCalculator"].functions.setPatt(new_patt), deployer)
                    w3.provider.make_request("evm_setAutomine", [False])
                    w3.provider.make_request("evm_setIntervalMining", [block_ms])
                    print(f"  ✅ patt impostato a {new_patt} bps ({new_patt/100:.1f}%)")
                    print(f"  ℹ️  patt è il parametro formula-premio. "
                          f"L'attack_rate dei bot è indipendente (usa [11] per modificarlo).")
                    # Verifica: rileggi dal contratto
                    try:
                        confirmed = contracts["PremiumCalculator"].functions.patt().call()
                        print(f"  ✅ Conferma on-chain: patt = {confirmed} bps ({confirmed/100:.1f}%)")
                    except Exception:
                        pass
                except Exception as e:
                    print(f"  ❌ Errore: {e}")

        elif cmd == "11":
            # ── Modifica bot: relaunch con nuovi parametri ──
            print(f"\n  {_c('── MODIFICA BOT (relaunch) ──', _BOLD + _CYAN)}")
            bots = (sim_config or {}).get('bots', [])
            if bots:
                print("  Bot attuali:")
                for i, b in enumerate(bots):
                    print(f"    [{i}] acc#{b['account_idx']} | max_swap={b['max_swap']} USDC | "
                          f"attack_rate={int(b['attack_rate']*100)}%")
            raw = ask("Account Hardhat idx del bot", default=1, cast=int)
            max_swap    = ask("Nuovo max_swap (USDC per attacco)", default=100.0, cast=float)
            attack_pct  = ask("Nuovo attack_rate (%)", default=5, cast=int)
            usdc_add    = ask("USDC aggiuntivi dal deployer (0 = nessuno)", default=0.0, cast=float)
            mevi_add    = ask("MEVI aggiuntivi dal deployer (0 = nessuno)", default=0.0, cast=float)
            if ask_yn(f"Rilanciare bot acc#{raw} con max_swap={max_swap}, rate={attack_pct}%?", default=True):
                try:
                    deployer = w3.eth.accounts[0]
                    addr = w3.eth.accounts[raw]
                    if usdc_add > 0 or mevi_add > 0:
                        w3.provider.make_request("evm_setAutomine", [True])
                        w3.provider.make_request("evm_setIntervalMining", [0])
                        if usdc_add > 0:
                            utils.send_tx(w3, contracts["MockUSDC"].functions.transfer(
                                addr, utils.to_wei(usdc_add)), deployer)
                        if mevi_add > 0:
                            utils.send_tx(w3, contracts["MEVToken"].functions.transfer(
                                addr, utils.to_wei(mevi_add)), deployer)
                        w3.provider.make_request("evm_setAutomine", [False])
                        w3.provider.make_request("evm_setIntervalMining", [block_ms])
                        print(f"  ✅ Fondi trasferiti a bot acc#{raw}")
                    launch_window(
                        f"MEV BOT #{raw} (new)",
                        ["python", "scripts/mev_bot.py",
                         "--account", raw,
                         "--usdc", 0, "--mevi", 0,
                         "--max-swap", max_swap,
                         "--attack-rate", attack_pct],
                        env_extra={"BLOCK_INTERVAL_MS": str(block_ms)}
                    )
                    print(f"  ✅ Bot acc#{raw} rilanciato in nuova finestra (chiudi la vecchia se necessario)")
                    # Aggiorna config se presente
                    for b in (sim_config or {}).get('bots', []):
                        if b['account_idx'] == raw:
                            b['max_swap'] = max_swap
                            b['attack_rate'] = attack_pct / 100.0
                except Exception as e:
                    print(f"  ❌ Errore: {e}")

        elif cmd == "12":
            # ── Modifica trader: relaunch con nuovi parametri ──
            print(f"\n  {_c('── MODIFICA TRADER (relaunch) ──', _BOLD + _CYAN)}")
            traders = traders_config or []
            if traders:
                print("  Trader attuali:")
                for i, t in enumerate(traders):
                    mode = "AUTO" if t.get('auto') else "INTERATTIVO"
                    print(f"    [{i}] acc#{t['account_idx']} | {mode} | "
                          f"interval={t.get('interval','?')}s | "
                          f"{t.get('amount_min','?')}-{t.get('amount_max','?')} USDC | "
                          f"cov={t.get('coverage','?')}")
            raw = ask("Account Hardhat idx del trader", default=4, cast=int)
            auto  = ask_yn("Modalità automatica?", default=True)
            usdc_add = ask("USDC aggiuntivi dal deployer (0 = nessuno)", default=0.0, cast=float)
            mevi_add = ask("MEVI aggiuntivi dal deployer (0 = nessuno)", default=0.0, cast=float)
            t_new: dict = {"account_idx": raw, "auto": auto}
            if auto:
                t_new['interval']   = ask("Intervallo TX (secondi)", default=15, cast=float)
                t_new['amount_min'] = ask("Importo minimo (USDC)", default=50.0, cast=float)
                t_new['amount_max'] = ask("Importo massimo (USDC)", default=150.0, cast=float)
                t_new['coverage']   = ask("Copertura (0=LOW 1=MED 2=HIGH)", default=2, cast=int)
                t_new['tx_count']   = ask("Numero TX (0=infinito)", default=0, cast=int)
            if ask_yn(f"Rilanciare trader acc#{raw}?", default=True):
                try:
                    deployer = w3.eth.accounts[0]
                    addr = w3.eth.accounts[raw]
                    if usdc_add > 0 or mevi_add > 0:
                        w3.provider.make_request("evm_setAutomine", [True])
                        w3.provider.make_request("evm_setIntervalMining", [0])
                        if usdc_add > 0:
                            utils.send_tx(w3, contracts["MockUSDC"].functions.transfer(
                                addr, utils.to_wei(usdc_add)), deployer)
                        if mevi_add > 0:
                            utils.send_tx(w3, contracts["MEVToken"].functions.transfer(
                                addr, utils.to_wei(mevi_add)), deployer)
                        w3.provider.make_request("evm_setAutomine", [False])
                        w3.provider.make_request("evm_setIntervalMining", [block_ms])
                        print(f"  ✅ Fondi trasferiti a trader acc#{raw}")
                    if auto:
                        cmd_parts = [
                            "python", "scripts/trader.py",
                            "--account", raw,
                            "--auto",
                            "--interval",   t_new['interval'],
                            "--amount-min", t_new['amount_min'],
                            "--amount-max", t_new['amount_max'],
                            "--coverage",   t_new['coverage'],
                            "--count",      t_new['tx_count'],
                        ]
                    else:
                        cmd_parts = ["python", "-i", "scripts/trader.py", "--account", raw]
                    launch_window(f"TRADER #{raw} (new)", cmd_parts,
                                  env_extra={"BLOCK_INTERVAL_MS": str(block_ms)})
                    print(f"  ✅ Trader acc#{raw} rilanciato in nuova finestra (chiudi la vecchia se necessario)")
                    # Aggiorna config in-memory
                    for t in traders_config:
                        if t['account_idx'] == raw:
                            t.update(t_new)
                except Exception as e:
                    print(f"  ❌ Errore: {e}")

        elif cmd == "13":
            # ── Aggiungi nuovo bot MEV ──
            print(f"\n  {_c('── AGGIUNGI NUOVO BOT MEV ──', _BOLD + _CYAN)}")
            raw        = ask("Account Hardhat idx", default=3, cast=int)
            usdc       = ask("USDC iniziali", default=10000.0, cast=float)
            mevi       = ask("MEVI iniziali", default=1000.0, cast=float)
            max_swap   = ask("Max USDC per attacco", default=100.0, cast=float)
            attack_pct = ask("Attack rate (%)", default=5, cast=int)
            try:
                deployer = w3.eth.accounts[0]
                addr = w3.eth.accounts[raw]
                w3.provider.make_request("evm_setAutomine", [True])
                w3.provider.make_request("evm_setIntervalMining", [0])
                setup_bot_on_chain(w3, contracts, deployer, addr, usdc, mevi)
                w3.provider.make_request("evm_setAutomine", [False])
                w3.provider.make_request("evm_setIntervalMining", [block_ms])
                launch_window(
                    f"MEV BOT #{raw}",
                    ["python", "scripts/mev_bot.py",
                     "--account", raw,
                     "--usdc", 0, "--mevi", 0,
                     "--max-swap", max_swap,
                     "--attack-rate", attack_pct],
                    env_extra={"BLOCK_INTERVAL_MS": str(block_ms)}
                )
                if sim_config is not None:
                    sim_config.setdefault('bots', []).append({
                        "account_idx": raw,
                        "usdc": usdc, "mevi": mevi,
                        "max_swap": max_swap,
                        "attack_rate": attack_pct / 100.0,
                    })
                print(f"  ✅ Bot acc#{raw} aggiunto e avviato ({attack_pct}% rate, max {max_swap} USDC)")
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "14":
            # ── Rete oracle ──
            try:
                show_oracle_network(w3, contracts)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "15":
            # ── Watchlist oracle ──
            try:
                show_watchlist(w3, contracts)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "16":
            # ── Slash report ──
            print(f"\n  {_c('── INVIA SLASH REPORT ──', _BOLD + _CYAN)}")
            raw_rep = ask("Reporter (idx o 0x...)", default="9")
            raw_acc = ask("Oracle accusato (idx o 0x...)", default="10")
            try:
                reporter = w3.eth.accounts[int(raw_rep)] if raw_rep.isdigit() else Web3.to_checksum_address(raw_rep)
                accused  = w3.eth.accounts[int(raw_acc)] if raw_acc.isdigit() else Web3.to_checksum_address(raw_acc)
                submit_slash_report(w3, contracts, reporter, accused)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "17":
            # ── Reintegra oracle slashato ──
            print(f"\n  {_c('── REINTEGRA ORACLE ──', _BOLD + _CYAN)}")
            raw = ask("Oracle da reintegrare (idx o 0x...)", default="10")
            try:
                oracle_addr = w3.eth.accounts[int(raw)] if raw.isdigit() else Web3.to_checksum_address(raw)
                oracle_reintegrate(w3, contracts, oracle_addr)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "18":
            # ── Reset deviation score ──
            print(f"\n  {_c('── RESET DEVIATION SCORE ──', _BOLD + _CYAN)}")
            raw = ask("Oracle (idx o 0x...)", default="10")
            try:
                oracle_addr = w3.eth.accounts[int(raw)] if raw.isdigit() else Web3.to_checksum_address(raw)
                oracle_reset_deviation(w3, contracts, oracle_addr)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "19":
            # ── Inietta guasto fraud_score oracle ──
            try:
                oracle_inject_fault(w3, contracts)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "20":
            # ── Lista guasti attivi ──
            try:
                oracle_list_faults(w3, contracts)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "21":
            # ── Gestione probabilità jury ──
            try:
                oracle_manage_jury_probs(w3, contracts)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "22":
            # ── Rispondi CAPTCHA Platinum ──
            print(f"\n  {_c('── RISPOSTA CAPTCHA PLATINUM ──', _BOLD + _CYAN)}")
            raw = ask("Indirizzo o indice trader", default="4")
            try:
                trader_addr = w3.eth.accounts[int(raw)]
            except (ValueError, IndexError):
                trader_addr = Web3.to_checksum_address(raw)
            try:
                ins = contracts["MEVInsurance"]
                req = ins.functions.platinumRequests(trader_addr).call()
                # platinumRequests getter omits address[3] assignedOracles:
                # [0]=desiredMaxSwap [1]=stakeDeposited [2]=answerHash [3]=challengeText
                # [4]=approveVotes [5]=rejectVotes [6]=totalVotes [7]=resolved
                # [8]=challengeSet [9]=answerSubmitted [10]=timestamp
                challenge_set    = req[8]  if len(req) > 8  else False
                resolved         = req[7]  if len(req) > 7  else True
                answer_submitted = req[9]  if len(req) > 9  else False
                challenge_text   = req[3]  if len(req) > 3  else ""
                stake_deposited  = req[1]  if len(req) > 1  else 0
                if stake_deposited == 0:
                    print(f"  ⚠️  Nessuna richiesta Platinum trovata per questo account.")
                    print(f"      Invia prima una richiesta con [4].")
                elif not challenge_set:
                    print(f"  ⚠️  CAPTCHA non ancora pubblicato dagli oracle.")
                    print(f"      Attendi qualche secondo e riprova.")
                elif resolved:
                    print(f"  ⚠️  Richiesta già risolta (tier aggiornato o rigettata).")
                elif answer_submitted:
                    print(f"  ⚠️  Risposta già inviata. Attendi il verdetto degli oracle.")
                else:
                    answer = _decode_leet(challenge_text)
                    print(f"\n  Challenge CAPTCHA: {_c(challenge_text, _YELLOW)}")
                    print(f"  Risposta auto-decodificata: {_c(answer, _GREEN)}")
                    w3.provider.make_request("evm_setAutomine", [True])
                    w3.provider.make_request("evm_setIntervalMining", [0])
                    utils.send_tx(w3, ins.functions.submitCaptchaAnswer(answer), trader_addr)
                    w3.provider.make_request("evm_setAutomine", [False])
                    w3.provider.make_request("evm_setIntervalMining", [block_ms])
                    print(f"  ✅ Risposta '{answer}' inviata! Gli oracle voteranno il verdetto.")
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "23":
            # ── Fonda pool ETH oracle ──
            deployer = w3.eth.accounts[0]
            try:
                fund_eth_pool(w3, contracts, deployer, block_ms)
            except Exception as e:
                print(f"  ❌ Errore: {e}")

        elif cmd == "q":
            if ask_yn("Uscire senza generare report?", default=False):
                return "quit"

        else:
            print(f"  ⚠️  Comando non riconosciuto. Digita '0' per il menu.")

# ──────────────────────────────────────────────────────────────
#  REPORT FINALE
# ──────────────────────────────────────────────────────────────

def generate_report(w3, contracts, sim_config, gov_params):
    """Genera un report testuale completo della simulazione."""
    import utils

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOGS_DIR / f"report_{ts}.txt"
    LOGS_DIR.mkdir(exist_ok=True)

    lines = []
    add = lines.append

    add("=" * 70)
    add("      MEV INSURANCE — REPORT SIMULAZIONE")
    add(f"      Generato: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    add("=" * 70)

    # ── Configurazione ──
    add("\n[CONFIGURAZIONE SIMULAZIONE]")
    add(f"  Blocco interval: {sim_config.get('block_ms', '?')} ms")
    dp = sim_config.get('deploy_params', {})
    if dp:
        add(f"  MockAMM riserve: {dp.get('amm_mevi', '?'):,} MEVI + {dp.get('amm_usdc', '?'):,} USDC")
        add(f"  Pool MEVI:       {dp.get('pool_mevi', '?'):,} MEVI")
        add(f"  Pool ETH:        {dp.get('pool_eth', '?'):,} ETH")
    add(f"  Trader configurati: {len(sim_config.get('traders', []))}")
    for i, t in enumerate(sim_config.get('traders', [])):
        add(f"    Trader #{i}: acc#{t['account_idx']} | {t['usdc']} USDC | {t['mevi']} MEVI | "
            f"auto={t.get('auto', False)} | interval={t.get('interval', '?')}s | "
            f"range={t.get('amount_min', '?')}-{t.get('amount_max', '?')} USDC")
    add(f"  Bot configurati: {len(sim_config.get('bots', []))}")
    for i, b in enumerate(sim_config.get('bots', [])):
        add(f"    Bot #{i}: acc#{b['account_idx']} | {b['usdc']} USDC | {b['mevi']} MEVI | "
            f"max_swap={b['max_swap']} USDC | attack_rate={int(b['attack_rate']*100)}%")
    add(f"  Oracle configurati: {len(sim_config.get('oracles', []))}")
    for i, o in enumerate(sim_config.get('oracles', [])):
        add(f"    Oracle #{i}: {o[:20]}...")

    # ── Parametri Governance ──
    add("\n[PARAMETRI GOVERNANCE (Tabella 8)]")
    for k, v in gov_params.items():
        meta = GOV_DEFAULTS.get(k, {})
        desc = meta.get('desc', k)
        unit = meta.get('unit', '')
        add(f"  {k:<22} = {v:>10}  {unit:>4}  ({desc})")

    # ── Stato Pool Finale ──
    add("\n[STATO POOL FINALE]")
    try:
        ins  = contracts["MEVInsurance"]
        mevi = contracts["MEVToken"]
        oreg = contracts["OracleRegistry"]

        pool_mevi = utils.from_wei(mevi.functions.balanceOf(ins.address).call())
        pool_eth  = float(Web3.from_wei(ins.functions.getPoolEthBalance().call(), 'ether'))
        active    = oreg.functions.activeOracleCount().call()
        n_oracle  = oreg.functions.getOracleCount().call()
        block     = w3.eth.block_number

        add(f"  Blocco finale:     {block}")
        add(f"  Pool MEVI:         {pool_mevi:>14.4f} MEVI")
        add(f"  Pool ETH:          {pool_eth:>14.6f} ETH")
        add(f"  Oracle attivi:     {active}/{n_oracle}")
    except Exception as e:
        add(f"  Errore lettura pool: {e}")

    # ── Log eventi (da simulation.log) ──
    log_file = LOGS_DIR / "simulation.log"
    if log_file.exists():
        with open(log_file, encoding="utf-8") as f:
            raw_lines = f.readlines()

        # Filtra per categoria
        tx_lines     = [l for l in raw_lines if "[TRADE]" in l or "[SHIELD]" in l]
        oracle_lines = [l for l in raw_lines if "[CLAIM]" in l or "[ORC-" in l]
        pool_lines   = [l for l in raw_lines if "[POOL]" in l]
        attack_lines = [l for l in raw_lines if "[ATTACK]" in l or "[BOT]" in l]

        add(f"\n[TX TRADER ({len(tx_lines)} eventi)]")
        for l in tx_lines:
            add("  " + l.rstrip())

        add(f"\n[ATTACCHI MEV BOT ({len(attack_lines)} eventi)]")
        for l in attack_lines:
            add("  " + l.rstrip())

        add(f"\n[VOTAZIONI ORACLE ({len(oracle_lines)} eventi)]")
        for l in oracle_lines:
            add("  " + l.rstrip())

        add(f"\n[VARIAZIONI POOL ({len(pool_lines)} eventi)]")
        for l in pool_lines:
            add("  " + l.rstrip())

        add(f"\n[LOG COMPLETO: {len(raw_lines)} righe totali in simulation.log]")
    else:
        add("\n[LOG: simulation.log non trovato]")

    add("\n" + "=" * 70)
    add("  Fine report.")
    add("=" * 70)

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n  {_c('Report generato:', _GREEN)} {out}")
    return out


#configurazione per tutti i tipi di simulazione
# ──────────────────────────────────────────────────────────────
#  PRESET SIMULAZIONI 
# ──────────────────────────────────────────────────────────────

SIM_PARAMS_FILE = CONFIG_DIR / "sim_params.json"

def _save_sim_params(scenario_id, scenario_name, key_params: dict):
    """Salva in config/sim_params.json per il monitor live."""
    data = {
        "scenario": scenario_id,
        "name": scenario_name,
        "params": key_params,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(SIM_PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _base_traders(n=5, interval=15, amount_min=50, amount_max=150, coverage=2):
    return [
        {"account_idx": 4 + i, "usdc": 1000.0, "mevi": 500.0, "auto": True,
         "interval": interval, "amount_min": amount_min, "amount_max": amount_max,
         "coverage": coverage, "tx_count": 0}
        for i in range(n)
    ]


def _base_bots(attack_rate_pct=5, max_swap=100.0, n=1):
    return [
        {"account_idx": 1 + i, "usdc": 10000.0, "mevi": 1000.0,
         "max_swap": max_swap, "attack_rate": attack_rate_pct / 100.0}
        for i in range(n)
    ]


def _base_gov(**overrides):
    gov = {k: v["val"] for k, v in GOV_DEFAULTS.items()}
    gov["patt"] = 1000

    gov.update(overrides)
    return gov


def collect_sim_preset():
    """Menu preset simulazioni ."""
    section("SIMULAZIONI PRESET ")

    print(f"""
  {_c('Asse 1 — Economia bot', _BOLD + _YELLOW)}
    [1.1] Sweep attack_rate   {{0%, 5%, 15%, 30%, 60%}}
    [1.2] Sweep eFNR          {{1000, 3000, 5000, 8000}} bps, attack_rate=20%
    [1.3] Sweep max_swap      {{20, 100, 500}} USDC, attack_rate=20%

  {_c('Asse 2 — Economia trader', _BOLD + _YELLOW)}
    [2.1] Gemelli insured vs naked   (2 trader, stesso attack_rate)
    [2.2] Breakeven attack_rate      {{2%, 5%, 10%, 20%, 40%}}, patt_gov=10%
    [2.3] Sweep coverage             3 trader (LOW/MED/HIGH), attack_rate=15%
    [2.4] Mismatch patt/attack_rate  patt_gov=5%, attack_rate=25%

  {_c('Asse 3 — Solvenza pool', _BOLD + _YELLOW)}
    [3.1] Baseline 30 giorni         (preset base, 30 min reali)
    [3.2] Sweep gasRefundAmount      {{0.002, 0.01, 0.02, 0.034, 0.05}} ETH
    [3.3] Pool concentrato           patt_gov=5%, attack_rate=40%
    [3.4] Slashing cascade           baseline + guasti oracle via menu [19]
    [3.5] Reintegrazione post-slash  baseline + oracle slash/reintegra

  {_c('Asse 4 — End-to-end', _BOLD + _YELLOW)}
    [4.1] Latenza claim              baseline + misura blocchi claim
    [4.2] Gas reali                  baseline (log gasUsed ogni tx)
    [4.3] Flusso Platinum completo   1 trader → richiesta Platinum
    [4.4] Ciclo watchlist            baseline + guasto dev+12 oracle
""")

    scelta = ask("Scenario (es. 1.1, 2.3, 3.1)", default="3.1")
    cfg = {}

    # ── HELPER: durata e day_advance ──────────────────────────────────
    def _ask_duration(default_min=10, default_day=1):
        dur = ask("Durata simulazione in minuti (0=infinita)", default=default_min, cast=int)
        day = ask("Durata 1 giorno simulato in minuti reali (0=nessun avanzamento auto)", default=default_day, cast=int)
        return dur, day

    # ─────────────────────────────────────────────────────────────────
    if scelta == "1.1":
        vals = [0, 5, 15, 30, 60]
        print(f"\n  Sweep attack_rate: {vals}%")
        ar = ask(f"Scegli attack_rate per questa run (%)", default=5, cast=int, choices=vals)
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=ar)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov()
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("1.1", f"Sweep attack_rate — run {ar}%",
                         {"attack_rate_bps": ar * 100, "max_swap_usdc": 100,
                          "patt_gov_bps": 1000, "eFNR_bps": 2000})

    elif scelta == "1.2":
        vals = [1000, 3000, 5000, 8000]
        print(f"\n  Sweep eFNR: {vals} bps | attack_rate fisso 20%")
        fnr = ask("Scegli eFNR (bps)", default=2000, cast=int, choices=vals)
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=20)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov(eFNR=fnr)
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("1.2", f"Sweep eFNR — run {fnr} bps",
                         {"attack_rate_bps": 2000, "eFNR_bps": fnr,
                          "patt_gov_bps": 1000, "max_swap_usdc": 100})

    elif scelta == "1.3":
        vals = [20, 100, 500]
        print(f"\n  Sweep max_swap: {vals} USDC | attack_rate fisso 20%")
        ms = ask("Scegli max_swap (USDC)", default=100, cast=int, choices=vals)
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=20, max_swap=float(ms))
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov()
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("1.3", f"Sweep max_swap — run {ms} USDC",
                         {"attack_rate_bps": 2000, "max_swap_usdc": ms,
                          "patt_gov_bps": 1000, "eFNR_bps": 2000})

    #simulazione base per prof
    elif scelta == "2.1":
        print(f"\n  5 insured (acc#2–6) vs 5 naked (acc#7–11) — importi 50–100 / 100–150")
        print(f"  Amount asimmetrici: insured 50-100/100-150, naked 50-100/100-150")
        ar = ask("attack_rate bot (%)", default=15, cast=int)
        cfg['block_ms'] = 1500
        cfg['traders'] = [
            {"account_idx": 2, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": False},
            {"account_idx": 3, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": False},
            {"account_idx": 4, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": False},
            {"account_idx": 5, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 100.0, "amount_max": 150.0,
             "coverage": 2, "tx_count": 0, "naked": False},
            {"account_idx": 6, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": False},
            {"account_idx": 7, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 100.0, "amount_max": 150.0,
             "coverage": 2, "tx_count": 0, "naked": True},
            {"account_idx": 8, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": True},
            {"account_idx": 9, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 100.0, "amount_max": 150.0,
             "coverage": 2, "tx_count": 0, "naked": True},
             {"account_idx": 10, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 100.0,
             "coverage": 2, "tx_count": 0, "naked": True},
            {"account_idx": 11, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 100.0, "amount_max": 150.0,
             "coverage": 2, "tx_count": 0, "naked": True},
        ]
        cfg['bots'] = _base_bots(attack_rate_pct=ar)
        cfg['oracle_count'] = 7; cfg['oracle_start_idx'] = 12; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov()
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params(
                            "2.1",
                            f"5 insured vs 5 naked — attack_rate {ar}%",
                            {
                                "attack_rate_bps": ar * 100,
                                "trader_insured_accs": [2,3,4,5,6],
                                "trader_naked_accs": [7,8,9,10,11],
                                "patt_gov_bps": 1000
                            }
                        )

    elif scelta == "2.2":
        vals = [2, 5, 10, 20, 40]
        print(f"\n  Breakeven attack_rate: {vals}% | patt_gov=10% fisso")
        ar = ask("Scegli attack_rate (%)", default=10, cast=int, choices=vals)
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=ar)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov(patt=1000)
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("2.2", f"Breakeven attack_rate — run {ar}%",
                         {"attack_rate_bps": ar * 100, "patt_gov_bps": 1000,
                          "max_swap_usdc": 100, "eFNR_bps": 2000})

    elif scelta == "2.3":
        print(f"\n  3 trader identici: acc#4=LOW, acc#5=MED, acc#6=HIGH | attack_rate=15%")
        cfg['block_ms'] = 1500
        cfg['traders'] = [
            {"account_idx": 4 + i, "usdc": 1000.0, "mevi": 500.0, "auto": True,
             "interval": 15, "amount_min": 50.0, "amount_max": 150.0,
             "coverage": i, "tx_count": 0}
            for i in range(3)
        ]
        cfg['bots'] = _base_bots(attack_rate_pct=15)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov()
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("2.3", "Sweep coverage — LOW/MED/HIGH, attack_rate=15%",
                         {"attack_rate_bps": 1500, "patt_gov_bps": 1000,
                          "coverage_acc4": "LOW(0)", "coverage_acc5": "MED(1)",
                          "coverage_acc6": "HIGH(2)"})

    elif scelta == "2.4":
        print(f"\n  Mismatch: patt_gov=5% (governance), attack_rate=25% (bot reale)")
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=25)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov(patt=500)  # 5%
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("2.4", "Mismatch patt/attack_rate — patt_gov=5%, rate=25%",
                         {"attack_rate_bps": 2500, "patt_gov_bps": 500,
                          "gap_bps": 2000, "max_swap_usdc": 100})

    elif scelta == "3.1":
        print(f"\n  Baseline 30 giorni — preset base, 1 giorno ogni 1 min reale")
        cfg = get_base_preset()
        cfg['duration_min'] = ask("Durata simulazione in minuti reali (default 30)", default=30, cast=int)
        cfg['day_duration_min'] = ask("Durata 1 giorno simulato in minuti reali", default=1, cast=int)
        _save_sim_params("3.1", "Baseline 30 giorni",
                         {"attack_rate_bps": 500, "patt_gov_bps": 1000,
                          "max_swap_usdc": 100, "oracle_count": 12})

    elif scelta == "3.2":
        vals = [0.002, 0.01, 0.02, 0.034, 0.05]
        print(f"\n  Sweep gasRefundAmount: {vals} ETH")
        gr_str = ask(f"Scegli gasRefundAmount (ETH)", default="0.01")
        try:
            gr_eth = float(gr_str)
        except ValueError:
            gr_eth = 0.01
        gr_wei = int(gr_eth * 1e18)
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=20)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov(gasRefundAmount=gr_wei)
        dur, day = _ask_duration()
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("3.2", f"Sweep gasRefundAmount — run {gr_eth} ETH",
                         {"gasRefundAmount_eth": gr_eth, "attack_rate_bps": 2000,
                          "patt_gov_bps": 1000, "eFNR_bps": 2000})

    elif scelta == "3.3":
        print(f"\n  Pool concentrato: patt_gov=5%, attack_rate=40%, misura solvenza nel tempo")
        cfg['block_ms'] = 1500
        cfg['traders'] = _base_traders()
        cfg['bots'] = _base_bots(attack_rate_pct=40)
        cfg['oracle_count'] = 12; cfg['oracle_start_idx'] = 9; cfg['oracle_base_stake'] = 0.1
        cfg['gov_params'] = _base_gov(patt=500)
        dur, day = _ask_duration(default_min=30, default_day=1)
        cfg['duration_min'] = dur; cfg['day_duration_min'] = day
        _save_sim_params("3.3", "Pool concentrato — patt_gov=5%, attack_rate=40%",
                         {"attack_rate_bps": 4000, "patt_gov_bps": 500,
                          "gap_bps": 3500, "max_swap_usdc": 100})

    elif scelta in ("3.4", "3.5"):
        label = "Slashing cascade" if scelta == "3.4" else "Reintegrazione post-slash"
        print(f"\n  {label}: avvia baseline, poi usa menu [19] per iniettare guasti oracle.")
        cfg = get_base_preset()
        cfg['duration_min'] = ask("Durata simulazione in minuti (0=infinita)", default=0, cast=int)
        cfg['day_duration_min'] = ask("Durata 1 giorno simulato in minuti reali", default=1, cast=int)
        _save_sim_params(scelta, label,
                         {"nota": "Inietta guasti via menu [19] dopo l'avvio",
                          "attack_rate_bps": 500, "patt_gov_bps": 1000})

    elif scelta in ("4.1", "4.2", "4.3", "4.4"):
        labels = {
            "4.1": "Latenza claim — misura blocchi da submitClaim a payout",
            "4.2": "Gas reali — logga gasUsed ogni commit/reveal/claim",
            "4.3": "Flusso Platinum completo — usa menu [4] dopo avvio",
            "4.4": "Ciclo watchlist — inietta dev+12 su un oracle",
        }
        label = labels.get(scelta, f"Asse {scelta}")
        print(f"\n  {label}")
        cfg = get_base_preset()
        cfg['duration_min'] = ask("Durata simulazione in minuti (0=infinita)", default=0, cast=int)
        cfg['day_duration_min'] = ask("Durata 1 giorno simulato in minuti reali", default=1, cast=int)
        _save_sim_params(scelta, label,
                         {"nota": "Interazione manuale via menu dopo avvio",
                          "attack_rate_bps": 500, "patt_gov_bps": 1000})

    else:
        print(f"  ⚠️  Scenario '{scelta}' non riconosciuto — uso BASE preset.")
        cfg = get_base_preset()
        cfg['duration_min'] = 10
        cfg['day_duration_min'] = 1

    cfg['block_ms'] = cfg.get('block_ms', 1500)
    os.environ["BLOCK_INTERVAL_MS"] = str(cfg['block_ms'])
    return cfg


# ──────────────────────────────────────────────────────────────
#  RACCOLTA CONFIGURAZIONE
# ──────────────────────────────────────────────────────────────

def collect_config():
    """Raccoglie tutta la configurazione dalla console."""
    banner()

    section("PRESET SIMULAZIONE")
    print(f"  [1] Simulazione BASE  — 5 trader, 3 bot 5%, 12 oracle, patt=10%")
    print(f"  [2] Configurazione MANUALE")
    print(f"  [3] Preset SIMULAZIONI TESI  — scegli scenario (assi 1–4)")
    scelta = ask("Scelta", default=1, cast=int, choices=[1, 2, 3])

    if scelta == 3:
        return collect_sim_preset()

    if scelta == 1:
        cfg = get_base_preset()
        show_preset_summary(cfg)
        os.environ["BLOCK_INTERVAL_MS"] = str(cfg['block_ms'])
        if not ask_yn("Confermi questa configurazione?", default=True):
            print(f"  {_c('Annullato. Riavvia per scegliere manuale.', _YELLOW)}")
            sys.exit(0)
        duration_min = ask("Durata simulazione in minuti (0 = infinita)", default=10, cast=int)
        cfg['duration_min'] = duration_min
        day_duration_min = ask("Durata di 1 giorno simulato in minuti reali (0 = nessun avanzamento automatico)", default=1, cast=int)
        cfg['day_duration_min'] = day_duration_min
        # Nessun preset tesi — rimuovi sim_params.json vecchio
        if SIM_PARAMS_FILE.exists():
            SIM_PARAMS_FILE.unlink()
        return cfg

    cfg = {}

    section("PASSO 1/5 — TEMPO BLOCCO")
    block_ms = ask("Millisecondi per blocco (1000-12000)", default=1500, cast=int)
    cfg['block_ms'] = block_ms
    os.environ["BLOCK_INTERVAL_MS"] = str(block_ms)

    section("PASSO 2/5 — TRADER")
    n_traders = ask("Quanti trader?", default=1, cast=int)
    traders = []
    total_accounts = 20  # Hardhat default

    for i in range(n_traders):
        print(f"\n  >> Trader #{i}")
        idx   = ask(f"  Account Hardhat idx", default=2 + i, cast=int)
        usdc  = ask(f"  USDC iniziali", default=1000.0, cast=float)
        mevi  = ask(f"  MEVI iniziali", default=500.0, cast=float)
        auto  = ask_yn(f"  Modalità automatica?", default=True)
        t = {"account_idx": idx, "usdc": usdc, "mevi": mevi, "auto": auto}
        if auto:
            t['interval']   = ask("  Intervallo tra TX (secondi)", default=15, cast=float)
            t['amount_min'] = ask("  Importo minimo swap (USDC)", default=50.0, cast=float)
            t['amount_max'] = ask("  Importo massimo swap (USDC)", default=150.0, cast=float)
            t['coverage']   = ask("  Copertura (0=LOW 1=MED 2=HIGH)", default=2, cast=int)
            t['tx_count']   = ask("  Numero TX (0=infinito)", default=0, cast=int)
        traders.append(t)
    cfg['traders'] = traders

    section("PASSO 3/5 — MEV BOT")
    n_bots = ask("Quanti bot MEV?", default=1, cast=int)
    bots = []
    for i in range(n_bots):
        print(f"\n  >> Bot #{i}")
        idx         = ask(f"  Account Hardhat idx", default=1, cast=int)
        usdc        = ask(f"  USDC iniziali", default=10000.0, cast=float)
        mevi        = ask(f"  MEVI iniziali", default=1000.0, cast=float)
        max_swap    = ask(f"  Max USDC per attacco", default=100.0, cast=float)
        attack_pct  = ask(f"  Percentuale attacchi (0-100)", default=70, cast=int)
        bots.append({
            "account_idx": idx,
            "usdc": usdc,
            "mevi": mevi,
            "max_swap": max_swap,
            "attack_rate": attack_pct / 100.0
        })
    cfg['bots'] = bots

    section("PASSO 4/5 — ORACLE")
    print(f"  Lo stake cresce logaritmicamente (ogni oracle paga il minimo corrente).")
    n_oracles  = ask("Quanti oracle deployare?", default=7, cast=int)
    start_idx  = ask("Account Hardhat di partenza per oracle", default=10, cast=int)
    base_stake = ask("Stake base ETH (primo oracle)", default=0.1, cast=float)
    cfg['oracle_count']      = n_oracles
    cfg['oracle_start_idx']  = start_idx
    cfg['oracle_base_stake'] = base_stake

    duration_min = ask("Durata simulazione in minuti (0 = infinita)", default=10, cast=int)
    cfg['duration_min'] = duration_min
    day_duration_min = ask("Durata di 1 giorno simulato in minuti reali (0 = nessun avanzamento automatico)", default=5, cast=int)
    cfg['day_duration_min'] = day_duration_min

    section("PASSO 5/5 — GOVERNANCE")
    gov_params = collect_governance_params()
    cfg['gov_params'] = gov_params

    return cfg

# ──────────────────────────────────────────────────────────────
#  RISERVE INIZIALI
# ──────────────────────────────────────────────────────────────

def collect_deploy_params() -> dict:
    """Chiede le riserve iniziali del MockAMM e del pool assicurativo."""
    section("RISERVE INIZIALI")
    print(f"""
  {_c('MockAMM — liquidità del DEX simulato', _BOLD)}
    Suggerimenti:
      Illiquido  →  10k MEVI + 10k USDC   (pool piccolo, slippage alto)
      Standard   → 100k MEVI + 100k USDC  (default corrente)
      Liquido    →   1M MEVI + 1M USDC    (pool profondo, slippage basso)

  {_c('Pool MEVI — fondo assicurativo', _BOLD)}
    Suggerimenti:
      Stressato  → 100k MEVI
      Standard   → 500k MEVI              (default corrente)
      Capiente   →   2M MEVI
""")

    amm_mevi = ask("MockAMM — MEVI iniziali", default=100000, cast=int)
    amm_usdc = ask("MockAMM — USDC iniziali", default=100000, cast=int)
    pool_mevi = ask("Pool assicurativo — MEVI iniziali", default=500000, cast=int)

    print(f"""
  {_c('Pool ETH — fondo oracle rewards', _BOLD)}
    Suggerimenti:
      Minimo    →   100 ETH   (test rapidi)
      Standard  →  5000 ETH   (default corrente)
      Capiente  → 20000 ETH   (simulazioni lunghe)
""")
    pool_eth = ask("Pool oracle — ETH iniziali", default=5000, cast=int)

    params = {"amm_mevi": amm_mevi, "amm_usdc": amm_usdc, "pool_mevi": pool_mevi, "pool_eth": pool_eth}

    print(f"\n  {_c('Riserve configurate:', _GREEN)}")
    print(f"    MockAMM:  {amm_mevi:>10,} MEVI  +  {amm_usdc:>10,} USDC")
    print(f"    Pool:     {pool_mevi:>10,} MEVI")
    print(f"    Pool ETH: {pool_eth:>10,} ETH")

    return params


def _save_deploy_params(params: dict) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(CONFIG_DIR / "deploy_params.json", "w") as f:
        json.dump(params, f, indent=2)


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────

def _ask_key_params(cfg: dict) -> None:
    """Chiede P_min, M_base ed E (eFNR) prima di ogni simulazione."""
    section("PARAMETRI CHIAVE PREMIUM")
    gov = cfg.setdefault('gov_params', {k: v['val'] for k, v in GOV_DEFAULTS.items()})

    # P_min
    cur_pmin = gov.get('pmin', 0)
    print(f"  P_min — premio minimo garantito (% sul valore swap)")
    print(f"  0 = disabilitato  |  150 = 1.5%  |  200 = 2.0%  |  300 = 3.0%")
    print(f"  Valore attuale: {cur_pmin} bps")
    pmin = ask("P_min (bps, 0=off)", default=cur_pmin, cast=int)
    gov['pmin'] = pmin
    label_pmin = f"{pmin} bps ({pmin/100:.2f}%)" if pmin > 0 else "disabilitato"
    print(f"  {_c(f'P_min: {label_pmin}', _GREEN)}")

    print()

    # M_base
    cur_mbase = gov.get('mBase', GOV_DEFAULTS['mBase']['val'])
    print(f"  M_base — margine base del protocollo sul premio")
    print(f"  Es: 1000 = 10%  |  2000 = 20% (default)  |  3000 = 30%")
    print(f"  Valore attuale: {cur_mbase} bps ({cur_mbase/100:.1f}%)")
    mbase = ask("M_base (bps)", default=cur_mbase, cast=int)
    gov['mBase'] = mbase
    print(f"  {_c(f'M_base: {mbase} bps ({mbase/100:.1f}%)', _GREEN)}")

    print()

    # E (eFNR)
    cur_efnr = gov.get('eFNR', GOV_DEFAULTS['eFNR']['val'])
    print(f"  E — false negative rate (prob. attacco non rilevato dall'oracolo)")
    print(f"  Es: 500 = 5%  |  2000 = 20% (default)  |  5000 = 50%")
    print(f"  Valore attuale: {cur_efnr} bps ({cur_efnr/100:.1f}%)")
    efnr = ask("E (bps)", default=cur_efnr, cast=int)
    gov['eFNR'] = efnr
    print(f"  {_c(f'E: {efnr} bps ({efnr/100:.1f}%)', _GREEN)}")


def main():
    LOGS_DIR.mkdir(exist_ok=True)

    # 1. Raccoglie configurazione
    cfg = collect_config()
    block_ms = cfg['block_ms']

    # 1b. Sempre chiedi P_min, M_base ed E (non chiesti nei preset automatici)
    _ask_key_params(cfg)

    # 2. Avvio nodo Hardhat in nuova finestra
    section("AVVIO NODO HARDHAT")
    print(f"  Block interval: {block_ms} ms")
    if ask_yn("Il nodo Hardhat è già in esecuzione?", default=False):
        print(f"  {_c('Nodo già attivo — skip.', _GREEN)}")
    else:
        launch_window("HARDHAT NODE",
                      ["npx", "hardhat", "node"],
                      env_extra={"BLOCK_INTERVAL_MS": str(block_ms)})
        print(f"  Nodo avviato in nuova finestra.")

    if not wait_for_node():
        print(f"\n  {_c('ERRORE: nodo non raggiungibile dopo 180s.', _RED)}")
        print(f"  Controlla la finestra 'HARDHAT NODE' per errori.")
        print(f"  Possibili cause:")
        print(f"    - Porta 8545 già occupata (chiudi altri nodi Hardhat)")
        print(f"    - npx/Node.js non installato o non nel PATH")
        print(f"    - Primo avvio lento (compilazione contratti) — riprova")
        input(f"\n  Premi INVIO per uscire...")
        return

    # 3. Riserve iniziali
    deploy_params = collect_deploy_params()
    _save_deploy_params(deploy_params)
    cfg['deploy_params'] = deploy_params

    # 4. Deploy contratti
    section("DEPLOY CONTRATTI")
    print(f"  Esecuzione deploy_all.js...")
    env_deploy = os.environ.copy()
    env_deploy["BLOCK_INTERVAL_MS"] = str(block_ms)
    result = subprocess.run(
        "npx hardhat run scripts/deploy_all.js --network localhost",
        cwd=str(ROOT),
        env=env_deploy,
        capture_output=False,
        shell=True
    )
    if result.returncode != 0:
        print(f"  {_c('ERRORE: deploy fallito.', _RED)}")
        input(f"  Premi INVIO per uscire...")
        return

    if not wait_for_deploy():
        print(f"  {_c('ERRORE: deployed_addresses.json non pronto.', _RED)}")
        input(f"  Premi INVIO per uscire...")
        return

    # 4. Connessione web3 e caricamento contratti
    import utils
    w3 = utils.get_web3()
    contracts = utils.get_all_contracts(w3)
    deployer  = w3.eth.accounts[0]

    # 5. Parametri governance
    section("CONFIGURAZIONE GOVERNANCE")
    apply_governance_params(w3, contracts, deployer, cfg['gov_params'])

    # 6. Setup attori
    section("SETUP ATTORI")

    # Automine per operazioni multiple
    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    trader_addrs = []
    for t in cfg['traders']:
        addr = w3.eth.accounts[t['account_idx']]
        trader_addrs.append(addr)
        setup_trader_on_chain(w3, contracts, deployer, addr, t['usdc'], t['mevi'])

    for b in cfg['bots']:
        addr = w3.eth.accounts[b['account_idx']]
        setup_bot_on_chain(w3, contracts, deployer, addr, b['usdc'], b['mevi'])

    oracle_addrs = [w3.eth.accounts[cfg['oracle_start_idx'] + i]
                    for i in range(cfg['oracle_count'])]

    # Salva oracle, bot, trader, deployer in actors.json
    bot_addrs    = [w3.eth.accounts[b['account_idx']] for b in cfg['bots']]
    trader_addrs = [w3.eth.accounts[t['account_idx']] for t in cfg['traders']]
    actors_data = {
        "oracles":           oracle_addrs,
        "bots":              bot_addrs,
        "traders":           trader_addrs,
        "deployer":          deployer,
        "oracle_base_stake": cfg['oracle_base_stake'],
    }
    utils.save_actors(actors_data)
    print(f"  ✅ actors.json salvato ({len(oracle_addrs)} oracle, {len(bot_addrs)} bot, {len(trader_addrs)} trader)")

    # Ripristina interval mining
    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [block_ms])

    cfg['oracles'] = oracle_addrs

    # Pulizia file attacchi precedenti
    attacked_file = CONFIG_DIR / "attacked_txs.json"
    if attacked_file.exists():
        attacked_file.unlink()
        print(f"  🗑️  attacked_txs.json ripulito (nuova simulazione)")

    victims_file = CONFIG_DIR / "victims.json"
    if victims_file.exists():
        victims_file.unlink()
        print(f"  🗑️  victims.json ripulito (nuova simulazione)")

    for _cleanup in ["attack_log.json", "naked_stats.json"]:
        _cf = CONFIG_DIR / _cleanup
        if _cf.exists():
            _cf.unlink()
            print(f"  🗑️  {_cleanup} ripulito (nuova simulazione)")

    # Reset guasti oracle — simulazione parte senza guasti iniettati
    _save_test_state({"bugs": {}, "jury_probs": {}})

    # 7. Avvio daemon in nuove finestre
    section("AVVIO COMPONENTI")

    env_extra = {"BLOCK_INTERVAL_MS": str(block_ms)}

    # Oracle daemon
    launch_window("ORACLE DAEMON",
                  ["python", "scripts/oraclec.py"], env_extra=env_extra)
    print(f"  ✅ Oracle daemon avviato ({len(oracle_addrs)} oracle)")

    # Pool monitor (generates economic report on exit)
    launch_window("POOL MONITOR",
                  ["python", "scripts/monitor.py"], env_extra=env_extra)
    print(f"  ✅ Monitor avviato (Ctrl+C nella finestra per generare il report economico)")

    # ── Attendi che tutti gli oracle siano registrati prima di avviare i trader ──
    target_oracles = len(oracle_addrs)
    oreg = contracts["OracleRegistry"]
    print(f"  ⏳ Attendo registrazione {target_oracles} oracle...", end="", flush=True)
    _oracle_timeout = 120  # secondi max
    _t0 = time.time()
    while time.time() - _t0 < _oracle_timeout:
        try:
            active = oreg.functions.activeOracleCount().call()
            if active >= target_oracles:
                break
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(2)
    else:
        active = oreg.functions.activeOracleCount().call()
        print(f"\n  ⚠️  Timeout: solo {active}/{target_oracles} oracle registrati. Procedo comunque.")
    print(f" OK ({active}/{target_oracles} attivi)")

    # Bot MEV
    for i, b in enumerate(cfg['bots']):
        rate_pct = int(b['attack_rate'] * 100)
        launch_window(
            f"MEV BOT #{i}",
            ["python", "scripts/mev_bot.py",
             "--account", b['account_idx'],
             "--usdc",    0,        # già fondato
             "--mevi",    0,
             "--max-swap", b['max_swap'],
             "--attack-rate", rate_pct],
            env_extra=env_extra
        )
        print(f"  ✅ Bot #{i} avviato (acc#{b['account_idx']}, rate={rate_pct}%)")

    # Trader auto
    for i, t in enumerate(cfg['traders']):
        is_naked = t.get('naked', False)
        if t.get('auto'):
            cmd = [
                "python", "scripts/trader.py",
                "--account", t['account_idx'],
                "--auto",
                "--interval",   t['interval'],
                "--amount-min", t['amount_min'],
                "--amount-max", t['amount_max'],
                "--coverage",   t['coverage'],
                "--count",      t['tx_count'],
            ]
            if is_naked:
                cmd.append("--naked")
        else:
            cmd = ["python", "-i", "scripts/trader.py",
                   "--account", t['account_idx']]
        launch_window(f"TRADER #{i}{'(NUDO)' if is_naked else ''}", cmd, env_extra=env_extra)
        mode = "AUTO-NUDO" if (t.get('auto') and is_naked) else ("AUTO" if t.get('auto') else "INTERATTIVO")
        print(f"  ✅ Trader #{i} avviato (acc#{t['account_idx']}, {mode})")

    # 8. Auto day-advance thread (opzionale)
    _stop_day_advance = threading.Event()
    day_dur = cfg.get('day_duration_min', 0)
    if day_dur and day_dur > 0:
        section("AUTO DAY ADVANCE")
        start_auto_day_advance(w3, day_dur, _stop_day_advance)
        print(f"  ✅ Auto day advance attivo: 1 giorno ogni {day_dur} min reali")

    # AMM rebalancer — riporta le reserve al rapporto iniziale ogni 30s se drift >5%
    _stop_rebalancer = threading.Event()
    start_amm_rebalancer(w3, contracts, deployer, interval_s=30, stop_event=_stop_rebalancer)
    print(f"  ✅ AMM rebalancer attivo (controllo ogni 30s, soglia drift 5%)")

    # Pool logger — snapshot MEVI+ETH su simulation.log ogni 2 min
    _stop_pool_logger = threading.Event()
    start_pool_logger(w3, contracts, _stop_pool_logger, interval_s=120)
    print(f"  ✅ Pool logger attivo (snapshot ogni 2 min su simulation.log)")

    # 9. Menu interattivo
    section("SIMULAZIONE IN CORSO")
    print(f"  Tutti i componenti avviati.")
    print(f"  Block time: {block_ms} ms | "
          f"Trader: {len(cfg['traders'])} | "
          f"Bot: {len(cfg['bots'])} | "
          f"Oracle: {len(oracle_addrs)}")
    if day_dur and day_dur > 0:
        print(f"  Auto day advance: 1 giorno ogni {day_dur} min reali")
    print(f"\n  Usa il menu qui sotto per interagire con la simulazione.")
    print(f"  Le finestre separate mostrano i daemon in tempo reale.")

    result = interactive_menu(w3, contracts, cfg['traders'], block_ms,
                              duration_min=cfg.get('duration_min', 0),
                              sim_config=cfg)

    # Ferma thread day-advance e rebalancer
    _stop_day_advance.set()
    _stop_rebalancer.set()
    _stop_pool_logger.set()

    # 10. Report finale
    if result == "report":
        section("GENERAZIONE REPORT")
        report_path = generate_report(w3, contracts, cfg, cfg['gov_params'])
        print(f"\n  Simulazione terminata. Report salvato in:")
        print(f"  {_c(str(report_path), _CYAN)}")

    print(f"\n{_c('Arrivederci!', _BOLD)}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n{_c('═'*60, _RED)}")
        print(_c("  ERRORE FATALE — traceback completo:", _RED + _BOLD))
        print(_c('═'*60, _RED))
        traceback.print_exc()
        print(_c('═'*60, _RED))
    finally:
        input("\n  Premi INVIO per chiudere...")
