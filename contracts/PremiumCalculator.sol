// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./libraries/DataTypes.sol";

contract PremiumCalculator is Ownable {
    using DataTypes for *;

    uint256 public patt = 500;
    uint256 public lPercent = 200;
    uint256 public tint;
    uint256 public eFNR = 2000;
    uint256 public vbase;
    uint256 public coracle24h;
    uint256 public mBase = 2000;
    uint256 public mAdj;
    uint256 public pmin = 0;
    mapping(DataTypes.CoverageLevel => uint256) public fcov;

    uint256 public poolBalance;
    uint256 public pendingLiabilities;
    uint256 public expectedClaims7d;
    uint256 public srSafe = 15000;
    uint256 public srCritical = 13000;
    uint256 public deltaMmed = 500;
    uint256 public deltaMhigh = 1000;
    address public authorizedUpdater;

    event PremiumCalculated(uint256 swapValue, uint256 premium, DataTypes.CoverageLevel coverageLevel);
    event PattUpdated(uint256 newPatt);
    event SolvencyUpdated(uint256 poolBalance, uint256 pendingLiabilities, uint256 expectedClaims7d, uint256 solvencyRatio, uint256 newMAdj);
    event ParameterUpdated(string param, uint256 value);
    event MarketDataUpdated(uint256 tint, uint256 vbase, uint256 coracle24h);

    constructor() Ownable(msg.sender) {
        // Fcov = premium multiplier (PDF Table 2); different from payout % in MEVInsurance
        // (Fcov: Low=70%, Med=90%, High=100% vs Payout: Low=50%, Med=70%, High=100%)
        fcov[DataTypes.CoverageLevel.Low] = 7000;
        fcov[DataTypes.CoverageLevel.Medium] = 9000;
        fcov[DataTypes.CoverageLevel.High] = 10000;
    }

    function calculatePremium(
        uint256 _swapValue,
        DataTypes.CoverageLevel _coverageLevel
    ) public view returns (uint256 premium) {
        require(_swapValue > 0, "Swap value must be > 0");

        // Component 1: Patt * L%
        uint256 comp1 = (patt * lPercent) / 10000;

        // Component 2: (Tint * E/(1-E)) / Vbase
        uint256 comp2 = 0;
        if (vbase > 0 && eFNR < 10000) {
            uint256 adjustedFraud = (tint * eFNR) / (10000 - eFNR);
            comp2 = (adjustedFraud * 10000) / vbase;
        }

        // Component 3: Coracle24h / Vbase
        uint256 comp3 = 0;
        if (vbase > 0) {
            comp3 = (coracle24h * 10000) / vbase;
        }

        // Base rate = comp1 + comp2 + comp3 (all in bps)
        uint256 baseRate = comp1 + comp2 + comp3;

        // Margin multiplier: (1 + M) where M = mBase + mAdj
        uint256 totalMargin = mBase + mAdj;
        uint256 marginMultiplier = 10000 + totalMargin;

        uint256 basePremium = (_swapValue * baseRate) / 10000;
        basePremium = (basePremium * marginMultiplier) / 10000;

        if (pmin > 0) {
            uint256 minPremium = (_swapValue * pmin) / 10000;
            if (basePremium < minPremium) {
                basePremium = minPremium;
            }
        }

        premium = (basePremium * fcov[_coverageLevel]) / 10000;
    }

    function getSolvencyRatio() public view returns (uint256) {
        uint256 totalLiabilities = pendingLiabilities + expectedClaims7d;
        if (totalLiabilities == 0) {
            return type(uint256).max;
        }
        return (poolBalance * 10000) / totalLiabilities;
    }

    function updateSolvencyData(
        uint256 _poolBalance,
        uint256 _pendingLiabilities,
        uint256 _expectedClaims7d
    ) external onlyOwner {
        poolBalance = _poolBalance;
        pendingLiabilities = _pendingLiabilities;
        expectedClaims7d = _expectedClaims7d;

        uint256 sr = getSolvencyRatio();

        if (sr >= srSafe) {
            mAdj = 0;
        } else if (sr >= srCritical) {
            mAdj = deltaMmed;
        } else {
            mAdj = deltaMhigh;
        }

        emit SolvencyUpdated(_poolBalance, _pendingLiabilities, _expectedClaims7d, sr, mAdj);
    }

    function updateMarketData(
        uint256 _tint,
        uint256 _vbase,
        uint256 _coracle24h
    ) external onlyOwner {
        tint = _tint;
        vbase = _vbase;
        coracle24h = _coracle24h;

        emit MarketDataUpdated(_tint, _vbase, _coracle24h);
    }

    function setPatt(uint256 _val) external {
        require(msg.sender == owner() || msg.sender == authorizedUpdater, "Not owner or authorized updater");
        patt = _val;
        emit PattUpdated(_val);
    }

    function setAuthorizedUpdater(address _updater) external onlyOwner {
        authorizedUpdater = _updater;
    }

    function setLPercent(uint256 _val) external onlyOwner {
        lPercent = _val;
        emit ParameterUpdated("lPercent", _val);
    }

    function setEFNR(uint256 _val) external onlyOwner {
        require(_val < 10000, "eFNR must be < 100%");
        eFNR = _val;
        emit ParameterUpdated("eFNR", _val);
    }

    function setMBase(uint256 _val) external onlyOwner {
        mBase = _val;
        emit ParameterUpdated("mBase", _val);
    }

    function setPmin(uint256 _val) external onlyOwner {
        pmin = _val;
        emit ParameterUpdated("pmin", _val);
    }

    function setFcov(DataTypes.CoverageLevel _level, uint256 _val) external onlyOwner {
        fcov[_level] = _val;
        emit ParameterUpdated("fcov", _val);
    }

    function setSRSafe(uint256 _val) external onlyOwner {
        srSafe = _val;
        emit ParameterUpdated("srSafe", _val);
    }

    function setSRCritical(uint256 _val) external onlyOwner {
        srCritical = _val;
        emit ParameterUpdated("srCritical", _val);
    }

    function setDeltaMmed(uint256 _val) external onlyOwner {
        deltaMmed = _val;
        emit ParameterUpdated("deltaMmed", _val);
    }

    function setDeltaMhigh(uint256 _val) external onlyOwner {
        deltaMhigh = _val;
        emit ParameterUpdated("deltaMhigh", _val);
    }
}
