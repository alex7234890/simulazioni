"""
Oracle Network Daemon — MEV Insurance Simulation
=================================================
Gestisce N nodi oracle: registrazione, attivazione, e processamento
automatico dei claim tramite protocollo commit-reveal.

Uso:  python -i scripts/oracle.py
      (interattivo: dopo il setup puoi chiamare oracle_status(), ecc.)

Flusso automatico per ogni claim:
  1. Evento ClaimSubmitted ricevuto
  2. Identifica quali dei nostri oracle sono assegnati
  3. Ogni oracle calcola FraudScore + patternValid
  4. Fase COMMIT: ogni oracle invia hash(score, pattern, salt)
  5. Fase REVEAL: ogni oracle rivela score, pattern, salt
  6. Se tutti hanno rivelato → finalizeClaim()
"""

import utils
import time
import secrets
import random
import struct
from datetime import datetime
from web3 import Web3
from collections import deque
import json
import os

TEST_STATE_FILE = "oracle_test_state.json"

#carico file json usato per iniettare malfuzionamenti negli oracle
def _load_test_state():
    """Carica lo stato di test (bugs + jury_probs)."""
    if not os.path.exists(TEST_STATE_FILE):
        return {"bugs": {}, "jury_probs": {}}
    try:
        with open(TEST_STATE_FILE, "r") as f:
            data = json.load(f)
        if "bugs" not in data:
            data["bugs"] = {}
        if "jury_probs" not in data:
            data["jury_probs"] = {}
        return data
    except:
        return {"bugs": {}, "jury_probs": {}}

# ══════════════════════════════════════════════════════════════
#  COLORI PER DIFFERENZIARE OGNI ORACLE NEL LOG
# ══════════════════════════════════════════════════════════════

_ORACLE_COLORS = [
    "\033[93m",   # 0: giallo
    "\033[96m",   # 1: ciano
    "\033[92m",   # 2: verde
    "\033[95m",   # 3: magenta
    "\033[94m",   # 4: blu
    "\033[91m",   # 5: rosso
    "\033[97m",   # 6: bianco brillante
    "\033[33m",   # 7: giallo scuro
    "\033[36m",   # 8: ciano scuro
    "\033[32m",   # 9: verde scuro
    "\033[35m",   # 10: magenta scuro
    "\033[34m",   # 11: blu scuro
    "\033[90m",   # 12: grigio
    "\033[37m",   # 13: bianco
]
_RESET = "\033[0m"
_BOLD  = "\033[1m"

# ══════════════════════════════════════════════════════════════
#  CAPTCHA LEETSPEAK GENERATOR
# ══════════════════════════════════════════════════════════════

#generatore sfida captcha

_LEET_MAP = {'a': '4', 'e': '3', 'i': '1', 'o': '0', 's': '5', 't': '7'}

_CAPTCHA_PHRASES = [
    "ciao", "come stai", "tutto bene", "buongiorno",
    "ethereum", "blockchain", "oracle attivo", "swap sicuro",
    "claim valido", "rete sicura", "nodo attivo", "firma valida",
    "hash corretto", "blocco nuovo", "token vero", "gas basso",
    "pool solvente", "stake pronto", "verifica ok", "mediana giusta",
]

def generate_captcha():
    """Genera una coppia (challenge_leetspeak, answer_plaintext)."""
    answer = random.choice(_CAPTCHA_PHRASES)
    challenge = ""
    for ch in answer:
        if ch.lower() in _LEET_MAP and random.random() > 0.3:
            challenge += _LEET_MAP[ch.lower()]
        else:
            challenge += ch
    # Assicura almeno una sostituzione
    if challenge == answer:
        for i, ch in enumerate(challenge):
            if ch.lower() in _LEET_MAP:
                challenge = challenge[:i] + _LEET_MAP[ch.lower()] + challenge[i+1:]
                break
    return challenge, answer

# ══════════════════════════════════════════════════════════════
#  ORACLE STATS JSON
# ══════════════════════════════════════════════════════════════

_ORACLE_STATS_FILE = utils.CONFIG_DIR / "oracle_stats.json"

#aggiorna il json orcale_stats con i dati degli oracle
def _update_oracle_stats(reward_eth: float = 0.0, gas_refund_mevi: float = 0.0,
                         claims_delta: int = 0) -> None:
    """Aggiorna atomicamente oracle_stats.json con i totali cumulativi."""
    try:
        try:
            with open(_ORACLE_STATS_FILE, "r") as f:
                stats = json.load(f)
        except Exception:
            stats = {"oracle_rewards_count": 0, "oracle_rewards_eth": 0.0,
                     "gas_refunds_count": 0, "gas_refunds_mevi": 0.0,
                     "claims_processed": 0, "last_updated": ""}
        if reward_eth > 0:
            stats["oracle_rewards_count"] += 1
            stats["oracle_rewards_eth"] = round(stats["oracle_rewards_eth"] + reward_eth, 8)
        if gas_refund_mevi > 0:
            stats["gas_refunds_count"] += 1
            stats["gas_refunds_mevi"] = round(stats["gas_refunds_mevi"] + gas_refund_mevi, 6)
        stats["claims_processed"] += claims_delta
        stats["last_updated"] = time.strftime("%H:%M:%S")
        tmp = str(_ORACLE_STATS_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, str(_ORACLE_STATS_FILE))
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
#  STATO GLOBALE
# ══════════════════════════════════════════════════════════════

w3 = None
contracts = None
deployer = None
oracle_accounts = []       # lista di indirizzi
oracle_index_map = {}      # addr -> indice nel nostro array
processed_claims = set()   # claim già processati
processed_reports = set()   # report già processati
claim_commit_data = {}     # claim_id -> {addr: (score, pattern, salt)}
captcha_answers = {}  
# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

def olog(oracle_idx, msg):
    """Log colorato per un singolo oracle"""
    color = _ORACLE_COLORS[oracle_idx % len(_ORACLE_COLORS)]
    ts = datetime.now().strftime("%H:%M:%S")
    tag = f"ORC-{oracle_idx}"

    line_colored = f"{color}[{ts}][{tag:>5s}] {msg}{_RESET}"
    print(line_colored)

