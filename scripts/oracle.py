import random

from utils import (
    send_tx,
    keccak256_commit,
    generate_salt,
    log
)


class OracleActor:

    def __init__(
        self,
        w3,
        contracts,
        address,
        oracle_id,
        network="localhost"
    ):

        self.w3 = w3
        self.contracts = contracts

        self.address = address
        self.oracle_id = oracle_id

        self.network = network

        self.insurance = contracts["MEVInsurance"]
        self.registry = contracts["OracleRegistry"]

        self.pending_reveals = {}

        # Oracle personal bias
        self.personal_variance = random.randint(-3, 3)

    # ─────────────────────────────────────

    def setup(self, deployer):

        is_reg = self.registry.functions.isOracle(
            self.address
        ).call()

        if not is_reg:

            min_stake = (
                self.registry.functions
                .getMinimumStake()
                .call()
            )

            send_tx(
                self.w3,
                self.registry.functions.registerOracle(),
                self.address,
                value=min_stake,
                network=self.network
            )

            send_tx(
                self.w3,
                self.registry.functions.activateOracle(),
                self.address,
                network=self.network
            )

            log(
                f"{self.oracle_id} registered & activated",
                "SETUP"
            )

    # ─────────────────────────────────────

    def analyze_claim(self, claim_id):

        try:

            claim_info = (
                self.insurance.functions
                .getClaimInfo(claim_id)
                .call()
            )

            claimant = claim_info[0]

        except Exception:

            claimant = None

        # Tier score

        tier_score = 50

        if claimant:

            try:

                profile = (
                    self.insurance.functions
                    .getUserProfile(claimant)
                    .call()
                )

                tier = profile[0]

                tier_map = {
                    0: 50,
                    1: 30,
                    2: 15,
                    3: 0
                }

                tier_score = tier_map.get(
                    tier,
                    50
                )

                swaps = profile[2]
                claims = profile[3]

            except Exception:

                swaps = 0
                claims = 0

        # Claim rate score

        claim_rate = (
            claims / max(swaps, 1)
        )

        if claim_rate > 0.30:
            rate_score = 30
        elif claim_rate > 0.20:
            rate_score = 25
        elif claim_rate > 0.10:
            rate_score = 20
        elif claim_rate <= 0.10:
            rate_score = 15
        else:
            rate_score = 0

        # Network score

        network_score = random.randint(
            0,
            15
        )

        base_score = (
            tier_score
            + rate_score
            + network_score
        )

        variance = self.personal_variance

        fraud_score = max(
            0,
            min(
                130,
                base_score + variance
            )
        )

        pattern_valid = (
            random.random() < 0.9
        )

        log(
            f"{self.oracle_id} | "
            f"Claim #{claim_id}: "
            f"FraudScore={fraud_score}",
            "ORACLE"
        )

        return fraud_score, pattern_valid

    # ─────────────────────────────────────

    def commit_verdict(
        self,
        claim_id,
        fraud_score,
        pattern_valid
    ):

        salt = generate_salt()

        commit_hash = keccak256_commit(
            fraud_score,
            pattern_valid,
            salt
        )

        send_tx(
            self.w3,
            self.insurance.functions.commitVerdict(
                claim_id,
                commit_hash
            ),
            self.address,
            network=self.network
        )

        self.pending_reveals[claim_id] = {
            "score": fraud_score,
            "pattern": pattern_valid,
            "salt": salt
        }

    # ─────────────────────────────────────

    def reveal_verdict(self, claim_id):

        if claim_id not in self.pending_reveals:
            return

        data = self.pending_reveals[claim_id]

        send_tx(
            self.w3,
            self.insurance.functions.revealVerdict(
                claim_id,
                data["score"],
                data["pattern"],
                data["salt"]
            ),
            self.address,
            network=self.network
        )

        del self.pending_reveals[claim_id]

    # ─────────────────────────────────────

    def process_claim(self, claim_id):

        fraud_score, pattern_valid = (
            self.analyze_claim(
                claim_id
            )
        )

        self.commit_verdict(
            claim_id,
            fraud_score,
            pattern_valid
        )

        self.reveal_verdict(
            claim_id
        )
