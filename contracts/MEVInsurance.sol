// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";
import "./OracleRegistry.sol";
import "./PremiumCalculator.sol";
import "./TierSystem.sol";

interface ITierSystem {
    function checkAndUpgrade(address _user) external;
    function getTier(address _user) external view returns (DataTypes.Tier);
    function syncUserData(address _user, uint256 _totalSwaps, uint256 _avgFraudScore) external;
    function getMaxDailySwaps(DataTypes.Tier _tier) external pure returns (uint256);
}

contract MEVInsurance is Ownable, ReentrancyGuard {
    using DataTypes for *;

    IERC20 public token;
    OracleRegistry public oracleRegistry;
    PremiumCalculator public premiumCalculator;
    ITierSystem public tierSystem;

    uint256 public nOracle = 7;
    uint256 public policyDuration = 30 days;
    uint256 public activationFee = 1 * 1e18;
    uint256 public defaultPremium = 100 * 1e18;
    uint256 public defaultCoverage = 1000 * 1e18;
    uint256 public oracleTimeout = 3 days;
    uint256 public patternInvalidThresholdBps = 7000;
    uint256 public dispersioneThreshold = 20;
    uint256 public thetaReject = 80;
    uint256 public thetaApprove = 60;

    mapping(DataTypes.CoverageLevel => uint256) public coveragePercentBps;
    mapping(DataTypes.Tier => uint256) public maxDailySwaps;
    mapping(DataTypes.Tier => uint256) public maxSwapValue;

    mapping(address => DataTypes.UserProfile) public userProfiles;
    mapping(address => DataTypes.Policy) public policies;
    DataTypes.Claim[] public claimsArray;
    mapping(uint256 => mapping(address => bytes32)) public claimCommits;
    mapping(uint256 => mapping(address => bool)) public hasRevealed;
    mapping(address => uint256[]) public userClaimIds;
    DataTypes.InsuredSwap[] public insuredSwaps;
    mapping(address => uint256[]) public userInsuredSwapIds;

    mapping(address => uint256) public botAttackCount;
    mapping(address => uint256) public botTotalDamage;
    mapping(address => bool) public botBlacklisted;

    address[] public blacklistedBots;
    mapping(address => bool) public isInBotBlacklist;
    address[] public blacklistedUsers;
    mapping(address => bool) public isInUserBlacklist;
    address[] public whitelistedAddresses;
    mapping(address => bool) public isInWhitelist;

    uint256 public inactivityPenalty = 0.001 ether;
    uint256 public gasRefundAmount = 0.01 ether;
    uint256 public alphaStakeBps = 2000;

    mapping(address => uint256) public platinumStake;
    uint256 public totalPlatinumStake;

    struct PlatinumRequest {
        uint256 desiredMaxSwap;
        uint256 stakeDeposited;
        address[3] assignedOracles;
        bytes32 answerHash;
        string challengeText;
        uint8 approveVotes;
        uint8 rejectVotes;
        uint8 totalVotes;
        bool resolved;
        bool challengeSet;
        bool answerSubmitted;
        uint256 timestamp;
    }
    mapping(address => PlatinumRequest) public platinumRequests;
    mapping(address => mapping(address => bool)) public platinumOracleVoted;

    struct ClaimCaptcha {
        address[3] assignedOracles;
        bytes32 answerHash;
        string challengeText;
        uint8 approveVotes;
        uint8 rejectVotes;
        uint8 totalVotes;
        bool resolved;
        bool challengeSet;
        bool answerSubmitted;
    }
    mapping(uint256 => ClaimCaptcha) public claimCaptchas;
    mapping(uint256 => mapping(address => bool)) public claimCaptchaVoted;

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
    event GasRefundIssued(uint256 indexed claimId, address indexed user, uint256 refundAmount);
    event SwapInsured(
        uint256 indexed swapId,
        address indexed user,
        uint256 swapValue,
        uint256 premiumPaid
    );
    event ParameterUpdated(string param, uint256 value);
    event WhitelistAdded(address[] addresses);
    event WhitelistRemoved(address indexed addr);
    event PlatinumRequested(
        address indexed user,
        uint256 desiredMaxSwap,
        uint256 stakeDeposited,
        address[3] assignedOracles
    );
    event CaptchaChallengePublished(address indexed user, string challengeText, bytes32 answerHash);
    event CaptchaAnswerSubmitted(address indexed user, bytes32 answerHash);
    event CaptchaVerdictSubmitted(address indexed user, address indexed oracle, bool isValid);
    event PlatinumUpgradeResult(address indexed user, bool approved, uint8 approveVotes, uint8 rejectVotes);
    event OracleRewarded(address indexed oracle, uint256 amount);
    event OracleRewardSkipped(address indexed oracle, string reason);
    event PoolRebalanced(uint256 meviConverted, uint256 ethReceived);
    event ClaimCaptchaRequired(uint256 indexed claimId, address indexed user, address[3] assignedOracles);
    event ClaimCaptchaChallengePublished(uint256 indexed claimId, string challengeText, bytes32 answerHash);
    event ClaimCaptchaAnswerSubmitted(uint256 indexed claimId, address indexed user, bytes32 answerHash);
    event ClaimCaptchaVerdictSubmitted(uint256 indexed claimId, address indexed oracle, bool isValid);

    constructor(address _token, address _oracleRegistry) Ownable(msg.sender) {
        token = IERC20(_token);
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));

        // Coverage payout percentages (basis points) - PDF Table 2
        coveragePercentBps[DataTypes.CoverageLevel.Low] = 5000;
        coveragePercentBps[DataTypes.CoverageLevel.Medium] = 7000;
        coveragePercentBps[DataTypes.CoverageLevel.High] = 10000;

        maxDailySwaps[DataTypes.Tier.Bronze] = 3;
        maxDailySwaps[DataTypes.Tier.Silver] = 3;
        maxDailySwaps[DataTypes.Tier.Gold] = 4;
        maxDailySwaps[DataTypes.Tier.Platinum] = type(uint256).max;
        maxSwapValue[DataTypes.Tier.Bronze]   = 1000 * 1e18;
        maxSwapValue[DataTypes.Tier.Silver]   = 3000 * 1e18;
        maxSwapValue[DataTypes.Tier.Gold]     = 5000 * 1e18;
        maxSwapValue[DataTypes.Tier.Platinum] = 0;
    }

    mapping(address => bool) public registeredUsers;

    function registerUser() external {
        if(!registeredUsers[msg.sender]) {
            registeredUsers[msg.sender] = true;
            userProfiles[msg.sender].tier = DataTypes.Tier.Bronze;
            emit UserRegistered(msg.sender);
        }
    }

    function insuredSwap(uint256 _swapValue, DataTypes.CoverageLevel _coverageLevel) external nonReentrant {
        if (address(tierSystem) != address(0) && userProfiles[msg.sender].tier != DataTypes.Tier.Platinum) {
            tierSystem.checkAndUpgrade(msg.sender);
        }

        if (!registeredUsers[msg.sender]) {
            registeredUsers[msg.sender] = true;
            if (userProfiles[msg.sender].tier != DataTypes.Tier.Platinum) {
                if (address(tierSystem) != address(0)) {
                    userProfiles[msg.sender].tier = tierSystem.getTier(msg.sender);
                } else {
                    userProfiles[msg.sender].tier = DataTypes.Tier.Bronze;
                }
            }
            emit UserRegistered(msg.sender);
        }
        DataTypes.UserProfile storage profile = userProfiles[msg.sender];

        if (address(tierSystem) != address(0) && profile.tier != DataTypes.Tier.Platinum) {
            profile.tier = tierSystem.getTier(msg.sender);
        }

        uint256 maxAllowed;
        require(!profile.isBlacklisted, "User is blacklisted");
        require(_swapValue > 0, "Swap value must be > 0");
        if (profile.tier == DataTypes.Tier.Platinum) {
            require(platinumStake[msg.sender] > 0, "No Platinum stake deposited");
            maxAllowed = (platinumStake[msg.sender] * 10000) / alphaStakeBps;
        } else {
            maxAllowed = maxSwapValue[profile.tier];
        }
        require(_swapValue <= maxAllowed, "Swap value exceeds tier limit");

        uint256 premium;
        require(address(premiumCalculator) != address(0), "PremiumCalculator not set");
        premium = premiumCalculator.calculatePremium(_swapValue, _coverageLevel);

        require(token.transferFrom(msg.sender, address(this), premium), "Premium payment failed");

        uint256 swapId = insuredSwaps.length;
        insuredSwaps.push(DataTypes.InsuredSwap({
            user: msg.sender,
            swapValue: _swapValue,
            premiumPaid: premium,
            coverageLevel: _coverageLevel,
            timestamp: block.timestamp,
            claimed: false
        }));
        userInsuredSwapIds[msg.sender].push(swapId);
        profile.totalSwaps++;

        emit SwapInsured(swapId, msg.sender, _swapValue, premium);
    }

    function submitClaim(
        uint256 _swapId,
        bytes32 _txHash1,
        bytes32 _txHash2,
        bytes32 _txHash3,
        uint256 _loss,
        address _botAddress
    ) external nonReentrant {
        require(_swapId < insuredSwaps.length, "Invalid swap ID");
        DataTypes.InsuredSwap storage s = insuredSwaps[_swapId];
        require(s.user == msg.sender, "Not your swap");
        require(!s.claimed, "Already claimed");
        require(!userProfiles[msg.sender].isBlacklisted, "User blacklisted");
        require(_loss <= s.swapValue, "Loss > swap value");

        s.claimed = true;
        uint256 claimId = claimsArray.length;

        uint256 seed = uint256(keccak256(abi.encodePacked(block.timestamp, block.prevrandao, msg.sender, claimId)));
        address[] memory assignedOracles = oracleRegistry.selectOracles(seed, nOracle);

        DataTypes.Claim storage newClaim = claimsArray.push();
        newClaim.user = msg.sender;
        newClaim.status = DataTypes.ClaimStatus.OracleReview;
        newClaim.txHash1 = _txHash1;
        newClaim.txHash2 = _txHash2;
        newClaim.txHash3 = _txHash3;
        newClaim.swapValue = s.swapValue;
        newClaim.loss = _loss;
        newClaim.botAddress = _botAddress;
        newClaim.coverageLevel = s.coverageLevel;
        newClaim.timestamp = block.timestamp;
        newClaim.assignedOracles = assignedOracles;

        userClaimIds[msg.sender].push(claimId);
        userProfiles[msg.sender].totalClaims++;

        emit ClaimSubmitted(claimId, msg.sender, _txHash1, _txHash2, _txHash3, s.swapValue, _loss, assignedOracles);
    }

    function commitVerdict(uint256 _claimId, bytes32 _commitHash) external {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        require(_isAssignedOracle(_claimId, msg.sender), "Not assigned");
        require(claimsArray[_claimId].status == DataTypes.ClaimStatus.OracleReview, "Not in review");
        require(claimCommits[_claimId][msg.sender] == bytes32(0), "Already committed");

        claimCommits[_claimId][msg.sender] = _commitHash;
        claimsArray[_claimId].commitCount++;

        emit VerdictCommitted(_claimId, msg.sender);
    }

    function revealVerdict(uint256 _claimId, uint8 _fraudScore, bool _patternValid, bytes32 _salt) external {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        require(_isAssignedOracle(_claimId, msg.sender), "Not assigned");
        require(!hasRevealed[_claimId][msg.sender], "Already revealed");
        require(_fraudScore <= 130, "Fraud score must be 0-130");

        bytes32 commit = claimCommits[_claimId][msg.sender];
        require(commit != bytes32(0), "No commit found");
        require(keccak256(abi.encodePacked(_fraudScore, _patternValid, _salt)) == commit, "Hash mismatch");

        hasRevealed[_claimId][msg.sender] = true;
        DataTypes.Claim storage claim = claimsArray[_claimId];
        claim.revealedScores.push(_fraudScore);
        if (_patternValid) {
            claim.patternValidVotes++;
        } else {
            claim.patternInvalidVotes++;
        }
        claim.revealCount++;

        emit VerdictRevealed(_claimId, msg.sender, _fraudScore, _patternValid);
    }

    function finalizeClaim(uint256 _claimId) external nonReentrant {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.OracleReview, "Not in review");

        bool timeoutReached = block.timestamp > claim.timestamp + oracleTimeout;
        bool allRevealed = (claim.revealCount == claim.assignedOracles.length);
        require(allRevealed || timeoutReached, "Wait for reveals or timeout");
        require(claim.revealCount > 0, "No reveals");

        if (claim.revealCount >= 3) {
            uint256 invalidBps = (claim.patternInvalidVotes * 10000) / claim.revealCount;
            if (invalidBps >= patternInvalidThresholdBps) {
                claim.status = DataTypes.ClaimStatus.InvalidPattern;
                claim.finalFraudScore = 100;
                _updateUserFraudScore(claim.user, 100);
                userProfiles[claim.user].rejectedClaims++;
                emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.InvalidPattern, 100, 0);
                return;
            }
        }

        uint256 median = _calculateMedian(claim.revealedScores);
        claim.finalFraudScore = median;

        uint256 minScore = type(uint256).max;
        uint256 maxScore = 0;
        for (uint256 i = 0; i < claim.revealedScores.length; i++) {
            uint256 s = claim.revealedScores[i];
            if (s < minScore) minScore = s;
            if (s > maxScore) maxScore = s;
        }
        claim.dispersione = maxScore - minScore;

        if (claim.dispersione > dispersioneThreshold && !claim.secondaryReview) {
            claim.secondaryReview = true;

            delete claim.revealedScores;
            claim.patternValidVotes = 0;
            claim.patternInvalidVotes = 0;
            claim.revealCount = 0;
            claim.commitCount = 0;
            claim.finalFraudScore = 0;
            claim.dispersione = 0;
            claim.status = DataTypes.ClaimStatus.OracleReview;
            claim.timestamp = block.timestamp;

            for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
                address oldOracle = claim.assignedOracles[i];
                claimCommits[_claimId][oldOracle] = bytes32(0);
                hasRevealed[_claimId][oldOracle] = false;
            }

            uint256 seed = uint256(keccak256(abi.encodePacked(
                block.timestamp, block.prevrandao, claim.user, _claimId, uint256(1)
            )));
            address[] memory newOracles = oracleRegistry.selectOracles(seed, nOracle);

            delete claim.assignedOracles;
            for (uint256 i = 0; i < newOracles.length; i++) {
                claim.assignedOracles.push(newOracles[i]);
            }
            _rewardRevealedOracles(_claimId);

            emit SecondaryReviewTriggered(_claimId, maxScore - minScore, newOracles);
            return;
        }

        DataTypes.Tier userTier = userProfiles[claim.user].tier;
        DataTypes.ClaimStatus finalStatus;

        if (userTier == DataTypes.Tier.Bronze || userTier == DataTypes.Tier.Silver) {
            if (median <= thetaReject) {
                finalStatus = DataTypes.ClaimStatus.CAPTCHARequired;
            } else {
                finalStatus = DataTypes.ClaimStatus.Rejected;
                userProfiles[claim.user].isBlacklisted = true;
                if (!isInUserBlacklist[claim.user]) {
                    isInUserBlacklist[claim.user] = true;
                    blacklistedUsers.push(claim.user);
                }
                userProfiles[claim.user].debt += (claim.loss * 2000) / 10000;
            }
        } else {
            if (median < thetaApprove) {
                finalStatus = DataTypes.ClaimStatus.Approved;
            } else if (median <= thetaReject) {
                finalStatus = DataTypes.ClaimStatus.CAPTCHARequired;
            } else {
                finalStatus = DataTypes.ClaimStatus.Rejected;
                userProfiles[claim.user].isBlacklisted = true;
                if (!isInUserBlacklist[claim.user]) {
                    isInUserBlacklist[claim.user] = true;
                    blacklistedUsers.push(claim.user);
                }
                userProfiles[claim.user].debt += (claim.loss * 2000) / 10000;
            }
        }

        claim.status = finalStatus;
        _updateUserFraudScore(claim.user, median);

        if (finalStatus == DataTypes.ClaimStatus.CAPTCHARequired) {
            uint256 captchaSeed = uint256(keccak256(abi.encodePacked(
                block.timestamp, block.prevrandao, claim.user, _claimId, uint256(99)
            )));
            address[] memory captchaOracles = oracleRegistry.selectOracles(captchaSeed, 3);
            address[3] memory co3;
            co3[0] = captchaOracles[0];
            co3[1] = captchaOracles[1];
            co3[2] = captchaOracles[2];

            claimCaptchas[_claimId] = ClaimCaptcha({
                assignedOracles: co3,
                answerHash: bytes32(0),
                challengeText: "",
                approveVotes: 0,
                rejectVotes: 0,
                totalVotes: 0,
                resolved: false,
                challengeSet: false,
                answerSubmitted: false
            });

            for (uint256 i = 0; i < 3; i++) {
                claimCaptchaVoted[_claimId][co3[i]] = false;
            }

            emit ClaimCaptchaRequired(_claimId, claim.user, co3);
        }

        if (finalStatus == DataTypes.ClaimStatus.Approved) {
            userProfiles[claim.user].approvedClaims++;
            uint256 coverPercent = coveragePercentBps[claim.coverageLevel];
            uint256 payout = (claim.loss * coverPercent) / 10000;
            uint256 totalPayout = payout + gasRefundAmount;

            require(
                token.transfer(claim.user, totalPayout),
                "Payout transfer failed"
            );

            emit PayoutIssued(_claimId, claim.user, payout);
            if (gasRefundAmount > 0) {
                emit GasRefundIssued(_claimId, claim.user, gasRefundAmount);
            }
        } else if (finalStatus == DataTypes.ClaimStatus.Rejected) {
            userProfiles[claim.user].rejectedClaims++;
        }

        if (finalStatus == DataTypes.ClaimStatus.Approved && claim.botAddress != address(0)) {
            botAttackCount[claim.botAddress]++;
            botTotalDamage[claim.botAddress] += claim.loss;
            if (!botBlacklisted[claim.botAddress]) {
                botBlacklisted[claim.botAddress] = true;
                if (!isInBotBlacklist[claim.botAddress]) {
                    isInBotBlacklist[claim.botAddress] = true;
                    blacklistedBots.push(claim.botAddress);
                }
                emit BotBlacklisted(claim.botAddress, botAttackCount[claim.botAddress], botTotalDamage[claim.botAddress]);
            }
        }

        emit ClaimFinalized(_claimId, finalStatus, median, claim.dispersione);

        _rewardRevealedOracles(_claimId);
        _penalizeInactiveOracles(_claimId);
    }

    function _rewardRevealedOracles(uint256 _claimId) internal {
        DataTypes.Claim storage claim = claimsArray[_claimId];
        for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
            address oracle = claim.assignedOracles[i];
            if (hasRevealed[_claimId][oracle]) {
                try this._payOracleReward(oracle) {} catch {}
            }
        }
    }

    function _penalizeInactiveOracles(uint256 _claimId) internal {
        DataTypes.Claim storage claim = claimsArray[_claimId];
        for (uint256 i = 0; i < claim.assignedOracles.length; i++) {
            address oracle = claim.assignedOracles[i];
            if (!hasRevealed[_claimId][oracle]) {
                try oracleRegistry.penalizeInactivity(oracle, inactivityPenalty, address(this)) {
                    emit OracleInactivityPenalized(_claimId, oracle, inactivityPenalty);
                } catch {}
            }
        }
    }

    function resolveCAPTCHA(uint256 _claimId, bool _approved) external onlyOwner nonReentrant {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage claim = claimsArray[_claimId];
        require(claim.status == DataTypes.ClaimStatus.CAPTCHARequired, "Not awaiting CAPTCHA");

        if (_approved) {
            claim.status = DataTypes.ClaimStatus.Approved;
            userProfiles[claim.user].approvedClaims++;

            uint256 coverPercent = coveragePercentBps[claim.coverageLevel];
            uint256 payout = (claim.loss * coverPercent) / 10000;
            uint256 totalPayout = payout + gasRefundAmount;

            require(
                token.transfer(claim.user, totalPayout),
                "Payout transfer failed"
            );

            emit PayoutIssued(_claimId, claim.user, payout);
            if (gasRefundAmount > 0) {
                emit GasRefundIssued(_claimId, claim.user, gasRefundAmount);
            }
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Approved, claim.finalFraudScore, claim.dispersione);
            if (claim.botAddress != address(0)) {
                botAttackCount[claim.botAddress]++;
                botTotalDamage[claim.botAddress] += claim.loss;
                if (!botBlacklisted[claim.botAddress]) {
                    botBlacklisted[claim.botAddress] = true;
                    if (!isInBotBlacklist[claim.botAddress]) {
                        isInBotBlacklist[claim.botAddress] = true;
                        blacklistedBots.push(claim.botAddress);
                    }
                    emit BotBlacklisted(claim.botAddress, botAttackCount[claim.botAddress], botTotalDamage[claim.botAddress]);
                }
            }
        } else {
            claim.status = DataTypes.ClaimStatus.Rejected;
            userProfiles[claim.user].rejectedClaims++;
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Rejected, claim.finalFraudScore, claim.dispersione);
        }
    }

    function requestPlatinum(uint256 _desiredMaxSwap) external payable nonReentrant {
        require(!userProfiles[msg.sender].isBlacklisted, "User blacklisted");
        require(userProfiles[msg.sender].tier != DataTypes.Tier.Platinum, "Already Platinum");
        require(!platinumRequests[msg.sender].challengeSet || platinumRequests[msg.sender].resolved, "Request already pending");

        uint256 requiredStake = (_desiredMaxSwap * alphaStakeBps) / 10000;
        require(msg.value >= requiredStake, "Insufficient stake");
        totalPlatinumStake += msg.value;

        uint256 seed = uint256(keccak256(abi.encodePacked(block.timestamp, block.prevrandao, msg.sender)));
        address[] memory selected = oracleRegistry.selectOracles(seed, 3);

        address[3] memory oracles3;
        oracles3[0] = selected[0];
        oracles3[1] = selected[1];
        oracles3[2] = selected[2];

        platinumRequests[msg.sender] = PlatinumRequest({
            desiredMaxSwap: _desiredMaxSwap,
            stakeDeposited: msg.value,
            assignedOracles: oracles3,
            answerHash: bytes32(0),
            challengeText: "",
            approveVotes: 0,
            rejectVotes: 0,
            totalVotes: 0,
            resolved: false,
            challengeSet: false,
            answerSubmitted: false,
            timestamp: block.timestamp
        });

        for (uint256 i = 0; i < 3; i++) {
            platinumOracleVoted[msg.sender][oracles3[i]] = false;
        }

        emit PlatinumRequested(msg.sender, _desiredMaxSwap, msg.value, oracles3);
    }

    function publishCaptchaChallenge(
        address _user,
        string calldata _challengeText,
        bytes32 _answerHash
    ) external {
        PlatinumRequest storage req = platinumRequests[_user];
        require(!req.resolved, "Already resolved");
        require(!req.challengeSet, "Challenge already set");
        require(
            msg.sender == req.assignedOracles[0] ||
            msg.sender == req.assignedOracles[1] ||
            msg.sender == req.assignedOracles[2],
            "Not assigned oracle"
        );

        req.challengeText = _challengeText;
        req.answerHash = _answerHash;
        req.challengeSet = true;

        emit CaptchaChallengePublished(_user, _challengeText, _answerHash);
    }

    function submitCaptchaAnswer(string calldata _answer) external {
        PlatinumRequest storage req = platinumRequests[msg.sender];
        require(req.challengeSet, "No challenge set");
        require(!req.resolved, "Already resolved");
        require(!req.answerSubmitted, "Answer already submitted");

        req.answerSubmitted = true;
        emit CaptchaAnswerSubmitted(msg.sender, keccak256(abi.encodePacked(_answer)));
    }

    function submitCaptchaVerdict(address _user, bool _isValid) external nonReentrant {
        PlatinumRequest storage req = platinumRequests[_user];
        require(!req.resolved, "Already resolved");
        require(req.answerSubmitted, "User hasn't answered yet");
        require(
            msg.sender == req.assignedOracles[0] ||
            msg.sender == req.assignedOracles[1] ||
            msg.sender == req.assignedOracles[2],
            "Not assigned oracle"
        );
        require(!platinumOracleVoted[_user][msg.sender], "Already voted");

        platinumOracleVoted[_user][msg.sender] = true;
        req.totalVotes++;

        if (_isValid) {
            req.approveVotes++;
        } else {
            req.rejectVotes++;
        }

        emit CaptchaVerdictSubmitted(_user, msg.sender, _isValid);

        try this._payOracleReward(msg.sender) {} catch {}

        if (req.approveVotes >= 2) {
            req.resolved = true;
            platinumStake[_user] = req.stakeDeposited;
            userProfiles[_user].tier = DataTypes.Tier.Platinum;

            if (address(tierSystem) != address(0)) {
                tierSystem.syncUserData(
                    _user,
                    userProfiles[_user].totalSwaps,
                    userProfiles[_user].avgFraudScore
                );
            }

            emit PlatinumUpgradeResult(_user, true, req.approveVotes, req.rejectVotes);
        } else if (req.rejectVotes >= 2) {
            req.resolved = true;
            uint256 stakeToReturn = req.stakeDeposited;
            req.stakeDeposited = 0;
            totalPlatinumStake -= stakeToReturn;

            (bool sent, ) = _user.call{value: stakeToReturn}("");
            require(sent, "Stake refund failed");

            emit PlatinumUpgradeResult(_user, false, req.approveVotes, req.rejectVotes);
        }
    }

    function getPlatinumRequest(address _user) external view returns (
        uint256 desiredMaxSwap,
        uint256 stakeDeposited,
        address[3] memory assignedOracles,
        string memory challengeText,
        uint8 approveVotes,
        uint8 rejectVotes,
        bool resolved,
        bool challengeSet,
        bool answerSubmitted
    ) {
        PlatinumRequest storage req = platinumRequests[_user];
        return (
            req.desiredMaxSwap,
            req.stakeDeposited,
            req.assignedOracles,
            req.challengeText,
            req.approveVotes,
            req.rejectVotes,
            req.resolved,
            req.challengeSet,
            req.answerSubmitted
        );
    }

    function setAlphaStakeBps(uint256 _val) external onlyOwner {
        alphaStakeBps = _val;
        emit ParameterUpdated("alphaStakeBps", _val);
    }

    function publishClaimCaptcha(
        uint256 _claimId,
        string calldata _challengeText,
        bytes32 _answerHash
    ) external {
        ClaimCaptcha storage cc = claimCaptchas[_claimId];
        require(!cc.resolved, "Already resolved");
        require(!cc.challengeSet, "Challenge already set");
        require(
            msg.sender == cc.assignedOracles[0] ||
            msg.sender == cc.assignedOracles[1] ||
            msg.sender == cc.assignedOracles[2],
            "Not assigned oracle"
        );

        cc.challengeText = _challengeText;
        cc.answerHash = _answerHash;
        cc.challengeSet = true;

        emit ClaimCaptchaChallengePublished(_claimId, _challengeText, _answerHash);
    }

    function submitClaimCaptchaAnswer(uint256 _claimId, string calldata _answer) external {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        require(claimsArray[_claimId].user == msg.sender, "Not your claim");
        ClaimCaptcha storage cc = claimCaptchas[_claimId];
        require(cc.challengeSet, "No challenge set");
        require(!cc.resolved, "Already resolved");
        require(!cc.answerSubmitted, "Already answered");

        cc.answerSubmitted = true;
        emit ClaimCaptchaAnswerSubmitted(_claimId, msg.sender, keccak256(abi.encodePacked(_answer)));
    }

    function submitClaimCaptchaVerdict(uint256 _claimId, bool _isValid) external nonReentrant {
        ClaimCaptcha storage cc = claimCaptchas[_claimId];
        require(!cc.resolved, "Already resolved");
        require(cc.answerSubmitted, "User hasn't answered");
        require(
            msg.sender == cc.assignedOracles[0] ||
            msg.sender == cc.assignedOracles[1] ||
            msg.sender == cc.assignedOracles[2],
            "Not assigned oracle"
        );
        require(!claimCaptchaVoted[_claimId][msg.sender], "Already voted");

        claimCaptchaVoted[_claimId][msg.sender] = true;
        cc.totalVotes++;

        if (_isValid) {
            cc.approveVotes++;
        } else {
            cc.rejectVotes++;
        }

        emit ClaimCaptchaVerdictSubmitted(_claimId, msg.sender, _isValid);

        try this._payOracleReward(msg.sender) {} catch {}

        if (cc.approveVotes >= 2) {
            cc.resolved = true;
            DataTypes.Claim storage claim = claimsArray[_claimId];
            claim.status = DataTypes.ClaimStatus.Approved;
            userProfiles[claim.user].approvedClaims++;

            uint256 coverPercent = coveragePercentBps[claim.coverageLevel];
            uint256 payout = (claim.loss * coverPercent) / 10000;
            uint256 totalPayout = payout + gasRefundAmount;

            require(token.transfer(claim.user, totalPayout), "Payout failed");

            emit PayoutIssued(_claimId, claim.user, payout);
            if (gasRefundAmount > 0) {
                emit GasRefundIssued(_claimId, claim.user, gasRefundAmount);
            }
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Approved, claim.finalFraudScore, claim.dispersione);
            if (claim.botAddress != address(0)) {
                botAttackCount[claim.botAddress]++;
                botTotalDamage[claim.botAddress] += claim.loss;
                if (!botBlacklisted[claim.botAddress]) {
                    botBlacklisted[claim.botAddress] = true;
                    if (!isInBotBlacklist[claim.botAddress]) {
                        isInBotBlacklist[claim.botAddress] = true;
                        blacklistedBots.push(claim.botAddress);
                    }
                    emit BotBlacklisted(claim.botAddress, botAttackCount[claim.botAddress], botTotalDamage[claim.botAddress]);
                }
            }
        } else if (cc.rejectVotes >= 2) {
            cc.resolved = true;
            DataTypes.Claim storage claim = claimsArray[_claimId];
            claim.status = DataTypes.ClaimStatus.Rejected;
            userProfiles[claim.user].rejectedClaims++;
            emit ClaimFinalized(_claimId, DataTypes.ClaimStatus.Rejected, claim.finalFraudScore, claim.dispersione);
        }
    }

    function getClaimCaptcha(uint256 _claimId) external view returns (
        address[3] memory assignedOracles,
        string memory challengeText,
        uint8 approveVotes,
        uint8 rejectVotes,
        bool resolved,
        bool challengeSet,
        bool answerSubmitted
    ) {
        ClaimCaptcha storage cc = claimCaptchas[_claimId];
        return (cc.assignedOracles, cc.challengeText, cc.approveVotes, cc.rejectVotes, cc.resolved, cc.challengeSet, cc.answerSubmitted);
    }

    function addWhitelist(address[] calldata _addrs) external onlyOwner {
        for (uint256 i = 0; i < _addrs.length; i++) {
            address addr = _addrs[i];
            if (!isInWhitelist[addr]) {
                isInWhitelist[addr] = true;
                whitelistedAddresses.push(addr);
            }
        }
        emit WhitelistAdded(_addrs);
    }

    function removeWhitelist(address _addr) external onlyOwner {
        require(isInWhitelist[_addr], "Address not in whitelist");

        isInWhitelist[_addr] = false;
        uint256 length = whitelistedAddresses.length;
        for (uint256 i = 0; i < length; i++) {
            if (whitelistedAddresses[i] == _addr) {
                whitelistedAddresses[i] = whitelistedAddresses[length - 1];
                whitelistedAddresses.pop();
                break;
            }
        }
        emit WhitelistRemoved(_addr);
    }

    function getBlacklistedBots() external view returns (address[] memory) {
        return blacklistedBots;
    }

    function getBlacklistedUsers() external view returns (address[] memory) {
        return blacklistedUsers;
    }

    function getWhitelist() external view returns (address[] memory) {
        return whitelistedAddresses;
    }

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

    function getClaimDetails(uint256 _claimId) external view returns (
        address user,
        bytes32 txHash1,
        bytes32 txHash2,
        bytes32 txHash3,
        uint256 swapValue,
        uint256 loss,
        address botAddress,
        bool secondaryReview
    ) {
        require(_claimId < claimsArray.length, "Invalid claim ID");
        DataTypes.Claim storage c = claimsArray[_claimId];
        return (c.user, c.txHash1, c.txHash2, c.txHash3, c.swapValue, c.loss, c.botAddress, c.secondaryReview);
    }

    function getPremiumEstimate(uint256 _swapValue, DataTypes.CoverageLevel _coverageLevel)
        external
        view
        returns (uint256 premium)
    {
        if (address(premiumCalculator) != address(0)) {
            premium = premiumCalculator.calculatePremium(_swapValue, _coverageLevel);
        }
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
        tierSystem = ITierSystem(_tierSystem);
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

    function setInactivityPenalty(uint256 _val) external onlyOwner {
        inactivityPenalty = _val;
        emit ParameterUpdated("inactivityPenalty", _val);
    }

    function setGasRefundAmount(uint256 _val) external onlyOwner {
        gasRefundAmount = _val;
        emit ParameterUpdated("gasRefundAmount", _val);
    }

    function setUserTier(address _user, DataTypes.Tier _tier) external onlyOwner {
        userProfiles[_user].tier = _tier;
    }

    function _isAssignedOracle(uint256 _claimId, address _oracle) internal view returns (bool) {
        address[] storage oracles = claimsArray[_claimId].assignedOracles;
        for (uint256 i = 0; i < oracles.length; i++) {
            if (oracles[i] == _oracle) return true;
        }
        return false;
    }

    function _calculateMedian(uint8[] storage _scores) internal view returns (uint256) {
        uint256 len = _scores.length;
        require(len > 0, "No scores");

        uint8[] memory sorted = new uint8[](len);
        for (uint256 i = 0; i < len; i++) {
            sorted[i] = _scores[i];
        }

        for (uint256 i = 1; i < len; i++) {
            uint8 key = sorted[i];
            uint256 j = i;
            while (j > 0 && sorted[j - 1] > key) {
                sorted[j] = sorted[j - 1];
                j--;
            }
            sorted[j] = key;
        }

        if (len % 2 == 1) {
            return uint256(sorted[len / 2]);
        } else {
            return (uint256(sorted[len / 2 - 1]) + uint256(sorted[len / 2])) / 2;
        }
    }

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

    // Must be called via `this._payOracleReward(oracle)` for try/catch compatibility.
    // NOT nonReentrant: callers hold the guard; adding it here would revert.
    function _payOracleReward(address oracle) external {
        require(msg.sender == address(this), "Only internal");

        uint256 baseReward = oracleRegistry.rClaim();
        uint256 bps = oracleRegistry.getRewardPercentBps(oracle);
        uint256 reward = (baseReward * bps) / 10000;

        if (reward == 0) {
            emit OracleRewardSkipped(oracle, "zero reward");
            return;
        }

        if (address(this).balance < totalPlatinumStake + reward) {
            emit OracleRewardSkipped(oracle, "insufficient pool ETH");
            return;
        }

        (bool sent, ) = oracle.call{value: reward}("");
        if (!sent) {
            emit OracleRewardSkipped(oracle, "transfer failed");
            return;
        }

        emit OracleRewarded(oracle, reward);
    }

   
    function rebalanceToEth(uint256 meviAmount) external payable onlyOwner nonReentrant {
        require(msg.value > 0, "Must send ETH");
        require(meviAmount > 0, "meviAmount must be > 0");
        require(token.balanceOf(address(this)) >= meviAmount, "Pool MEVI insufficient");

        require(token.transfer(owner(), meviAmount), "MEVI transfer failed");
        emit PoolRebalanced(meviAmount, msg.value);
    }

    function getPoolEthBalance() external view returns (uint256) {
        if (address(this).balance < totalPlatinumStake) return 0;
        return address(this).balance - totalPlatinumStake;
    }

    receive() external payable {}
}
