from __future__ import annotations

import utils
import argparse
import json
import os
import signal
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from web3 import Web3
from web3.exceptions import ContractLogicError

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
ABIS_DIR = os.getenv("ABIS_DIR", "./abis")
DEPLOYED_FILE = os.getenv("DEPLOYED_FILE", "./config/deployed_addresses.json")
ATTACKED_TXNS_FILE = os.getenv("ATTACKED_TXNS_FILE", "./config/attacked_txs.json")
SIM_PARAMS_FILE  = "./config/sim_params.json"
ACTORS_FILE      = "./config/actors.json"
ATTACK_LOG_FILE  = "./config/attack_log.json"
NAKED_STATS_FILE = "./config/naked_stats.json"
LOGS_DIR = Path(os.getenv("LOGS_DIR", "./logs"))
SNAPSHOT_INTERVAL_SEC = 120

LOGS_DIR.mkdir(exist_ok=True)

_deadline = time.time() + 120
while True:
    try:
        with open(DEPLOYED_FILE) as f:
            _addrs = json.load(f)
        if {"MEVToken", "MEVInsurance", "OracleRegistry"}.issubset(_addrs.keys()):
            break
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if time.time() > _deadline:
        raise TimeoutError(f"Timeout: {DEPLOYED_FILE} non disponibile")
    print(f"[monitor] In attesa del deploy ({DEPLOYED_FILE})...")
    time.sleep(3)

MEV_INSURANCE_ADDR = _addrs["MEVInsurance"]
ORACLE_REGISTRY_ADDR = _addrs["OracleRegistry"]
PREMIUM_CALC_ADDR = _addrs["PremiumCalculator"]
TIER_SYSTEM_ADDR = _addrs.get("TierSystem", "")
SLASHING_ADDR = _addrs.get("SlashingSystem", "")
TOKEN_ADDR = _addrs["MEVToken"]
AMM_ADDR = _addrs.get("MockAMM", "")

ERC20_MIN_ABI = [
    {"constant": True, "inputs": [{"name": "who", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False,
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "type": "function"},
]


def _load_abi(name: str, fallback: list[dict] | None = None) -> list[dict]:
    path = os.path.join(ABIS_DIR, f"{name}.json")
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
            return data["abi"] if isinstance(data, dict) and "abi" in data else data

    hardhat_path = os.path.join("artifacts", "contracts", f"{name}.sol", f"{name}.json")
    if os.path.isfile(hardhat_path):
        with open(hardhat_path) as f:
            return json.load(f)["abi"]

    if fallback is None:
        raise FileNotFoundError(
            f"ABI {name}.json non trovato né in {ABIS_DIR} né in artifacts/contracts/{name}.sol/"
        )
    return fallback


ORACLE_STATS_FILE = "./config/oracle_stats.json"


def generate_economic_report() -> Path:
    import re

    ts_now = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOGS_DIR / f"economic_report_{ts_now}.txt"

    swap_re = re.compile(
        r'\[(\d{2}:\d{2}:\d{2})\]\[  SWAP\] TRADER (0x[0-9a-fA-F]+) \| ([\d.]+) USDC'
        r' \| prot: (\S+)'
        r'(?:\s+\| premio: ([\d.]+) MEVI)?'
        r'.*?\| ricevuti: ([\d.]+) MEVI'
        r' \| ATTACCO: (SI|NO)'
        r'(?:\s+\| perdita: ([\d.]+) MEVI)?'
        r'(?:\s+\| rimborso: ([^\n]+))?'
    )
    pool_re = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\]\[\s*POOL\]\s+POOL UPDATE'
    r'\s*\|\s*mevi=([\d.]+)\s*MEVI'
    r'\s*\|\s*eth=([\d.]+)\s*ETH'
)

    log_file = LOGS_DIR / "simulation.log"
    raw_lines: list[str] = []
    if log_file.exists():
        with open(log_file, encoding="utf-8") as f:
            raw_lines = f.readlines()

    traders: dict = {}
    pool_snaps: list = []

    for line in raw_lines:
        m = swap_re.search(line)
        if m:
            ts_l, addr, usdc, prot, premio, ricevuti, attacco, perdita, rimborso = m.groups()
            addr = addr.lower()
            if addr not in traders:
                traders[addr] = {"prot": prot, "txs": [],
                                 "tot_mevi": 0.0, "tot_attacked": 0.0,
                                 "tot_premio": 0.0, "tot_rimborso": 0.0}
            d = traders[addr]
            d["prot"] = prot
            mevi_r = float(ricevuti) if ricevuti else 0.0
            pr     = float(premio)   if premio   else 0.0
            perd   = float(perdita)  if perdita  else 0.0
            rim_str = rimborso.strip() if rimborso else ""
            rim_val = 0.0
            if rim_str and rim_str not in ("NON ASSICURATO", "NO"):
                try:
                    rim_val = float(rim_str.split()[0])
                except Exception:
                    pass
            d["tot_mevi"]     += mevi_r
            d["tot_attacked"] += perd
            d["tot_premio"]   += pr
            d["tot_rimborso"] += rim_val
            d["txs"].append((ts_l, float(usdc), mevi_r, pr, attacco == "SI", perd, rim_str))
            continue
        m = pool_re.search(line)
        if m:
            pool_snaps.append((m.group(1), float(m.group(2)), float(m.group(3))))

    oracle_stats: dict = {}
    try:
        with open(ORACLE_STATS_FILE, encoding="utf-8") as f:
            oracle_stats = json.load(f)
    except Exception:
        pass

    W = 70
    ls: list[str] = []
    def sep(c="="): ls.append(c * W)
    def h(t=""): ls.append(t)

    sep()
    h("  MEV INSURANCE — ECONOMIC REPORT")
    h(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sep()
    h()

    if pool_snaps:
        sep("─")
        h("  EVOLUZIONE POOL (ogni 2 min)")
        sep("─")
        h(f"  {'Ora':>8s}  {'MEVI Pool':>16s}  {'ETH Pool':>14s}")
        for ts_p, mevi_p, eth_p in pool_snaps:
            h(f"  {ts_p:>8s}  {mevi_p:>16,.4f}  {eth_p:>14,.6f}")
        h()

    sep("─")
    h("  SOMMARIO TRADER")
    sep("─")
    if traders:
        h(f"  {'Trader':>22s}  {'Prot':>6s}  {'MEVI tot':>10s}  "
          f"{'Attaccato':>10s}  {'Premio':>8s}  {'Rimborso':>9s}")
        for addr, d in sorted(traders.items()):
            h(f"  {addr[:22]:>22s}  {d['prot']:>6s}  {d['tot_mevi']:>10.4f}  "
              f"{d['tot_attacked']:>10.4f}  {d['tot_premio']:>8.4f}  {d['tot_rimborso']:>9.4f}")
    else:
        h("  Nessun dato trader nel log.")
    h()

    sep("─")
    h("  DETTAGLIO TX PER TRADER")
    sep("─")
    for addr, d in sorted(traders.items()):
        h()
        h(f"  TRADER {addr}  [prot: {d['prot']}]")
        for (ts_t, usdc, mevi, pr, att, perd, rim) in d["txs"]:
            row = f"  {ts_t}  {usdc:>7.2f} USDC → {mevi:>9.4f} MEVI"
            if pr > 0:
                row += f"  | premio: {pr:.4f}"
            row += f"  | att: {'SI' if att else 'NO'}"
            if att and perd > 0:
                row += f"  perdita: {perd:.4f}"
            if rim:
                row += f"  rimborso: {rim}"
            h(row)
    h()

    if oracle_stats:
        sep("─")
        h("  ORACLE STATS")
        sep("─")
        h(f"  Rewards:           {oracle_stats.get('oracle_rewards_count', '?')} pagamenti  "
          f"tot {oracle_stats.get('oracle_rewards_eth', '?')} ETH")
        h(f"  Claims processati: {oracle_stats.get('claims_processed', '?')}")
        h(f"  Ultimo update:     {oracle_stats.get('last_updated', '?')}")
        h()

    sep()

    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(ls) + "\n")

    now_s = datetime.now().strftime("%H:%M:%S")
    print(f"\n\033[1;32m[{now_s}] Economic report: {out}\033[0m")
    return out


