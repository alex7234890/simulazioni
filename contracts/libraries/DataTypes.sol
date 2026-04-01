// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title DataTypes
 * @dev All shared data structures and enumerations for the MEV Insurance protocol.
 *
 * Enums:
 *   - Tier: User reputation tiers (Bronze -> Platinum)
 *   - CoverageLevel: Insurance coverage levels (Low, Medium, High)
 *   - ClaimStatus: Lifecycle states of a claim
 *   - OracleStatus: Lifecycle states of an oracle
 *
 * Structs:
 *   - UserProfile: Full user state including tier, policy, swap history, fraud metrics
 *   - OracleInfo: Oracle stake, status, deviation tracking
 *   - Claim: Complete claim data with commit-reveal oracle verdicts
 *   - Policy: Insurance policy parameters
 *
 * Numeric convention: percentages are expressed in basis points (10000 = 100%).
 */
library DataTypes {

    // -------------------------------------------------------
    //  Enums
    // -------------------------------------------------------

    /// @dev User reputation tier — determines limits and claim thresholds
    enum Tier {
        Bronze,     // Entry level
        Silver,     // 18 swaps, 30 days, avg fraud < 52
        Gold,       // 55 swaps, 60 days, avg fraud < 35
        Platinum    // Manual upgrade via CAPTCHA + stake
    }

    /// @dev Coverage level chosen when purchasing a policy
    enum CoverageLevel {
        Low,    // Fcov = 70%
        Medium, // Fcov = 90%
        High    // Fcov = 100%
    }

    /// @dev Full lifecycle of a claim through oracle evaluation
    enum ClaimStatus {
        Pending,          // Submitted, awaiting oracle commits
        OracleReview,     // Oracles are committing / revealing
        CAPTCHARequired,  // Intermediate fraud score — needs CAPTCHA verification
        Approved,         // Claim approved, payout issued
        Rejected,         // Claim rejected by oracle consensus
        InvalidPattern    // >= 70% oracles flagged invalid sandwich pattern
    }

    /// @dev Oracle lifecycle from registration to potential expulsion
    enum OracleStatus {
        Inactive,     // Registered but not yet activated
        Pending,      // Waiting for activation delay (Tactivation)
        Active,       // Fully active, can evaluate claims
        Watchlisted,  // Accumulated deviation warnings
        Contested,    // Under slashing review
        Slashed,      // Stake slashed, may reintegrate
        Expelled      // Permanently removed (mediana > thetaExpulsion)
    }

    // -------------------------------------------------------
    //  Structs
    // -------------------------------------------------------

    /// @dev Complete profile for an insured user
    struct UserProfile {
        Tier tier;
        bool policyActive;
        uint256 policyStart;
        uint256 policyEnd;
        CoverageLevel coverageLevel;
        uint256 totalSwaps;         // Lifetime swap count (for tier upgrade)
        uint256 totalClaims;        // Total claims submitted
        uint256 approvedClaims;     // Claims that were approved
        uint256 rejectedClaims;     // Claims that were rejected
        uint256 avgFraudScore;      // Running average fraud score (0-100)
        bool isBlacklisted;         // Blacklisted for fraud
        uint256 debt;               // Outstanding debt (from blacklist penalty)
        uint256 lastSwapDay;        // Day number of last swap (block.timestamp / 1 days)
        uint256 dailySwapCount;     // Swaps executed on lastSwapDay
    }

    /// @dev Oracle node information
    struct OracleInfo {
        uint256 stake;              // ETH staked
        OracleStatus status;
        uint256 registrationTime;   // Timestamp of registerOracle()
        uint256 activationTime;     // Timestamp when oracle became Active
        uint256 deviationScore;     // Cumulative sum of absolute deviations from median
        uint256 watchlistStrikes;   // Count of deviations >= deltaWatchlist
        uint256 watchlistPosition;  // Position in watchlist queue (0 = not watchlisted)
        uint256 lastResetTime;      // Last periodic score reset timestamp
        uint256 claimsEvaluated;    // Total claims evaluated
        uint256 withdrawRequestTime; // Timestamp when withdrawal was requested (0 = none)
    }

    /// @dev Insurance claim with commit-reveal oracle evaluation
    /// @notice The `commits` mapping (address => bytes32) is stored separately
    ///         in the main contract because mappings cannot live inside structs
    ///         that are stored in arrays.
    struct Claim {
        address user;               // Claimant address
        bytes32 txHash1;            // Frontrun transaction hash
        bytes32 txHash2;            // Victim transaction hash
        bytes32 txHash3;            // Backrun transaction hash
        ClaimStatus status;
        uint256 finalFraudScore;    // Median fraud score after reveal
        CoverageLevel coverageLevel;
        uint256 swapValue;          // Value of the swap in MEVI
        uint256 loss;               // Claimed loss amount in MEVI
        address[] assignedOracles;  // Noracle oracles selected for evaluation
        uint8[] revealedScores;     // Fraud scores revealed by oracles
        uint256 patternValidVotes;  // Oracles that voted pattern = valid
        uint256 patternInvalidVotes;// Oracles that voted pattern = invalid
        uint256 dispersione;        // max(scores) - min(scores)
        uint256 timestamp;          // Claim submission time
        uint256 revealCount;        // Number of oracles that have revealed
        uint256 commitCount;        // Number of oracles that have committed
        bool secondaryReview;       // Whether this claim has been through secondary review
        address botAddress;         // MEV bot address (C9)
    }

    /// @dev Insurance policy parameters
    struct Policy {
        address holder;
        CoverageLevel coverageLevel;
        uint256 premium;            // Activation fee paid in MEVI
        uint256 startTime;
        uint256 endTime;
        uint256 maxSwapValue;       // Max insurable swap value for this policy
        bool active;
    }

    /// @dev Insured swap registered before claim submission
    struct InsuredSwap {
        address user;
        uint256 swapValue;          // Value of the swap in MEVI
        uint256 premiumPaid;        // Premium calculated and paid for this swap
        uint256 timestamp;          // When the swap was insured
        bool claimed;               // Whether a claim was submitted for this swap
    }
}