def clog(msg):
    """Log per eventi claim"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}][CLAIM] {msg}"

    print(f"{_BOLD}\033[95m{line}{_RESET}")


# ══════════════════════════════════════════════════════════════
#  SETUP INTERATTIVO
# ══════════════════════════════════════════════════════════════

#cnfigurazione rete oracle manuale
def setup():
    """Configura la rete oracle: numero, account, stake."""
    print("\n" + "=" * 55)
    print("      ⚡ CONFIGURAZIONE RETE ORACLE ⚡")
    print("=" * 55)

    n = int(input(f"  Quanti oracle deployare? (default 7): ") or "7")
    start_idx = int(input(f"  Account di partenza (default 10): ") or "10")
    stake = float(input(f"  Stake per oracle in ETH (default 0.1): ") or "0.1")

    total_accounts = len(w3.eth.accounts)
    if start_idx + n > total_accounts:
        print(f"  ⚠️  Servono {n} account da indice {start_idx}, ma Hardhat ne ha solo {total_accounts}.")
        print(f"      Riduco a {total_accounts - start_idx} oracle.")
        n = total_accounts - start_idx

    accounts = []
    print(f"\n  {'─' * 50}")
    for i in range(n):
        idx = start_idx + i
        addr = w3.eth.accounts[idx]
        accounts.append(addr)
        bal = Web3.from_wei(w3.eth.get_balance(addr), 'ether')
        print(f"  Oracle #{i}  →  Account #{idx}  {addr[:18]}...  ({bal:.1f} ETH)")
    print(f"  {'─' * 50}")

    return accounts, stake


# ══════════════════════════════════════════════════════════════
#  REGISTRAZIONE E ATTIVAZIONE
# ══════════════════════════════════════════════════════════════

_STATUS_NAMES = [
    "Inactive", "Pending", "Active", "Watchlisted",
    "Contested", "Slashed", "Expelled"
]

def _status_name(code):
    if code < len(_STATUS_NAMES):
        return _STATUS_NAMES[code]
    return f"Unknown({code})"


#prima di registrare gli oracle attivo l'automine di hardhat altrimenti ci metterebbero troppo
def register_and_activate(accounts, stake_eth):
    """Registra e attiva tutti gli oracle su OracleRegistry."""
    oreg = contracts["OracleRegistry"]
    stake_wei = Web3.to_wei(stake_eth, 'ether')

    print(f"\n  Registrazione e attivazione di {len(accounts)} oracle...\n")

    w3.provider.make_request("evm_setAutomine", [True])      # ← AGGIUNGI
    w3.provider.make_request("evm_setIntervalMining", [0])    # ← AGGIUNGI

    #per ogni oracle verifico lo stato iniziale e registro solo non sia già registrato
    for i, addr in enumerate(accounts):
        try:
            info = oreg.functions.getOracleInfo(addr).call()
            current_status = info[1]
        except Exception:
            current_status = 0  # Non ancora registrato

        # ── Registrazione ──
        try:
            _already_oracle = oreg.functions.isOracle(addr).call()
        except Exception:
            _already_oracle = False
        if current_status == 0 and not _already_oracle:
            try:
                min_stake = oreg.functions.getMinimumStake().call()
                actual_stake = max(stake_wei, min_stake)
                utils.send_tx(w3, oreg.functions.registerOracle(), addr, value=actual_stake)
                olog(i, f"Registrato con stake {Web3.from_wei(actual_stake, 'ether'):.4f} ETH")
            except Exception as e:
                olog(i, f"Errore registrazione: {e}")
                continue
            info = oreg.functions.getOracleInfo(addr).call()
            current_status = info[1]
        elif current_status >= 2:
            olog(i, f"Gia registrato ({_status_name(current_status)})")

        #dopo la registrazione lo stato è pending e si chiama activate oracle per attivarlo
        # ── Attivazione ──
        if current_status == 1:
            try:
                utils.send_tx(w3, oreg.functions.activateOracle(), addr)
                olog(i, f"Attivato! Pronto per i claim")
            except Exception as e:
                olog(i, f"Errore attivazione: {e}")
        elif current_status == 2:
            olog(i, f"Gia attivo")

    w3.provider.make_request("evm_setAutomine", [False])       # ← AGGIUNGI
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS]) # ← AGGIUNGI

    # Popola la mappa indice
    global oracle_index_map
    oracle_index_map = {addr: i for i, addr in enumerate(accounts)}

    # Verifica conteggio
    active_count = oreg.functions.activeOracleCount().call()
    utils.log(f"Oracle attivi nella rete: {active_count}", "SYSTEM", to_file=False)

# ══════════════════════════════════════════════════════════════
#  COMMIT-REVEAL: CRITTOGRAFIA
# ══════════════════════════════════════════════════════════════

#funziona crittata con salt che viene inviata al contratto che poi verificherà
def compute_commit_hash(fraud_score, pattern_valid, salt_bytes32):
    packed = (
        fraud_score.to_bytes(1, 'big') +
        (b'\x01' if pattern_valid else b'\x00') +
        salt_bytes32
    )
    return Web3.keccak(packed)


# ══════════════════════════════════════════════════════════════
#  PROCESSAMENTO CLAIM
# ══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# FUNZIONI PER IL GRAFO (BFS & NEIGHBORS)
# ═══════════════════════════════════════════════════════════════════════════

#scansiono ultimi 200 blocchi e costruisco set di indirizzi che hanno avuto interazione col target_address
def fetch_onchain_neighbors(w3, target_address, blocks_back=200):
  
    neighbors = set()
    target_address = Web3.to_checksum_address(target_address)
    try:
        curr_block = w3.eth.block_number
        start_block = max(0, curr_block - blocks_back)
        
        # Analisi dei blocchi per ricostruire gli archi del grafo
        for i in range(start_block, curr_block + 1):
            block = w3.eth.get_block(i, full_transactions=True)
            for tx in block.transactions:
                f = Web3.to_checksum_address(tx['from'])
                t = Web3.to_checksum_address(tx['to']) if tx['to'] else None
                
                if f == target_address and t:
                    neighbors.add(t)
                elif t == target_address:
                    neighbors.add(f)
    except Exception as e:
        print(f"LOG: Errore nel recupero vicini per {target_address}: {e}")
    
    return neighbors

#esclude subito i nodi whitelist e poi fa bfs su grafo delle tx
def get_network_distance(w3, start_node, blacklist, whitelist, max_depth=3):
    """
    Esegue una Breadth-First Search (BFS) per trovare la distanza minima
    tra l'utente e i bot in blacklist, ignorando i nodi in whitelist.
    """
    start_node = Web3.to_checksum_address(start_node)
    b_set = {Web3.to_checksum_address(a) for a in blacklist}
    w_set = {Web3.to_checksum_address(a) for a in whitelist}
    
    print(f"      [BFS] Inizio ricerca per {start_node}")
    print(f"      [BFS] Blacklist: {len(b_set)} nodi | Whitelist: {len(w_set)} nodi")

    # Coda BFS: (indirizzo_corrente, distanza_dal_punto_zero)
    queue = deque([(start_node, 0)])
    visited = {start_node}
    visited.update(w_set) # I nodi whitelistati non vengono esplorati

    while queue:
        curr, dist = queue.popleft()
        
        # Abbiamo raggiunto un bot MEV?
        if curr in b_set:
            print(f"      [BFS] MATCH TROVATO! Nodo: {curr} | Distanza: {dist}")
            return dist
            
        # Se non abbiamo superato la profondità massima, esploriamo i vicini
        if dist < max_depth:
            neighbors = fetch_onchain_neighbors(w3, curr)
            print(f"      [BFS] Profondità {dist}: trovati {len(neighbors)} vicini per {curr}")
            for n in neighbors:
                if n not in visited:
                    visited.add(n)
                    queue.append((n, dist + 1))
                    
    print(f"      [BFS] Nessuna connessione trovata entro profondità {max_depth}")
    return 0  # Nessuna connessione trovata entro max_depth

# ═══════════════════════════════════════════════════════════════════════════
# FUNZIONE PRINCIPALE DI ANALISI (SELF ANALYZE)
# ═══════════════════════════════════════════════════════════════════════════


#le 3 tx devono essere nello stesso blocco
def self_analyze_sandwich(w3, tx_front, tx_user, tx_back, loss, swap_value, user_profile, blacklist, whitelist, precomputed_network_score=None):
    """
    Analisi investigativa dell'Oracle.
    Implementa: Pattern Check, Tier Score, Claim Rate Score e Network Score.
    """
    try:
        # 1. ANALISI TECNICA DEL PATTERN (A-B-A)
        # ──────
        rf = w3.eth.get_transaction_receipt(tx_front)
        ru = w3.eth.get_transaction_receipt(tx_user)
        rb = w3.eth.get_transaction_receipt(tx_back)
        tf = w3.eth.get_transaction(tx_front)
        tb = w3.eth.get_transaction(tx_back)

        pattern_valid = (
            rf.blockNumber == ru.blockNumber == rb.blockNumber and
            rf.transactionIndex < ru.transactionIndex < rb.transactionIndex and
            Web3.to_checksum_address(tf['from']) == Web3.to_checksum_address(tb['from'])
        )

        print(f"      [PATTERN CHECK]")
        print(f"      • Block: front={rf.blockNumber}, user={ru.blockNumber}, back={rb.blockNumber}")
        print(f"      • Index: front={rf.transactionIndex}, user={ru.transactionIndex}, back={rb.transactionIndex}")
        print(f"      • From:  front={tf['from']}, back={tb['from']}")
        print(f"      • Result: {'✅ VALID' if pattern_valid else '❌ INVALID'}")

        # 2. CALCOLO COMPONENTI FRAUDSCORE (PDF §1.3.11)
        # ─────────────────────────────────────────────────────────
        
        # A. Score Tier (Tabella 4)
        tier_id = user_profile[0]
        tier_names = ["Bronze", "Silver", "Gold", "Platinum"]
        tier_name = tier_names[tier_id] if tier_id < 4 else "Unknown"
        score_tier = {0: 50, 1: 30, 2: 15, 3: 0}.get(tier_id, 50)
        
        print(f"      [SCORE TIER]")
        print(f"      • Tier ID: {tier_id} ({tier_name}) | Punteggio: {score_tier}")

        # B. Score Claim Rate (Tabella 5)
        total_swaps = user_profile[2]
        total_claims = user_profile[3]
        effective_swaps = total_swaps + 5 if total_swaps <= 3 else total_swaps
        claim_rate = (total_claims / effective_swaps) if effective_swaps > 0 else 0
        
        match_str = ""
        if   claim_rate >= 0.30: 
            score_claim = 30
            match_str = ">=30%"
        elif claim_rate >= 0.20: 
            score_claim = 25
            match_str = ">=20%"
        elif claim_rate >= 0.10: 
            score_claim = 20
            match_str = ">=10%"
        elif claim_rate >= 0.06: 
            score_claim = 15
            match_str = ">=6%"
        else:                    
            score_claim = 0
            match_str = "<6%"

        print(f"      [SCORE CLAIM RATE]")
        print(f"      • Claims: {total_claims} | Swaps: {total_swaps} | Rate: {claim_rate:.2%}")
        print(f"      • Soglia: {match_str} | Punteggio: {score_claim}")

        # C. Score Network (Tabella 6 - Grafo BFS)
        if precomputed_network_score is not None:
            score_network = precomputed_network_score
            print(f"      [SCORE NETWORK BFS] (pre-calcolato)")
            print(f"      • Punteggio: {score_network}")
        else:
            user_addr = Web3.to_checksum_address(w3.eth.get_transaction(tx_user)['from'])
            print(f"      [SCORE NETWORK BFS]")
            dist = get_network_distance(w3, user_addr, blacklist, whitelist)
            score_network = 0
            if dist == 1:
                score_network = 50
            elif dist == 2:
                score_network = 30
            elif dist == 3:
                score_network = 15
            elif dist >= 4:
                score_network = max(5, 16 - dist)
            print(f"      • Utente: {user_addr}")
            print(f"      • Distanza: {dist if dist > 0 else 'nessuna connessione'} | Punteggio: {score_network}")

        # 3. FINALIZZAZIONE
        # ─────────────────────────────────────────────────────────
        
        # Somma dei componenti
        fraud_score = score_tier + score_claim + score_network
        print(f"      [TOTAL]")
        print(f"      • {score_tier} (Tier) + {score_claim} (Claim) + {score_network} (Network) = {fraud_score}")

        # Se il pattern tecnico fallisce, il claim è considerato fraudolento (Score 120)
        if not pattern_valid:
            print(f"      • ⚠️ Pattern INVALIDO: override score a 120")
            fraud_score = 120
            
        # Applichiamo il limite massimo di 130 punti
        final_score = int(min(130, fraud_score))
        return final_score, pattern_valid

    except Exception as e:
        # In caso di errore (es. hash inesistenti), l'oracle rigetta per sicurezza
        print(f"LOG: Errore critico durante self_analyze: {e}")
        return 130, False
    
def process_claim(claim_id):
    """
    Processa un claim: commit + reveal per ogni oracle assegnato,
    poi finalizza se tutti hanno rivelato.
    """
    ins = contracts["MEVInsurance"]
    # ── 1. Leggi dettagli claim ──
    try:
        claim_info = ins.functions.getClaimInfo(claim_id).call()
    except Exception as e:
        clog(f"Errore lettura claim #{claim_id}: {e}")
        return

    user        = claim_info[0]
    status      = claim_info[1]
    swap_value  = claim_info[3]
    loss        = claim_info[4]
    reveal_count = claim_info[6]
    commit_count = claim_info[7]

    # Solo claim in OracleReview (status=1)
    if status != 1:
        clog(f"Claim #{claim_id} non in OracleReview (status={status}), skip")
        return

    assigned_oracles = ins.functions.getClaimOracles(claim_id).call()

    # Dettagli aggiuntivi
    try:
        details = ins.functions.getClaimDetails(claim_id).call()
        bot_addr = details[6]
        tx1, tx2, tx3 = details[1], details[2], details[3]
    except:
        bot_addr = "0x?"
        tx1 = tx2 = tx3 = b'\x00' * 32

    clog(f"{'━' * 60}")
    clog(f"NUOVO CLAIM #{claim_id}")
    clog(f"  Utente:     {user}")
    clog(f"  Swap Value: {utils.from_wei(swap_value):.4f} MEVI")
    clog(f"  Loss:       {utils.from_wei(loss):.4f} MEVI")
    clog(f"  Bot:        {bot_addr}")
    clog(f"  TX Hashes:  front={tx1.hex()[:16]}... victim={tx2.hex()[:16]}... back={tx3.hex()[:16]}...")
    clog(f"  Oracle assegnati: {len(assigned_oracles)}")
    clog(f"{'━' * 60}")

    # ── 2. Identifica i NOSTRI oracle assegnati ──
    my_assigned = []
    for addr in assigned_oracles:
        if addr in oracle_index_map:
            idx = oracle_index_map[addr]
            my_assigned.append((idx, addr))
            olog(idx, f"📋 ASSEGNATO al claim #{claim_id}")

    not_ours = len(assigned_oracles) - len(my_assigned)
    if not_ours > 0:
        clog(f"  ⚠️  {not_ours} oracle assegnati NON sono nostri (non gestiamo)")

    if not my_assigned:
        clog(f"  Nessun nostro oracle assegnato a questo claim. Attendo finalizzazione esterna.")
        return

    # ── 3. Leggi profilo utente per FraudScore ──
    profile = ins.functions.getUserProfile(user).call()
    tier_id      = profile[0]
    total_swaps  = profile[2]
    total_claims = profile[3]

    tier_names = ["Bronze", "Silver", "Gold", "Platinum"]
    tier_name = tier_names[tier_id] if tier_id < 4 else f"Tier({tier_id})"

    clog(f"  Profilo utente: {tier_name} | Swaps: {total_swaps} | Claims: {total_claims}")

    # --- INTEGRAZIONE BLACKLIST & WHITELIST ---
    try:
        blacklisted_bots = ins.functions.getBlacklistedBots().call()
        blacklisted_users = ins.functions.getBlacklistedUsers().call()
        WHITELIST = ins.functions.getWhitelist().call()
        BLACKLIST = blacklisted_bots + blacklisted_users
    except Exception as e:
        clog(f"  ⚠️ Errore lettura blacklist/whitelist: {e}")
        BLACKLIST = []
        WHITELIST = []

    # ── 3.5 Pre-calcola BFS network score una sola volta per tutti gli oracle ──
    precomputed_network_score = None
    try:
        user_addr_bfs = Web3.to_checksum_address(w3.eth.get_transaction(tx2)['from'])
        clog(f"  [BFS] Pre-calcolo distanza rete per {user_addr_bfs[:16]}...")
        dist_bfs = get_network_distance(w3, user_addr_bfs, BLACKLIST, WHITELIST)
        if dist_bfs == 1:
            precomputed_network_score = 50
        elif dist_bfs == 2:
            precomputed_network_score = 30
        elif dist_bfs == 3:
            precomputed_network_score = 15
        elif dist_bfs >= 4:
            precomputed_network_score = max(5, 16 - dist_bfs)
        else:
            precomputed_network_score = 0
        clog(f"  [BFS] Distanza: {dist_bfs if dist_bfs > 0 else 'nessuna'} | Score network: {precomputed_network_score}")
    except Exception as e:
        clog(f"  [BFS] Errore pre-calcolo: {e}")
        precomputed_network_score = 0

    # ── 4. FASE COMMIT ──
    clog(f"  ── FASE 1: COMMIT ──")
    commit_store = {}

    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    # Carica stato di test (malfunzionamenti)
    test_state = _load_test_state()
    active_bugs = test_state.get("bugs", {})

    for idx, addr in my_assigned:
        # Chiamata aggiornata con user_profile, blacklist e whitelist
        fraud_score, pattern_valid = self_analyze_sandwich(
            w3, tx1, tx2, tx3, loss, swap_value, profile, BLACKLIST, WHITELIST,
            precomputed_network_score=precomputed_network_score
        )
        
        # Applica eventuale malfunzionamento FraudScore
        bug_deviation = active_bugs.get(addr.lower(), 0)
        if bug_deviation != 0:
            original_score = fraud_score
            fraud_score = max(0, min(130, fraud_score + bug_deviation))
            olog(idx, f"  🔧 MALFUNZIONAMENTO: score {original_score} → {fraud_score} (deviation {bug_deviation:+d})")

        # Genera salt crittografico
        salt = secrets.token_bytes(32)

        # Calcola hash del commit
        commit_hash = compute_commit_hash(fraud_score, pattern_valid, salt)

        olog(idx, f"  🧮 FraudScore calcolato: {fraud_score}/130 | Pattern: {'✅ VALID' if pattern_valid else '❌ INVALID'}")

        # Invia commit on-chain
        try:
            utils.send_tx(w3, ins.functions.commitVerdict(claim_id, commit_hash), addr)
            commit_store[addr] = (fraud_score, pattern_valid, salt)
            olog(idx, f"  🔒 COMMIT inviato | Hash: {commit_hash.hex()[:20]}...")
        except Exception as e:
            olog(idx, f"  ❌ Errore COMMIT: {e}")

    # Piccola pausa per far minare il blocco col commit
    time.sleep(0.5)

    # ── 5. FASE REVEAL ──
    clog(f"  ── FASE 2: REVEAL ──")

    for idx, addr in my_assigned:
        if addr not in commit_store:
            olog(idx, f"  ⏭️  Skip reveal (commit fallito)")
            continue

        fraud_score, pattern_valid, salt = commit_store[addr]

        try:
            utils.send_tx(
                w3,
                ins.functions.revealVerdict(claim_id, fraud_score, pattern_valid, salt),
                addr
            )
            olog(idx, f"  🔓 REVEAL completato | Score: {fraud_score} | Pattern: {'VALID' if pattern_valid else 'INVALID'}")
        except Exception as e:
            olog(idx, f"  ❌ Errore REVEAL: {e}")

    # ── 6. VERIFICA E FINALIZZAZIONE ──
    time.sleep(0.5)

    claim_info = ins.functions.getClaimInfo(claim_id).call()
    current_reveals = claim_info[6]
    total_assigned  = len(assigned_oracles)

    clog(f"  Reveal completati: {current_reveals}/{total_assigned}")

    if current_reveals >= total_assigned:
        clog(f"  ── FASE 3: FINALIZZAZIONE ──")

        # Salvataggio balance PRIMA della finalizzazione
        balances_before = {}
        for idx, addr in my_assigned:
            if has_revealed := ins.functions.hasRevealed(claim_id, addr).call():
                balances_before[addr] = w3.eth.get_balance(addr)

        # Qualsiasi account può chiamare finalizeClaim una volta che tutti hanno fato reveal
        finalizer = my_assigned[0][1]
        finalizer_idx = my_assigned[0][0]

        try:
            receipt = utils.send_tx(w3, ins.functions.finalizeClaim(claim_id), finalizer)

            # Calcolo gas speso dal finalizer
            gas_used = receipt['gasUsed'] * receipt['effectiveGasPrice']

            # Leggi risultato finale
            final_info = ins.functions.getClaimInfo(claim_id).call()
            final_status  = final_info[1]
            median_score  = final_info[2]
            dispersione   = final_info[5]

            status_labels = {
                0: "Pending",
                1: "OracleReview",
                2: "⏳ CAPTCHA Required",
                3: "✅ APPROVED",
                4: "❌ REJECTED",
                5: "🚫 Invalid Pattern"
            }
            status_str = status_labels.get(final_status, f"Unknown({final_status})")

            clog(f"  ╔══════════════════════════════════════╗")
            clog(f"  ║  CLAIM #{claim_id} → {status_str}")
            clog(f"  ║  Mediana FraudScore: {median_score}/130")
            clog(f"  ║  Dispersione: {dispersione}")
            clog(f"  ╚══════════════════════════════════════╝")

            # Pool ETH MEVInsurance PRIMA dei reward
            ins_pool_before = w3.eth.get_balance(ins.address)

            # Log dei reward effettivi
            total_reward_ours = 0
            for idx, addr in my_assigned:
                if addr in balances_before:
                    balance_after = w3.eth.get_balance(addr)
                    diff_wei = balance_after - balances_before[addr]

                    # Compensa il gas se l'oracle è il finalizer
                    if addr == finalizer:
                        diff_wei += gas_used

                    diff_eth = Web3.from_wei(diff_wei, 'ether')

                    if diff_wei > 0:
                        olog(idx, f"  💰 Reward: +{diff_eth:.6f} ETH (balance: {Web3.from_wei(balance_after, 'ether'):.4f} ETH)")
                        total_reward_ours += diff_eth
                    else:
                        olog(idx, f"  ⚠️ Nessun reward ricevuto")

            # Log variazione pool ETH MEVInsurance
            ins_pool_after = w3.eth.get_balance(ins.address)
            ins_pool_delta = ins_pool_before - ins_pool_after
            clog(f"  🏦 Pool ETH Insurance: {Web3.from_wei(ins_pool_before,'ether'):.6f} → "
                 f"{Web3.from_wei(ins_pool_after,'ether'):.6f} ETH  "
                 f"(−{Web3.from_wei(ins_pool_delta,'ether'):.6f} ETH reward oracle)")
            # Aggiorna contatori cumulativi oracle_stats.json
            _update_oracle_stats(reward_eth=float(total_reward_ours), claims_delta=1)

            # Se secondary review è stata triggerata, riprocessa il claim
            if final_status == 1:  # OracleReview = secondary review triggered
                clog(f"  🔄 SECONDARY REVIEW triggerata (dispersione {dispersione} > soglia)!")
                clog(f"  🔄 Riprocesso claim #{claim_id} con nuovi oracle...")
                processed_claims.discard(claim_id)
                w3.provider.make_request("evm_setAutomine", [False])
                w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])
                time.sleep(1)
                process_claim(claim_id)
                return

            # ── CAPTCHA: pubblica challenge immediatamente senza aspettare il prossimo tick ──
            if final_status == 2:  # CAPTCHARequired
                clog(f"  ⏰ CAPTCHA richiesto: pubblico challenge subito (senza aspettare il loop)...")
                w3.provider.make_request("evm_setAutomine", [False])
                w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])
                process_claim_captcha(claim_id, user)
                return

            # ── 7. TRACKING SCOSTAMENTI (WATCHLIST) ──
            # Salta InvalidPattern (5): finalFraudScore è forzato a 100 dal contratto
            # indipendentemente dai voti oracle — non penalizzare oracle corretti.
            if final_status in [3, 4]:  # Approved, Rejected only
                _track_deviations(claim_id, median_score)

        except Exception as e:
            err_str = str(e).lower()
            if "erc20insufficientbalance" in err_str or "erc20insufficient" in err_str:
                clog(f"  ⚠️  Claim #{claim_id}: pool MEVI insufficiente per il payout ({e})")
            else:
                clog(f"  ⚠️  Errore finalizzazione: {e}")

    elif current_reveals > 0:
        clog(f"  ⏳ Claim #{claim_id}: in attesa di altri {total_assigned - current_reveals} reveal (oracle esterni)")
    else:
        clog(f"  ⚠️  Nessun reveal riuscito per il claim #{claim_id}")

    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])

# ══════════════════════════════════════════════════════════════
#  FUNZIONI INTERATTIVE (per uso con python -i)
# ══════════════════════════════════════════════════════════════

#per ogni oracle che ha rivelato si calcola quanto ha scostato dalla mediana
def _track_deviations(claim_id, median_score):
    """Registra lo scostamento di ogni oracle dal mediano per watchlist."""
    ins = contracts["MEVInsurance"]
    oreg = contracts["OracleRegistry"]
    
    assigned = ins.functions.getClaimOracles(claim_id).call()
    scores = ins.functions.getClaimScores(claim_id).call()
    
    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])
    
    for i, oracle_addr in enumerate(assigned):
        if not ins.functions.hasRevealed(claim_id, oracle_addr).call():
            continue
        if i >= len(scores):
            continue
        deviation = abs(int(scores[i]) - int(median_score))
        if deviation > 0:
            try:
                utils.send_tx(w3, oreg.functions.recordDeviation(oracle_addr, deviation), deployer)
                idx = oracle_index_map.get(oracle_addr, "?")
                if deviation >= 10:
                    clog(f"  ⚠️  Oracle {idx} ({oracle_addr[:12]}...): scostamento {deviation} (STRIKE!)")
                else:
                    clog(f"  📊 Oracle {idx} ({oracle_addr[:12]}...): scostamento {deviation}")
            except Exception as e:
                clog(f"  ⚠️  Errore recordDeviation per {oracle_addr[:12]}...: {e}")
    
    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])
def process_report(report_id):
    """
    Processa un report di slashing: ogni nostro oracle nella jury
    vota una percentuale di slash (0-100) tramite commit-reveal.
    
    Include il log dettagliato dei reward e della loro origine.
    """
    slashing = contracts["SlashingSystem"]
    
    # ── 1. Leggi dettagli report ──
    try:
        # getReportInfo restituisce: [reporter, accused, status, slashPercent, median, reveals]
        info = slashing.functions.getReportInfo(report_id).call()
        jury = slashing.functions.getReportJury(report_id).call()
    except Exception as e:
        clog(f"Errore lettura report #{report_id}: {e}")
        return
    
    reporter = info[0]
    accused = info[1]
    status_code = info[2]
    
    # Procediamo solo se il report è in attesa (Pending = 0)
    if status_code != 0:
        clog(f"Report #{report_id} non Pending (status={status_code}), skip")
        return
    
    clog(f"{'━' * 60}")
    clog(f"⚖️  NUOVO REPORT #{report_id}")
    clog(f"  Reporter: {reporter[:16]}...")
    clog(f"  Accusato: {accused[:16]}...")
    clog(f"  Jury size: {len(jury)}")
    clog(f"{'━' * 60}")
    
    # ── 2. Identifica i NOSTRI juror ──
    my_jurors = []
    for addr in jury:
        if addr in oracle_index_map:
            idx = oracle_index_map[addr]
            my_jurors.append((idx, addr))
            olog(idx, f"⚖️  ASSEGNATO come juror al report #{report_id}")
    
    if not my_jurors:
        clog(f"  Nessun nostro oracle nella jury. Skip.")
        return
    
    # ── 3. FASE COMMIT (Voto segreto) ──
    clog(f"  ── FASE 1: COMMIT (voto segreto) ──")
    commit_store = {}
    
    # Velocizziamo il mining per i test
    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])
    
    test_state = _load_test_state()
    jury_probs = test_state.get("jury_probs", {})
    DEFAULT_PROB = test_state.get("jury_default_prob", 0.55)

    for idx, addr in my_jurors:
        # Probabilità di voto "innocente" (da test_state o default globale)
        innocent_prob = jury_probs.get(addr.lower(), DEFAULT_PROB)
        
        if random.random() < innocent_prob:
            slash_percent = 0
            verdict_str = f"0% (innocente) [P_inn={innocent_prob:.2f}]"
        else:
            slash_percent = random.randint(1, 100)
            verdict_str = f"{slash_percent}% (colpevole) [P_inn={innocent_prob:.2f}]"
        
        # Generazione commit: keccak256(slash_percent + salt)
        salt = secrets.token_bytes(32)
        packed = slash_percent.to_bytes(1, 'big') + salt
        commit_hash = Web3.keccak(packed)
        
        olog(idx, f"  🧮 Voto generato: {verdict_str}")
        
        try:
            utils.send_tx(w3, slashing.functions.commitJuryVote(report_id, commit_hash), addr)
            commit_store[addr] = (slash_percent, salt)
            olog(idx, f"  🔒 COMMIT inviato on-chain")
        except Exception as e:
            olog(idx, f"  ❌ Errore COMMIT: {e}")
    
    time.sleep(0.5)
    
    # ── 4. FASE REVEAL (Voto pubblico) ──
    clog(f"  ── FASE 2: REVEAL (apertura voti) ──")
    for idx, addr in my_jurors:
        if addr not in commit_store:
            continue
        
        slash_percent, salt = commit_store[addr]
        try:
            utils.send_tx(w3, slashing.functions.revealJuryVote(report_id, slash_percent, salt), addr)
            olog(idx, f"  🔓 REVEAL completato | Slash: {slash_percent}%")
        except Exception as e:
            olog(idx, f"  ❌ Errore REVEAL: {e}")
    
    time.sleep(0.5)
    
    # ── 5. FINALIZZAZIONE E ANALISI REWARD ──
    # Rileggiamo lo stato per vedere se tutti hanno rivelato
    info = slashing.functions.getReportInfo(report_id).call()
    current_reveals = info[5]
    total_jury = len(jury)
    
    if current_reveals >= total_jury:
        clog(f"  ── FASE 3: FINALIZZAZIONE E LOG REWARDS ──")
        
        # Uno dei nostri oracle (il primo della lista) esegue la finalizzazione
        f_idx, f_addr = my_jurors[0]
        
        try:
            # Chiamata al contratto per calcolare la mediana e applicare lo slash
            utils.send_tx(w3, slashing.functions.finalizeSlashing(report_id), f_addr)
            
            # Recuperiamo i dati finali dal report per i log
            # Assumiamo la struct Report = [accused, reporter, status, slashAmount, ...]
            report_final = slashing.functions.reports(report_id).call()
            final_status = report_final[2]
            # enum fields from mapping getters come back as bytes in some web3 versions
            if isinstance(final_status, (bytes, bytearray)):
                final_status = int.from_bytes(final_status, 'big') if final_status else 0
            else:
                final_status = int(final_status)
            slash_amount_wei = report_final[3]
            
            votes = slashing.functions.getReportVotes(report_id).call()
            
            status_labels = {
                0: "Pending",
                1: "✅ SLASHED",
                2: "🔻 EXPELLED",
                3: "❌ DISMISSED (innocente)"
            }
            status_str = status_labels.get(final_status, f"Unknown({final_status})")
            
            sorted_votes = sorted(votes)
            median = sorted_votes[len(sorted_votes) // 2] if sorted_votes else 0

            clog(f"  ╔══════════════════════════════════════════════════════╗")
            clog(f"  ║  REPORT #{report_id} FINALIZZATO → {status_str}")
            clog(f"  ║  Taglio Totale (Slash): {Web3.from_wei(slash_amount_wei, 'ether'):.6f} ETH")
            
            # Determiniamo l'origine del Reward
            if slash_amount_wei > 0:
                # Se c'è stato uno slash, i juror si dividono la torta dell'accusato
                reward_per_juror = slash_amount_wei / len(jury)
                clog(f"  ║  Origine Reward: 🔪 STAKE TAGLIATO ALL'ACCUSATO")
            else:
                # Se l'accusato è innocente, cerchiamo l'incentivo dal protocol pool
                try:
                    pool_reward = slashing.functions.fixedJurorReward().call()
                    reward_per_juror = pool_reward
                    clog(f"  ║  Origine Reward: 🏦 INSURANCE POOL (Incentivo)")
                except:
                    reward_per_juror = 0
                    clog(f"  ║  Origine Reward: NESSUNO (Innocenza senza premio)")

            clog(f"  ║  Mediana Riscontrata: {median}%")
            clog(f"  ║  Reward Stimato x Juror: {Web3.from_wei(reward_per_juror, 'ether'):.6f} ETH")
            clog(f"  ╟──────────────────────────────────────────────────────╢")
            
            # Ciclo sui NOSTRI oracle per vedere l'integrazione del reward
            for idx, addr in my_jurors:
                # Otteniamo il bilancio ETH attuale
                eth_bal = w3.eth.get_balance(addr)
                # Se il reward va nello stake, dovresti usare: oreg.functions.getOracleInfo(addr).call()[0]
                clog(f"  ║  Oracle #{idx:2d} | Juror: ✅ | Bal: {Web3.from_wei(eth_bal, 'ether'):.4f} ETH")
            
            clog(f"  ╚══════════════════════════════════════════════════════╝")
            
        except Exception as e:
            clog(f"  ⚠️  Errore durante la finalizzazione o il calcolo: {e}")
    else:
        clog(f"  ⏳ Report #{report_id}: in attesa di reveal esterni ({current_reveals}/{total_jury})")
    
    # Ripristino mining normale
    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])
def oracle_status():
    """Stampa lo stato completo di tutti gli oracle."""
    oreg = contracts["OracleRegistry"]

    print(f"\n{'═' * 70}")
    print(f"  ⚡ STATO RETE ORACLE ({len(oracle_accounts)} nodi)")
    print(f"{'═' * 70}")

    for i, addr in enumerate(oracle_accounts):
        info = oreg.functions.getOracleInfo(addr).call()

        stake_eth   = Web3.from_wei(info[0], 'ether')
        status      = _status_name(info[1])
        dev_score   = info[4]
        claims_eval = info[5]
        strikes     = info[6]

        color = _ORACLE_COLORS[i % len(_ORACLE_COLORS)]
        eth_bal = Web3.from_wei(w3.eth.get_balance(addr), 'ether')

        print(f"{color}"
              f"  Oracle #{i:2d} │ {addr[:14]}... │ {status:<12s} │ "
              f"Stake: {stake_eth:>6.3f} ETH │ Claims: {claims_eval:>3d} │ "
              f"DevScore: {dev_score:>4d} │ Strikes: {strikes} │ "
              f"Balance: {eth_bal:>8.2f} ETH"
              f"{_RESET}")

    active = oreg.functions.activeOracleCount().call()
    print(f"{'─' * 70}")
    print(f"  Oracle attivi totali nella rete: {active}")
    print(f"{'═' * 70}")


def force_finalize(claim_id):
    """Forza la finalizzazione di un claim (utile se timeout scaduto)."""
    ins = contracts["MEVInsurance"]
    try:
        utils.send_tx(w3, ins.functions.finalizeClaim(claim_id), oracle_accounts[0])
        clog(f"Claim #{claim_id} finalizzato manualmente")

        final_info = ins.functions.getClaimInfo(claim_id).call()
        status_labels = {
            0: "Pending", 1: "OracleReview", 2: "CAPTCHA Required",
            3: "APPROVED", 4: "REJECTED", 5: "Invalid Pattern"
        }
        clog(f"  Risultato: {status_labels.get(final_info[1], final_info[1])} | Mediana: {final_info[2]}")
    except Exception as e:
        clog(f"Errore: {e}")

# ══════════════════════════════════════════════════════════════
#  GESTIONE RICHIESTE PLATINUM (CAPTCHA 2-of-3)
# ══════════════════════════════════════════════════════════════

def process_platinum_request(user_addr):
    """
    Gestisce una richiesta Platinum:
    1. Oracle assegnato genera challenge CAPTCHA
    2. Pubblica challenge on-chain
    3. Attende risposta utente
    4. Verifica e vota
    """
    ins = contracts["MEVInsurance"]

    req = ins.functions.getPlatinumRequest(user_addr).call()
    desired_max  = req[0]
    stake_dep    = req[1]
    assigned     = req[2]  # array di 3 indirizzi
    challenge_tx = req[3]
    approve_v    = req[4]
    reject_v     = req[5]
    resolved     = req[6]
    challenge_set = req[7]
    answer_sub   = req[8]

    if resolved:
        clog(f"Richiesta Platinum di {user_addr[:16]}... già risolta")
        return

    # Identifica i nostri oracle assegnati
    my_assigned = []
    for addr in assigned:
        if addr in oracle_index_map:
            my_assigned.append((oracle_index_map[addr], addr))

    if not my_assigned:
        clog(f"Nessun nostro oracle assegnato per Platinum di {user_addr[:16]}...")
        return

    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    # ── Fase 1: Pubblica challenge (solo se non già pubblicato) ──
    if not challenge_set:
        publisher_idx, publisher_addr = my_assigned[0]
        challenge, answer = generate_captcha()
        answer_hash = Web3.keccak(text=answer)

        try:
            utils.send_tx(
                w3,
                ins.functions.publishCaptchaChallenge(user_addr, challenge, answer_hash),
                publisher_addr
            )
            olog(publisher_idx, f"📝 CAPTCHA pubblicato: '{challenge}' → risposta: '{answer}'")
            # Salva la risposta per verifica successiva
            captcha_answers[user_addr] = answer_hash
        except Exception as e:
            olog(publisher_idx, f"❌ Errore pubblicazione CAPTCHA: {e}")
            return
    else:
        clog(f"Challenge già pubblicato per {user_addr[:16]}...")

    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])


def verify_platinum_answer(user_addr):
    """
    Dopo che l'utente ha risposto, ogni nostro oracle verifica
    e vota on-chain.
    """
    ins = contracts["MEVInsurance"]

    req = ins.functions.getPlatinumRequest(user_addr).call()
    assigned     = req[2]
    resolved     = req[6]
    answer_sub   = req[8]
    stored_hash  = req[0]  # We need answerHash — get from contract

    if resolved:
        return
    if not answer_sub:
        clog(f"Utente {user_addr[:16]}... non ha ancora risposto al CAPTCHA")
        return

    # Leggi l'hash della risposta dal contratto
    # platinumRequests getter omits address[3] assignedOracles:
    # [0]=desiredMaxSwap [1]=stakeDeposited [2]=answerHash [3]=challengeText
    # [4]=approveVotes [5]=rejectVotes [6]=totalVotes [7]=resolved
    # [8]=challengeSet [9]=answerSubmitted [10]=timestamp
    req_full = ins.functions.platinumRequests(user_addr).call()
    answer_hash_stored = req_full[2]  # answerHash

    # Identifica i nostri oracle
    my_assigned = []
    for addr in assigned:
        if addr in oracle_index_map:
            my_assigned.append((oracle_index_map[addr], addr))

    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    for idx, addr in my_assigned:
        # Controlla se ha già votato
        already_voted = ins.functions.platinumOracleVoted(user_addr, addr).call()
        if already_voted:
            olog(idx, f"⏭️  Già votato per Platinum di {user_addr[:16]}...")
            continue

        # L'oracle verifica: l'hash della risposta dell'utente corrisponde?
        # Leggiamo l'evento CaptchaAnswerSubmitted per ottenere l'hash
        # In produzione si leggerebbero gli eventi, qui usiamo l'hash salvato
        try:
            # Recupera l'hash della risposta utente dall'evento
            try:
                events = ins.events.CaptchaAnswerSubmitted.get_logs(
                    fromBlock=0,
                    toBlock='latest',
                    argument_filters={'user': user_addr}
                )
            except TypeError:
                events = ins.events.CaptchaAnswerSubmitted.get_logs(
                    from_block=0,
                    to_block='latest',
                    argument_filters={'user': user_addr}
                )
            if not events:
                olog(idx, f"❌ Nessun evento CaptchaAnswerSubmitted trovato")
                continue

            user_answer_hash = events[-1]['args']['answerHash']
            is_valid = (user_answer_hash == answer_hash_stored)

            utils.send_tx(
                w3,
                ins.functions.submitCaptchaVerdict(user_addr, is_valid),
                addr
            )
            olog(idx, f"🗳️  Voto Platinum: {'✅ VALIDO' if is_valid else '❌ INVALIDO'} per {user_addr[:16]}...")

        except Exception as e:
                err_str = str(e).lower()
                if "already resolved" in err_str:
                    olog(idx, f"⏭️  Consenso già raggiunto, skip")
                else:
                    olog(idx, f"❌ Errore voto Platinum: {e}")

    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])

    # Controlla risultato finale
    req = ins.functions.getPlatinumRequest(user_addr).call()
    if req[6]:  # resolved
        if req[4] >= 2:  # approveVotes
            clog(f"🏆 {user_addr[:16]}... PROMOSSO A PLATINUM! (voti: {req[4]}/{req[5]})")
        else:
            clog(f"❌ {user_addr[:16]}... PLATINUM NEGATO (voti: {req[4]}/{req[5]})")


def process_claim_captcha(claim_id, user_addr):
    """Genera e pubblica CAPTCHA per un claim in stato CAPTCHARequired."""
    ins = contracts["MEVInsurance"]

    cc = ins.functions.getClaimCaptcha(claim_id).call()
    assigned = cc[0]
    challenge_set = cc[5]
    resolved = cc[4]

    if resolved or challenge_set:
        return

    my_assigned = []
    for addr in assigned:
        if addr in oracle_index_map:
            my_assigned.append((oracle_index_map[addr], addr))

    if not my_assigned:
        return

    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    publisher_idx, publisher_addr = my_assigned[0]
    challenge, answer = generate_captcha()
    answer_hash = Web3.keccak(text=answer)

    try:
        utils.send_tx(
            w3,
            ins.functions.publishClaimCaptcha(claim_id, challenge, answer_hash),
            publisher_addr
        )
        olog(publisher_idx, f"📝 CAPTCHA claim #{claim_id}: '{challenge}' → '{answer}'")
    except Exception as e:
        olog(publisher_idx, f"❌ Errore CAPTCHA claim: {e}")

    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])


def verify_claim_captcha(claim_id):
    """Verifica la risposta CAPTCHA di un claim."""
    ins = contracts["MEVInsurance"]

    cc = ins.functions.getClaimCaptcha(claim_id).call()
    assigned = cc[0]
    resolved = cc[4]
    answer_sub = cc[6]

    if resolved or not answer_sub:
        return

    cc_raw = ins.functions.claimCaptchas(claim_id).call()
    answer_hash_stored = cc_raw[0]  # answerHash (index 0, address[3] skippato dal getter)

    my_assigned = []
    for addr in assigned:
        if addr in oracle_index_map:
            my_assigned.append((oracle_index_map[addr], addr))

    w3.provider.make_request("evm_setAutomine", [True])
    w3.provider.make_request("evm_setIntervalMining", [0])

    for idx, addr in my_assigned:
        # Ri-controlla resolved: un voto precedente può aver raggiunto consenso
        if ins.functions.getClaimCaptcha(claim_id).call()[4]:
            break
        already_voted = ins.functions.claimCaptchaVoted(claim_id, addr).call()
        if already_voted:
            continue

        try:
            try:
                events = ins.events.ClaimCaptchaAnswerSubmitted.get_logs(
                    fromBlock=0, toBlock='latest',
                    argument_filters={'claimId': claim_id}
                )
            except TypeError:
                events = ins.events.ClaimCaptchaAnswerSubmitted.get_logs(
                    from_block=0, to_block='latest',
                    argument_filters={'claimId': claim_id}
                )

            if not events:
                continue

            user_answer_hash = events[-1]['args']['answerHash']
            is_valid = (user_answer_hash == answer_hash_stored)

            utils.send_tx(
                w3,
                ins.functions.submitClaimCaptchaVerdict(claim_id, is_valid),
                addr
            )
            olog(idx, f"🗳️  CAPTCHA claim #{claim_id}: {'✅' if is_valid else '❌'}")

        except Exception as e:
            err_str = str(e).lower()
            if "already resolved" in err_str or "already voted" in err_str:
                olog(idx, f"⏭️  CAPTCHA claim #{claim_id}: consenso raggiunto")
            elif "erc20insufficientbalance" in err_str or "erc20insufficient" in err_str:
                olog(idx, f"⚠️  CAPTCHA claim #{claim_id}: pool MEVI insufficiente per il payout — skip")
            else:
                olog(idx, f"❌ Errore CAPTCHA claim #{claim_id}: {e}")

    w3.provider.make_request("evm_setAutomine", [False])
    w3.provider.make_request("evm_setIntervalMining", [utils.BLOCK_INTERVAL_MS])

def claim_info(claim_id):
    """Mostra i dettagli completi di un claim."""
    ins = contracts["MEVInsurance"]

    info = ins.functions.getClaimInfo(claim_id).call()
    details = ins.functions.getClaimDetails(claim_id).call()
    oracles = ins.functions.getClaimOracles(claim_id).call()
    scores = ins.functions.getClaimScores(claim_id).call()

    status_labels = {
        0: "Pending", 1: "OracleReview", 2: "CAPTCHARequired",
        3: "Approved", 4: "Rejected", 5: "InvalidPattern"
    }

    print(f"\n{'─' * 50}")
    print(f"  CLAIM #{claim_id}")
    print(f"  Utente:      {info[0]}")
    print(f"  Status:      {status_labels.get(info[1], info[1])}")
    print(f"  FraudScore:  {info[2]}")
    print(f"  Swap Value:  {utils.from_wei(info[3]):.4f}")
    print(f"  Loss:        {utils.from_wei(info[4]):.4f}")
    print(f"  Dispersione: {info[5]}")
    print(f"  Reveals:     {info[6]}/{len(oracles)}")
    print(f"  Commits:     {info[7]}")
    print(f"  Scores:      {list(scores)}")
    print(f"  Bot:         {details[6]}")
    print(f"  Oracle assegnati:")
    for j, o in enumerate(oracles):
        marker = " ← nostro" if o in oracle_index_map else ""
        print(f"    {j}: {o}{marker}")
    print(f"{'─' * 50}")


# ══════════════════════════════════════════════════════════════
#  MENU
# ══════════════════════════════════════════════════════════════

def menu():
    print("\n" + "=" * 55)
    print("      ⚡ ORACLE NETWORK — COMANDI INTERATTIVI ⚡")
    print("=" * 55)
    print("  oracle_status()       - Stato di tutti gli oracle")
    print("  claim_info(id)        - Dettagli di un claim")
    print("  force_finalize(id)    - Forza finalizzazione claim")
    print("  main_loop()           - Riavvia il loop automatico")
    print("=" * 55)

# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def _check_and_topup_eth_pool():
    """
    Tesoreria unificata: se il pool ETH per i reward oracle scende sotto 2 ETH,
    converte fondi dal pool MEVI in ETH tramite rebalanceToEth().
    """
    ins = contracts["MEVInsurance"]
    amm = contracts["MockAMM"]
    mevi_token = contracts["MEVToken"]
    try:
        pool_eth = ins.functions.getPoolEthBalance().call()
        soglia = Web3.to_wei(2, 'ether')
        if pool_eth >= soglia:
            return

        # ETH da aggiungere
        eth_amount = Web3.to_wei(5, 'ether')

        # Tasso MEVI/ETH dall'AMM (reserveA = MEVI, reserveB = USDC; 1 USDC = 1 ETH in sim)
        reserve_mevi = amm.functions.reserveA().call()   # tokenA = MEVToken
        reserve_usdc = amm.functions.reserveB().call()   # tokenB = MockUSDC
        if reserve_usdc == 0:
            utils.log("AMM senza liquidità, impossibile calcolare tasso MEVI/ETH", "ERROR", to_file=False)
            return

        # mevi_per_eth = (eth_amount * reserveA) / reserveB  (stessa scala wei)
        mev_amount = (eth_amount * reserve_mevi) // reserve_usdc

        # Verifica che il pool MEVI abbia abbastanza fondi
        pool_mevi = mevi_token.functions.balanceOf(ins.address).call()
        if mev_amount > pool_mevi:
            mev_amount = pool_mevi  # converti solo quello che c'è
            if mev_amount == 0:
                utils.log("Pool MEVI esaurito, impossibile ricaricare pool ETH", "ERROR", to_file=False)
                return
            # Ricalcola ETH proporzionale
            eth_amount = (mev_amount * reserve_usdc) // reserve_mevi

        # Chiama rebalanceToEth: il deployer invia ETH, riceve MEVI dal pool
        utils.send_tx(
            w3,
            ins.functions.rebalanceToEth(mev_amount),
            deployer,
            value=eth_amount
        )
        utils.log(
            f"[TREASURY] Pool ETH basso ({Web3.from_wei(pool_eth, 'ether'):.4f} ETH) "
            f"→ convertiti {Web3.from_wei(mev_amount, 'ether'):.2f} MEVI "
            f"in {Web3.from_wei(eth_amount, 'ether'):.4f} ETH",
            "POOL", to_file=False
        )
    except Exception as e:
        utils.log(f"Errore controllo pool ETH: {e}", "ERROR", to_file=False)


def main_loop():
    """Loop principale: ascolta eventi ClaimSubmitted, PlatinumRequested e CaptchaAnswerSubmitted."""
    ins = contracts["MEVInsurance"]
    slashing = contracts.get("SlashingSystem")
    if slashing is None:
        utils.log("⚠️  SlashingSystem non caricato, i report non verranno processati", "WARNING", to_file=False)

    oracle_status()

    utils.log("🔍 Oracle in ascolto di eventi ClaimSubmitted + PlatinumRequested...", "SYSTEM", to_file=False)
    utils.log("   (Ctrl+C per tornare alla console interattiva)", "SYSTEM", to_file=False)

    last_block = w3.eth.block_number
    _topup_tick = 0

    while True:
        try:
            current_block = w3.eth.block_number

            if current_block > last_block:

                # ── 1. Cerca nuovi eventi PlatinumRequested (prima dei claim per minimizzare delay captcha) ──
                try:
                    plat_events = ins.events.PlatinumRequested.get_logs(
                        fromBlock=last_block + 1,
                        toBlock=current_block
                    )
                except TypeError:
                    plat_events = ins.events.PlatinumRequested.get_logs(
                        from_block=last_block + 1,
                        to_block=current_block
                    )

                for event in plat_events:
                    user = event['args']['user']
                    clog(f"🔔 Nuova richiesta Platinum da {user[:16]}...")
                    process_platinum_request(user)

                # ── 2. Cerca nuovi eventi ClaimSubmitted ──
                try:
                    events = ins.events.ClaimSubmitted.get_logs(
                        fromBlock=last_block + 1,
                        toBlock=current_block
                    )
                except TypeError:
                    events = ins.events.ClaimSubmitted.get_logs(
                        from_block=last_block + 1,
                        to_block=current_block
                    )

                for event in events:
                    cid = event['args']['claimId']
                    if cid not in processed_claims:
                        processed_claims.add(cid)
                        process_claim(cid)

                # ── 3. Cerca risposte CAPTCHA dagli utenti ──
                try:
                    answer_events = ins.events.CaptchaAnswerSubmitted.get_logs(
                        fromBlock=last_block + 1,
                        toBlock=current_block
                    )
                except TypeError:
                    answer_events = ins.events.CaptchaAnswerSubmitted.get_logs(
                        from_block=last_block + 1,
                        to_block=current_block
                    )

                for event in answer_events:
                    user = event['args']['user']
                    clog(f"📩 Risposta CAPTCHA ricevuta da {user[:16]}...")
                    verify_platinum_answer(user)
                    
                # ── 4. Cerca eventi ClaimCaptchaRequired ──
                try:
                    cc_events = ins.events.ClaimCaptchaRequired.get_logs(
                        fromBlock=last_block + 1, toBlock=current_block
                    )
                except TypeError:
                    cc_events = ins.events.ClaimCaptchaRequired.get_logs(
                        from_block=last_block + 1, to_block=current_block
                    )

                for event in cc_events:
                    cid = event['args']['claimId']
                    user = event['args']['user']
                    clog(f"🔔 CAPTCHA richiesto per claim #{cid} (utente {user[:16]}...)")
                    process_claim_captcha(cid, user)

                # ── 5. Cerca risposte CAPTCHA claim ──
                try:
                    cca_events = ins.events.ClaimCaptchaAnswerSubmitted.get_logs(
                        fromBlock=last_block + 1, toBlock=current_block
                    )
                except TypeError:
                    cca_events = ins.events.ClaimCaptchaAnswerSubmitted.get_logs(
                        from_block=last_block + 1, to_block=current_block
                    )

                for event in cca_events:
                    cid = event['args']['claimId']
                    clog(f"📩 Risposta CAPTCHA claim #{cid} ricevuta")
                    verify_claim_captcha(cid)

                # ── 6. Cerca nuovi eventi ReportSubmitted (slashing) ──
                if slashing is not None:
                    try:
                        report_events = slashing.events.ReportSubmitted.get_logs(
                            fromBlock=last_block + 1, toBlock=current_block
                        )
                    except TypeError:
                        report_events = slashing.events.ReportSubmitted.get_logs(
                            from_block=last_block + 1, to_block=current_block
                        )
                    except Exception:
                        report_events = []

                    for event in report_events:
                        rid = event['args']['reportId']
                        if rid not in processed_reports:
                            processed_reports.add(rid)
                            clog(f"🔔 Nuovo report slashing #{rid}")
                            process_report(rid)

                last_block = current_block

            # Controllo pool ETH ogni 30 secondi
            _topup_tick += 1
            if _topup_tick % 30 == 0:
                _check_and_topup_eth_pool()

            time.sleep(1)

        except KeyboardInterrupt:
            utils.log("⏸️  Loop interrotto. Usa main_loop() per riprendere.", "SYSTEM", to_file=False)
            menu()
            break
        except Exception as e:
            err_str = str(e).lower()
            if "not found" not in err_str and "filter" not in err_str:
                utils.log(f"Errore nel loop: {e}", "ERROR", to_file=False)
            time.sleep(2)


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse as _ap_oracle
    _p = _ap_oracle.ArgumentParser(description="Oracle Network Daemon")
    _p.add_argument('--count', type=int, default=None,
                    help="Numero oracle da deployare")
    _p.add_argument('--start-idx', type=int, default=None,
                    help="Indice account Hardhat di partenza")
    _p.add_argument('--stake', type=float, default=None,
                    help="Stake base in ETH per oracle")
    _cli_args = _p.parse_args()
else:
    _cli_args = None

# Connessione e caricamento contratti (eseguito all'import)
utils.wait_for_deploy()
w3 = utils.get_web3()
contracts = utils.get_all_contracts(w3)
deployer = w3.eth.accounts[0]

# Prova a caricare oracle da actors.json oppure da args CLI (actors.json scritto da orchestratore)
_saved_actors = utils.load_actors()

if _saved_actors.get("oracles"):
    oracle_accounts = _saved_actors["oracles"]
    oracle_index_map = {addr: i for i, addr in enumerate(oracle_accounts)}
    _stake_saved = _saved_actors.get("oracle_base_stake", 0.1)
    utils.log(f"Oracle caricati da actors.json: {len(oracle_accounts)} nodi", "SYSTEM", to_file=False)
    register_and_activate(oracle_accounts, _stake_saved)
elif _cli_args is not None and _cli_args.count is not None:
    # Modalità CLI: usa i parametri passati
    _n = _cli_args.count
    _start = _cli_args.start_idx if _cli_args.start_idx is not None else 10
    _stake_cli = _cli_args.stake if _cli_args.stake is not None else 0.1
    _total = len(w3.eth.accounts)
    if _start + _n > _total:
        _n = _total - _start
        utils.log(f"Ridotto a {_n} oracle (limite account Hardhat)", "SYSTEM", to_file=False)
    accounts = [w3.eth.accounts[_start + i] for i in range(_n)]
    oracle_accounts = accounts
    oracle_index_map = {addr: i for i, addr in enumerate(accounts)}
    register_and_activate(oracle_accounts, _stake_cli)
    utils.log(f"Oracle CLI: {_n} nodi da idx={_start}, stake={_stake_cli} ETH", "SYSTEM", to_file=False)
else:
    # Setup interattivo se actors.json non disponibile e nessun arg CLI
    accounts, stake = setup()
    oracle_accounts = accounts
    oracle_index_map = {addr: i for i, addr in enumerate(accounts)}
    register_and_activate(oracle_accounts, stake)

# Avvio loop automatico
main_loop()