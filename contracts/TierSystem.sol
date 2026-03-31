// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "./libraries/DataTypes.sol";

/**
 * @title TierSystem
 * @dev Manages user tier upgrades, Platinum requests, blacklist, and debt.
 *
 * Tier requirements (PDF Table 8):
 *   Bronze -> Silver: 18 swaps, 30 days active, avg fraud score < 52
 *   Silver -> Gold:   55 swaps, 60 days active, avg fraud score < 35
 *   Gold -> Platinum: CAPTCHA verification + stake (20% of max insurable)
 *
 * Tier limits:
 *   Bronze/Silver: max 3 swaps/day
 *   Gold: max 4 swaps/day
 *   Platinum: unlimited
 *
 * Blacklist:
 *   Flagged users must pay debt (20% penalty on loss) to be un-blacklisted.
 *
 * All percentages in basis points (10000 = 100%).
 */
contract TierSystem is Ownable, ReentrancyGuard {
    using DataTypes for *;

    // -------------------------------------------------------
    //  External contract reference
    // -------------------------------------------------------

    /// @dev Reference to the main insurance contract for reading user profiles
    address public insuranceContract;

    /// @dev Token for debt payments and Platinum stake
    IERC20 public token;

    // -------------------------------------------------------
    //  Tier Upgrade Requirements
    // -------------------------------------------------------

    /// @dev Minimum swaps for Bronze -> Silver
    uint256 public silverMinSwaps = 18;

    /// @dev Minimum membership days for Bronze -> Silver
    uint256 public silverMinDays = 30 days;

    /// @dev Maximum avg fraud score for Bronze -> Silver
    uint256 public silverMaxFraudScore = 52;

    /// @dev Minimum swaps for Silver -> Gold
    uint256 public goldMinSwaps = 55;

    /// @dev Minimum membership days for Silver -> Gold
    uint256 public goldMinDays = 60 days;

    /// @dev Maximum avg fraud score for Silver -> Gold
    uint256 public goldMaxFraudScore = 35;

    /// @dev Platinum stake percentage of max insurable in basis points (2000 = 20%)
    uint256 public platinumStakeBps = 2000;

    /// @dev Blacklist penalty percentage in basis points (2000 = 20%)
    uint256 public blacklistPenaltyBps = 2000;

    // -------------------------------------------------------
    //  State
    // -------------------------------------------------------

    /// @dev User tiers (authoritative source)
    mapping(address => DataTypes.Tier) public userTiers;

    /// @dev Registration timestamp per user (for membership duration)
    mapping(address => uint256) public registrationTime;

    /// @dev Whether user is registered in this system
    mapping(address => bool) public isRegistered;

    /// @dev User total swaps (synced from insurance or tracked here)
    mapping(address => uint256) public userTotalSwaps;

    /// @dev User average fraud score (synced from insurance)
    mapping(address => uint256) public userAvgFraudScore;

    /// @dev Blacklisted users
    mapping(address => bool) public isBlacklisted;

    /// @dev Outstanding debt per user
    mapping(address => uint256) public userDebt;

    /// @dev Platinum stake deposits
    mapping(address => uint256) public platinumStakes;

    /// @dev Platinum CAPTCHA verified flag (set by owner after off-chain check)
    mapping(address => bool) public platinumCaptchaVerified;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event UserTierRegistered(address indexed user, DataTypes.Tier tier);
    event TierUpgraded(address indexed user, DataTypes.Tier fromTier, DataTypes.Tier toTier);
    event PlatinumRequested(address indexed user, uint256 stakeAmount);
    event PlatinumCaptchaVerified(address indexed user);
    event PlatinumGranted(address indexed user);
    event UserBlacklisted(address indexed user, uint256 debt);
    event DebtPaid(address indexed user, uint256 amount, uint256 remaining);
    event UserUnblacklisted(address indexed user);
    event UserDataSynced(address indexed user, uint256 totalSwaps, uint256 avgFraudScore);
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor(address _token) Ownable(msg.sender) {
        token = IERC20(_token);
    }

    // -------------------------------------------------------
    //  User Registration (called by insurance contract or owner)
    // -------------------------------------------------------

    /**
     * @dev Register a user in the tier system with Bronze tier.
     * @param _user Address to register
     */
    function registerUser(address _user) external onlyOwner {
        require(!isRegistered[_user], "Already registered");

        isRegistered[_user] = true;
        userTiers[_user] = DataTypes.Tier.Bronze;
        registrationTime[_user] = block.timestamp;

        emit UserTierRegistered(_user, DataTypes.Tier.Bronze);
    }

    // -------------------------------------------------------
    //  Data Sync (from MEVInsurance)
    // -------------------------------------------------------

    /**
     * @dev Sync user statistics from the insurance contract.
     * Called by owner (or insurance contract) to update swap/fraud data.
     * @param _user User address
     * @param _totalSwaps Total swap count
     * @param _avgFraudScore Average fraud score
     */
    function syncUserData(
        address _user,
        uint256 _totalSwaps,
        uint256 _avgFraudScore
    ) external onlyOwner {
        require(isRegistered[_user], "User not registered");

        userTotalSwaps[_user] = _totalSwaps;
        userAvgFraudScore[_user] = _avgFraudScore;

        emit UserDataSynced(_user, _totalSwaps, _avgFraudScore);
    }

    // -------------------------------------------------------
    //  Tier Upgrade Logic
    // -------------------------------------------------------

    /**
     * @dev Check if a user qualifies for a tier upgrade and apply it.
     * Bronze -> Silver: 18 swaps, 30 days, avgFraud < 52
     * Silver -> Gold: 55 swaps, 60 days, avgFraud < 35
     * @param _user Address to check
     */
    function checkAndUpgrade(address _user) external {
        require(isRegistered[_user], "User not registered");
        require(!isBlacklisted[_user], "User is blacklisted");

        DataTypes.Tier currentTier = userTiers[_user];
        uint256 membershipDuration = block.timestamp - registrationTime[_user];
        uint256 swaps = userTotalSwaps[_user];
        uint256 fraudScore = userAvgFraudScore[_user];

        if (currentTier == DataTypes.Tier.Bronze) {
            require(swaps >= silverMinSwaps, "Not enough swaps for Silver");
            require(membershipDuration >= silverMinDays, "Not enough time for Silver");
            require(fraudScore < silverMaxFraudScore, "Fraud score too high for Silver");

            userTiers[_user] = DataTypes.Tier.Silver;
            emit TierUpgraded(_user, DataTypes.Tier.Bronze, DataTypes.Tier.Silver);

        } else if (currentTier == DataTypes.Tier.Silver) {
            require(swaps >= goldMinSwaps, "Not enough swaps for Gold");
            require(membershipDuration >= goldMinDays, "Not enough time for Gold");
            require(fraudScore < goldMaxFraudScore, "Fraud score too high for Gold");

            userTiers[_user] = DataTypes.Tier.Gold;
            emit TierUpgraded(_user, DataTypes.Tier.Silver, DataTypes.Tier.Gold);

        } else {
            revert("No automatic upgrade available");
        }
    }

    // -------------------------------------------------------
    //  Platinum Request
    // -------------------------------------------------------

    /**
     * @dev Request Platinum tier upgrade. Requires Gold tier + token stake.
     * Stake amount = 20% of max insurable value (configurable).
     * After staking, user must pass off-chain CAPTCHA (verified by owner).
     * @param _maxInsurable The max insurable value for stake calculation
     */
    function requestPlatinum(uint256 _maxInsurable) external nonReentrant {
        require(isRegistered[msg.sender], "User not registered");
        require(!isBlacklisted[msg.sender], "User is blacklisted");
        require(userTiers[msg.sender] == DataTypes.Tier.Gold, "Must be Gold tier");
        require(platinumStakes[msg.sender] == 0, "Already requested");

        uint256 stakeAmount = (_maxInsurable * platinumStakeBps) / 10000;
        require(stakeAmount > 0, "Stake must be > 0");

        // Effects
        platinumStakes[msg.sender] = stakeAmount;

        // Interaction
        require(
            token.transferFrom(msg.sender, address(this), stakeAmount),
            "Stake transfer failed"
        );

        emit PlatinumRequested(msg.sender, stakeAmount);
    }

    /**
     * @dev Owner verifies CAPTCHA completion for Platinum request.
     * @param _user User who passed CAPTCHA
     */
    function verifyCaptcha(address _user) external onlyOwner {
        require(isRegistered[_user], "User not registered");
        require(platinumStakes[_user] > 0, "No Platinum request pending");
        require(!platinumCaptchaVerified[_user], "Already verified");

        platinumCaptchaVerified[_user] = true;

        emit PlatinumCaptchaVerified(_user);
    }

    /**
     * @dev Finalize Platinum upgrade after CAPTCHA verification.
     * Can be called by the user or owner.
     * @param _user User to upgrade to Platinum
     */
    function finalizePlatinum(address _user) external {
        require(isRegistered[_user], "User not registered");
        require(userTiers[_user] == DataTypes.Tier.Gold, "Must be Gold tier");
        require(platinumStakes[_user] > 0, "No stake deposited");
        require(platinumCaptchaVerified[_user], "CAPTCHA not verified");

        userTiers[_user] = DataTypes.Tier.Platinum;

        emit PlatinumGranted(_user);
        emit TierUpgraded(_user, DataTypes.Tier.Gold, DataTypes.Tier.Platinum);
    }

    // -------------------------------------------------------
    //  Blacklist & Debt
    // -------------------------------------------------------

    /**
     * @dev Blacklist a user with a debt penalty.
     * Called by owner (from MEVInsurance when claim is rejected with high fraud).
     * @param _user User to blacklist
     * @param _loss The claimed loss amount (debt = loss * penalty%)
     */
    function blacklistUser(address _user, uint256 _loss) external onlyOwner {
        require(isRegistered[_user], "User not registered");

        isBlacklisted[_user] = true;
        uint256 debt = (_loss * blacklistPenaltyBps) / 10000;
        userDebt[_user] += debt;

        emit UserBlacklisted(_user, debt);
    }

    /**
     * @dev Pay off blacklist debt. User is un-blacklisted when debt reaches zero.
     * @param _amount Amount to pay
     */
    function payDebt(uint256 _amount) external nonReentrant {
        require(isBlacklisted[msg.sender], "Not blacklisted");
        require(_amount > 0, "Amount must be > 0");
        require(_amount <= userDebt[msg.sender], "Amount exceeds debt");

        // Effects
        userDebt[msg.sender] -= _amount;

        bool fullyPaid = userDebt[msg.sender] == 0;
        if (fullyPaid) {
            isBlacklisted[msg.sender] = false;
            emit UserUnblacklisted(msg.sender);
        }

        // Interaction
        require(
            token.transferFrom(msg.sender, address(this), _amount),
            "Debt payment failed"
        );

        emit DebtPaid(msg.sender, _amount, userDebt[msg.sender]);
    }

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    /**
     * @dev Get the tier for a user.
     */
    function getTier(address _user) external view returns (DataTypes.Tier) {
        return userTiers[_user];
    }

    /**
     * @dev Check if user can upgrade to Silver.
     */
    function canUpgradeToSilver(address _user) external view returns (bool) {
        if (!isRegistered[_user] || isBlacklisted[_user]) return false;
        if (userTiers[_user] != DataTypes.Tier.Bronze) return false;

        uint256 duration = block.timestamp - registrationTime[_user];
        return userTotalSwaps[_user] >= silverMinSwaps &&
               duration >= silverMinDays &&
               userAvgFraudScore[_user] < silverMaxFraudScore;
    }

    /**
     * @dev Check if user can upgrade to Gold.
     */
    function canUpgradeToGold(address _user) external view returns (bool) {
        if (!isRegistered[_user] || isBlacklisted[_user]) return false;
        if (userTiers[_user] != DataTypes.Tier.Silver) return false;

        uint256 duration = block.timestamp - registrationTime[_user];
        return userTotalSwaps[_user] >= goldMinSwaps &&
               duration >= goldMinDays &&
               userAvgFraudScore[_user] < goldMaxFraudScore;
    }

    /**
     * @dev Get max daily swaps allowed for a tier.
     */
    function getMaxDailySwaps(DataTypes.Tier _tier) external pure returns (uint256) {
        if (_tier == DataTypes.Tier.Bronze || _tier == DataTypes.Tier.Silver) return 3;
        if (_tier == DataTypes.Tier.Gold) return 4;
        return type(uint256).max; // Platinum = unlimited
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setSilverMinSwaps(uint256 _val) external onlyOwner {
        silverMinSwaps = _val;
        emit ParameterUpdated("silverMinSwaps", _val);
    }

    function setSilverMinDays(uint256 _val) external onlyOwner {
        silverMinDays = _val;
        emit ParameterUpdated("silverMinDays", _val);
    }

    function setSilverMaxFraudScore(uint256 _val) external onlyOwner {
        silverMaxFraudScore = _val;
        emit ParameterUpdated("silverMaxFraudScore", _val);
    }

    function setGoldMinSwaps(uint256 _val) external onlyOwner {
        goldMinSwaps = _val;
        emit ParameterUpdated("goldMinSwaps", _val);
    }

    function setGoldMinDays(uint256 _val) external onlyOwner {
        goldMinDays = _val;
        emit ParameterUpdated("goldMinDays", _val);
    }

    function setGoldMaxFraudScore(uint256 _val) external onlyOwner {
        goldMaxFraudScore = _val;
        emit ParameterUpdated("goldMaxFraudScore", _val);
    }

    function setPlatinumStakeBps(uint256 _val) external onlyOwner {
        platinumStakeBps = _val;
        emit ParameterUpdated("platinumStakeBps", _val);
    }

    function setBlacklistPenaltyBps(uint256 _val) external onlyOwner {
        blacklistPenaltyBps = _val;
        emit ParameterUpdated("blacklistPenaltyBps", _val);
    }

    function setInsuranceContract(address _insurance) external onlyOwner {
        insuranceContract = _insurance;
        emit ParameterUpdated("insuranceContract", uint256(uint160(_insurance)));
    }
}
