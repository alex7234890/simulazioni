// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./OracleRegistry.sol";
import "./PremiumCalculator.sol";

/**
 * @title PattUpdater
 * @dev Decentralized Patt (attack probability) update mechanism.
 *
 * Oracles submit Patt estimates via commit-reveal, then the median is
 * computed and a variable safety margin ms is applied based on swap volume:
 *   - ms = 5% (500 bps) if volumeSwaps >= 10,000
 *   - ms = 10% (1000 bps) if 1,000 <= volumeSwaps < 10,000
 *   - ms = 20% (2000 bps) if volumeSwaps < 1,000
 *
 * Final: pattFinal = medianPatt * (10000 + ms) / 10000
 *
 * PDF reference: section 1.4.6.1 - 1.4.6.2
 */
contract PattUpdater is Ownable, ReentrancyGuard {

    OracleRegistry public oracleRegistry;
    PremiumCalculator public premiumCalculator;

    // -------------------------------------------------------
    //  Parameters
    // -------------------------------------------------------

    /// @dev Number of oracles required for a Patt update round
    uint256 public nPattOracles = 7;

    /// @dev Commit timeout for Patt estimates
    uint256 public commitTimeout = 2 days;

    /// @dev Reveal timeout for Patt estimates
    uint256 public revealTimeout = 2 days;

    /// @dev Current swap volume (updated by owner or external oracle)
    uint256 public volumeSwaps;

    /// @dev Safety margin thresholds (in bps)
    uint256 public msHigh = 500;      // 5% for volume >= 10000
    uint256 public msMedium = 1000;   // 10% for 1000 <= volume < 10000
    uint256 public msLow = 2000;      // 20% for volume < 1000

    /// @dev Volume thresholds
    uint256 public volumeHighThreshold = 10000;
    uint256 public volumeLowThreshold = 1000;

    // -------------------------------------------------------
    //  Round State
    // -------------------------------------------------------

    struct PattRound {
        uint256 timestamp;
        address[] assignedOracles;
        uint256 commitCount;
        uint256 revealCount;
        uint16[] revealedEstimates; // Patt estimates in bps (0-10000)
        bool finalized;
        uint256 finalPatt;          // Final Patt after safety margin (in bps)
    }

    PattRound[] public rounds;

    /// @dev Commit hashes per round per oracle
    mapping(uint256 => mapping(address => bytes32)) public commitHashes;

    /// @dev Whether oracle has committed in a round
    mapping(uint256 => mapping(address => bool)) public hasCommitted;

    /// @dev Whether oracle has revealed in a round
    mapping(uint256 => mapping(address => bool)) public hasRevealed;

    /// @dev Dataset hashes per round per oracle (PDF section 1.4.6.2)
    mapping(uint256 => mapping(address => bytes32)) public datasetHashes;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event RoundStarted(uint256 indexed roundId, address[] assignedOracles);
    event PattCommitted(uint256 indexed roundId, address indexed oracle);
    event PattRevealed(uint256 indexed roundId, address indexed oracle, uint16 pattEstimate, bytes32 datasetHash);
    event PattFinalized(uint256 indexed roundId, uint256 medianPatt, uint256 safetyMargin, uint256 finalPatt);
    event VolumeSwapsUpdated(uint256 newVolume);
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor(address _oracleRegistry, address _premiumCalculator) Ownable(msg.sender) {
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));
        premiumCalculator = PremiumCalculator(_premiumCalculator);
    }

    // -------------------------------------------------------
    //  Round Management
    // -------------------------------------------------------

    /**
     * @dev Start a new Patt update round. Selects oracles via OracleRegistry.
     */
    function startRound() external onlyOwner {
        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.timestamp, block.prevrandao, rounds.length
        )));
        address[] memory selected = oracleRegistry.selectOracles(seed, nPattOracles);

        PattRound storage r = rounds.push();
        r.timestamp = block.timestamp;
        for (uint256 i = 0; i < selected.length; i++) {
            r.assignedOracles.push(selected[i]);
        }

        emit RoundStarted(rounds.length - 1, selected);
    }

    /**
     * @dev Oracle commits a Patt estimate hash.
     * @param _roundId Round ID
     * @param _commitHash keccak256(pattEstimate, datasetHash, salt)
     */
    function commitPattEstimate(uint256 _roundId, bytes32 _commitHash) external {
        require(_roundId < rounds.length, "Invalid round");
        PattRound storage r = rounds[_roundId];
        require(!r.finalized, "Round finalized");
        require(block.timestamp <= r.timestamp + commitTimeout, "Commit timeout");
        require(_isAssigned(_roundId, msg.sender), "Not assigned oracle");
        require(!hasCommitted[_roundId][msg.sender], "Already committed");
        require(_commitHash != bytes32(0), "Empty hash");

        commitHashes[_roundId][msg.sender] = _commitHash;
        hasCommitted[_roundId][msg.sender] = true;
        r.commitCount++;

        emit PattCommitted(_roundId, msg.sender);
    }

    /**
     * @dev Oracle reveals Patt estimate and dataset hash.
     * @param _roundId Round ID
     * @param _pattEstimate Patt estimate in basis points (0-10000)
     * @param _datasetHash Hash of the block range data analyzed
     * @param _salt Random salt used in commit
     */
    function revealPattEstimate(
        uint256 _roundId,
        uint16 _pattEstimate,
        bytes32 _datasetHash,
        bytes32 _salt
    ) external {
        require(_roundId < rounds.length, "Invalid round");
        PattRound storage r = rounds[_roundId];
        require(!r.finalized, "Round finalized");
        require(hasCommitted[_roundId][msg.sender], "Not committed");
        require(!hasRevealed[_roundId][msg.sender], "Already revealed");
        require(_pattEstimate <= 10000, "Patt must be 0-10000 bps");

        // Verify commit hash
        bytes32 expectedHash = keccak256(abi.encodePacked(_pattEstimate, _datasetHash, _salt));
        require(commitHashes[_roundId][msg.sender] == expectedHash, "Hash mismatch");

        hasRevealed[_roundId][msg.sender] = true;
        r.revealedEstimates.push(_pattEstimate);
        r.revealCount++;

        // Store dataset hash for external verifiability
        datasetHashes[_roundId][msg.sender] = _datasetHash;

        emit PattRevealed(_roundId, msg.sender, _pattEstimate, _datasetHash);
    }

    /**
     * @dev Finalize a Patt update round. Computes median and applies safety margin.
     * @param _roundId Round ID
     */
    function finalizePattUpdate(uint256 _roundId) external {
        require(_roundId < rounds.length, "Invalid round");
        PattRound storage r = rounds[_roundId];
        require(!r.finalized, "Already finalized");

        bool allRevealed = r.revealCount == r.assignedOracles.length;
        bool timeoutReached = block.timestamp >= r.timestamp + commitTimeout + revealTimeout;
        require(allRevealed || timeoutReached, "Reveal phase not complete");
        require(r.revealCount > 0, "No reveals");

        // Calculate median
        uint256 median = _calculateMedian(r.revealedEstimates);

        // Calculate safety margin based on volume
        uint256 ms = _getSafetyMargin();

        // Apply margin: pattFinal = medianPatt * (10000 + ms) / 10000
        uint256 finalPatt = (median * (10000 + ms)) / 10000;

        r.finalized = true;
        r.finalPatt = finalPatt;

        // Update PremiumCalculator
        premiumCalculator.setPatt(finalPatt);

        emit PattFinalized(_roundId, median, ms, finalPatt);
    }

    // -------------------------------------------------------
    //  Volume Management
    // -------------------------------------------------------

    /**
     * @dev Update the current swap volume. Used for safety margin calculation.
     * @param _volume Current number of swaps in the observation period
     */
    function updateVolumeSwaps(uint256 _volume) external onlyOwner {
        volumeSwaps = _volume;
        emit VolumeSwapsUpdated(_volume);
    }

    // -------------------------------------------------------
    //  Internal Helpers
    // -------------------------------------------------------

    function _isAssigned(uint256 _roundId, address _oracle) internal view returns (bool) {
        PattRound storage r = rounds[_roundId];
        for (uint256 i = 0; i < r.assignedOracles.length; i++) {
            if (r.assignedOracles[i] == _oracle) return true;
        }
        return false;
    }

    /**
     * @dev Get safety margin in bps based on current swap volume.
     * PDF section 1.4.6.1:
     *   ms = 5% (500) if volume >= 10,000
     *   ms = 10% (1000) if 1,000 <= volume < 10,000
     *   ms = 20% (2000) if volume < 1,000
     */
    function _getSafetyMargin() internal view returns (uint256) {
        if (volumeSwaps >= volumeHighThreshold) {
            return msHigh;    // 500 bps = 5%
        } else if (volumeSwaps >= volumeLowThreshold) {
            return msMedium;  // 1000 bps = 10%
        } else {
            return msLow;     // 2000 bps = 20%
        }
    }

    /**
     * @dev Calculate median of a uint16 array using insertion sort.
     */
    function _calculateMedian(uint16[] storage arr) internal view returns (uint256) {
        uint256 len = arr.length;
        require(len > 0, "Empty array");

        // Copy to memory for sorting
        uint16[] memory sorted = new uint16[](len);
        for (uint256 i = 0; i < len; i++) {
            sorted[i] = arr[i];
        }

        // Insertion sort
        for (uint256 i = 1; i < len; i++) {
            uint16 key = sorted[i];
            int256 j = int256(i) - 1;
            while (j >= 0 && sorted[uint256(j)] > key) {
                sorted[uint256(j) + 1] = sorted[uint256(j)];
                j--;
            }
            sorted[uint256(j + 1)] = key;
        }

        return sorted[len / 2];
    }

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    function getRoundsCount() external view returns (uint256) {
        return rounds.length;
    }

    function getRoundOracles(uint256 _roundId) external view returns (address[] memory) {
        require(_roundId < rounds.length, "Invalid round");
        return rounds[_roundId].assignedOracles;
    }

    function getRoundEstimates(uint256 _roundId) external view returns (uint16[] memory) {
        require(_roundId < rounds.length, "Invalid round");
        return rounds[_roundId].revealedEstimates;
    }

    function getSafetyMargin() external view returns (uint256) {
        return _getSafetyMargin();
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setNPattOracles(uint256 _val) external onlyOwner {
        nPattOracles = _val;
        emit ParameterUpdated("nPattOracles", _val);
    }

    function setCommitTimeout(uint256 _val) external onlyOwner {
        commitTimeout = _val;
        emit ParameterUpdated("commitTimeout", _val);
    }

    function setRevealTimeout(uint256 _val) external onlyOwner {
        revealTimeout = _val;
        emit ParameterUpdated("revealTimeout", _val);
    }

    function setMsHigh(uint256 _val) external onlyOwner {
        msHigh = _val;
        emit ParameterUpdated("msHigh", _val);
    }

    function setMsMedium(uint256 _val) external onlyOwner {
        msMedium = _val;
        emit ParameterUpdated("msMedium", _val);
    }

    function setMsLow(uint256 _val) external onlyOwner {
        msLow = _val;
        emit ParameterUpdated("msLow", _val);
    }

    function setVolumeHighThreshold(uint256 _val) external onlyOwner {
        volumeHighThreshold = _val;
        emit ParameterUpdated("volumeHighThreshold", _val);
    }

    function setVolumeLowThreshold(uint256 _val) external onlyOwner {
        volumeLowThreshold = _val;
        emit ParameterUpdated("volumeLowThreshold", _val);
    }
}
