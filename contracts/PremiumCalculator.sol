// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./libraries/DataTypes.sol";

/**
 * @title PremiumCalculator
 * @dev Calculates insurance premiums based on the formula from PDF section 1.4.6:
 *
 *   P = max(V * [(Patt * L%) + (Tint * E/(1-E))/Vbase + Coracle24h/Vbase] * (1+M) * Fcov, Pmin * V)
 *
 * Where:
 *   V = swap value
 *   Patt = probability of attack (updated periodically by oracles)
 *   L% = average loss percentage
 *   Tint = intercepted fraud value in last 24h (wei)
 *   E = false negative rate (eFNR)
 *   Vbase = total insured swap volume in last 24h
 *   Coracle24h = oracle costs in last 24h (wei)
 *   M = margin = mBase + mAdj (adaptive based on solvency ratio)
 *   Fcov = coverage factor per level
 *   Pmin = minimum premium rate
 *
 * All percentages in basis points (10000 = 100%).
 * Solvency ratio uses 4 decimals precision (10000 = 1.0x).
 */
contract PremiumCalculator is Ownable {
    using DataTypes for *;

    // -------------------------------------------------------
    //  Protocol Parameters (Table 8 defaults)
    // -------------------------------------------------------

    /// @dev Probability of attack in basis points (500 = 5%)
    uint256 public patt = 500;

    /// @dev Average loss percentage in basis points (2500 = 25%)
    uint256 public lPercent = 2500;

    /// @dev Intercepted fraud value in last 24h (in wei/tokens)
    uint256 public tint;

    /// @dev False negative rate in basis points (2000 = 20%)
    uint256 public eFNR = 2000;

    /// @dev Total insured swap volume in last 24h (in wei/tokens)
    uint256 public vbase;

    /// @dev Oracle costs in last 24h (in wei/tokens)
    uint256 public coracle24h;

    /// @dev Base margin in basis points (2000 = 20%)
    uint256 public mBase = 2000;

    /// @dev Adaptive margin adjustment in basis points (set by solvency ratio)
    uint256 public mAdj;

    /// @dev Minimum premium rate in basis points (150 = 1.5%)
    uint256 public pmin = 150;

    /// @dev Coverage factor per level in basis points
    mapping(DataTypes.CoverageLevel => uint256) public fcov;

    // -------------------------------------------------------
    //  Solvency Ratio Parameters
    // -------------------------------------------------------

    /// @dev Current pool balance (updated externally)
    uint256 public poolBalance;

    /// @dev Pending liabilities (sum of approved but unpaid claims)
    uint256 public pendingLiabilities;

    /// @dev Expected claims in next 7 days (estimated)
    uint256 public expectedClaims7d;

    /// @dev Safe solvency ratio threshold (15000 = 1.5x, using 4 decimals)
    uint256 public srSafe = 15000;

    /// @dev Critical solvency ratio threshold (13000 = 1.3x)
    uint256 public srCritical = 13000;

    /// @dev Medium margin delta in basis points (500 = 5%)
    uint256 public deltaMmed = 500;

    /// @dev High margin delta in basis points (1000 = 10%)
    uint256 public deltaMhigh = 1000;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event PremiumCalculated(uint256 swapValue, uint256 premium, DataTypes.CoverageLevel coverageLevel);
    event PattUpdated(uint256 newPatt);
    event SolvencyUpdated(uint256 poolBalance, uint256 pendingLiabilities, uint256 expectedClaims7d, uint256 solvencyRatio, uint256 newMAdj);
    event ParameterUpdated(string param, uint256 value);
    event MarketDataUpdated(uint256 tint, uint256 vbase, uint256 coracle24h);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor() Ownable(msg.sender) {
        // Coverage factors (basis points)
        fcov[DataTypes.CoverageLevel.Low] = 7000;     // 70%
        fcov[DataTypes.CoverageLevel.Medium] = 9000;  // 90%
        fcov[DataTypes.CoverageLevel.High] = 10000;   // 100%
    }

    // -------------------------------------------------------
    //  Premium Calculation
    // -------------------------------------------------------

    /**
     * @dev Calculate the premium for a given swap value and coverage level.
     *
     * Formula: P = max(V * [(Patt * L%) + (Tint * E/(1-E))/Vbase + Coracle24h/Vbase] * (1+M) * Fcov, Pmin * V)
     *
     * To avoid overflow and maintain precision, we use a scaled intermediate calculation.
     * All basis points are divided by 10000 at the end.
     *
     * @param _swapValue The value of the swap to insure
     * @param _coverageLevel The desired coverage level
     * @return premium The calculated premium amount
     */
    function calculatePremium(
        uint256 _swapValue,
        DataTypes.CoverageLevel _coverageLevel
    ) public view returns (uint256 premium) {
        require(_swapValue > 0, "Swap value must be > 0");

        // Component 1: Patt * L%
        // patt is in bps, lPercent is in bps
        // patt * lPercent / 10000 gives result in bps
        uint256 comp1 = (patt * lPercent) / 10000;

        // Component 2: (Tint * E/(1-E)) / Vbase
        // eFNR is in bps (e.g., 2000 = 20%)
        // E/(1-E) = eFNR / (10000 - eFNR)
        // Result needs to be in bps relative to swap value
        uint256 comp2 = 0;
        if (vbase > 0 && eFNR < 10000) {
            // tint * eFNR / (10000 - eFNR) gives the adjusted fraud cost
            // then divide by vbase to get per-unit rate
            // multiply by 10000 to keep in bps
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
        // (10000 + mBase + mAdj) / 10000
        uint256 totalMargin = mBase + mAdj;
        uint256 marginMultiplier = 10000 + totalMargin;

        // Coverage factor
        uint256 covFactor = fcov[_coverageLevel];

        // Premium = V * baseRate * marginMultiplier * covFactor / (10000^3)
        // To avoid overflow with large values, we chain divisions
        // P = V * baseRate / 10000 * marginMultiplier / 10000 * covFactor / 10000
        premium = _swapValue;
        premium = (premium * baseRate) / 10000;
        premium = (premium * marginMultiplier) / 10000;
        premium = (premium * covFactor) / 10000;

        // Minimum premium: Pmin * V / 10000
        uint256 minPremium = (_swapValue * pmin) / 10000;

        // P = max(calculated, minimum)
        if (premium < minPremium) {
            premium = minPremium;
        }
    }

    // -------------------------------------------------------
    //  Solvency Ratio & Adaptive Margin
    // -------------------------------------------------------

    /**
     * @dev Calculate the current solvency ratio.
     * SR = poolBalance / (pendingLiabilities + expectedClaims7d)
     * Returns value with 4 decimal precision (10000 = 1.0x)
     */
    function getSolvencyRatio() public view returns (uint256) {
        uint256 totalLiabilities = pendingLiabilities + expectedClaims7d;
        if (totalLiabilities == 0) {
            return type(uint256).max; // infinite solvency
        }
        return (poolBalance * 10000) / totalLiabilities;
    }

    /**
     * @dev Update solvency data and recalculate adaptive margin.
     * @param _poolBalance Current token balance in the insurance pool
     * @param _pendingLiabilities Sum of approved but unpaid claim amounts
     * @param _expectedClaims7d Estimated claims value for next 7 days
     */
    function updateSolvencyData(
        uint256 _poolBalance,
        uint256 _pendingLiabilities,
        uint256 _expectedClaims7d
    ) external onlyOwner {
        poolBalance = _poolBalance;
        pendingLiabilities = _pendingLiabilities;
        expectedClaims7d = _expectedClaims7d;

        // Calculate SR and update mAdj
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

    // -------------------------------------------------------
    //  Market Data Updates
    // -------------------------------------------------------

    /**
     * @dev Update 24h market data used in premium formula.
     * @param _tint Intercepted fraud value in last 24h
     * @param _vbase Total insured swap volume in last 24h
     * @param _coracle24h Oracle costs in last 24h
     */
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

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setPatt(uint256 _val) external onlyOwner {
        patt = _val;
        emit PattUpdated(_val);
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
