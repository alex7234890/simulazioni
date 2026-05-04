// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library DataTypes {

    enum Tier {
        Bronze,
        Silver,
        Gold,
        Platinum
    }

    enum CoverageLevel {
        Low,
        Medium,
        High
    }

    enum ClaimStatus {
        Pending,
        OracleReview,
        CAPTCHARequired,
        Approved,
        Rejected,
        InvalidPattern
    }

    enum OracleStatus {
        Inactive,
        Pending,
        Active,
        Watchlisted,
        Contested,
        Slashed,
        Expelled
    }

    struct UserProfile {
        Tier tier;
        bool policyActive;
        uint256 policyStart;
        uint256 policyEnd;
        CoverageLevel coverageLevel;
        uint256 totalSwaps;
        uint256 totalClaims;
        uint256 approvedClaims;
        uint256 rejectedClaims;
        uint256 avgFraudScore;
        bool isBlacklisted;
        uint256 debt;
        uint256 lastSwapDay;
        uint256 dailySwapCount;
    }

    struct OracleInfo {
        uint256 stake;
        OracleStatus status;
        uint256 registrationTime;
        uint256 activationTime;
        uint256 deviationScore;
        uint256 watchlistStrikes;
        uint256 watchlistPosition;
        uint256 lastResetTime;
        uint256 claimsEvaluated;
        uint256 withdrawRequestTime;
    }

    // mappings cannot live inside structs stored in arrays;
    // the `commits` mapping (address => bytes32) is stored separately in the main contract
    struct Claim {
        address user;
        bytes32 txHash1;
        bytes32 txHash2;
        bytes32 txHash3;
        ClaimStatus status;
        uint256 finalFraudScore;
        CoverageLevel coverageLevel;
        uint256 swapValue;
        uint256 loss;
        address[] assignedOracles;
        uint8[] revealedScores;
        uint256 patternValidVotes;
        uint256 patternInvalidVotes;
        uint256 dispersione;
        uint256 timestamp;
        uint256 revealCount;
        uint256 commitCount;
        bool secondaryReview;
        address botAddress;
        uint256 submitGasUsed;
    }

    struct Policy {
        address holder;
        CoverageLevel coverageLevel;
        uint256 premium;
        uint256 startTime;
        uint256 endTime;
        uint256 maxSwapValue;
        bool active;
    }

    struct InsuredSwap {
        address user;
        uint256 swapValue;
        uint256 premiumPaid;
        uint256 timestamp;
        DataTypes.CoverageLevel coverageLevel;
        bool claimed;
    }
}
