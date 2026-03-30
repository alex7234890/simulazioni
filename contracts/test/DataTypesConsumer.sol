// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../libraries/DataTypes.sol";

/**
 * @title DataTypesConsumer
 * @dev Test contract to verify DataTypes library compiles and is usable.
 *      Not deployed in production — used only for compilation and unit tests.
 */
contract DataTypesConsumer {
    using DataTypes for *;

    mapping(address => DataTypes.UserProfile) public users;
    mapping(address => DataTypes.OracleInfo) public oracles;
    DataTypes.Claim[] public claims;
    DataTypes.Policy[] public policies;

    // -- Tier enum getters/setters --

    function setUserTier(address user, DataTypes.Tier tier) external {
        users[user].tier = tier;
    }

    function getUserTier(address user) external view returns (DataTypes.Tier) {
        return users[user].tier;
    }

    // -- CoverageLevel --

    function setCoverageLevel(address user, DataTypes.CoverageLevel level) external {
        users[user].coverageLevel = level;
    }

    function getCoverageLevel(address user) external view returns (DataTypes.CoverageLevel) {
        return users[user].coverageLevel;
    }

    // -- ClaimStatus --

    function createClaim(address _user, bytes32 tx1, bytes32 tx2, bytes32 tx3) external returns (uint256) {
        uint256 id = claims.length;
        DataTypes.Claim storage c = claims.push();
        c.user = _user;
        c.txHash1 = tx1;
        c.txHash2 = tx2;
        c.txHash3 = tx3;
        c.status = DataTypes.ClaimStatus.Pending;
        c.timestamp = block.timestamp;
        return id;
    }

    function getClaimStatus(uint256 id) external view returns (DataTypes.ClaimStatus) {
        return claims[id].status;
    }

    function setClaimStatus(uint256 id, DataTypes.ClaimStatus status) external {
        claims[id].status = status;
    }

    // -- OracleStatus --

    function setOracleStatus(address oracle, DataTypes.OracleStatus status) external {
        oracles[oracle].status = status;
    }

    function getOracleStatus(address oracle) external view returns (DataTypes.OracleStatus) {
        return oracles[oracle].status;
    }

    // -- UserProfile fields --

    function setUserProfile(
        address user,
        uint256 totalSwaps,
        uint256 totalClaims,
        uint256 avgFraudScore,
        bool isBlacklisted,
        uint256 debt
    ) external {
        DataTypes.UserProfile storage p = users[user];
        p.totalSwaps = totalSwaps;
        p.totalClaims = totalClaims;
        p.avgFraudScore = avgFraudScore;
        p.isBlacklisted = isBlacklisted;
        p.debt = debt;
    }

    function getUserProfile(address user) external view returns (
        DataTypes.Tier tier,
        uint256 totalSwaps,
        uint256 totalClaims,
        uint256 avgFraudScore,
        bool isBlacklisted,
        uint256 debt
    ) {
        DataTypes.UserProfile storage p = users[user];
        return (p.tier, p.totalSwaps, p.totalClaims, p.avgFraudScore, p.isBlacklisted, p.debt);
    }

    // -- Policy --

    function createPolicy(
        address holder,
        DataTypes.CoverageLevel cov,
        uint256 premium,
        uint256 maxSwapValue
    ) external returns (uint256) {
        uint256 id = policies.length;
        policies.push(DataTypes.Policy({
            holder: holder,
            coverageLevel: cov,
            premium: premium,
            startTime: block.timestamp,
            endTime: block.timestamp + 30 days,
            maxSwapValue: maxSwapValue,
            active: true
        }));
        return id;
    }

    function getPolicy(uint256 id) external view returns (
        address holder,
        DataTypes.CoverageLevel cov,
        uint256 premium,
        bool active
    ) {
        DataTypes.Policy storage p = policies[id];
        return (p.holder, p.coverageLevel, p.premium, p.active);
    }

    function getClaimsCount() external view returns (uint256) {
        return claims.length;
    }

    function getPoliciesCount() external view returns (uint256) {
        return policies.length;
    }
}
