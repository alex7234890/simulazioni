// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";

/**
 * @title OracleRegistry
 * @dev Manages oracle registration, activation, staking, watchlist, and selection.
 *
 * Lifecycle: register (Pending) -> activate (Active) -> withdraw (cooldown) -> exit
 *
 * Stake scaling: BaseStake * log2(1 + numActiveOracles)
 * Watchlist: after kwatchlist deviations >= deltaWatchlist points
 * Periodic reset: every Treset for active oracles
 * Selection: pseudo-random from active pool, excludes watchlisted for jury duty
 */
contract OracleRegistry is Ownable, ReentrancyGuard {
    using DataTypes for *;

    constructor() Ownable(msg.sender) {}

    // -------------------------------------------------------
    //  Protocol Parameters (configurable, initialized to defaults from Table 8)
    // -------------------------------------------------------

    /// @dev Base stake required for first oracle (in wei)
    uint256 public baseStake = 0.1 ether;

    /// @dev Activation delay after registration
    uint256 public tActivation = 7 days;

    /// @dev Cooldown period before stake withdrawal
    uint256 public tCooldown = 30 days;

    /// @dev Number of deviations to trigger watchlist
    uint256 public kWatchlist = 2;

    /// @dev Minimum deviation to count as a watchlist strike (in fraud score points)
    uint256 public deltaWatchlist = 10;

    /// @dev Periodic reset interval for deviation scores
    uint256 public tReset = 30 days;

    /// @dev Reward per claim evaluation (in wei)
    uint256 public rClaim = 0.002 ether;

    /// @dev Watchlist reward penalty in basis points (e.g., 5000 = 50%)
    uint256 public watchlistPenaltyBps = 5000;

    // -------------------------------------------------------
    //  State
    // -------------------------------------------------------

    /// @dev Oracle data by address
    mapping(address => DataTypes.OracleInfo) public oracleData;

    /// @dev List of all registered oracle addresses (for iteration and selection)
    address[] public oracleList;

    /// @dev Quick lookup: is address in oracleList
    mapping(address => bool) public isOracle;

    /// @dev Count of currently active oracles
    uint256 public activeOracleCount;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event OracleRegistered(address indexed oracle, uint256 stake);
    event OracleActivated(address indexed oracle);
    event OracleWithdrawRequested(address indexed oracle, uint256 cooldownEnd);
    event OracleWithdrawn(address indexed oracle, uint256 stakeReturned);
    event OracleWatchlisted(address indexed oracle, uint256 deviationScore);
    event OracleDeviationRecorded(address indexed oracle, uint256 newDeviationScore);
    event OracleScoreReset(address indexed oracle);
    event OracleSlashed(address indexed oracle, uint256 slashAmount);
    event OracleExpelled(address indexed oracle);
    event OracleReintegrated(address indexed oracle, uint256 newStake);
    event OracleRewarded(address indexed oracle, uint256 amount);
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Modifiers
    // -------------------------------------------------------

    modifier onlyActiveOracle() {
        require(
            oracleData[msg.sender].status == DataTypes.OracleStatus.Active,
            "Not an active oracle"
        );
        _;
    }

    // -------------------------------------------------------
    //  Oracle Lifecycle
    // -------------------------------------------------------

    /**
     * @dev Register as an oracle by staking ETH.
     * Minimum stake scales logarithmically: baseStake * log2(1 + activeOracleCount).
     * Initial status = Pending.
     */
    function registerOracle() external payable nonReentrant {
        require(!isOracle[msg.sender], "Already registered");
        require(oracleData[msg.sender].status != DataTypes.OracleStatus.Expelled, "Expelled oracle");

        uint256 minStake = getMinimumStake();
        require(msg.value >= minStake, "Insufficient stake");

        oracleData[msg.sender] = DataTypes.OracleInfo({
            stake: msg.value,
            status: DataTypes.OracleStatus.Pending,
            registrationTime: block.timestamp,
            activationTime: 0,
            deviationScore: 0,
            watchlistPosition: 0,
            lastResetTime: block.timestamp,
            claimsEvaluated: 0,
            withdrawRequestTime: 0
        });

        oracleList.push(msg.sender);
        isOracle[msg.sender] = true;

        emit OracleRegistered(msg.sender, msg.value);
    }

    /**
     * @dev Activate oracle after the activation delay has passed.
     * Transitions from Pending to Active.
     */
    function activateOracle() external {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(info.status == DataTypes.OracleStatus.Pending, "Not in Pending status");
        require(
            block.timestamp >= info.registrationTime + tActivation,
            "Activation delay not passed"
        );

        info.status = DataTypes.OracleStatus.Active;
        info.activationTime = block.timestamp;
        info.lastResetTime = block.timestamp;
        activeOracleCount++;

        emit OracleActivated(msg.sender);
    }

    /**
     * @dev Request stake withdrawal. Starts the cooldown timer.
     * Oracle must be Active or Watchlisted.
     */
    function withdrawOracle() external {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Cannot withdraw in current status"
        );
        require(info.withdrawRequestTime == 0, "Withdrawal already requested");

        info.withdrawRequestTime = block.timestamp;

        emit OracleWithdrawRequested(msg.sender, block.timestamp + tCooldown);
    }

    /**
     * @dev Complete withdrawal after cooldown. Returns staked ETH.
     */
    function completeWithdrawal() external nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(info.withdrawRequestTime > 0, "No withdrawal requested");
        require(
            block.timestamp >= info.withdrawRequestTime + tCooldown,
            "Cooldown not complete"
        );

        uint256 stakeToReturn = info.stake;

        // Decrease active count if oracle was active/watchlisted
        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        info.stake = 0;
        info.status = DataTypes.OracleStatus.Inactive;
        info.withdrawRequestTime = 0;

        (bool sent, ) = msg.sender.call{value: stakeToReturn}("");
        require(sent, "ETH transfer failed");

        emit OracleWithdrawn(msg.sender, stakeToReturn);
    }

    // -------------------------------------------------------
    //  Deviation & Watchlist Management
    // -------------------------------------------------------

    /**
     * @dev Record a deviation for an oracle. Called internally by ClaimManager
     * when an oracle's score deviates significantly from the median.
     * @param _oracle Address of the oracle
     */
    function recordDeviation(address _oracle) external onlyOwner {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );

        info.deviationScore++;
        emit OracleDeviationRecorded(_oracle, info.deviationScore);

        // Move to watchlist if threshold reached
        if (info.deviationScore >= kWatchlist &&
            info.status == DataTypes.OracleStatus.Active) {
            info.status = DataTypes.OracleStatus.Watchlisted;
            info.watchlistPosition = block.timestamp; // Use timestamp as position marker
            emit OracleWatchlisted(_oracle, info.deviationScore);
        }
    }

    /**
     * @dev Reset deviation score for an oracle. Callable after Treset period.
     * @param _oracle Address of the oracle to reset
     */
    function resetDeviationScore(address _oracle) external {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );
        require(
            block.timestamp >= info.lastResetTime + tReset,
            "Reset period not elapsed"
        );

        info.deviationScore = 0;
        info.lastResetTime = block.timestamp;

        // Remove from watchlist if currently watchlisted
        if (info.status == DataTypes.OracleStatus.Watchlisted) {
            info.status = DataTypes.OracleStatus.Active;
            info.watchlistPosition = 0;
        }

        emit OracleScoreReset(_oracle);
    }

    // -------------------------------------------------------
    //  Slashing (called by SlashingSystem)
    // -------------------------------------------------------

    /**
     * @dev Slash an oracle's stake. Called by owner (SlashingSystem contract).
     * @param _oracle Oracle to slash
     * @param _amount Amount to slash from stake
     */
    function slashOracle(address _oracle, uint256 _amount) external onlyOwner nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(info.stake >= _amount, "Slash exceeds stake");

        info.stake -= _amount;
        info.status = DataTypes.OracleStatus.Slashed;

        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        emit OracleSlashed(_oracle, _amount);
    }

    /**
     * @dev Expel an oracle permanently. Called by owner (SlashingSystem).
     * @param _oracle Oracle to expel
     */
    function expelOracle(address _oracle) external onlyOwner {
        DataTypes.OracleInfo storage info = oracleData[_oracle];

        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        info.status = DataTypes.OracleStatus.Expelled;

        emit OracleExpelled(_oracle);
    }

    /**
     * @dev Allow a slashed oracle to reintegrate by paying slash debt + new full stake.
     */
    function reintegrateOracle() external payable nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(info.status == DataTypes.OracleStatus.Slashed, "Not slashed");

        uint256 minStake = getMinimumStake();
        require(msg.value >= minStake, "Insufficient new stake");

        info.stake = msg.value;
        info.status = DataTypes.OracleStatus.Pending;
        info.registrationTime = block.timestamp;
        info.activationTime = 0;
        info.deviationScore = 0;
        info.watchlistPosition = 0;
        info.lastResetTime = block.timestamp;
        info.withdrawRequestTime = 0;

        emit OracleReintegrated(msg.sender, msg.value);
    }

    // -------------------------------------------------------
    //  Oracle Selection
    // -------------------------------------------------------

    /**
     * @dev Select n active oracles pseudo-randomly. Excludes watchlisted oracles.
     * @param _seed Random seed (e.g., blockhash-based)
     * @param _n Number of oracles to select
     * @return selected Array of selected oracle addresses
     */
    function selectOracles(uint256 _seed, uint256 _n)
        external
        view
        returns (address[] memory selected)
    {
        // Build pool of eligible oracles (Active only, not Watchlisted)
        address[] memory eligible = new address[](oracleList.length);
        uint256 eligibleCount = 0;

        for (uint256 i = 0; i < oracleList.length; i++) {
            DataTypes.OracleInfo storage info = oracleData[oracleList[i]];
            if (info.status == DataTypes.OracleStatus.Active &&
                info.withdrawRequestTime == 0) {
                eligible[eligibleCount] = oracleList[i];
                eligibleCount++;
            }
        }

        require(eligibleCount >= _n, "Not enough eligible oracles");

        selected = new address[](_n);
        uint256 remaining = eligibleCount;

        for (uint256 i = 0; i < _n; i++) {
            // Fisher-Yates inspired selection
            uint256 idx = uint256(keccak256(abi.encodePacked(_seed, i))) % remaining;
            selected[i] = eligible[idx];
            // Swap selected with last eligible to avoid re-selection
            eligible[idx] = eligible[remaining - 1];
            remaining--;
        }
    }

    // -------------------------------------------------------
    //  Reward Distribution
    // -------------------------------------------------------

    /**
     * @dev Pay reward to an oracle for claim evaluation. Watchlisted oracles
     * receive a penalized reward. Called by owner (ClaimManager).
     * @param _oracle Oracle to reward
     */
    function rewardOracle(address _oracle) external onlyOwner nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Oracle not eligible for reward"
        );

        info.claimsEvaluated++;

        uint256 reward = rClaim;
        if (info.status == DataTypes.OracleStatus.Watchlisted) {
            // Penalized reward: reward * (1 - penalty%)
            reward = (reward * (10000 - watchlistPenaltyBps)) / 10000;
        }

        // Reward paid from contract balance
        require(address(this).balance >= reward, "Insufficient reward funds");

        (bool sent, ) = _oracle.call{value: reward}("");
        require(sent, "Reward transfer failed");

        emit OracleRewarded(_oracle, reward);
    }

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    /**
     * @dev Calculate minimum stake with logarithmic scaling.
     * minStake = baseStake * log2(1 + activeOracleCount)
     * For 0 active oracles: returns baseStake (log2(1) = 0, so we use max(1, ...))
     */
    function getMinimumStake() public view returns (uint256) {
        if (activeOracleCount == 0) {
            return baseStake;
        }
        uint256 logValue = _log2(1 + activeOracleCount);
        if (logValue == 0) logValue = 1;
        return baseStake * logValue;
    }

    /**
     * @dev Get oracle info for an address.
     */
    function getOracleInfo(address _oracle) external view returns (
        uint256 stake,
        DataTypes.OracleStatus status,
        uint256 registrationTime,
        uint256 activationTime,
        uint256 deviationScore,
        uint256 claimsEvaluated
    ) {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        return (
            info.stake,
            info.status,
            info.registrationTime,
            info.activationTime,
            info.deviationScore,
            info.claimsEvaluated
        );
    }

    /**
     * @dev Get total number of registered oracles.
     */
    function getOracleCount() external view returns (uint256) {
        return oracleList.length;
    }

    /**
     * @dev Check if an oracle is eligible for claim evaluation.
     */
    function isEligible(address _oracle) external view returns (bool) {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        return info.status == DataTypes.OracleStatus.Active &&
               info.withdrawRequestTime == 0;
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setBaseStake(uint256 _val) external onlyOwner {
        baseStake = _val;
        emit ParameterUpdated("baseStake", _val);
    }

    function setTActivation(uint256 _val) external onlyOwner {
        tActivation = _val;
        emit ParameterUpdated("tActivation", _val);
    }

    function setTCooldown(uint256 _val) external onlyOwner {
        tCooldown = _val;
        emit ParameterUpdated("tCooldown", _val);
    }

    function setKWatchlist(uint256 _val) external onlyOwner {
        kWatchlist = _val;
        emit ParameterUpdated("kWatchlist", _val);
    }

    function setDeltaWatchlist(uint256 _val) external onlyOwner {
        deltaWatchlist = _val;
        emit ParameterUpdated("deltaWatchlist", _val);
    }

    function setRClaim(uint256 _val) external onlyOwner {
        rClaim = _val;
        emit ParameterUpdated("rClaim", _val);
    }

    // -------------------------------------------------------
    //  Internal Utilities
    // -------------------------------------------------------

    /**
     * @dev Integer log base 2 (floor). Returns 0 for input 0 or 1.
     */
    function _log2(uint256 x) internal pure returns (uint256 result) {
        if (x <= 1) return 0;
        result = 0;
        while (x > 1) {
            x >>= 1;
            result++;
        }
    }

    /// @dev Allow contract to receive ETH (for reward funding)
    receive() external payable {}
}
