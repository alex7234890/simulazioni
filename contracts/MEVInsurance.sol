// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";
import "./OracleRegistry.sol";
import "./PremiumCalculator.sol";
import "./TierSystem.sol";

/**
 * @title MEVInsurance
 * @dev Insurance contract protecting traders against MEV sandwich attacks.
 *
 * Phase 3 refactor: full commit-reveal oracle evaluation system.
 * - Users register, buy policies, submit claims with 3 tx hashes
 * - Oracles are assigned via OracleRegistry, commit-reveal fraud scores
 * - Claims finalized by median fraud score with tier-based thresholds
 * - Pattern validity voting (>=70% invalid -> immediate rejection)
 * - Dispersione check (max-min > 20 -> flagged for review)
 *
 * Uses Checks-Effects-Interactions pattern throughout.
 * All percentages in basis points (10000 = 100%).
 */
contract MEVInsurance is Ownable, ReentrancyGuard {
    using DataTypes for *;

    // -------------------------------------------------------
    //  External contracts
    // -------------------------------------------------------

    IERC20 public token;
    OracleRegistry public oracleRegistry;
    PremiumCalculator public premiumCalculator;
    TierSystem public tierSystem;

    // -------------------------------------------------------
    //  Protocol Parameters (configurable, Table 8 defaults)
    // -------------------------------------------------------

    /// @dev Number of oracles assigned per claim
    uint256 public nOracle = 7;

    /// @dev Default policy duration
    uint256 public policyDuration = 30 days;

    /// @dev Symbolic policy activation fee (1 MEVI)
    uint256 public activationFee = 1 * 1e18;

    /// @dev Default premium fallback if no PremiumCalculator set
    uint256 public defaultPremium = 100 * 1e18;

    /// @dev Default coverage amount
    uint256 public defaultCoverage = 1000 * 1e18;

    /// @dev Timeout for oracle reveals (after which finalize can proceed)
    uint256 public oracleTimeout = 3 days;

    /// @dev Pattern invalidity threshold in basis points (7000 = 70%)
    uint256 public patternInvalidThresholdBps = 7000;

    /// @dev Dispersione threshold (max - min fraud score)
    uint256 public dispersioneThreshold = 20;

    /// @dev Fraud score threshold for rejection (basis points of 100-scale)
    uint256 public thetaReject = 80;

    /// @dev Fraud score threshold for approval (Gold/Platinum only)
    uint256 public thetaApprove = 60;

    /// @dev Coverage percentage per level in basis points
    mapping(DataTypes.CoverageLevel => uint256) public coveragePercentBps;

    /// @dev Max daily swaps per tier
    mapping(DataTypes.Tier => uint256) public maxDailySwaps;

    // -------------------------------------------------------
    //  State
    // -------------------------------------------------------

    /// @dev User profiles
    mapping(address => DataTypes.UserProfile) public userProfiles;

    /// @dev User policies
    mapping(address => DataTypes.Policy) public policies;

    /// @dev All claims
    DataTypes.Claim[] public claimsArray;

    /// @dev Oracle commit hashes per claim: claimId => oracle => commitHash
    mapping(uint256 => mapping(address => bytes32)) public claimCommits;

    /// @dev Track if oracle has revealed for a claim: claimId => oracle => bool
    mapping(uint256 => mapping(address => bool)) public hasRevealed;

    /// @dev Mapping from user to their claim IDs
    mapping(address => uint256[]) public userClaimIds;

    /// @dev All insured swaps
    DataTypes.InsuredSwap[] public insuredSwaps;

    /// @dev Mapping from user to their insured swap IDs
    mapping(address => uint256[]) public userInsuredSwapIds;

    // -------------------------------------------------------
    //  Bot Blacklist (C9)
    // -------------------------------------------------------

    /// @dev Number of approved claims attributed to a bot address
    mapping(address => uint256) public botAttackCount;

    /// @dev Total damage (loss) attributed to a bot address
    mapping(address => uint256) public botTotalDamage;

    /// @dev Whether a bot address is blacklisted
    mapping(address => bool) public botBlacklisted;

    /// @dev Threshold: number of approved claims before bot is blacklisted
    uint256 public botBlacklistThreshold = 3;

    // -------------------------------------------------------
    //  Inactivity Penalty (C10)
    // -------------------------------------------------------

    /// @dev Penalty deducted from non-revealing oracle stake (in wei)
    uint256 public inactivityPenalty = 0.001 ether;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event UserRegistered(address indexed user);
    event PolicyPurchased(
        address indexed user,
        DataTypes.CoverageLevel coverageLevel,
        uint256 premium,
        uint256 startTime,
        uint256 endTime
    );
    event ClaimSubmitted(
        uint256 indexed claimId,
        address indexed user,
        bytes32 txHash1,
        bytes32 txHash2,
        bytes32 txHash3,
        uint256 swapValue,
        uint256 loss,
        address[] assignedOracles
    );
    event VerdictCommitted(uint256 indexed claimId, address indexed oracle);
    event VerdictRevealed(
        uint256 indexed claimId,
        address indexed oracle,
        uint8 fraudScore,
        bool patternValid
    );
    event ClaimFinalized(
        uint256 indexed claimId,
        DataTypes.ClaimStatus status,
        uint256 medianFraudScore,
        uint256 dispersione
    );
    event PayoutIssued(uint256 indexed claimId, address indexed user, uint256 amount);
    event SecondaryReviewTriggered(uint256 indexed claimId, uint256 dispersione, address[] newOracles);
    event BotBlacklisted(address indexed botAddress, uint256 attackCount, uint256 totalDamage);
    event OracleInactivityPenalized(uint256 indexed claimId, address indexed oracle, uint256 penalty);
    event SwapInsured(
        uint256 indexed swapId,
        address indexed user,
        uint256 swapValue,
        uint256 premiumPaid
    );
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor(address _token, address _oracleRegistry) Ownable(msg.sender) {
        token = IERC20(_token);
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));

        // Coverage payout percentages (basis points) - PDF Table 2
        coveragePercentBps[DataTypes.CoverageLevel.Low] = 5000;    // 50%
        coveragePercentBps[DataTypes.CoverageLevel.Medium] = 7000; // 70%
        coveragePercentBps[DataTypes.CoverageLevel.High] = 10000;  // 100%

        // Max daily swaps per tier
        maxDailySwaps[DataTypes.Tier.Bronze] = 3;
        maxDailySwaps[DataTypes.Tier.Silver] = 3;
        maxDailySwaps[DataTypes.Tier.Gold] = 4;
        maxDailySwaps[DataTypes.Tier.Platinum] = type(uint256).max; // unlimited
    }

    // -------------------------------------------------------
    //  User Management
    // -------------------------------------------------------

    /// @dev Track registered users
    mapping(address => bool) public registeredUsers;

    /**
     * @dev Register as a user in the insurance system.
     */
    function registerUser() external {
        require(!registeredUsers[msg.sender], "Already registered");

        registeredUsers[msg.sender] = true;
        userProfiles[msg.sender].tier = DataTypes.Tier.Bronze;

        emit UserRegistered(msg.sender);
    }

    /**
     * @dev Buy an insurance policy. Charges only a symbolic activation fee (1 MEVI).
     *      Premium is calculated and paid per swap via insuredSwap().
     * @param _coverageLevel Desired coverage level
     */
    function buyPolicy(DataTypes.CoverageLevel _coverageLevel) external {
        require(registeredUsers[msg.sender], "Not registered");
        require(!policies[msg.sender].active, "Policy already active");
        require(!userProfiles[msg.sender].isBlacklisted, "User is blacklisted");

        uint256 start = block.timestamp;
        uint256 end = start + policyDuration;

        // Effects
        policies[msg.sender] = DataTypes.Policy({
            holder: msg.sender,
            coverageLevel: _coverageLevel,
            premium: activationFee,
            startTime: start,
            endTime: end,
            maxSwapValue: defaultCoverage,
            active: true
        });

        userProfiles[msg.sender].policyActive = true;
        userProfiles[msg.sender].policyStart = start;
        userProfiles[msg.sender].policyEnd = end;
        userProfiles[msg.sender].coverageLevel = _coverageLevel;

        // Interaction: transfer symbolic activation fee
        require(
            token.transferFrom(msg.sender, address(this), activationFee),
            "Activation fee transfer failed"
        );

        emit PolicyPurchased(msg.sender, _coverageLevel, activationFee, start, end);
    }

    // -------------------------------------------------------
    //  Insured Swap (premium per swap)
    // -------------------------------------------------------

    /**
     * @dev Register an insured swap. Premium is calculated and paid here.
     *      User must have an active policy. After calling this, the user can
     *      submit a claim if the swap is attacked.
     * @param _swapValue Value of the swap to insure
     */
    function insuredSwap(uint256 _swapValue) external nonReentrant {
        DataTypes.Policy storage policy = policies[msg.sender];
        require(policy.active, "No active policy");
        require(block.timestamp <= policy.endTime, "Policy expired");
        require(_swapValue > 0, "Swap value must be > 0");
        require(_swapValue <= policy.maxSwapValue, "Swap value exceeds policy max");

        // Calculate premium
        uint256 premium;
        if (address(premiumCalculator) != address(0)) {
            premium = premiumCalculator.calculatePremium(_swapValue, policy.coverageLevel);
            if (premium == 0) premium = (_swapValue * 150) / 10000; // fallback: 1.5% min
        } else {
            premium = (_swapValue * 150) / 10000; // 1.5% of swap value
        }

        // Check daily swap limits per tier
        DataTypes.UserProfile storage profile = userProfiles[msg.sender];
        uint256 today = block.timestamp / 1 days;
        if (profile.lastSwapDay == today) {
            require(
                profile.dailySwapCount < maxDailySwaps[profile.tier],
                "Daily swap limit reached"
            );
            profile.dailySwapCount++;
        } else {
            profile.lastSwapDay = today;
            profile.dailySwapCount = 1;
        }
        profile.totalSwaps++;

        // Store insured swap
        uint256 swapId = insuredSwaps.length;
        insuredSwaps.push(DataTypes.InsuredSwap({
            user: msg.sender,
            swapValue: _swapValue,
            premiumPaid: premium,
            timestamp: block.timestamp,
            claimed: false
        }));
        userInsuredSwapIds[msg.sender].push(swapId);

        // Interaction: transfer premium
        require(
            token.transferFrom(msg.sender, address(this), premium),
            "Premium transfer failed"
        );

        emit SwapInsured(swapId, msg.sender, _swapValue, premium);
    }

    // -------------------------------------------------------
    //  Claim Submission
    // -------------------------------------------------------

    /**
     * @dev Submit a claim for a sandwich attack on a previously insured swap.
     * @param _swapId ID of the insured swap (from insuredSwap())
     * @param _txHash1 Frontrun transaction hash
     * @param _txHash2 Victim transaction hash
     * @param _txHash3 Backrun transaction hash
     * @param _loss Claimed loss amount
     * @param _botAddress Address of the suspected MEV bot (C9)
     */
    function submitClaim(
        uint256 _swapId,
        bytes32 _txHash1,
        bytes32 _txHash2,
        bytes32 _txHash3,
        uint256 _loss,
        address _botAddress
    ) external nonReentrant {
        require(_swapId < insuredSwaps.length, "Invalid swap ID");
        DataTypes.InsuredSwap storage insSwap = insuredSwaps[_swapId];
        require(insSwap.user == msg.sender, "Not swap owner");
        require(!insSwap.claimed, "Swap already claimed");

        DataTypes.Policy storage policy = policies[msg.sender];
        require(policy.active, "No active policy");
        require(block.timestamp <= policy.endTime, "Policy expired");
        require(_loss > 0, "Loss must be > 0");
        require(_loss <= insSwap.swapValue, "Loss exceeds swap value");

        insSwap.claimed = true;

        DataTypes.UserProfile storage profile = userProfiles[msg.sender];

        // Select oracles
        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.timestamp, block.prevrandao, msg.sender, claimsArray.length
        )));
        address[] memory selectedOracles = oracleRegistry.selectOracles(seed, nOracle);

        uint256 claimId = claimsArray.length;

        // Push a new claim
        claimsArray.push();
        DataTypes.Claim storage newClaim = claimsArray[claimId];
        newClaim.user = msg.sender;
        newClaim.txHash1 = _txHash1;
        newClaim.txHash2 = _txHash2;
        newClaim.txHash3 = _txHash3;
        newClaim.status = DataTypes.ClaimStatus.OracleReview;
        newClaim.coverageLevel = policy.coverageLevel;
        newClaim.swapValue = insSwap.swapValue;
        newClaim.loss = _loss;
        newClaim.botAddress = _botAddress;
        newClaim.timestamp = block.timestamp;

        for (uint256 i = 0; i < selectedOracles.length; i++) {
            newClaim.assignedOracles.push(selectedOracles[i]);
        }

        userClaimIds[msg.sender].push(claimId);
        profile.totalClaims++;

        emit ClaimSubmitted(
            claimId, msg.sender,
            _txHash1, _txHash2, _txHash3,
            insSwap.swapValue, _loss,
            selectedOracles
        );
    }

    // -------------------------------------------------------
    //  Oracle Commit-Reveal
    // -------------------------------------------------------

    /**
     * @dev Oracle commits a verdict hash for a claim.
     * @param _claimId The claim ID
     * @param _commitHash keccak256(abi.encodePacked(fraudScore, patternValid, salt))
     */
    function commitVerdict(uint256 _claimId, bytes32 _commitHash) external {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.OracleReview, "Claim not in review");
        require(_isAssignedOracle(_claimId, msg.sender), "Not assigned oracle");
        require(claimCommits[_claimId][msg.sender] == bytes32(0), "Already committed");
        require(_commitHash != bytes32(0), "Invalid commit hash");

        claimCommits[_claimId][msg.sender] = _commitHash;
        claim.commitCount++;

        emit VerdictCommitted(_claimId, msg.sender);
    }

    /**
     * @dev Oracle reveals their verdict for a claim.
     * @param _claimId The claim ID
     * @param _fraudScore Fraud score (0-100)
     * @param _patternValid Whether the sandwich pattern is valid
     * @param _salt Random salt used in commit
     */
    function revealVerdict(
        uint256 _claimId,
        uint8 _fraudScore,
        bool _patternValid,
        bytes32 _salt
    ) external {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.OracleReview, "Claim not in review");
        require(_isAssignedOracle(_claimId, msg.sender), "Not assigned oracle");
        require(!hasRevealed[_claimId][msg.sender], "Already revealed");
        require(_fraudScore <= 130, "Fraud score must be 0-130");

        // Verify commit hash
        bytes32 expectedHash = keccak256(abi.encodePacked(_fraudScore, _patternValid, _salt));
        require(claimCommits[_claimId][msg.sender] == expectedHash, "Commit hash mismatch");

        hasRevealed[_claimId][msg.sender] = true;
        claim.revealedScores.push(_fraudScore);
        claim.revealCount++;

        if (_patternValid) {
            claim.patternValidVotes++;
        } else {
            claim.patternInvalidVotes++;
        }

        emit VerdictRevealed(_claimId, msg.sender, _fraudScore, _patternValid);
    }

    // -------------------------------------------------------
    //  Claim Finalization
    // -------------------------------------------------------

    /**
     * @dev Finalize a claim after all oracles have revealed (or timeout).
     * @param _claimId The claim ID
     */
    function finalizeClaim(uint256 _claimId) external nonReentrant {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.OracleReview, "Claim not in review");

        // All oracles revealed OR timeout elapsed
        bool allRevealed = claim.revealCount == claim.assignedOracles.length;
        bool timeoutReached = block.timestamp >= claim.timestamp + oracleTimeout;
        require(allRevealed || timeoutReached, "Not all oracles revealed and timeout not reached");
        require(claim.revealCount > 0, "No oracle reveals");

        uint256 totalVotes = claim.patternValidVotes + claim.patternInvalidVotes;

        // Step 1: Check pattern validity - if >= 70% say invalid, reject
        if (totalVotes > 0) {
            uint256 invalidPercent = (claim.patternInvalidVotes * 10000) / totalVotes;
            if (invalidPercent >= patternInvalidThresholdBps) {
                claim.status = DataTypes.ClaimStatus.InvalidPattern;
                claim.finalFraudScore = 100; // max fraud
                _updateUserFraudScore(claim.user, 100);
                userProfiles[claim.user].rejectedClaims++;
                emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.InvalidPattern, 100, 0);
                return;
            }
        }

        // Step 2: Calculate median fraud score
        uint256 median = _calculateMedian(claim.revealedScores);
        claim.finalFraudScore = median;

        // Step 3: Calculate dispersione (max - min)
        uint256 minScore = type(uint256).max;
        uint256 maxScore = 0;
        for (uint256 i = 0; i < claim.revealedScores.length; i++) {
            uint256 s = claim.revealedScores[i];
            if (s < minScore) minScore = s;
            if (s > maxScore) maxScore = s;
        }
        claim.dispersione = maxScore - minScore;

        // Step 3b: Secondary review if dispersione > threshold and not already reviewed
        if (claim.dispersione > dispersioneThreshold && !claim.secondaryReview) {
            claim.secondaryReview = true;

            // Reset claim for new oracle round
            delete claim.revealedScores;
            claim.patternValidVotes = 0;
            claim.patternInvalidVotes = 0;
            claim.revealCount = 0;
            claim.commitCount = 0;
            claim.finalFraudScore = 0;
            claim.dispersione = 0;
            claim.status = DataTypes.ClaimStatus.OracleReview;
            claim.timestamp = block.timestamp;

            // Clear old commits/reveals for this claim
            for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
                address oldOracle = claim.assignedOracles[i];
                claimCommits[_claimId][oldOracle] = bytes32(0);
                hasRevealed[_claimId][oldOracle] = false;
            }

            // Select new oracles
            uint256 seed = uint256(keccak256(abi.encodePacked(
                block.timestamp, block.prevrandao, claim.user, _claimId, uint256(1)
            )));
            address[] memory newOracles = oracleRegistry.selectOracles(seed, nOracle);

            // Replace assigned oracles
            delete claim.assignedOracles;
            for (uint256 i = 0; i < newOracles.length; i++) {
                claim.assignedOracles.push(newOracles[i]);
            }

            emit SecondaryReviewTriggered(_claimId, maxScore - minScore, newOracles);
            return;
        }

        // Step 4: Tier-based decision
        DataTypes.Tier userTier = userProfiles[claim.user].tier;
        DataTypes.ClaimStatus finalStatus;

        if (userTier == DataTypes.Tier.Bronze || userTier == DataTypes.Tier.Silver) {
            // Bronze/Silver: score < thetaReject -> CAPTCHA, score >= thetaReject -> blacklist/reject
            if (median < thetaReject) {
                finalStatus = DataTypes.ClaimStatus.CAPTCHARequired;
            } else {
                finalStatus = DataTypes.ClaimStatus.Rejected;
                userProfiles[claim.user].isBlacklisted = true;
                // Add 20% penalty debt on the loss
                userProfiles[claim.user].debt += (claim.loss * 2000) / 10000;
            }
        } else {
            // Gold/Platinum: score < thetaApprove -> approved,
            // thetaApprove <= score <= thetaReject -> CAPTCHA,
            // score > thetaReject -> blacklist/reject
            if (median < thetaApprove) {
                finalStatus = DataTypes.ClaimStatus.Approved;
            } else if (median <= thetaReject) {
                finalStatus = DataTypes.ClaimStatus.CAPTCHARequired;
            } else {
                finalStatus = DataTypes.ClaimStatus.Rejected;
                userProfiles[claim.user].isBlacklisted = true;
                userProfiles[claim.user].debt += (claim.loss * 2000) / 10000;
            }
        }

        claim.status = finalStatus;
        _updateUserFraudScore(claim.user, median);

        // Step 5: If approved, calculate and issue payout
        if (finalStatus == DataTypes.ClaimStatus.Approved) {
            userProfiles[claim.user].approvedClaims++;
            uint256 coverPercent = coveragePercentBps[claim.coverageLevel];
            uint256 payout = (claim.loss * coverPercent) / 10000;

            // Interaction: transfer payout
            require(
                token.transfer(claim.user, payout),
                "Payout transfer failed"
            );

            emit PayoutIssued(_claimId, claim.user, payout);
        } else if (finalStatus == DataTypes.ClaimStatus.Rejected) {
            userProfiles[claim.user].rejectedClaims++;
        }

        // Step 6: Track bot stats if approved (C9)
        if (finalStatus == DataTypes.ClaimStatus.Approved && claim.botAddress != address(0)) {
            botAttackCount[claim.botAddress]++;
            botTotalDamage[claim.botAddress] += claim.loss;
            if (!botBlacklisted[claim.botAddress] && botAttackCount[claim.botAddress] >= botBlacklistThreshold) {
                botBlacklisted[claim.botAddress] = true;
                emit BotBlacklisted(claim.botAddress, botAttackCount[claim.botAddress], botTotalDamage[claim.botAddress]);
            }
        }

        emit ClaimFinalized(_claimId, finalStatus, median, claim.dispersione);

        // Reward revealed oracles + penalize inactive ones (C10)
        _rewardRevealedOracles(_claimId);
        _penalizeInactiveOracles(_claimId);
    }

    /**
     * @dev Reward all oracles that revealed their verdict for a claim.
     * Reward is fixed (rClaim in OracleRegistry) and independent of outcome.
     */
    function _rewardRevealedOracles(uint256 _claimId) internal {
        DataTypes.Claim storage claim = claimsArray[_claimId];
        for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
            address oracle = claim.assignedOracles[i];
            if (hasRevealed[_claimId][oracle]) {
                // Try to reward; skip silently if registry lacks funds or oracle ineligible
                try oracleRegistry.rewardOracle(oracle) {} catch {}
            }
        }
    }

    /**
     * @dev Penalize oracles that were assigned but did not reveal (C10).
     * Small stake deduction via OracleRegistry.slashOracle.
     */
    function _penalizeInactiveOracles(uint256 _claimId) internal {
        DataTypes.Claim storage claim = claimsArray[_claimId];
        for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
            address oracle = claim.assignedOracles[i];
            if (!hasRevealed[_claimId][oracle]) {
                try oracleRegistry.penalizeInactivity(oracle, inactivityPenalty) {
                    emit OracleInactivityPenalized(_claimId, oracle, inactivityPenalty);
                } catch {}
            }
        }
    }

    /**
     * @dev Resolve a claim that requires CAPTCHA verification.
     * Called by owner after off-chain CAPTCHA check.
     * @param _claimId The claim ID
     * @param _approved Whether the CAPTCHA was passed and claim is approved
     */
    function resolveCAPTCHA(uint256 _claimId, bool _approved) external onlyOwner nonReentrant {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.CAPTCHARequired, "Not awaiting CAPTCHA");

        if (_approved) {
            claim.status = DataTypes.ClaimStatus.Approved;
            userProfiles[claim.user].approvedClaims++;

            uint256 coverPercent = coveragePercentBps[claim.coverageLevel];
            uint256 payout = (claim.loss * coverPercent) / 10000;

            require(
                token.transfer(claim.user, payout),
                "Payout transfer failed"
            );

            emit PayoutIssued(_claimId, claim.user, payout);
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Approved, claim.finalFraudScore, claim.dispersione);
        } else {
            claim.status = DataTypes.ClaimStatus.Rejected;
            userProfiles[claim.user].rejectedClaims++;
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Rejected, claim.finalFraudScore, claim.dispersione);
        }
    }

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    function getClaimsCount() external view returns (uint256) {
        return claimsArray.length;
    }

    function getUserClaims(address _user) external view returns (uint256[] memory) {
        return userClaimIds[_user];
    }

    function getClaimOracles(uint256 _claimId) external view returns (address[] memory) {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        return claimsArray[_claimId].assignedOracles;
    }

    function getClaimScores(uint256 _claimId) external view returns (uint8[] memory) {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        return claimsArray[_claimId].revealedScores;
    }

    function getClaimInfo(uint256 _claimId) external view returns (
        address user,
        DataTypes.ClaimStatus status,
        uint256 finalFraudScore,
        uint256 swapValue,
        uint256 loss,
        uint256 dispersione,
        uint256 revealCount,
        uint256 commitCount
    ) {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage c = claimsArray[_claimId];
        return (c.user, c.status, c.finalFraudScore, c.swapValue, c.loss, c.dispersione, c.revealCount, c.commitCount);
    }

    function getInsuredSwapsCount() external view returns (uint256) {
        return insuredSwaps.length;
    }

    function getUserInsuredSwaps(address _user) external view returns (uint256[] memory) {
        return userInsuredSwapIds[_user];
    }

    function getUserProfile(address _user) external view returns (
        DataTypes.Tier tier,
        bool policyActive,
        uint256 totalSwaps,
        uint256 totalClaims,
        uint256 approvedClaims,
        uint256 rejectedClaims,
        uint256 avgFraudScore,
        bool isBlacklisted,
        uint256 debt
    ) {
        DataTypes.UserProfile storage p = userProfiles[_user];
        return (p.tier, p.policyActive, p.totalSwaps, p.totalClaims, p.approvedClaims, p.rejectedClaims, p.avgFraudScore, p.isBlacklisted, p.debt);
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setNOracle(uint256 _val) external onlyOwner {
        nOracle = _val;
        emit ParameterUpdated("nOracle", _val);
    }

    function setPolicyDuration(uint256 _val) external onlyOwner {
        policyDuration = _val;
        emit ParameterUpdated("policyDuration", _val);
    }

    function setActivationFee(uint256 _val) external onlyOwner {
        activationFee = _val;
        emit ParameterUpdated("activationFee", _val);
    }

    function setDefaultPremium(uint256 _val) external onlyOwner {
        defaultPremium = _val;
        emit ParameterUpdated("defaultPremium", _val);
    }

    function setDefaultCoverage(uint256 _val) external onlyOwner {
        defaultCoverage = _val;
        emit ParameterUpdated("defaultCoverage", _val);
    }

    function setPremiumCalculator(address _calculator) external onlyOwner {
        premiumCalculator = PremiumCalculator(_calculator);
        emit ParameterUpdated("premiumCalculator", uint256(uint160(_calculator)));
    }

    function setTierSystem(address _tierSystem) external onlyOwner {
        tierSystem = TierSystem(_tierSystem);
        emit ParameterUpdated("tierSystem", uint256(uint160(_tierSystem)));
    }

    function setOracleTimeout(uint256 _val) external onlyOwner {
        oracleTimeout = _val;
        emit ParameterUpdated("oracleTimeout", _val);
    }

    function setThetaReject(uint256 _val) external onlyOwner {
        thetaReject = _val;
        emit ParameterUpdated("thetaReject", _val);
    }

    function setThetaApprove(uint256 _val) external onlyOwner {
        thetaApprove = _val;
        emit ParameterUpdated("thetaApprove", _val);
    }

    function setPatternInvalidThresholdBps(uint256 _val) external onlyOwner {
        patternInvalidThresholdBps = _val;
        emit ParameterUpdated("patternInvalidThresholdBps", _val);
    }

    function setDispersioneThreshold(uint256 _val) external onlyOwner {
        dispersioneThreshold = _val;
        emit ParameterUpdated("dispersioneThreshold", _val);
    }

    function setBotBlacklistThreshold(uint256 _val) external onlyOwner {
        botBlacklistThreshold = _val;
        emit ParameterUpdated("botBlacklistThreshold", _val);
    }

    function setInactivityPenalty(uint256 _val) external onlyOwner {
        inactivityPenalty = _val;
        emit ParameterUpdated("inactivityPenalty", _val);
    }

    function setUserTier(address _user, DataTypes.Tier _tier) external onlyOwner {
        userProfiles[_user].tier = _tier;
    }

    // -------------------------------------------------------
    //  Internal Helpers
    // -------------------------------------------------------

    /**
     * @dev Check if an address is an assigned oracle for a claim.
     */
    function _isAssignedOracle(uint256 _claimId, address _oracle) internal view returns (bool) {
        address[] storage oracles = claimsArray[_claimId].assignedOracles;
        for (uint256 i = 0; i < oracles.length; i++) {
            if (oracles[i] == _oracle) return true;
        }
        return false;
    }

    /**
     * @dev Calculate the median of a uint8 array using insertion sort.
     * @param _scores Array of fraud scores
     * @return median The median value
     */
    function _calculateMedian(uint8[] storage _scores) internal view returns (uint256) {
        uint256 len = _scores.length;
        require(len > 0, "No scores");

        // Copy to memory for sorting
        uint8[] memory sorted = new uint8[](len);
        for (uint256 i = 0; i < len; i++) {
            sorted[i] = _scores[i];
        }

        // Insertion sort (small arrays, n <= 7 typically)
        for (uint256 i = 1; i < len; i++) {
            uint8 key = sorted[i];
            uint256 j = i;
            while (j > 0 && sorted[j - 1] > key) {
                sorted[j] = sorted[j - 1];
                j--;
            }
            sorted[j] = key;
        }

        // Median
        if (len % 2 == 1) {
            return uint256(sorted[len / 2]);
        } else {
            return (uint256(sorted[len / 2 - 1]) + uint256(sorted[len / 2])) / 2;
        }
    }

    /**
     * @dev Update a user's running average fraud score.
     */
    function _updateUserFraudScore(address _user, uint256 _newScore) internal {
        DataTypes.UserProfile storage profile = userProfiles[_user];
        uint256 totalEvaluated = profile.approvedClaims + profile.rejectedClaims;
        if (totalEvaluated == 0) {
            profile.avgFraudScore = _newScore;
        } else {
            // Running average: (old * count + new) / (count + 1)
            profile.avgFraudScore = (profile.avgFraudScore * totalEvaluated + _newScore) / (totalEvaluated + 1);
        }
    }
}
