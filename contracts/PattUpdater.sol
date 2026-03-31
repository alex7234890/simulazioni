// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";
import "./OracleRegistry.sol";
import "./PremiumCalculator.sol";

/**
 * @title PattUpdater
 * @dev Periodically updates the Patt (probability of attack) parameter in PremiumCalculator
 *      via oracle commit-reveal consensus.
 *
 * Flow:
 *   1. Owner (or automated trigger) calls startPattUpdate()
 *   2. N_oracle_patt (default 5) oracles are selected
 *   3. Oracles commit their estimated attack ratio (in basis points, 0-10000)
 *   4. Oracles reveal their estimates
 *   5. finalizePattUpdate() calculates median, applies safety margin ms,
 *      and writes the new Patt value to PremiumCalculator
 *   6. Participating oracles receive Rclaim reward
 *
 * All percentages in basis points (10000 = 100%).
 */
contract PattUpdater is Ownable, ReentrancyGuard {

    // -------------------------------------------------------
    //  External contracts
    // -------------------------------------------------------

    OracleRegistry public oracleRegistry;
    PremiumCalculator public premiumCalculator;

    // -------------------------------------------------------
    //  Protocol Parameters
    // -------------------------------------------------------

    /// @dev Number of oracles for Patt estimation
    uint256 public nOraclePatt = 5;

    /// @dev Safety margin in basis points (200 = 2%) applied to median
    uint256 public ms = 200;

    /// @dev Commit timeout for oracle submissions
    uint256 public commitTimeout = 2 days;

    /// @dev Reveal timeout for oracle submissions
    uint256 public revealTimeout = 2 days;

    /// @dev Minimum interval between Patt updates
    uint256 public updateInterval = 1 days;

    /// @dev Reward per participating oracle (in wei, matches rClaim)
    uint256 public oracleReward = 0.002 ether;

    // -------------------------------------------------------
    //  Data Structures
    // -------------------------------------------------------

    enum UpdateStatus {
        None,       // No active update
        Committing, // Oracles are committing
        Revealing,  // Oracles are revealing
        Finalized   // Update complete
    }

    struct PattRound {
        uint256 roundId;
        UpdateStatus status;
        address[] assignedOracles;
        uint256 commitCount;
        uint256 revealCount;
        uint16[] revealedEstimates; // Attack ratio in basis points (0-10000)
        uint256 timestamp;
        uint256 finalPatt;          // Final Patt value after median + margin
    }

    /// @dev All update rounds
    PattRound[] public rounds;

    /// @dev Oracle commit hashes: roundId => oracle => hash
    mapping(uint256 => mapping(address => bytes32)) public commitHashes;

    /// @dev Oracle reveal status: roundId => oracle => bool
    mapping(uint256 => mapping(address => bool)) public hasCommitted;

    /// @dev Oracle reveal status: roundId => oracle => bool
    mapping(uint256 => mapping(address => bool)) public hasRevealed;

    /// @dev Oracle assignment check: roundId => oracle => bool
    mapping(uint256 => mapping(address => bool)) public isAssigned;

    /// @dev Timestamp of last finalized update
    uint256 public lastUpdateTime;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event PattUpdateStarted(uint256 indexed roundId, address[] assignedOracles);
    event PattEstimateCommitted(uint256 indexed roundId, address indexed oracle);
    event PattEstimateRevealed(uint256 indexed roundId, address indexed oracle, uint16 estimate);
    event PattUpdateFinalized(uint256 indexed roundId, uint256 medianEstimate, uint256 finalPatt);
    event OracleRewarded(uint256 indexed roundId, address indexed oracle, uint256 amount);
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor(address _oracleRegistry, address _premiumCalculator) Ownable(msg.sender) {
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));
        premiumCalculator = PremiumCalculator(_premiumCalculator);
    }

    // -------------------------------------------------------
    //  Start Update Round
    // -------------------------------------------------------

    /**
     * @dev Start a new Patt update round. Selects nOraclePatt oracles.
     * Can only be called after updateInterval has passed since last finalization.
     */
    function startPattUpdate() external onlyOwner {
        // Check no active round is in progress
        if (rounds.length > 0) {
            PattRound storage last = rounds[rounds.length - 1];
            require(
                last.status == UpdateStatus.Finalized || last.status == UpdateStatus.None,
                "Previous round still active"
            );
        }

        require(
            block.timestamp >= lastUpdateTime + updateInterval,
            "Update interval not elapsed"
        );

        // Select oracles
        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.timestamp, block.prevrandao, rounds.length
        )));
        address[] memory selected = oracleRegistry.selectOracles(seed, nOraclePatt);

        uint256 roundId = rounds.length;
        rounds.push();
        PattRound storage r = rounds[roundId];
        r.roundId = roundId;
        r.status = UpdateStatus.Committing;
        r.timestamp = block.timestamp;

        for (uint256 i = 0; i < selected.length; i++) {
            r.assignedOracles.push(selected[i]);
            isAssigned[roundId][selected[i]] = true;
        }

        emit PattUpdateStarted(roundId, selected);
    }

    // -------------------------------------------------------
    //  Commit-Reveal
    // -------------------------------------------------------

    /**
     * @dev Oracle commits their Patt estimate hash.
     * commitHash = keccak256(abi.encodePacked(estimate, salt))
     * where estimate is uint16 in basis points (0-10000)
     * @param _roundId Round ID
     * @param _commitHash Hash of the estimate
     */
    function commitEstimate(uint256 _roundId, bytes32 _commitHash) external {
        require(_roundId < rounds.length, "Invalid round ID");
        PattRound storage r = rounds[_roundId];

        require(
            r.status == UpdateStatus.Committing,
            "Not in commit phase"
        );
        require(isAssigned[_roundId][msg.sender], "Not assigned oracle");
        require(!hasCommitted[_roundId][msg.sender], "Already committed");
        require(_commitHash != bytes32(0), "Empty hash");
        require(
            block.timestamp <= r.timestamp + commitTimeout,
            "Commit period expired"
        );

        commitHashes[_roundId][msg.sender] = _commitHash;
        hasCommitted[_roundId][msg.sender] = true;
        r.commitCount++;

        // Transition to revealing when all committed
        if (r.commitCount == r.assignedOracles.length) {
            r.status = UpdateStatus.Revealing;
        }

        emit PattEstimateCommitted(_roundId, msg.sender);
    }

    /**
     * @dev Oracle reveals their Patt estimate.
     * @param _roundId Round ID
     * @param _estimate Attack ratio in basis points (0-10000)
     * @param _salt Salt used in the commit
     */
    function revealEstimate(uint256 _roundId, uint16 _estimate, bytes32 _salt) external {
        require(_roundId < rounds.length, "Invalid round ID");
        PattRound storage r = rounds[_roundId];

        require(
            r.status == UpdateStatus.Committing || r.status == UpdateStatus.Revealing,
            "Not in reveal-eligible phase"
        );
        require(isAssigned[_roundId][msg.sender], "Not assigned oracle");
        require(hasCommitted[_roundId][msg.sender], "Not committed");
        require(!hasRevealed[_roundId][msg.sender], "Already revealed");
        require(_estimate <= 10000, "Estimate must be 0-10000 bps");
        require(
            block.timestamp <= r.timestamp + commitTimeout + revealTimeout,
            "Reveal period expired"
        );

        // Verify hash
        bytes32 expectedHash = keccak256(abi.encodePacked(_estimate, _salt));
        require(commitHashes[_roundId][msg.sender] == expectedHash, "Hash mismatch");

        hasRevealed[_roundId][msg.sender] = true;
        r.revealedEstimates.push(_estimate);
        r.revealCount++;

        // Auto-transition to revealing phase if still committing
        if (r.status == UpdateStatus.Committing) {
            r.status = UpdateStatus.Revealing;
        }

        emit PattEstimateRevealed(_roundId, msg.sender, _estimate);
    }

    // -------------------------------------------------------
    //  Finalization
    // -------------------------------------------------------

    /**
     * @dev Finalize the Patt update round.
     * Calculates median of revealed estimates, applies safety margin,
     * writes new Patt to PremiumCalculator, and rewards oracles.
     */
    function finalizePattUpdate(uint256 _roundId) external nonReentrant {
        require(_roundId < rounds.length, "Invalid round ID");
        PattRound storage r = rounds[_roundId];

        require(
            r.status == UpdateStatus.Committing || r.status == UpdateStatus.Revealing,
            "Round not active"
        );

        bool allRevealed = r.revealCount == r.assignedOracles.length;
        bool timeoutReached = block.timestamp >= r.timestamp + commitTimeout + revealTimeout;
        require(allRevealed || timeoutReached, "Reveal phase not complete");
        require(r.revealCount > 0, "No reveals received");

        // Calculate median
        uint256 median = _calculateMedian(r.revealedEstimates);

        // Apply safety margin: finalPatt = median + ms
        // Capped at 10000 (100%)
        uint256 finalPatt = median + ms;
        if (finalPatt > 10000) finalPatt = 10000;

        // Effects
        r.finalPatt = finalPatt;
        r.status = UpdateStatus.Finalized;
        lastUpdateTime = block.timestamp;

        // Write to PremiumCalculator
        premiumCalculator.setPatt(finalPatt);

        // Reward oracles
        _rewardOracles(_roundId);

        emit PattUpdateFinalized(_roundId, median, finalPatt);
    }

    // -------------------------------------------------------
    //  Internal Helpers
    // -------------------------------------------------------

    /**
     * @dev Distribute rewards to oracles that revealed their estimates.
     */
    function _rewardOracles(uint256 _roundId) internal {
        PattRound storage r = rounds[_roundId];

        for (uint256 i = 0; i < r.assignedOracles.length; i++) {
            address oracle = r.assignedOracles[i];
            if (hasRevealed[_roundId][oracle]) {
                if (address(this).balance >= oracleReward) {
                    (bool sent, ) = oracle.call{value: oracleReward}("");
                    if (sent) {
                        emit OracleRewarded(_roundId, oracle, oracleReward);
                    }
                }
            }
        }
    }

    /**
     * @dev Calculate median of uint16 array using insertion sort.
     */
    function _calculateMedian(uint16[] storage _values) internal view returns (uint256) {
        uint256 len = _values.length;
        if (len == 0) return 0;
        if (len == 1) return _values[0];

        uint16[] memory sorted = new uint16[](len);
        for (uint256 i = 0; i < len; i++) {
            sorted[i] = _values[i];
        }

        // Insertion sort (small N, typically 5)
        for (uint256 i = 1; i < len; i++) {
            uint16 key = sorted[i];
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

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    function getRoundsCount() external view returns (uint256) {
        return rounds.length;
    }

    function getRoundOracles(uint256 _roundId) external view returns (address[] memory) {
        return rounds[_roundId].assignedOracles;
    }

    function getRoundEstimates(uint256 _roundId) external view returns (uint16[] memory) {
        return rounds[_roundId].revealedEstimates;
    }

    function getRoundInfo(uint256 _roundId) external view returns (
        UpdateStatus status,
        uint256 commitCount,
        uint256 revealCount,
        uint256 finalPatt,
        uint256 timestamp
    ) {
        PattRound storage r = rounds[_roundId];
        return (r.status, r.commitCount, r.revealCount, r.finalPatt, r.timestamp);
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setNOraclePatt(uint256 _val) external onlyOwner {
        nOraclePatt = _val;
        emit ParameterUpdated("nOraclePatt", _val);
    }

    function setMs(uint256 _val) external onlyOwner {
        ms = _val;
        emit ParameterUpdated("ms", _val);
    }

    function setCommitTimeout(uint256 _val) external onlyOwner {
        commitTimeout = _val;
        emit ParameterUpdated("commitTimeout", _val);
    }

    function setRevealTimeout(uint256 _val) external onlyOwner {
        revealTimeout = _val;
        emit ParameterUpdated("revealTimeout", _val);
    }

    function setUpdateInterval(uint256 _val) external onlyOwner {
        updateInterval = _val;
        emit ParameterUpdated("updateInterval", _val);
    }

    function setOracleReward(uint256 _val) external onlyOwner {
        oracleReward = _val;
        emit ParameterUpdated("oracleReward", _val);
    }

    /// @dev Allow contract to receive ETH for oracle rewards
    receive() external payable {}
}