@dataclass
class PoolState:
    balance: int = 0
    inflow_total: int = 0
    outflow_total: int = 0
    delta_last_tick: int = 0

    solvency_ratio_bps: int = 0
    pending_liabilities: int = 0
    expected_claims_7d: int = 0

    oracle_stake_total: int = 0
    oracle_count: int = 0
    active_oracle_count: int = 0
    oracle_registry_balance: int = 0
    insurance_eth_balance: int = 0
    oracle_rewards_paid_total: int = 0
    oracle_slashed_total: int = 0
    platinum_stake_total: int = 0
    platinum_active_users: int = 0
    platinum_refunded_total: int = 0
    _platinum_users_seen: set = field(default_factory=set)
    _platinum_stake_by_user: dict = field(default_factory=dict)

    jury_rewards_slashing: int = 0
    report_deposits_total: int = 0

    swaps_insured_total: int = 0
    attacks_executed_total: int = 0
    claims_submitted_total: int = 0
    attacks_paid_total: int = 0
    naked_seen: int = 0
    insured_seen: int = 0
    naked_swaps_total: int = 0
    naked_attacked: int = 0
    insured_attacked: int = 0
    naked_total_loss_usdc: float = 0.0
    naked_avg_loss_pct: float = 0.0
    avg_loss_pct_all: float = 0.0
    cond_loss_pct: float = 0.0

    last_block: int = 0
    tx_log: deque = field(default_factory=lambda: deque(maxlen=25))
    gov_params: dict[str, Any] = field(default_factory=dict)
    token_symbol: str = "MEVI"
    token_decimals: int = 18


def fmt_token(wei: int, decimals: int = 18, symbol: str = "MEVI") -> str:
    if wei == 0:
        return f"0 {symbol}"
    val = Decimal(wei) / (Decimal(10) ** decimals)
    return f"{val:,.4f} {symbol}"


def fmt_eth(wei: int) -> str:
    if wei == 0:
        return "0 ETH"
    return f"{Decimal(wei) / Decimal(10**18):,.6f} ETH"


def fmt_bps(bps: int) -> str:
    return f"{bps / 100:.2f}%"


