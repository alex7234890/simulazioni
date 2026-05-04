// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "./libraries/DataTypes.sol";

interface IMEVInsurance {
    function registeredUsers(address) external view returns (bool);
}

contract TierSystem is Ownable, ReentrancyGuard {
    using DataTypes for *;

    address public insuranceContract;
    IERC20 public token;

    uint256 public silverMinSwaps = 18;
    uint256 public silverMinDays = 30 days;
    uint256 public silverMaxFraudScore = 52;
    uint256 public goldMinSwaps = 55;
    uint256 public goldMinDays = 60 days;
    uint256 public goldMaxFraudScore = 35;
    uint256 public platinumStakeBps = 2000;
    uint256 public blacklistPenaltyBps = 2000;

    mapping(address => DataTypes.Tier) public userTiers;
    mapping(address => uint256) public registrationTime;
    mapping(address => bool) public isRegistered;
    mapping(address => uint256) public userTotalSwaps;
    mapping(address => uint256) public userAvgFraudScore;
    mapping(address => bool) public isBlacklisted;
    mapping(address => uint256) public userDebt;
    mapping(address => uint256) public platinumStakes;
    mapping(address => bool) public platinumCaptchaVerified;

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

    constructor(address _token) Ownable(msg.sender) {
        token = IERC20(_token);
    }

    function registerUser(address _user) public {
        if(!isRegistered[_user]) {
            isRegistered[_user] = true;
            userTiers[_user] = DataTypes.Tier.Bronze;
            registrationTime[_user] = block.timestamp;
            emit UserTierRegistered(_user, DataTypes.Tier.Bronze);
        }
    }

    function registerFromInsurance(address _user) external {
        require(msg.sender == insuranceContract || msg.sender == owner(), "Unauthorized");
        registerUser(_user);
    }

    function syncUserData(
        address _user,
        uint256 _totalSwaps,
        uint256 _avgFraudScore
    ) external {
        require(
            msg.sender == owner() || msg.sender == insuranceContract,
            "TierSystem: Unauthorized caller"
        );
        if (!isRegistered[_user]) {
            registerUser(_user);
        }
        userTotalSwaps[_user] = _totalSwaps;
        userAvgFraudScore[_user] = _avgFraudScore;
        emit UserDataSynced(_user, _totalSwaps, _avgFraudScore);
    }

    function checkAndUpgrade(address _user) external {
        if (!isRegistered[_user]) {
            registerUser(_user);
        }
        if (isBlacklisted[_user]) return;

        DataTypes.Tier currentTier = userTiers[_user];
        uint256 membershipDuration = block.timestamp - registrationTime[_user];
        uint256 swaps = userTotalSwaps[_user];
        uint256 fraudScore = userAvgFraudScore[_user];

        if (currentTier == DataTypes.Tier.Bronze) {
            if (
                swaps >= silverMinSwaps &&
                membershipDuration >= silverMinDays &&
                fraudScore < silverMaxFraudScore
            ) {
                userTiers[_user] = DataTypes.Tier.Silver;
                emit TierUpgraded(_user, DataTypes.Tier.Bronze, DataTypes.Tier.Silver);
            }
        } else if (currentTier == DataTypes.Tier.Silver) {
            if (
                swaps >= goldMinSwaps &&
                membershipDuration >= goldMinDays &&
                fraudScore < goldMaxFraudScore
            ) {
                userTiers[_user] = DataTypes.Tier.Gold;
                emit TierUpgraded(_user, DataTypes.Tier.Silver, DataTypes.Tier.Gold);
            }
        } else {
            return;
        }
    }

    function requestPlatinum(uint256 _maxInsurable) external nonReentrant {
        require(isRegistered[msg.sender], "User not registered");
        require(!isBlacklisted[msg.sender], "User is blacklisted");
        require(userTiers[msg.sender] == DataTypes.Tier.Gold, "Must be Gold tier");
        require(platinumStakes[msg.sender] == 0, "Already requested");

        uint256 stakeAmount = (_maxInsurable * platinumStakeBps) / 10000;
        require(stakeAmount > 0, "Stake must be > 0");

        platinumStakes[msg.sender] = stakeAmount;
        require(
            token.transferFrom(msg.sender, address(this), stakeAmount),
            "Stake transfer failed"
        );
        emit PlatinumRequested(msg.sender, stakeAmount);
    }

    function verifyCaptcha(address _user) external onlyOwner {
        require(isRegistered[_user], "User not registered");
        require(platinumStakes[_user] > 0, "No Platinum request pending");
        require(!platinumCaptchaVerified[_user], "Already verified");
        platinumCaptchaVerified[_user] = true;
        emit PlatinumCaptchaVerified(_user);
    }

    function finalizePlatinum(address _user) external {
        require(isRegistered[_user], "User not registered");
        require(userTiers[_user] == DataTypes.Tier.Gold, "Must be Gold tier");
        require(platinumStakes[_user] > 0, "No stake deposited");
        require(platinumCaptchaVerified[_user], "CAPTCHA not verified");
        userTiers[_user] = DataTypes.Tier.Platinum;
        emit PlatinumGranted(_user);
        emit TierUpgraded(_user, DataTypes.Tier.Gold, DataTypes.Tier.Platinum);
    }

    function blacklistUser(address _user, uint256 _loss) external onlyOwner {
        require(isRegistered[_user], "User not registered");
        isBlacklisted[_user] = true;
        uint256 debt = (_loss * blacklistPenaltyBps) / 10000;
        userDebt[_user] += debt;
        emit UserBlacklisted(_user, debt);
    }

    function payDebt(uint256 _amount) external nonReentrant {
        require(isBlacklisted[msg.sender], "Not blacklisted");
        require(_amount > 0, "Amount must be > 0");
        require(_amount <= userDebt[msg.sender], "Amount exceeds debt");

        userDebt[msg.sender] -= _amount;
        bool fullyPaid = userDebt[msg.sender] == 0;
        if (fullyPaid) {
            isBlacklisted[msg.sender] = false;
            emit UserUnblacklisted(msg.sender);
        }
        require(
            token.transferFrom(msg.sender, address(this), _amount),
            "Debt payment failed"
        );
        emit DebtPaid(msg.sender, _amount, userDebt[msg.sender]);
    }

    function getTier(address _user) external view returns (DataTypes.Tier) {
        return userTiers[_user];
    }

    function canUpgradeToSilver(address _user) external view returns (bool) {
        if (!isRegistered[_user] || isBlacklisted[_user]) return false;
        if (userTiers[_user] != DataTypes.Tier.Bronze) return false;
        uint256 duration = block.timestamp - registrationTime[_user];
        return userTotalSwaps[_user] >= silverMinSwaps &&
               duration >= silverMinDays &&
               userAvgFraudScore[_user] < silverMaxFraudScore;
    }

    function canUpgradeToGold(address _user) external view returns (bool) {
        if (!isRegistered[_user] || isBlacklisted[_user]) return false;
        if (userTiers[_user] != DataTypes.Tier.Silver) return false;
        uint256 duration = block.timestamp - registrationTime[_user];
        return userTotalSwaps[_user] >= goldMinSwaps &&
               duration >= goldMinDays &&
               userAvgFraudScore[_user] < goldMaxFraudScore;
    }

    function getMaxDailySwaps(DataTypes.Tier _tier) external pure returns (uint256) {
        if (_tier == DataTypes.Tier.Bronze || _tier == DataTypes.Tier.Silver) return 3;
        if (_tier == DataTypes.Tier.Gold) return 4;
        return type(uint256).max; // Platinum = unlimited
    }

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
