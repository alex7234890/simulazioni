// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";

contract OracleRegistry is Ownable, ReentrancyGuard {
    using DataTypes for *;

    constructor() Ownable(msg.sender) {}

    uint256 public baseStake = 0.1 ether;
    uint256 public tActivation = 7 days;
    uint256 public tCooldown = 30 days;
    uint256 public kWatchlist = 2;
    uint256 public deltaWatchlist = 10;
    uint256 public tReset = 30 days;
    uint256 public rClaim = 0.002 ether;
    uint256 public watchlistPenaltyBps = 5000;
    uint256 public tWatchlist = 90 days;

    mapping(address => DataTypes.OracleInfo) public oracleData;
    address[] public oracleList;
    mapping(address => bool) public isOracle;
    uint256 public activeOracleCount;
    mapping(address => bool) public authorizedCallers;

    address[] public watchlistOracles;
    mapping(address => bool) public isInWatchlist;

    event OracleRegistered(address indexed oracle, uint256 stake);
    event OracleActivated(address indexed oracle);
    event OracleWithdrawRequested(address indexed oracle, uint256 cooldownEnd);
    event OracleWithdrawn(address indexed oracle, uint256 stakeReturned);
    event OracleWatchlisted(address indexed oracle, uint256 deviationScore);
    event OracleDeviationRecorded(address indexed oracle, uint256 newDeviationScore);
    event OracleScoreReset(address indexed oracle);
    event OracleInactivityPenalized(address indexed oracle, uint256 amount);
    event OracleSlashed(address indexed oracle, uint256 slashAmount);
    event OracleExpelled(address indexed oracle);
    event OracleReintegrated(address indexed oracle, uint256 newStake);
    event ParameterUpdated(string param, uint256 value);

    modifier onlyActiveOracle() {
        require(
            oracleData[msg.sender].status == DataTypes.OracleStatus.Active,
            "Not an active oracle"
        );
        _;
    }

    modifier onlyOwnerOrAuthorized() {
        require(
            msg.sender == owner() || authorizedCallers[msg.sender],
            "Not owner or authorized"
        );
        _;
    }

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
            watchlistStrikes: 0,
            watchlistPosition: 0,
            lastResetTime: block.timestamp,
            claimsEvaluated: 0,
            withdrawRequestTime: 0
        });

        oracleList.push(msg.sender);
        isOracle[msg.sender] = true;

        emit OracleRegistered(msg.sender, msg.value);
    }

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

    function completeWithdrawal() external nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(info.withdrawRequestTime > 0, "No withdrawal requested");
        require(
            block.timestamp >= info.withdrawRequestTime + tCooldown,
            "Cooldown not complete"
        );

        uint256 stakeToReturn = info.stake;

        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        if (isInWatchlist[msg.sender]) _removeFromWatchlist(msg.sender);

        info.stake = 0;
        info.status = DataTypes.OracleStatus.Inactive;
        info.withdrawRequestTime = 0;

        (bool sent, ) = msg.sender.call{value: stakeToReturn}("");
        require(sent, "ETH transfer failed");

        emit OracleWithdrawn(msg.sender, stakeToReturn);
    }

    function recordDeviation(address _oracle, uint256 _absoluteDeviation) external onlyOwnerOrAuthorized {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );

        info.deviationScore += _absoluteDeviation;
        emit OracleDeviationRecorded(_oracle, info.deviationScore);

        if (_absoluteDeviation >= deltaWatchlist) {
            info.watchlistStrikes++;

            if (info.watchlistStrikes >= kWatchlist &&
                info.status == DataTypes.OracleStatus.Active) {
                info.status = DataTypes.OracleStatus.Watchlisted;
                _addToWatchlist(_oracle);
                info.watchlistPosition = block.timestamp;
                emit OracleWatchlisted(_oracle, info.deviationScore);
            }
        }
    }

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

        // C11: watchlisted oracles must observe tWatchlist minimum before exit
        if (info.status == DataTypes.OracleStatus.Watchlisted) {
            require(
                block.timestamp >= info.watchlistPosition + tWatchlist,
                "Watchlist observation period not elapsed"
            );
        }

        info.deviationScore = 0;
        info.watchlistStrikes = 0;
        info.lastResetTime = block.timestamp;

        if (info.status == DataTypes.OracleStatus.Watchlisted) {
            info.status = DataTypes.OracleStatus.Active;
            _removeFromWatchlist(_oracle);
            info.watchlistPosition = 0;
        }

        emit OracleScoreReset(_oracle);
    }

    function penalizeInactivity(address _oracle, uint256 _amount, address _recipient) external onlyOwnerOrAuthorized nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(
            info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );
        require(_recipient != address(0), "Bad recipient");

        if (_amount > info.stake) {
            _amount = info.stake;
        }
        info.stake -= _amount;

        emit OracleInactivityPenalized(_oracle, _amount);

        if (_amount > 0) {
            (bool sent, ) = _recipient.call{value: _amount}("");
            require(sent, "Penalty transfer failed");
        }
    }

    function slashOracle(address _oracle, uint256 _amount, address _recipient) external onlyOwnerOrAuthorized nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        require(info.stake >= _amount, "Slash exceeds stake");
        require(_recipient != address(0), "Bad recipient");

        info.stake -= _amount;

        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        if (isInWatchlist[_oracle]) _removeFromWatchlist(_oracle);
        info.status = DataTypes.OracleStatus.Slashed;

        emit OracleSlashed(_oracle, _amount);

        (bool sent, ) = _recipient.call{value: _amount}("");
        require(sent, "Slash transfer failed");
    }

    function expelOracle(address _oracle) external onlyOwnerOrAuthorized {
        DataTypes.OracleInfo storage info = oracleData[_oracle];

        if (isInWatchlist[_oracle]) _removeFromWatchlist(_oracle);

        if (info.status == DataTypes.OracleStatus.Active ||
            info.status == DataTypes.OracleStatus.Watchlisted) {
            activeOracleCount--;
        }

        info.status = DataTypes.OracleStatus.Expelled;
        emit OracleExpelled(_oracle);
    }

    function reintegrateOracle() external payable nonReentrant {
        DataTypes.OracleInfo storage info = oracleData[msg.sender];
        require(info.status == DataTypes.OracleStatus.Slashed, "Not slashed");

        uint256 minStake = getMinimumStake();
        require(msg.value + info.stake >= minStake * 2, "Insufficient integration for 2x stake");

        info.stake += msg.value;
        info.status = DataTypes.OracleStatus.Active;
        info.activationTime = block.timestamp + 7 days;
        info.deviationScore = 0;
        info.watchlistPosition = 0;
        info.lastResetTime = block.timestamp;
        info.withdrawRequestTime = 0;

        emit OracleReintegrated(msg.sender, info.stake);
    }

    // Fisher-Yates selection from Active-only pool (excludes watchlisted)
    function selectOracles(uint256 _seed, uint256 _n)
        external
        view
        returns (address[] memory selected)
    {
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
            uint256 idx = uint256(keccak256(abi.encodePacked(_seed, i))) % remaining;
            selected[i] = eligible[idx];
            eligible[idx] = eligible[remaining - 1];
            remaining--;
        }
    }

    function _addToWatchlist(address _oracle) internal {
        if (!isInWatchlist[_oracle]) {
            isInWatchlist[_oracle] = true;
            watchlistOracles.push(_oracle);
        }
    }

    function _removeFromWatchlist(address _oracle) internal {
        if (!isInWatchlist[_oracle]) return;
        isInWatchlist[_oracle] = false;
        uint256 len = watchlistOracles.length;
        for (uint256 i = 0; i < len; i++) {
            if (watchlistOracles[i] == _oracle) {
                watchlistOracles[i] = watchlistOracles[len - 1];
                watchlistOracles.pop();
                break;
            }
        }
    }

    function _getWatchlistPosition(address _oracle) internal view returns (uint256) {
        if (!isInWatchlist[_oracle]) return 0;
        uint256 myScore = oracleData[_oracle].deviationScore;
        uint256 pos = 1;
        for (uint256 i = 0; i < watchlistOracles.length; i++) {
            if (watchlistOracles[i] != _oracle && oracleData[watchlistOracles[i]].deviationScore > myScore) {
                pos++;
            }
        }
        return pos;
    }

    // minStake = baseStake * log2(1 + activeOracleCount); returns baseStake when count=0
    function getMinimumStake() public view returns (uint256) {
        if (activeOracleCount == 0) {
            return baseStake;
        }
        uint256 logValue = _log2(1 + activeOracleCount);
        if (logValue == 0) logValue = 1;
        return baseStake * logValue;
    }

    function getOracleInfo(address _oracle) external view returns (
        uint256 stake,
        DataTypes.OracleStatus status,
        uint256 registrationTime,
        uint256 activationTime,
        uint256 deviationScore,
        uint256 claimsEvaluated,
        uint256 watchlistStrikes
    ) {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        return (
            info.stake,
            info.status,
            info.registrationTime,
            info.activationTime,
            info.deviationScore,
            info.claimsEvaluated,
            info.watchlistStrikes
        );
    }

    function getOracleCount() external view returns (uint256) {
        return oracleList.length;
    }

    function isEligible(address _oracle) external view returns (bool) {
        DataTypes.OracleInfo storage info = oracleData[_oracle];
        return info.status == DataTypes.OracleStatus.Active &&
               info.withdrawRequestTime == 0;
    }

    function getWatchlist() external view returns (address[] memory) {
        return watchlistOracles;
    }

    function getWatchlistCount() external view returns (uint256) {
        return watchlistOracles.length;
    }

    function getRewardPercentBps(address _oracle) external view returns (uint256) {
        if (oracleData[_oracle].status != DataTypes.OracleStatus.Watchlisted) return 10000;
        uint256 pos = _getWatchlistPosition(_oracle);
        if (pos == 0) return 10000;
        if (pos <= 99) return 5000 + ((pos - 1) * 4000) / 99;
        return 9000;
    }

    function setAuthorizedCaller(address _caller, bool _authorized) external onlyOwner {
        authorizedCallers[_caller] = _authorized;
    }

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

    function setTWatchlist(uint256 _val) external onlyOwner {
        tWatchlist = _val;
        emit ParameterUpdated("tWatchlist", _val);
    }

    function setRClaim(uint256 _val) external onlyOwner {
        rClaim = _val;
        emit ParameterUpdated("rClaim", _val);
    }

    function _log2(uint256 x) internal pure returns (uint256 result) {
        if (x <= 1) return 0;
        result = 0;
        while (x > 1) {
            x >>= 1;
            result++;
        }
    }
}