def fmt_duration(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def sr_color(sr_bps: int) -> str:
    if sr_bps >= 15000:
        return "bold green"
    if sr_bps >= 13000:
        return "bold yellow"
    return "bold red"


class Monitor:
    def __init__(self, w3: Web3, poll_interval: int, from_block: str | int):
        self.w3 = w3
        self.poll_interval = poll_interval
        self.state = PoolState()
        self.console = Console()

        self.token = w3.eth.contract(
            address=Web3.to_checksum_address(TOKEN_ADDR),
            abi=_load_abi("ERC20", ERC20_MIN_ABI),
        )
        self.insurance = w3.eth.contract(
            address=Web3.to_checksum_address(MEV_INSURANCE_ADDR),
            abi=_load_abi("MEVInsurance"),
        )
        self.oracle_reg = w3.eth.contract(
            address=Web3.to_checksum_address(ORACLE_REGISTRY_ADDR),
            abi=_load_abi("OracleRegistry"),
        )
        self.premium = w3.eth.contract(
            address=Web3.to_checksum_address(PREMIUM_CALC_ADDR),
            abi=_load_abi("PremiumCalculator"),
        )
        self.tier = (
            w3.eth.contract(
                address=Web3.to_checksum_address(TIER_SYSTEM_ADDR),
                abi=_load_abi("TierSystem"),
            ) if TIER_SYSTEM_ADDR else None
        )
        self.slashing = (
            w3.eth.contract(
                address=Web3.to_checksum_address(SLASHING_ADDR),
                abi=_load_abi("SlashingSystem"),
            ) if SLASHING_ADDR else None
        )
        try:
            self.amm = (
                w3.eth.contract(
                    address=Web3.to_checksum_address(AMM_ADDR),
                    abi=_load_abi("MockAMM"),
                ) if AMM_ADDR else None
            )
        except Exception:
            self.amm = None

        _usdc_addr = _addrs.get("MockUSDC", "")
        self.usdc = (
            w3.eth.contract(
                address=Web3.to_checksum_address(_usdc_addr),
                abi=ERC20_MIN_ABI,
            ) if _usdc_addr else None
        )
        self.deployer: str = w3.eth.accounts[0]

        self._contract_addrs: set[str] = {
            v.lower() for v in _addrs.values() if isinstance(v, str) and v.startswith("0x")
        }

        try:
            self.state.token_decimals = self.token.functions.decimals().call()
            self.state.token_symbol = self.token.functions.symbol().call()
        except Exception:
            pass

        self.state.last_block = (
            w3.eth.block_number if from_block == "latest" else int(from_block)
        )

        self._last_patt_update_time: float = 0.0
        self._patt_update_interval: float = 60.0
        self._last_l_update_attack_count: int = -1
        self._gov_tick: int = 0

        self._insured_addrs: set = set()
        self._bot_addrs: set = set()
        try:
            with open(ACTORS_FILE) as _af:
                _ac = json.load(_af)
            self._bot_addrs = {a.lower() for a in _ac.get("bots", [])}
            self._bot_addrs.add(_ac.get("deployer", "").lower())
        except Exception:
            pass

        try:
            _hist_swaps = self.insurance.events.SwapInsured().get_logs(
                from_block=0, to_block=self.state.last_block
            )
            self.state.swaps_insured_total = len(_hist_swaps)
        except Exception:
            try:
                _hist_swaps = self.insurance.events.SwapInsured().get_logs(
                    fromBlock=0, toBlock=self.state.last_block
                )
                self.state.swaps_insured_total = len(_hist_swaps)
            except Exception:
                pass
        try:
            _hist_claims = self.insurance.events.ClaimSubmitted().get_logs(
                from_block=0, to_block=self.state.last_block
            )
            self.state.claims_submitted_total = len(_hist_claims)
        except Exception:
            try:
                _hist_claims = self.insurance.events.ClaimSubmitted().get_logs(
                    fromBlock=0, toBlock=self.state.last_block
                )
                self.state.claims_submitted_total = len(_hist_claims)
            except Exception:
                pass
        try:
            _hist_paid = self.insurance.events.PayoutIssued().get_logs(
                from_block=0, to_block=self.state.last_block
            )
            self.state.attacks_paid_total = len(_hist_paid)
        except Exception:
            try:
                _hist_paid = self.insurance.events.PayoutIssued().get_logs(
                    fromBlock=0, toBlock=self.state.last_block
                )
                self.state.attacks_paid_total = len(_hist_paid)
            except Exception:
                pass

        if self.amm is not None:
            try:
                def _hist_logs(contract, event_name):
                    try:
                        return getattr(contract.events, event_name)().get_logs(
                            from_block=0, to_block=self.state.last_block)
                    except Exception:
                        try:
                            return getattr(contract.events, event_name)().get_logs(
                                fromBlock=0, toBlock=self.state.last_block)
                        except Exception:
                            return []
                for ev in _hist_logs(self.insurance, "SwapInsured"):
                    self._insured_addrs.add(ev["args"]["user"].lower())
                for ev in _hist_logs(self.amm, "Swap"):
                    s = ev["args"]["sender"].lower()
                    if s not in self._insured_addrs and s not in self._bot_addrs:
                        self.state.naked_swaps_total += 1
            except Exception:
                pass

    def fetch_balance(self) -> None:
        try:
            new_bal = self.token.functions.balanceOf(self.insurance.address).call()
            self.state.delta_last_tick = new_bal - self.state.balance
            self.state.balance = new_bal
        except Exception as e:
            self.console.log(f"[yellow]balanceOf fallito: {e}[/]")

    def fetch_stakes(self) -> None:
        # Oracle stake: itera oracleList e somma solo Active(2)+Watchlisted(3).
        # Esclusi: Inactive(0), Pending(1), Slashed(4), Expelled(5) — il loro
        # stake residuo non è "collaterale attivo" e non va sottratto dal reward fund.
        try:
            count = self.oracle_reg.functions.getOracleCount().call()
            self.state.oracle_count = count
            active_stake = 0
            active = 0
            for i in range(count):
                addr = self.oracle_reg.functions.oracleList(i).call()
                info = self.oracle_reg.functions.oracleData(addr).call()
                # info = (stake, status, registrationTime, activationTime,
                #         deviationScore, watchlistStrikes, watchlistPosition,
                #         lastResetTime, claimsEvaluated, withdrawRequestTime)
                # status: 0=Inactive 1=Pending 2=Active 3=Watchlisted 4=Slashed 5=Expelled
                stake = info[0]
                status = info[1]
                if status in (2, 3):
                    active_stake += stake
                    active += 1
            self.state.oracle_stake_total = active_stake
            self.state.active_oracle_count = active
        except Exception as e:
            self.console.log(f"[yellow]fetch_stakes(oracle) fallito: {e}[/]")

        try:
            self.state.oracle_registry_balance = self.w3.eth.get_balance(
                self.oracle_reg.address
            )
        except Exception:
            pass

        try:
            self.state.insurance_eth_balance = self.insurance.functions.getPoolEthBalance().call()
        except Exception:
            pass

        try:
            total_plat = 0
            active_plat = 0
            for user in self.state._platinum_users_seen:
                stake = self.insurance.functions.platinumStake(user).call()
                if stake > 0:
                    total_plat += stake
                    active_plat += 1
            self.state.platinum_stake_total = total_plat
            self.state.platinum_active_users = active_plat
        except Exception as e:
            self.console.log(f"[yellow]fetch_stakes(platinum) fallito: {e}[/]")

    def fetch_events(self) -> None:
        latest = self.w3.eth.block_number
        if latest <= self.state.last_block:
            return

        from_block = self.state.last_block + 1
        to_block = latest

        def _safe_event(contract, event_name: str):
            try:
                return getattr(contract.events, event_name)()
            except Exception:
                return None

        def _safe_get_logs(event_obj, label: str):
            if event_obj is None:
                return []
            try:
                return event_obj.get_logs(from_block=from_block, to_block=to_block)
            except TypeError:
                try:
                    return event_obj.get_logs(fromBlock=from_block, toBlock=to_block)
                except Exception as e:
                    self.console.log(f"[yellow]get_logs {label}: {e}[/]")
                    return []
            except Exception as e:
                self.console.log(f"[yellow]get_logs {label}: {e}[/]")
                return []

        try:
            for ev in _safe_get_logs(_safe_event(self.insurance, "SwapInsured"), "SwapInsured"):
                args = ev["args"]
                amt = args.get("premiumPaid", 0)
                self.state.inflow_total += amt
                self.state.swaps_insured_total += 1
                self._insured_addrs.add(args["user"].lower())
                self._log_tx(
                    ev["blockNumber"], "IN", "SwapInsured", amt, "MEVI",
                    f"user={args['user'][:10]}... swapId={args.get('swapId', '?')}",
                )

            if self.amm is not None:
                for ev in _safe_get_logs(_safe_event(self.amm, "Swap"), "AMMSwap"):
                    sender = ev["args"]["sender"].lower()
                    if sender not in self._insured_addrs and sender not in self._bot_addrs:
                        self.state.naked_swaps_total += 1

            for ev in _safe_get_logs(_safe_event(self.insurance, "ClaimSubmitted"), "ClaimSubmitted"):
                self.state.claims_submitted_total += 1

            for ev_name in ("PayoutIssued", "GasRefundIssued"):
                event_obj = _safe_event(self.insurance, ev_name)
                for ev in _safe_get_logs(event_obj, ev_name):
                    args = ev["args"]
                    amt = args.get("amount") or args.get("refundAmount", 0)
                    self.state.outflow_total += amt
                    if ev_name == "PayoutIssued":
                        self.state.attacks_paid_total += 1
                    self._log_tx(
                        ev["blockNumber"], "OUT", ev_name, amt, "MEVI",
                        f"claim={args.get('claimId', '?')} user={args['user'][:10]}...",
                    )

            for ev_name in ("ClaimFinalized", "BotBlacklisted", "SecondaryReviewTriggered"):
                event_obj = _safe_event(self.insurance, ev_name)
                for ev in _safe_get_logs(event_obj, ev_name):
                    args = ev["args"]
                    desc = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                    self._log_tx(ev["blockNumber"], "INFO", ev_name, 0, "", desc[:60])

            for ev in _safe_get_logs(
                _safe_event(self.insurance, "PlatinumRequested"), "PlatinumRequested"
            ):
                args = ev["args"]
                user = args["user"]
                stake = args.get("stakeDeposited", 0)
                self.state._platinum_users_seen.add(user)
                self.state._platinum_stake_by_user[user] = stake
                self._log_tx(
                    ev["blockNumber"], "IN", "PlatinumRequested", stake, "ETH",
                    f"user={user[:10]}... maxSwap={args.get('desiredMaxSwap', '?')}",
                )

            for ev in _safe_get_logs(
                _safe_event(self.insurance, "PlatinumUpgradeResult"), "PlatinumUpgradeResult"
            ):
                args = ev["args"]
                user = args["user"]
                approved = args.get("approved", False)
                if approved:
                    self._log_tx(
                        ev["blockNumber"], "INFO", "PlatinumUpgradeResult", 0, "",
                        f"user={user[:10]}... ✓ APPROVED (stake bloccato)",
                    )
                else:
                    refund = self.state._platinum_stake_by_user.get(user, 0)
                    self.state.platinum_refunded_total += refund
                    self._log_tx(
                        ev["blockNumber"], "OUT", "PlatinumUpgradeResult", refund, "ETH",
                        f"user={user[:10]}... ✗ REJECTED → stake restituito {fmt_eth(refund)}",
                    )

            for ev in _safe_get_logs(
                _safe_event(self.insurance, "OracleRewarded"), "OracleRewarded"
            ):
                args = ev["args"]
                amt = args.get("amount", 0)
                self.state.oracle_rewards_paid_total += amt
                self._log_tx(
                    ev["blockNumber"], "OUT", "OracleRewarded", amt, "ETH",
                    f"oracle={args['oracle'][:10]}...",
                )

            for ev in _safe_get_logs(
                _safe_event(self.oracle_reg, "OracleSlashed"), "OracleSlashed"
            ):
                args = ev["args"]
                amt = args.get("slashAmount") or args.get("amount", 0)
                self.state.oracle_slashed_total += amt
                self._log_tx(
                    ev["blockNumber"], "IN", "OracleSlashed", amt, "ETH",
                    f"oracle={args['oracle'][:10]}...",
                )

            for ev_name in ("OracleInactivityPenalized", "OracleRegistered", "OracleWithdrawn"):
                event_obj = _safe_event(self.oracle_reg, ev_name)
                for ev in _safe_get_logs(event_obj, ev_name):
                    args = ev["args"]
                    amt = (
                        args.get("amount")
                        or args.get("stake")
                        or args.get("stakeReturned")
                        or args.get("penalty", 0)
                    )
                    self._log_tx(
                        ev["blockNumber"], "INFO", ev_name, amt, "ETH",
                        f"oracle={args['oracle'][:10]}...",
                    )

            for ev in _safe_get_logs(
                _safe_event(self.oracle_reg, "OracleReintegrated"), "OracleReintegrated"
            ):
                args = ev["args"]
                new_stake = args.get("newStake", 0)
                self._log_tx(
                    ev["blockNumber"], "STAKE", "OracleReintegrated", new_stake, "ETH",
                    f"oracle={args['oracle'][:10]}... → stake={fmt_eth(new_stake)}",
                )

            for ev in _safe_get_logs(
                _safe_event(self.oracle_reg, "OracleActivated"), "OracleActivated"
            ):
                args = ev["args"]
                self._log_tx(
                    ev["blockNumber"], "INFO", "OracleActivated", 0, "",
                    f"oracle={args['oracle'][:10]}... ora Active",
                )

            if self.slashing is not None:
                for ev in _safe_get_logs(
                    _safe_event(self.slashing, "ReportSubmitted"), "ReportSubmitted"
                ):
                    args = ev["args"]
                    dep = args.get("deposit", 0)
                    self.state.report_deposits_total += dep
                    self._log_tx(
                        ev["blockNumber"], "STAKE", "ReportSubmitted", dep, "ETH",
                        f"report={args.get('reportId', '?')} "
                        f"reporter={args['reporter'][:10]}... "
                        f"vs {args['accusedOracle'][:10]}...",
                    )

                # ReportStatus enum: 0=Pending 1=Slashed 2=Expelled 3=Dismissed
                for ev in _safe_get_logs(
                    _safe_event(self.slashing, "SlashingFinalized"), "SlashingFinalized"
                ):
                    args = ev["args"]
                    slash = args.get("slashAmount", 0)
                    median = args.get("medianPercent", 0)
                    report_id = args.get("reportId", "?")
                    status_int = args.get("status", 0)

                    jury_reward_unit = self.state.gov_params.get("juryReward", 0)
                    n_jury = self.state.gov_params.get("nJury", 7)
                    report_deposit = self.state.gov_params.get("reportDeposit", 0)
                    status_label = "EXPELLED" if status_int == 2 else "SLASHED"

                    if slash > 0:
                        reveal_count = n_jury
                        reporter_addr = ""
                        try:
                            if report_id != "?":
                                r_info = self.slashing.functions.getReportInfo(int(report_id)).call()
                                reporter_addr = r_info[0]
                                report_deposit = r_info[3]
                                reveal_count = r_info[5]
                        except Exception:
                            pass

                        total_jury = min(slash, jury_reward_unit * reveal_count)
                        residual = slash - total_jury if slash > total_jury else 0
                        reporter_share = (residual * 2500) // 10000
                        pool_share = residual - reporter_share
                        reporter_total = report_deposit + reporter_share

                        self.state.jury_rewards_slashing += total_jury

                        reporter_note = (
                            f"report={report_id} [{status_label}] "
                            f"deposito {fmt_eth(report_deposit)} + 25% residuo {fmt_eth(reporter_share)}"
                        )
                        self._log_tx(
                            ev["blockNumber"], "OUT", "Slash→Reporter",
                            reporter_total, "ETH", reporter_note,
                        )

                        jury_note = (
                            f"report={report_id} {reveal_count} giurati "
                            f"× {fmt_eth(jury_reward_unit)} = {fmt_eth(total_jury)}"
                        )
                        self._log_tx(
                            ev["blockNumber"], "OUT", "Slash→Jury",
                            total_jury, "ETH", jury_note,
                        )

                        if pool_share > 0:
                            pool_note = (
                                f"report={report_id} [{status_label}] mediana={median}% "
                                f"75% residuo {fmt_eth(pool_share)} → pool ETH"
                            )
                            self._log_tx(
                                ev["blockNumber"], "IN", "Slash→Pool",
                                pool_share, "ETH", pool_note,
                            )
                    else:
                        try:
                            if report_id != "?":
                                r_info = self.slashing.functions.getReportInfo(int(report_id)).call()
                                report_deposit = r_info[3]
                        except Exception:
                            pass
                        self.state.jury_rewards_slashing += report_deposit
                        self._log_tx(
                            ev["blockNumber"], "OUT", "Slash→Jury(DISMISS)",
                            report_deposit, "ETH",
                            f"report={report_id} [DISMISSED] deposito reporter confiscato → jury",
                        )

        except Exception as e:
            self.console.log(f"[yellow]fetch_events: {e}[/]")

        try:
            with open(ATTACKED_TXNS_FILE) as _f:
                self.state.attacks_executed_total = len(json.load(_f))
        except Exception:
            pass

        try:
            with open(NAKED_STATS_FILE) as _f:
                _ns = json.load(_f)
            self.state.naked_seen   = _ns.get("naked_seen", 0)
            self.state.insured_seen = _ns.get("insured_seen", 0)
        except Exception:
            pass
        try:
            with open(ATTACK_LOG_FILE) as _f:
                _al = json.load(_f)
            try:
                with open(ACTORS_FILE) as _af:
                    _actors = json.load(_af)
                _valid_victims = {a.lower() for a in _actors.get("traders", [])}
            except Exception:
                _valid_victims = set()
            def _is_valid_victim(entry: dict) -> bool:
                v = entry.get("victim", "").lower()
                return (not _valid_victims) or (v in _valid_victims)
            naked_attacks = [v for v in _al.values()
                             if not v.get("is_insured", True) and _is_valid_victim(v)]
            insured_attacks = [v for v in _al.values() if v.get("is_insured", False)]
            self.state.naked_attacked    = len(naked_attacks)
            self.state.insured_attacked  = len(insured_attacks)
            if naked_attacks:
                total_loss = sum(a.get("loss_sandwich_mevi", 0) for a in naked_attacks)
                self.state.naked_total_loss_usdc = total_loss
                pcts = [a.get("loss_pct", 0) for a in naked_attacks if a.get("loss_pct", 0) > 0]
                self.state.naked_avg_loss_pct = sum(pcts) / len(pcts) if pcts else 0.0
            else:
                self.state.naked_total_loss_usdc = 0.0
                self.state.naked_avg_loss_pct    = 0.0
            all_pcts = [a.get("loss_pct", 0) for a in _al.values() if a.get("loss_pct", 0) > 0]
            n_total_swaps = self.state.swaps_insured_total + self.state.naked_swaps_total
            denom = n_total_swaps if n_total_swaps > 0 else len(all_pcts)
            self.state.avg_loss_pct_all = sum(all_pcts) / denom if (all_pcts and denom > 0) else 0.0
            # L% metric: perdita sandwich condizionale per swap attaccato (pura, senza base slippage)
            # = Σ(loss_sandwich_mevi) / Σ(swap_amount_usdc) * 100  [1:1 price assumption]
            _total_loss_mevi = sum(a.get("loss_sandwich_mevi", 0) for a in _al.values())
            _total_att_vol   = sum(a.get("swap_amount_usdc", 0) for a in _al.values()
                                   if a.get("swap_amount_usdc", 0) > 0)
            self.state.cond_loss_pct = (
                _total_loss_mevi / _total_att_vol * 100
                if _total_att_vol > 0 else 0.0
            )
        except Exception:
            pass

        # Filtra: value > 0, mittente EOA (non contratto), destinatario EOA (non contratto)
        # Limite: al massimo 50 blocchi per tick per non rallentare
        try:
            scan_start = max(from_block, to_block - 49)
            for block_num in range(scan_start, to_block + 1):
                blk = self.w3.eth.get_block(block_num, full_transactions=True)
                for tx in blk.get("transactions", []):
                    val = tx.get("value", 0)
                    if val <= 0:
                        continue
                    to_addr = (tx.get("to") or "").lower()
                    from_addr = (tx.get("from") or "").lower()
                    if (to_addr
                            and to_addr not in self._contract_addrs
                            and from_addr not in self._contract_addrs):
                        self._log_tx(
                            block_num, "ETH", "ETHTransfer", val, "ETH",
                            f"{from_addr[:10]}... → {to_addr[:10]}...",
                        )
        except Exception as e:
            self.console.log(f"[yellow]raw ETH scan: {e}[/]")

        finally:
            self.state.last_block = latest

    def fetch_gov_params(self) -> None:
        p: dict[str, Any] = {}

        try:
            p["nOracle"] = self.insurance.functions.nOracle().call()
            p["policyDuration"] = self.insurance.functions.policyDuration().call()
            p["oracleTimeout"] = self.insurance.functions.oracleTimeout().call()
            p["thetaReject"] = self.insurance.functions.thetaReject().call()
            p["thetaApprove"] = self.insurance.functions.thetaApprove().call()
            p["patternInvalidBps"] = self.insurance.functions.patternInvalidThresholdBps().call()
            p["dispersioneThreshold"] = self.insurance.functions.dispersioneThreshold().call()
            p["alphaStakeBps"] = self.insurance.functions.alphaStakeBps().call()
            p["gasRefundAmount"] = self.insurance.functions.gasRefundAmount().call()
            p["inactivityPenalty"] = self.insurance.functions.inactivityPenalty().call()
        except Exception as e:
            self.console.log(f"[yellow]gov(insurance): {e}[/]")

        try:
            p["baseStake"] = self.oracle_reg.functions.baseStake().call()
            p["tActivation"] = self.oracle_reg.functions.tActivation().call()
            p["tCooldown"] = self.oracle_reg.functions.tCooldown().call()
            p["kWatchlist"] = self.oracle_reg.functions.kWatchlist().call()
            p["deltaWatchlist"] = self.oracle_reg.functions.deltaWatchlist().call()
            p["tReset"] = self.oracle_reg.functions.tReset().call()
            p["tWatchlist"] = self.oracle_reg.functions.tWatchlist().call()
            p["rClaim"] = self.oracle_reg.functions.rClaim().call()
            p["activeOracleCount"] = self.oracle_reg.functions.activeOracleCount().call()
            p["minStake"] = self.oracle_reg.functions.getMinimumStake().call()
        except Exception as e:
            self.console.log(f"[yellow]gov(oracle): {e}[/]")

        try:
            p["patt"] = self.premium.functions.patt().call()
            p["lPercent"] = self.premium.functions.lPercent().call()
            p["eFNR"] = self.premium.functions.eFNR().call()
            p["mBase"] = self.premium.functions.mBase().call()
            p["mAdj"] = self.premium.functions.mAdj().call()
            p["pmin"] = self.premium.functions.pmin().call()
            p["srSafe"] = self.premium.functions.srSafe().call()
            p["srCritical"] = self.premium.functions.srCritical().call()
            p["deltaMmed"] = self.premium.functions.deltaMmed().call()
            p["deltaMhigh"] = self.premium.functions.deltaMhigh().call()
            self.state.solvency_ratio_bps = self.premium.functions.getSolvencyRatio().call()
            self.state.pending_liabilities = self.premium.functions.pendingLiabilities().call()
            self.state.expected_claims_7d = self.premium.functions.expectedClaims7d().call()
        except Exception as e:
            self.console.log(f"[yellow]gov(premium): {e}[/]")

        if self.tier is not None:
            try:
                p["silverMinSwaps"] = self.tier.functions.silverMinSwaps().call()
                p["silverMinDays"] = self.tier.functions.silverMinDays().call()
                p["silverMaxFraud"] = self.tier.functions.silverMaxFraudScore().call()
                p["goldMinSwaps"] = self.tier.functions.goldMinSwaps().call()
                p["goldMinDays"] = self.tier.functions.goldMinDays().call()
                p["goldMaxFraud"] = self.tier.functions.goldMaxFraudScore().call()
                p["platinumStakeBps"] = self.tier.functions.platinumStakeBps().call()
                p["blacklistPenaltyBps"] = self.tier.functions.blacklistPenaltyBps().call()
            except Exception:
                pass

        if self.slashing is not None:
            try:
                p["nJury"] = self.slashing.functions.nJury().call()
                p["reportDeposit"] = self.slashing.functions.reportDeposit().call()
                p["juryReward"] = self.slashing.functions.juryReward().call()
                p["thetaExpulsion"] = self.slashing.functions.thetaExpulsion().call()
                p["poolShareBps"] = self.slashing.functions.poolShareBps().call()
            except Exception:
                pass

        self.state.gov_params = p

    def _maybe_update_patt(self) -> None:
        if time.time() - self._last_patt_update_time < self._patt_update_interval:
            return
        self._last_patt_update_time = time.time()

        n_insured = self.state.swaps_insured_total
        n_naked   = self.state.naked_swaps_total
        n_swaps   = n_insured + n_naked
        n_attacks = self.state.naked_attacked + self.state.insured_attacked
        if n_swaps < 5:
            return

        empirical_bps = int(n_attacks * 10000 / n_swaps)
        current_patt = self.state.gov_params.get("patt", 0)
        if not isinstance(current_patt, int) or empirical_bps == current_patt:
            return

        try:
            deployer = self.w3.eth.accounts[0]
            self.w3.provider.make_request("evm_setAutomine", [True])
            try:
                self.premium.functions.setPatt(empirical_bps).transact(
                    {"from": deployer, "gas": 100_000}
                )
                arrow = "↑" if empirical_bps > current_patt else "↓"
                color = "green" if empirical_bps > current_patt else "yellow"
                self.state.gov_params["patt"] = empirical_bps
                self.console.log(
                    f"[bold {color}]PATT {arrow} auto: {current_patt/100:.1f}% → "
                    f"{empirical_bps/100:.1f}%  "
                    f"({n_attacks} att / {n_swaps} swap tot)[/]"
                )
            finally:
                self.w3.provider.make_request("evm_setAutomine", [False])
        except Exception as e:
            self.console.log(f"[yellow]patt auto-update: {e}[/]")

    def _maybe_update_lPercent(self) -> None:
        total_attacks = self.state.naked_attacked + self.state.insured_attacked
        if total_attacks == 0 or total_attacks == self._last_l_update_attack_count:
            return
        self._last_l_update_attack_count = total_attacks

        avg_pct = self.state.cond_loss_pct
        if avg_pct <= 0:
            return
        new_bps = max(1, int(avg_pct * 100))
        current_l = self.state.gov_params.get("lPercent", 0)
        if not isinstance(current_l, int) or new_bps == current_l:
            return
        try:
            deployer = self.w3.eth.accounts[0]
            self.w3.provider.make_request("evm_setAutomine", [True])
            try:
                self.premium.functions.setLPercent(new_bps).transact(
                    {"from": deployer, "gas": 100_000}
                )
                arrow = "↑" if new_bps > current_l else "↓"
                color = "yellow" if new_bps > current_l else "green"
                self.state.gov_params["lPercent"] = new_bps
                self.console.log(
                    f"[bold {color}]L% {arrow} auto: {current_l/100:.2f}% → {new_bps/100:.2f}%  "
                    f"(avg_loss={avg_pct:.2f}% su {total_attacks} attacchi)[/]"
                )
            finally:
                self.w3.provider.make_request("evm_setAutomine", [False])
        except Exception as e:
            self.console.log(f"[yellow]L% auto-update: {e}[/]")

    def _log_tx(self, block: int, direction: str, name: str,
                amount: int, unit: str, note: str) -> None:
        self.state.tx_log.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "block": block,
            "dir": direction,
            "event": name,
            "amount": amount,
            "unit": unit,
            "note": note,
        })

    def _render_header(self) -> Panel:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        txt = Text.from_markup(
            f"[bold cyan]MEV Insurance — Pool Monitor[/]   "
            f"block [yellow]{self.state.last_block}[/]   "
            f"chain_id [yellow]{self.w3.eth.chain_id}[/]   "
            f"{now}"
        )
        return Panel(Align.center(txt), style="cyan")

    def _render_pool_panel(self) -> Panel:
        s = self.state
        dec, sym = s.token_decimals, s.token_symbol

        # MEVI: il pool MEVI copre solo payout e gas refund (entrambi in MEVI).
        # I reward oracle sono in ETH e vengono dal fondo ETH separato — non incidono sul netto MEVI.
        net_mevi = s.inflow_total - s.outflow_total
        delta_color = "green" if s.delta_last_tick >= 0 else "red"
        delta_sign = "+" if s.delta_last_tick >= 0 else ""

        # ETH: reward fund disponibile = balance registry - stake bloccato attivi
        reward_fund_available = max(
            0, s.oracle_registry_balance - s.oracle_stake_total
        )

        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column(justify="right")

        _net_eth_oracle = s.insurance_eth_balance
        _mevi_color = "green" if net_mevi >= 0 else "red"
        _eth_color  = "green" if _net_eth_oracle >= 0 else "red"
        t.add_row("[bold cyan]── Pool Protocollo (MEVI + ETH oracle) ──[/]", "")
        t.add_row("Pool MEVI (assicurazione)", f"[bold {_mevi_color}]{fmt_token(s.balance, dec, sym)}[/]")
        t.add_row("Pool ETH (fondo reward oracle)", f"[bold {_eth_color}]{fmt_eth(s.insurance_eth_balance)}[/]")
        t.add_row(
            "  Netto MEVI (premi − uscite)",
            f"[{_mevi_color}]{fmt_token(net_mevi, dec, sym)}[/]",
        )
        t.add_row("", "")

        t.add_row("[bold yellow]── MEVI (Assicurazione) ──[/]", "")
        t.add_row("Pool balance", f"[bold white]{fmt_token(s.balance, dec, sym)}[/]")
        t.add_row(
            "Δ ultimo tick",
            f"[{delta_color}]{delta_sign}{fmt_token(s.delta_last_tick, dec, sym)}[/]",
        )
        t.add_row("Entrate (premi)", f"[green]+{fmt_token(s.inflow_total, dec, sym)}[/]")
        t.add_row("Uscite (payout+gas refund)", f"[red]-{fmt_token(s.outflow_total, dec, sym)}[/]")
        t.add_row(
            "Netto MEVI",
            f"[bold {'green' if net_mevi >= 0 else 'red'}]{fmt_token(net_mevi, dec, sym)}[/]",
        )

        t.add_row("", "")

        t.add_row("[bold yellow]── ETH Fondo Reward Oracle (MEVInsurance) ──[/]", "")
        t.add_row(
            "Pool ETH Insurance (live)",
            f"[bold white]{fmt_eth(s.insurance_eth_balance)}[/]",
        )
        t.add_row(
            "Reward oracle pagati (usciti)",
            f"[red]-{fmt_eth(s.oracle_rewards_paid_total)}[/]",
        )

        t.add_row("", "")
        t.add_row("[bold yellow]── ETH OracleRegistry ──[/]", "")
        t.add_row(
            "Reward fund disponibile",
            f"[bold white]{fmt_eth(reward_fund_available)}[/]",
        )
        t.add_row(
            "Jury rewards (report)",
            f"[red]-{fmt_eth(s.jury_rewards_slashing)}[/]",
        )
        t.add_row(
            "Depositi contestazione (in)",
            f"[cyan]+{fmt_eth(s.report_deposits_total)}[/]",
        )
        t.add_row(
            "Slashed cumulativo",
            f"[green]+{fmt_eth(s.oracle_slashed_total)}[/]",
        )
        t.add_row("", "")
        t.add_row(
            "Collaterale oracle (attivi+watchl.)",
            f"[cyan]{fmt_eth(s.oracle_stake_total)}[/]  ({s.active_oracle_count} su {s.oracle_count} nodi)",
        )
        t.add_row(
            "Platinum stake bloccato",
            f"[magenta]{fmt_eth(s.platinum_stake_total)}[/]  ({s.platinum_active_users} utenti)",
        )

        t.add_row("", "")

        t.add_row("[bold yellow]── Statistiche Swap (sessione) ──[/]", "")

        _ins_n   = s.swaps_insured_total
        _nak_n   = s.naked_swaps_total
        _ins_att = s.insured_attacked
        _nak_att = s.naked_attacked
        _tot_att = _ins_att + _nak_att

        t.add_row("Swap assicurati totali", f"[bold cyan]{_ins_n}[/]")
        t.add_row("Swap nudi totali (AMM)", f"[bold white]{_nak_n}[/]")
        t.add_row("", "")

        _ins_att_pct = f"{_ins_att*100//_ins_n}%" if _ins_n > 0 else "—"
        _nak_att_pct = f"{_nak_att*100//_nak_n}%" if _nak_n > 0 else "—"
        t.add_row("Attacchi su assicurati", f"[bold yellow]{_ins_att}[/]  [dim]({_ins_att_pct})[/]")
        t.add_row("Attacchi su nudi",       f"[bold red]{_nak_att}[/]  [dim]({_nak_att_pct})[/]")
        t.add_row("Attacchi totali (bot)",  f"[bold red]{_tot_att}[/]")
        t.add_row("", "")

        t.add_row("[dim]attacchi assicurati → claim attesi[/]",
                  f"[dim]{_ins_att} attacchi → {s.claims_submitted_total} claim[/]")
        t.add_row("Claim approvati (payout)", f"[bold white]{s.attacks_paid_total}[/]")
        t.add_row("", "")

        if _ins_n > 0:
            claim_rate_bps = int(s.claims_submitted_total * 10000 / _ins_n)
            claim_rate_str = fmt_bps(claim_rate_bps)
        else:
            claim_rate_bps = 0
            claim_rate_str = "—"

        # PATT empirico corretto (attacchi_totali / swap_totali DEX)
        _total_sw  = _ins_n + _nak_n
        if _total_sw > 0:
            patt_emp_bps = int(_tot_att * 10000 / _total_sw)
            patt_emp_str = fmt_bps(patt_emp_bps)
        else:
            patt_emp_bps = 0
            patt_emp_str = "—"

        patt_set = s.gov_params.get("patt")
        patt_set_str = fmt_bps(patt_set) if isinstance(patt_set, int) else "—"
        patt_color = "green" if patt_emp_bps == 0 else (
            "yellow" if abs(patt_emp_bps - (patt_set or 0)) <= 300 else "red"
        )
        t.add_row("Claim rate assicurati", f"[dim]{claim_rate_str}[/]")
        t.add_row("PATT empirico (DEX)",   f"[{patt_color}]{patt_emp_str}[/]")
        t.add_row("PATT impostato (gov)",  f"[dim]{patt_set_str}[/]")

        if _ins_att > 0 or _nak_att > 0:
            t.add_row("", "")
            t.add_row("[bold yellow]── Perdite ──[/]", "")
        if _nak_att > 0:
            t.add_row("Perdita totale naked", f"[bold red]{s.naked_total_loss_usdc:.4f} USDC[/]")
        if s.avg_loss_pct_all > 0:
            t.add_row("Perdita media % (perdite/tot swap)", f"[bold red]{s.avg_loss_pct_all:.2f}%[/]")
        if s.cond_loss_pct > 0:
            t.add_row("Perdita media % (per attacco)",  f"[bold red]{s.cond_loss_pct:.2f}%[/]")

        l_val = s.gov_params.get("lPercent")
        if isinstance(l_val, int):
            obs_str = f"  [dim](osservata: {s.cond_loss_pct:.2f}%)[/]" if s.cond_loss_pct > 0 else ""
            t.add_row("L% (gov, auto-aggiornato)", f"[bold magenta]{l_val/100:.2f}%[/]{obs_str}")

        return Panel(
            t,
            title="💰 Pool Protocollo (Tesoreria Unificata)",
            border_style="green",
        )

    def _render_tx_log(self) -> Panel:
        t = Table(expand=True, show_header=True, header_style="bold dim")
        t.add_column("time", width=8)
        t.add_column("blk", width=7, justify="right")
        t.add_column("dir", width=5)
        t.add_column("event", width=20)
        t.add_column("amount", justify="right", width=22)
        t.add_column("note", overflow="fold")

        if not self.state.tx_log:
            t.add_row("—", "—", "—", "waiting for events...", "—", "—")

        dir_style = {
            "IN": "green",
            "OUT": "red",
            "STAKE": "magenta",
            "INFO": "dim",
            "ETH": "yellow",
        }
        for row in self.state.tx_log:
            unit = row.get("unit", "")
            if row["amount"] == 0:
                amt = "—"
            elif unit == "MEVI":
                amt = fmt_token(
                    row["amount"], self.state.token_decimals, self.state.token_symbol
                )
            elif unit == "ETH":
                amt = fmt_eth(row["amount"])
            else:
                amt = str(row["amount"])
            t.add_row(
                row["time"],
                str(row["block"]),
                Text(row["dir"], style=dir_style.get(row["dir"], "white")),
                row["event"],
                amt,
                row["note"],
            )
        return Panel(
            t,
            title="📜 Transazioni recenti (storico sessione)",
            border_style="blue",
        )

    def _render_gov_panel(self) -> Panel:
        p = self.state.gov_params

        def row_bps(label: str, key: str) -> tuple[str, str]:
            v = p.get(key)
            return label, fmt_bps(v) if isinstance(v, int) else "—"

        def row_int(label: str, key: str, suffix: str = "") -> tuple[str, str]:
            v = p.get(key)
            return label, (f"{v}{suffix}" if v is not None else "—")

        def row_dur(label: str, key: str) -> tuple[str, str]:
            v = p.get(key)
            return label, (fmt_duration(v) if isinstance(v, int) else "—")

        def row_eth(label: str, key: str) -> tuple[str, str]:
            v = p.get(key)
            return label, (fmt_eth(v) if isinstance(v, int) else "—")

        sections: list[tuple[str, list[tuple[str, str]]]] = [
            ("Oracle Claim Flow", [
                row_int("N_oracle (claim)", "nOracle"),
                row_dur("Oracle timeout", "oracleTimeout"),
                row_bps("θ_reject", "thetaReject"),
                row_bps("θ_approve", "thetaApprove"),
                row_bps("θ_pattern (invalid)", "patternInvalidBps"),
                row_int("Δ_dispersione", "dispersioneThreshold"),
                row_eth("Gas refund", "gasRefundAmount"),
                row_eth("Inactivity penalty", "inactivityPenalty"),
                row_bps("α_stake (Platinum)", "alphaStakeBps"),
            ]),
            ("Oracle Registry", [
                row_eth("Base stake", "baseStake"),
                row_eth("Min stake (live)", "minStake"),
                row_int("Active oracles", "activeOracleCount"),
                row_dur("T_activation", "tActivation"),
                row_dur("T_cooldown", "tCooldown"),
                row_int("k_watchlist", "kWatchlist"),
                row_int("Δ_watchlist (pt)", "deltaWatchlist"),
                row_dur("T_watchlist", "tWatchlist"),
                row_dur("T_reset", "tReset"),
                row_eth("R_claim", "rClaim"),
            ]),
            ("Premium Formula", [
                row_bps("P_att", "patt"),
                row_bps("L%", "lPercent"),
                row_bps("E (FNR)", "eFNR"),
                row_bps("M_base", "mBase"),
                row_bps("M_adj (live)", "mAdj"),
                row_bps("P_min", "pmin"),
                row_bps("SR_safe", "srSafe"),
                row_bps("SR_critical", "srCritical"),
                row_bps("ΔM_med", "deltaMmed"),
                row_bps("ΔM_high", "deltaMhigh"),
            ]),
            ("Tier System", [
                row_int("Silver min swaps", "silverMinSwaps"),
                row_dur("Silver min days", "silverMinDays"),
                row_int("Silver max fraud", "silverMaxFraud"),
                row_int("Gold min swaps", "goldMinSwaps"),
                row_dur("Gold min days", "goldMinDays"),
                row_int("Gold max fraud", "goldMaxFraud"),
                row_bps("Platinum stake", "platinumStakeBps"),
                row_bps("Blacklist penalty", "blacklistPenaltyBps"),
            ]),
            ("Slashing", [
                row_int("N_jury", "nJury"),
                row_eth("Report deposit", "reportDeposit"),
                row_eth("Jury reward", "juryReward"),
                row_int("θ_expulsion", "thetaExpulsion", "%"),
                row_bps("Pool share", "poolShareBps"),
            ]),
        ]

        tables = []
        for title, rows in sections:
            t = Table.grid(padding=(0, 2), expand=True)
            t.add_column(style="dim", ratio=2)
            t.add_column(justify="right", ratio=1, style="white")
            for label, value in rows:
                t.add_row(label, value)
            tables.append(
                Panel(t, title=f"[bold]{title}[/]", border_style="magenta", padding=(0, 1))
            )

        return Panel(
            Group(*tables),
            title="📋 Parametri di Governance (Tabella 8)",
            border_style="magenta",
        )

    def _render_sim_panel(self) -> Panel | None:
        rows: list[tuple[str, str]] = []

        try:
            with open(SIM_PARAMS_FILE, encoding="utf-8") as f:
                sp = json.load(f)
            scenario_id = sp.get("scenario", "?")
            scenario_name = sp.get("name", "?")
            started = sp.get("started_at", "?")
            rows.append(("Scenario", f"[bold cyan]{scenario_id}[/] — {scenario_name}"))
            rows.append(("Avviato", started))
            params = sp.get("params", {})
            for k, v in params.items():
                if k == "attack_rate_bps":
                    rows.append(("attack_rate bot", f"[yellow]{v / 100:.1f}%[/] ({v} bps)"))
                elif k == "patt_gov_bps":
                    rows.append(("patt governance", f"[dim]{v / 100:.1f}% ({v} bps)[/]"))
                elif k == "eFNR_bps":
                    rows.append(("eFNR", f"{v} bps ({v/100:.1f}%)"))
                elif k == "max_swap_usdc":
                    rows.append(("max_swap", f"{v} USDC"))
                elif k == "gasRefundAmount_eth":
                    rows.append(("gasRefundAmount", f"{v} ETH"))
                elif k == "gap_bps":
                    rows.append(("Gap patt/rate", f"[bold red]{v} bps ({v/100:.1f}%)[/]"))
                elif k == "nota":
                    rows.append(("Nota", f"[dim]{v}[/]"))
                elif k.startswith("coverage_"):
                    rows.append((k, str(v)))
                elif k.endswith("_acc"):
                    pass
        except (FileNotFoundError, json.JSONDecodeError):
            return None

        try:
            with open(ACTORS_FILE, encoding="utf-8") as f:
                actors = json.load(f)
            bots = actors.get("bots", [])
            if bots:
                rows.append(("", ""))
                rows.append(("[bold yellow]Bot attivi[/]", f"{len(bots)} indirizzi"))
                for i, b in enumerate(bots):
                    rows.append((f"  bot #{i}", f"[dim]{b[:20]}...[/]"))
        except Exception:
            pass

        if not rows:
            return None

        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim", ratio=2)
        t.add_column(ratio=3)
        for label, value in rows:
            t.add_row(label, value)

        return Panel(t, title="🧪 Simulazione corrente", border_style="cyan")

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._render_header(), size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        sim_panel = self._render_sim_panel()
        if sim_panel is not None:
            layout["body"]["left"].split_column(
                Layout(self._render_pool_panel(), size=36),
                Layout(sim_panel, size=12),
                Layout(self._render_tx_log(), name="tx"),
            )
        else:
            layout["body"]["left"].split_column(
                Layout(self._render_pool_panel(), size=36),
                Layout(self._render_solvency_panel(), size=9),
                Layout(self._render_tx_log(), name="tx"),
            )
        layout["body"]["right"].update(self._render_gov_panel())
        return layout

    def _check_and_refill_amm(self):
        if self.amm is None or self.usdc is None:
            return
        try:
            init_mevi = self.amm.functions.initReserveA().call()
            init_usdc = self.amm.functions.initReserveB().call()
            if init_mevi == 0:
                return

            THRESHOLD = init_mevi // 10

            reserve_mevi = self.amm.functions.reserveA().call()
            reserve_usdc = self.amm.functions.reserveB().call()

            if reserve_mevi >= THRESHOLD:
                return

            mevi_before = reserve_mevi / 1e18
            add_mevi = max(0, init_mevi - reserve_mevi)
            add_usdc = max(0, init_usdc - reserve_usdc)

            if add_mevi == 0:
                return

            self.console.log(
                f"[yellow]AMM LOW ({mevi_before:,.0f} MEVI) → ripristino a "
                f"{init_mevi/1e18:,.0f} MEVI / {init_usdc/1e18:,.0f} USDC[/]"
            )

            BIG = 10**27
            utils.send_tx(self.w3, self.token.functions.approve(self.amm.address, BIG), self.deployer)
            utils.send_tx(self.w3, self.usdc.functions.approve(self.amm.address, BIG), self.deployer)
            utils.send_tx(self.w3, self.amm.functions.addLiquidity(add_mevi, add_usdc), self.deployer)

            mevi_after = self.amm.functions.reserveA().call() / 1e18
            usdc_after = self.amm.functions.reserveB().call() / 1e18
            self.console.log(
                f"[green]AMM refill OK → {mevi_after:,.0f} MEVI / {usdc_after:,.0f} USDC[/]"
            )
            utils.sim_log(
                f"AMM REFILL | mevi_prima={mevi_before:.0f} → mevi_dopo={mevi_after:.0f} MEVI"
                f" | usdc_pool={usdc_after:.0f} USDC",
                tag="  AMM"
            )
        except Exception as e:
            self.console.log(f"[red]AMM refill error: {e}[/]")

    def run(self) -> None:
        self.fetch_gov_params()
        self.fetch_balance()
        self.fetch_stakes()
        with Live(
            self.render(), console=self.console, refresh_per_second=2, screen=True
        ) as live:
            while True:
                try:
                    self.fetch_balance()
                except Exception as e:
                    self.console.log(f"[yellow]fetch_balance: {e}[/]")

                try:
                    self.fetch_events()
                except Exception as e:
                    self.console.log(f"[yellow]fetch_events: {e}[/]")

                try:
                    self.fetch_stakes()
                    self._gov_tick += 1

                    if self._gov_tick % 5 == 0:
                        self.fetch_gov_params()

                    self._check_and_refill_amm()

                    live.update(self.render())
                except Exception as e:
                    self.console.log(f"[yellow]render: {e}[/]")
                try:
                    self._maybe_update_patt()
                except Exception as e:
                    self.console.log(f"[yellow]patt_update: {e}[/]")
                try:
                    self._maybe_update_lPercent()
                except Exception as e:
                    self.console.log(f"[yellow]l_update: {e}[/]")
                time.sleep(self.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor live on-chain del pool MEV Insurance"
    )
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--from-block", default="latest")
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(f"ERRORE: impossibile connettersi a {RPC_URL}")
        return

    monitor = Monitor(w3, poll_interval=args.interval, from_block=args.from_block)
    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\n[monitor] stopped — generating economic report...")
        generate_economic_report()


if __name__ == "__main__":
    main()
