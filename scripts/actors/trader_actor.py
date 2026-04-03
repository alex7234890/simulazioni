"""
Trader Actor - Simulates a user in the MEV Insurance protocol.

Traders register, buy policies, execute insured swaps, detect MEV
attacks, and submit claims. They do NOT compute fraud scores —
that's the oracle's job.
"""
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import send_tx, to_wei, from_wei, log

# ── Coverage level enum ──
COVERAGE_HIGH = 2


class TraderActor:
    """
    Simulates a single trader/user in the protocol.

    Lifecycle per day:
      1. ensure_policy_active() — renew if expired
      2. execute_insured_swap(value) — up to Smax per day
      3. submit_claim(swap_id, loss, bot_addr) — if attacked
    """

    def __init__(self, w3, contracts, address, deployer, trader_id,
                 network="localhost", private_key=None, deployer_key=None):
        self.w3 = w3
        self.contracts = contracts
        self.address = address
        self.deployer = deployer
        self.trader_id = trader_id          # e.g. "Trader-1"
        self.network = network
        self.private_key = private_key
        self.deployer_key = deployer_key
        self.insurance = contracts["MEVInsurance"]
        self.token = contracts["MEVToken"]

        # Stats
        self.stats = {
            "swaps": 0,
            "claims_submitted": 0,
            "claims_approved": 0,
            "claims_rejected": 0,
            "claims_captcha": 0,
            "total_premium": 0,
            "total_payout": 0,
            "total_loss_claimed": 0,
        }

    def _send(self, fn, value=0):
        """Send transaction from this trader."""
        return send_tx(
            self.w3, fn, self.address,
            value=value, network=self.network, private_key=self.private_key,
        )

    def _send_deployer(self, fn, value=0):
        """Send transaction from the deployer (for funding)."""
        return send_tx(
            self.w3, fn, self.deployer,
            value=value, network=self.network, private_key=self.deployer_key,
        )

    # ── Setup ──

    def setup(self):
        """Fund trader, register, buy policy, approve tokens."""
        # Fund with MEVI if needed
        bal = self.token.functions.balanceOf(self.address).call()
        if bal < to_wei(10000):
            self._send_deployer(self.token.functions.transfer(self.address, to_wei(50000)))
            log(f"{self.trader_id} funded with 50,000 MEVI", "SETUP")

        # Register
        is_registered = self.insurance.functions.registeredUsers(self.address).call()
        if not is_registered:
            self._send(self.insurance.functions.registerUser())
            log(f"{self.trader_id} registered", "SETUP")

        # Approve token spending (large allowance)
        self._send(self.token.functions.approve(
            self.insurance.address, to_wei(1_000_000)
        ))

        # Buy policy if needed
        self.ensure_policy_active()
        log(f"{self.trader_id} setup complete (balance={from_wei(self.token.functions.balanceOf(self.address).call()):.0f} MEVI)", "SETUP")

    # ── Policy Management ──

    def ensure_policy_active(self):
        """Check if the policy is active; buy/renew if not."""
        try:
            policy = self.insurance.functions.policies(self.address).call()
            is_active = policy[6]  # active field
            if is_active:
                return True
        except Exception:
            pass

        try:
            self._send(self.insurance.functions.buyPolicy(COVERAGE_HIGH))
            log(f"{self.trader_id} bought High coverage policy", "TRADE")
            return True
        except Exception as e:
            log(f"{self.trader_id} policy purchase failed: {e}", "ERROR")
            return False

    # ── Swap Execution ──

    def execute_insured_swap(self, swap_value):
        """
        Call insuredSwap(). Returns swap_id or None on failure.
        The premium is calculated and paid automatically by the contract.
        """
        swap_value_wei = to_wei(swap_value)

        try:
            self._send(self.insurance.functions.insuredSwap(swap_value_wei))
            swap_id = self.insurance.functions.getInsuredSwapsCount().call() - 1
            swap_info = self.insurance.functions.insuredSwaps(swap_id).call()
            premium_paid = swap_info[2]  # premiumPaid field

            self.stats["swaps"] += 1
            self.stats["total_premium"] += premium_paid

            log(
                f"{self.trader_id} | Swap #{swap_id}: {swap_value} MEVI, "
                f"premium={from_wei(premium_paid):.4f} MEVI",
                "TRADE",
            )
            return swap_id
        except Exception as e:
            err = str(e)
            if "Daily swap limit" in err:
                log(f"{self.trader_id} | Daily swap limit reached — skipping", "TRADE")
            elif "Policy expired" in err:
                log(f"{self.trader_id} | Policy expired — renewing", "TRADE")
                if self.ensure_policy_active():
                    return self.execute_insured_swap(swap_value)
            elif "Swap value exceeds" in err:
                log(f"{self.trader_id} | Swap {swap_value} MEVI exceeds policy max — skipping", "TRADE")
            else:
                log(f"{self.trader_id} | insuredSwap failed: {e}", "ERROR")
            return None

    # ── Claim Submission ──

    def submit_claim(self, swap_id, loss, bot_address):
        """
        Submit a claim for a previously insured swap.
        Returns claim_id or None on failure.
        """
        loss_wei = to_wei(loss)
        tx1 = self.w3.keccak(text=f"frontrun_{swap_id}_{random.randint(0, 999999)}")
        tx2 = self.w3.keccak(text=f"victim_{swap_id}_{random.randint(0, 999999)}")
        tx3 = self.w3.keccak(text=f"backrun_{swap_id}_{random.randint(0, 999999)}")

        try:
            self._send(self.insurance.functions.submitClaim(
                swap_id, tx1, tx2, tx3, loss_wei, bot_address,
            ))
            claim_id = self.insurance.functions.getClaimsCount().call() - 1
            self.stats["claims_submitted"] += 1
            self.stats["total_loss_claimed"] += loss_wei

            log(
                f"{self.trader_id} | Claim #{claim_id} submitted: "
                f"loss={loss:.2f} MEVI, bot={'Yes' if bot_address != '0x' + '0'*40 else 'No'}",
                "CLAIM",
            )
            return claim_id
        except Exception as e:
            log(f"{self.trader_id} | submitClaim failed: {e}", "ERROR")
            return None

    # ── Profile ──

    def get_profile(self):
        """Read user profile from contract. Returns dict."""
        try:
            profile = self.insurance.functions.getUserProfile(self.address).call()
            balance = self.token.functions.balanceOf(self.address).call()
            tier_names = {0: "Bronze", 1: "Silver", 2: "Gold", 3: "Platinum"}
            return {
                "tier": tier_names.get(profile[0], "Unknown"),
                "tier_id": profile[0],
                "total_swaps": profile[2],
                "total_claims": profile[3],
                "approved_claims": profile[4],
                "rejected_claims": profile[5],
                "avg_fraud_score": profile[6],
                "is_blacklisted": profile[7],
                "debt": profile[8],
                "balance": balance,
            }
        except Exception:
            return {
                "tier": "Unknown", "tier_id": 0,
                "total_swaps": 0, "total_claims": 0,
                "approved_claims": 0, "rejected_claims": 0,
                "avg_fraud_score": 0, "is_blacklisted": False,
                "debt": 0, "balance": 0,
            }

    def record_payout(self, amount):
        """Record a payout received (called by orchestrator after CAPTCHA/finalize)."""
        self.stats["total_payout"] += amount
        self.stats["claims_approved"] += 1

    def record_rejection(self):
        """Record a claim rejection."""
        self.stats["claims_rejected"] += 1

    def record_captcha(self):
        """Record a CAPTCHA-required outcome."""
        self.stats["claims_captcha"] += 1
