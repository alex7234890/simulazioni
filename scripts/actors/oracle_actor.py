"""
Oracle Actor - Simulates an oracle node in the MEV Insurance protocol.

Each oracle independently analyzes claims, computes FraudScore
following the PDF specification (section 1.3.11), and participates
in the commit-reveal voting process.
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import (
    send_tx, keccak256_commit, generate_salt, to_wei, from_wei, log,
)

# ── Tier score mapping (PDF Table 5) ──
TIER_SCORES = {
    0: 50,   # Bronze
    1: 30,   # Silver
    2: 15,   # Gold
    3: 0,    # Platinum
}

# Oracle personal variance range (small, to stay under dispersione threshold 20)
ORACLE_VARIANCE_RANGE = 3


class OracleActor:
    """
    Simulates a single oracle node.

    Responsibilities:
      - Register and stake on OracleRegistry
      - Analyze claims (FraudScore = ScoreTier + ScoreClaimRate + ScoreNetwork + variance)
      - Commit-reveal verdicts on MEVInsurance
    """

    def __init__(self, w3, contracts, address, oracle_id, network="localhost", private_key=None):
        self.w3 = w3
        self.contracts = contracts
        self.address = address
        self.oracle_id = oracle_id        # e.g. "Oracle-1"
        self.network = network
        self.private_key = private_key
        self.insurance = contracts["MEVInsurance"]
        self.registry = contracts["OracleRegistry"]

        # Per-oracle persistent random bias (stays consistent across claims)
        self.personal_bias = random.randint(-ORACLE_VARIANCE_RANGE, ORACLE_VARIANCE_RANGE)

        # Pending reveals: {claim_id: {fraud_score, pattern_valid, salt}}
        self.pending_reveals = {}

        # Stats
        self.stats = {
            "claims_evaluated": 0,
            "commits": 0,
            "reveals": 0,
            "patterns_valid": 0,
            "patterns_invalid": 0,
        }

    def _send(self, fn, value=0):
        """Send transaction from this oracle."""
        return send_tx(
            self.w3, fn, self.address,
            value=value, network=self.network, private_key=self.private_key,
        )

    # ── Setup ──

    def setup(self, deployer, deployer_key=None):
        """Register oracle with stake and activate."""
        try:
            is_registered = self.registry.functions.isOracle(self.address).call()
            if is_registered:
                log(f"{self.oracle_id} already registered", "ORACLE")
                return True
        except Exception:
            pass

        try:
            min_stake = self.registry.functions.getMinimumStake().call()
            self._send(self.registry.functions.registerOracle(), value=min_stake)
            self._send(self.registry.functions.activateOracle())
            log(f"{self.oracle_id} registered & activated (stake={from_wei(min_stake):.4f} ETH)", "ORACLE")
            return True
        except Exception as e:
            log(f"{self.oracle_id} setup failed: {e}", "ERROR")
            return False

    # ── Fraud Score Analysis (PDF section 1.3.11) ──

    def _compute_claim_rate_score(self, total_claims, total_swaps):
        """
        Score Claim Rate (0-30) — PDF Table 5.
        Based on claim_rate = total_claims / total_swaps.
        """
        if total_swaps == 0:
            return 0

        rate = total_claims / total_swaps

        if rate > 0.30:
            return 30
        elif rate > 0.20:
            return 25
        elif rate > 0.10:
            return 20
        elif rate >= 0.06:
            return 15
        else:
            return 0  # < 6% → score 0

    def _compute_network_score(self, is_real_attack):
        """
        Score Network (0-50) — PDF section 1.3.11.
        Simulates BFS distance from known MEV bots.

        For legitimate claims (is_real_attack=True):
          - 90% chance score=0 (pattern verified, no suspicious network link)
          - 8% chance score=5 (distant coincidence)
          - 2% chance score=15 (false positive)

        For non-attack claims:
          - 70% chance score=0 (no connection)
          - 20% chance score=5 (d>=4, coincidence)
          - 8% chance score=15 (d=3)
          - 2% chance score=30 (d=2, suspicious)
        """
        r = random.random()
        if is_real_attack:
            if r < 0.90:
                return 0
            elif r < 0.98:
                return 5
            else:
                return 15
        else:
            if r < 0.70:
                return 0
            elif r < 0.90:
                return 5
            elif r < 0.98:
                return 15
            else:
                return 30

    def analyze_claim(self, claim_id, is_real_attack=True):
        """
        Analyze a claim and compute FraudScore + patternValid.

        FraudScore = ScoreTier + ScoreClaimRate + ScoreNetwork + personal_variance
        Range: 0-130

        Returns: (fraud_score, pattern_valid, score_breakdown)
        """
        # Read user profile from contract
        tier_score = 50  # default Bronze
        claim_rate_score = 0
        try:
            claim_info = self.insurance.functions.getClaimInfo(claim_id).call()
            claimant = claim_info[0]
            profile = self.insurance.functions.getUserProfile(claimant).call()
            tier = profile[0]
            total_swaps = profile[2]
            total_claims = profile[3]

            tier_score = TIER_SCORES.get(tier, 50)
            claim_rate_score = self._compute_claim_rate_score(total_claims, total_swaps)
        except Exception:
            pass

        # Network score (simulated BFS)
        network_score = self._compute_network_score(is_real_attack)

        # Base score before variance
        base_score = tier_score + claim_rate_score + network_score

        # Per-oracle variance: personal bias + small random jitter
        jitter = random.randint(-2, 2)
        variance = self.personal_bias + jitter

        fraud_score = max(0, min(130, base_score + variance))

        # Pattern validity: 90% valid for real attacks, 70% for non-attacks
        if is_real_attack:
            pattern_valid = random.random() < 0.95
        else:
            pattern_valid = random.random() < 0.70

        breakdown = {
            "tier": tier_score,
            "rate": claim_rate_score,
            "net": network_score,
            "var": variance,
        }

        return fraud_score, pattern_valid, breakdown

    # ── Commit-Reveal ──

    def commit_verdict(self, claim_id, fraud_score, pattern_valid):
        """Commit a verdict hash for a claim. Saves data for later reveal."""
        salt = generate_salt()
        commit_hash = keccak256_commit(fraud_score, pattern_valid, salt)

        try:
            self._send(self.insurance.functions.commitVerdict(claim_id, commit_hash))
            self.pending_reveals[claim_id] = {
                "fraud_score": fraud_score,
                "pattern_valid": pattern_valid,
                "salt": salt,
            }
            self.stats["commits"] += 1
            return True
        except Exception as e:
            log(f"{self.oracle_id} commit failed (claim #{claim_id}): {e}", "ERROR")
            return False

    def reveal_verdict(self, claim_id):
        """Reveal a previously committed verdict."""
        data = self.pending_reveals.get(claim_id)
        if not data:
            log(f"{self.oracle_id} no pending reveal for claim #{claim_id}", "ERROR")
            return False

        try:
            self._send(self.insurance.functions.revealVerdict(
                claim_id, data["fraud_score"], data["pattern_valid"], data["salt"],
            ))
            self.stats["reveals"] += 1
            self.stats["claims_evaluated"] += 1
            if data["pattern_valid"]:
                self.stats["patterns_valid"] += 1
            else:
                self.stats["patterns_invalid"] += 1
            del self.pending_reveals[claim_id]
            return True
        except Exception as e:
            log(f"{self.oracle_id} reveal failed (claim #{claim_id}): {e}", "ERROR")
            return False

    def process_claim(self, claim_id, is_real_attack=True):
        """
        Full pipeline: analyze -> commit -> reveal.
        Returns (fraud_score, pattern_valid, breakdown) or None on failure.
        """
        # Check if we're assigned to this claim
        try:
            assigned = self.insurance.functions.getClaimOracles(claim_id).call()
            if self.address not in assigned:
                return None
        except Exception:
            return None

        fraud_score, pattern_valid, breakdown = self.analyze_claim(claim_id, is_real_attack)

        log(
            f"{self.oracle_id} | Claim #{claim_id}: FraudScore={fraud_score} "
            f"(tier={breakdown['tier']} + rate={breakdown['rate']} "
            f"+ net={breakdown['net']} + var={breakdown['var']:+d}), "
            f"pattern={'Valid' if pattern_valid else 'Invalid'}",
            "ORACLE",
        )

        if not self.commit_verdict(claim_id, fraud_score, pattern_valid):
            return None
        if not self.reveal_verdict(claim_id):
            return None

        return fraud_score, pattern_valid, breakdown

    # ── Info ──

    def get_info(self):
        """Get oracle status from registry."""
        try:
            info = self.registry.functions.getOracleInfo(self.address).call()
            return {
                "stake": info[0],
                "status": info[1],
                "deviation_score": info[2] if len(info) > 2 else 0,
            }
        except Exception:
            return {"stake": 0, "status": -1, "deviation_score": 0}
