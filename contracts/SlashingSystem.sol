// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";
import "./OracleRegistry.sol";

/**
 * @title SlashingSystem
 * @dev Handles slashing disputes against oracles via a jury-based commit-reveal process.
 *
 * Flow:
 *   1. Reporter submits report against an oracle, depositing Creport = Rclaim * Njury
 *   2. Jury of 7 oracles selected pseudo-randomly (excludes watchlisted)
 *   3. Jury members commit-reveal a slash percentage (0-100%)
 *   4. Finalization based on median vote:
 *      - Median > 0: slash = stake * median / 100
 *        - Jury reward deducted from slash
 *        - Residual: 75% to pool, 25% to reporter
 *        - Reporter deposit refunded
 *        - If median > thetaExpulsion (50%): oracle expelled permanently
 *        - If median <= 50%: oracle slashed, may reintegrate
 *      - Median = 0: reporter deposit confiscated, distributed as jury reward
 *
 * All percentages in basis points (10000 = 100%).
 */
contract SlashingSystem is Ownable, ReentrancyGuard {

    // -------------------------------------------------------
    //  External contracts
    // -------------------------------------------------------

    OracleRegistry public oracleRegistry;

    // -------------------------------------------------------
    //  Protocol Parameters
    // -------------------------------------------------------

    /// @dev Number of jury members per dispute
    uint256 public nJury = 7;

    /// @dev Report deposit = rClaim * nJury (default: 0.002 * 7 = 0.014 ETH)
    uint256 public reportDeposit = 0.014 ether;

    /// @dev Reward per jury member (matches rClaim from OracleRegistry)
    uint256 public juryReward = 0.002 ether;

    /// @dev Threshold for permanent expulsion (50% median -> expelled)
    uint256 public thetaExpulsion = 50;

    /// @dev Pool share of residual slash (in basis points, 7500 = 75%)
    uint256 public poolShareBps = 7500;

    /// @dev Commit timeout for jury votes
    uint256 public juryCommitTimeout = 2 days;

    /// @dev Reveal timeout for jury votes
    uint256 public juryRevealTimeout = 2 days;

    // -------------------------------------------------------
    //  Data Structures
    // -------------------------------------------------------

    enum ReportStatus {
        Pending,      // Awaiting jury votes
        Slashed,      // Oracle was slashed
        Expelled,     // Oracle was expelled
        Dismissed     // Report dismissed (median = 0)
    }

    struct Report {
        address reporter;
        address accusedOracle;
        bytes proofs;
        uint256 deposit;
        ReportStatus status;
        address[] jury;
        uint256 commitCount;
        uint256 revealCount;
        uint8[] revealedVotes;   // Slash percentages (0-100)
        uint256 timestamp;
    }

    /// @dev All reports
    Report[] public reports;

    /// @dev Jury commit hashes: reportId => juror => commitHash
    mapping(uint256 => mapping(address => bytes32)) public juryCommits;

    /// @dev Whether juror has revealed: reportId => juror => bool
    mapping(uint256 => mapping(address => bool)) public juryHasRevealed;

    /// @dev Whether juror has committed: reportId => juror => bool
    mapping(uint256 => mapping(address => bool)) public juryHasCommitted;

    /// @dev Accumulated pool funds from slashing
    uint256 public poolBalance;

    // -------------------------------------------------------
    //  Events
    // -------------------------------------------------------

    event ReportSubmitted(uint256 indexed reportId, address indexed reporter,
                          address indexed accusedOracle, uint256 deposit);
    event JurySelected(uint256 indexed reportId, address[] jury);
    event JuryVoteCommitted(uint256 indexed reportId, address indexed juror);
    event JuryVoteRevealed(uint256 indexed reportId, address indexed juror, uint8 slashPercent);
    event SlashingFinalized(uint256 indexed reportId, ReportStatus status,
                            uint256 medianPercent, uint256 slashAmount);
    event ReportDismissed(uint256 indexed reportId);
    event PoolFunded(uint256 amount);
    event PoolWithdrawn(address indexed to, uint256 amount);
    event ParameterUpdated(string param, uint256 value);

    // -------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------

    constructor(address _oracleRegistry) Ownable(msg.sender) {
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));
    }

    // -------------------------------------------------------
    //  Report Submission
    // -------------------------------------------------------

    /**
     * @dev Submit a slashing report against an oracle.
     * Reporter must deposit reportDeposit ETH.
     * @param _accusedOracle The oracle to report
     * @param _proofs Arbitrary proof data (e.g., tx hashes, evidence)
     */
    function submitReport(address _accusedOracle, bytes calldata _proofs) external payable nonReentrant {
        require(msg.value >= reportDeposit, "Insufficient report deposit");

        // Verify accused oracle is active or watchlisted
        (uint256 stake, DataTypes.OracleStatus oStatus,,,,,) = oracleRegistry.getOracleInfo(_accusedOracle);
        require(
            oStatus == DataTypes.OracleStatus.Active ||
            oStatus == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );
        require(stake > 0, "Oracle has no stake");

        // Select jury (excludes watchlisted). We request extra oracles
        // in case the accused is among them, then filter the accused out.
        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.timestamp, block.prevrandao, msg.sender, reports.length
        )));
        // Request nJury + 1 to have room to exclude the accused
        uint256 requestCount = nJury + 1;
        address[] memory candidates = oracleRegistry.selectOracles(seed, requestCount);

        // Filter out accused
        address[] memory selectedJury = new address[](nJury);
        uint256 picked = 0;
        for (uint256 i = 0; i < candidates.length && picked < nJury; i++) {
            if (candidates[i] != _accusedOracle) {
                selectedJury[picked] = candidates[i];
                picked++;
            }
        }
        require(picked == nJury, "Not enough jurors after excluding accused");

        uint256 reportId = reports.length;
        reports.push();
        Report storage r = reports[reportId];
        r.reporter = msg.sender;
        r.accusedOracle = _accusedOracle;
        r.proofs = _proofs;
        r.deposit = msg.value;
        r.status = ReportStatus.Pending;
        r.timestamp = block.timestamp;

        for (uint256 i = 0; i < selectedJury.length; i++) {
            r.jury.push(selectedJury[i]);
        }

        emit ReportSubmitted(reportId, msg.sender, _accusedOracle, msg.value);
        emit JurySelected(reportId, selectedJury);
    }

    // -------------------------------------------------------
    //  Jury Commit-Reveal
    // -------------------------------------------------------

    /**
     * @dev Jury member commits their vote hash.
     * commitHash = keccak256(abi.encodePacked(slashPercent, salt))
     * @param _reportId The report ID
     * @param _commitHash Hash of the vote
     */
    function commitJuryVote(uint256 _reportId, bytes32 _commitHash) external {
        require(_reportId < reports.length, "Invalid report ID");
        Report storage r = reports[_reportId];

        require(r.status == ReportStatus.Pending, "Report not pending");
        require(_isJuror(_reportId, msg.sender), "Not a juror");
        require(!juryHasCommitted[_reportId][msg.sender], "Already committed");
        require(_commitHash != bytes32(0), "Empty commit hash");
        require(
            block.timestamp <= r.timestamp + juryCommitTimeout,
            "Commit period expired"
        );

        juryCommits[_reportId][msg.sender] = _commitHash;
        juryHasCommitted[_reportId][msg.sender] = true;
        r.commitCount++;

        emit JuryVoteCommitted(_reportId, msg.sender);
    }

    /**
     * @dev Jury member reveals their vote.
     * @param _reportId The report ID
     * @param _slashPercent Slash percentage (0-100)
     * @param _salt Salt used in commit
     */
    function revealJuryVote(uint256 _reportId, uint8 _slashPercent, bytes32 _salt) external {
        require(_reportId < reports.length, "Invalid report ID");
        Report storage r = reports[_reportId];

        require(r.status == ReportStatus.Pending, "Report not pending");
        require(_isJuror(_reportId, msg.sender), "Not a juror");
        require(juryHasCommitted[_reportId][msg.sender], "Not committed");
        require(!juryHasRevealed[_reportId][msg.sender], "Already revealed");
        require(_slashPercent <= 100, "Percent must be 0-100");
        require(
            block.timestamp <= r.timestamp + juryCommitTimeout + juryRevealTimeout,
            "Reveal period expired"
        );

        // Verify commit hash
        bytes32 expectedHash = keccak256(abi.encodePacked(_slashPercent, _salt));
        require(juryCommits[_reportId][msg.sender] == expectedHash, "Hash mismatch");

        juryHasRevealed[_reportId][msg.sender] = true;
        r.revealedVotes.push(_slashPercent);
        r.revealCount++;

        emit JuryVoteRevealed(_reportId, msg.sender, _slashPercent);
    }

    // -------------------------------------------------------
    //  Finalization
    // -------------------------------------------------------

    /**
     * @dev Finalize a slashing report after jury reveals (or timeout).
     *
     * If median > 0:
     *   - slash = oracleStake * median / 100
     *   - Deduct juryReward * nJury from slash amount
     *   - Residual: 75% to pool, 25% to reporter
     *   - Refund reporter deposit
     *   - If median > thetaExpulsion: oracle expelled
     *   - Else: oracle slashed, can reintegrate
     *
     * If median = 0:
     *   - Confiscate reporter deposit
     *   - Distribute deposit as jury reward
     */
    function finalizeSlashing(uint256 _reportId) external nonReentrant {
        require(_reportId < reports.length, "Invalid report ID");
        Report storage r = reports[_reportId];
        require(r.status == ReportStatus.Pending, "Report not pending");

        bool allRevealed = r.revealCount == r.jury.length;
        bool timeoutReached = block.timestamp >= r.timestamp + juryCommitTimeout + juryRevealTimeout;
        require(allRevealed || timeoutReached, "Reveal phase not complete");
        require(r.revealCount > 0, "No jury reveals");

        uint256 median = _calculateMedian(r.revealedVotes);

        if (median > 0) {
            // Oracle gets slashed
            (uint256 oracleStake,,,,,,) = oracleRegistry.getOracleInfo(r.accusedOracle);
            uint256 slashAmount = (oracleStake * median) / 100;

            // Slash the oracle in the registry
            oracleRegistry.slashOracle(r.accusedOracle, slashAmount);

            // Calculate distributions
            uint256 totalJuryReward = juryReward * r.revealCount;
            uint256 residual = 0;
            if (slashAmount > totalJuryReward) {
                residual = slashAmount - totalJuryReward;
            } else {
                totalJuryReward = slashAmount; // Cap jury reward at slash amount
            }

            uint256 poolShare = (residual * poolShareBps) / 10000;
            uint256 reporterShare = residual - poolShare;

            // Effects
            if (median > thetaExpulsion) {
                r.status = ReportStatus.Expelled;
                oracleRegistry.expelOracle(r.accusedOracle);
            } else {
                r.status = ReportStatus.Slashed;
            }

            poolBalance += poolShare;

            // Interactions: pay reporter (share + deposit refund)
            uint256 reporterTotal = reporterShare + r.deposit;
            if (reporterTotal > 0) {
                (bool sent1, ) = r.reporter.call{value: reporterTotal}("");
                require(sent1, "Reporter payment failed");
            }

            // Pay jury rewards
            _distributeJuryRewards(_reportId, totalJuryReward);

            emit SlashingFinalized(_reportId, r.status, median, slashAmount);
        } else {
            // Median = 0: dismiss report, confiscate deposit
            r.status = ReportStatus.Dismissed;

            // Distribute confiscated deposit as jury reward
            _distributeJuryRewards(_reportId, r.deposit);

            emit ReportDismissed(_reportId);
            emit SlashingFinalized(_reportId, ReportStatus.Dismissed, 0, 0);
        }
    }

    // -------------------------------------------------------
    //  Internal Helpers
    // -------------------------------------------------------

    function _isJuror(uint256 _reportId, address _juror) internal view returns (bool) {
        address[] storage jury = reports[_reportId].jury;
        for (uint256 i = 0; i < jury.length; i++) {
            if (jury[i] == _juror) return true;
        }
        return false;
    }

    /**
     * @dev Distribute rewards equally among revealed jurors.
     */
    function _distributeJuryRewards(uint256 _reportId, uint256 _totalReward) internal {
        Report storage r = reports[_reportId];
        if (r.revealCount == 0 || _totalReward == 0) return;

        uint256 perJuror = _totalReward / r.revealCount;
        if (perJuror == 0) return;

        for (uint256 i = 0; i < r.jury.length; i++) {
            if (juryHasRevealed[_reportId][r.jury[i]]) {
                (bool sent, ) = r.jury[i].call{value: perJuror}("");
                // Silently skip if transfer fails (juror contract that rejects ETH)
                if (!sent) {
                    poolBalance += perJuror;
                }
            }
        }
    }

    /**
     * @dev Calculate median of a uint8 array (insertion sort).
     */
    function _calculateMedian(uint8[] storage _votes) internal view returns (uint256) {
        uint256 len = _votes.length;
        if (len == 0) return 0;
        if (len == 1) return _votes[0];

        uint8[] memory sorted = new uint8[](len);
        for (uint256 i = 0; i < len; i++) {
            sorted[i] = _votes[i];
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

    // -------------------------------------------------------
    //  View Functions
    // -------------------------------------------------------

    function getReportsCount() external view returns (uint256) {
        return reports.length;
    }

    function getReportInfo(uint256 _reportId) external view returns (
        address reporter,
        address accusedOracle,
        ReportStatus status,
        uint256 deposit,
        uint256 commitCount,
        uint256 revealCount,
        uint256 timestamp
    ) {
        Report storage r = reports[_reportId];
        return (r.reporter, r.accusedOracle, r.status, r.deposit,
                r.commitCount, r.revealCount, r.timestamp);
    }

    function getReportJury(uint256 _reportId) external view returns (address[] memory) {
        return reports[_reportId].jury;
    }

    function getReportVotes(uint256 _reportId) external view returns (uint8[] memory) {
        return reports[_reportId].revealedVotes;
    }

    // -------------------------------------------------------
    //  Pool Management
    // -------------------------------------------------------

    /**
     * @dev Withdraw pool funds to a recipient. Owner only.
     */
    function withdrawPool(address _to, uint256 _amount) external onlyOwner nonReentrant {
        require(_amount <= poolBalance, "Exceeds pool balance");
        poolBalance -= _amount;

        (bool sent, ) = _to.call{value: _amount}("");
        require(sent, "Withdrawal failed");

        emit PoolWithdrawn(_to, _amount);
    }

    // -------------------------------------------------------
    //  Parameter Setters (owner only)
    // -------------------------------------------------------

    function setNJury(uint256 _val) external onlyOwner {
        nJury = _val;
        emit ParameterUpdated("nJury", _val);
    }

    function setReportDeposit(uint256 _val) external onlyOwner {
        reportDeposit = _val;
        emit ParameterUpdated("reportDeposit", _val);
    }

    function setJuryReward(uint256 _val) external onlyOwner {
        juryReward = _val;
        emit ParameterUpdated("juryReward", _val);
    }

    function setThetaExpulsion(uint256 _val) external onlyOwner {
        thetaExpulsion = _val;
        emit ParameterUpdated("thetaExpulsion", _val);
    }

    function setPoolShareBps(uint256 _val) external onlyOwner {
        require(_val <= 10000, "Invalid bps");
        poolShareBps = _val;
        emit ParameterUpdated("poolShareBps", _val);
    }

    function setJuryCommitTimeout(uint256 _val) external onlyOwner {
        juryCommitTimeout = _val;
        emit ParameterUpdated("juryCommitTimeout", _val);
    }

    function setJuryRevealTimeout(uint256 _val) external onlyOwner {
        juryRevealTimeout = _val;
        emit ParameterUpdated("juryRevealTimeout", _val);
    }

    /// @dev Allow contract to receive ETH
    receive() external payable {
        emit PoolFunded(msg.value);
    }
}
