// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./libraries/DataTypes.sol";
import "./OracleRegistry.sol";

contract SlashingSystem is Ownable, ReentrancyGuard {

    OracleRegistry public oracleRegistry;

    uint256 public nJury = 7;
    uint256 public reportDeposit = 0.014 ether;
    uint256 public juryReward = 0.002 ether;
    uint256 public thetaExpulsion = 50;
    uint256 public poolShareBps = 7500;
    uint256 public juryCommitTimeout = 2 days;
    uint256 public juryRevealTimeout = 2 days;

    enum ReportStatus {
        Pending,
        Slashed,
        Expelled,
        Dismissed
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
        uint8[] revealedVotes;
        uint256 timestamp;
    }

    Report[] public reports;
    mapping(uint256 => mapping(address => bytes32)) public juryCommits;
    mapping(uint256 => mapping(address => bool)) public juryHasRevealed;
    mapping(uint256 => mapping(address => bool)) public juryHasCommitted;
    uint256 public totalSlashedToPoolCumulative;
    address public mevInsurance;

    event MEVInsuranceSet(address indexed addr);
    event ReportSubmitted(uint256 indexed reportId, address indexed reporter,
                          address indexed accusedOracle, uint256 deposit);
    event JurySelected(uint256 indexed reportId, address[] jury);
    event JuryVoteCommitted(uint256 indexed reportId, address indexed juror);
    event JuryVoteRevealed(uint256 indexed reportId, address indexed juror, uint8 slashPercent);
    event SlashingFinalized(uint256 indexed reportId, ReportStatus status,
                            uint256 medianPercent, uint256 slashAmount);
    event ReportDismissed(uint256 indexed reportId);
    event PoolFunded(uint256 amount);
    event ParameterUpdated(string param, uint256 value);

    constructor(address _oracleRegistry) Ownable(msg.sender) {
        oracleRegistry = OracleRegistry(payable(_oracleRegistry));
    }

    function setMEVInsurance(address _addr) external onlyOwner {
        require(_addr != address(0), "Zero address");
        mevInsurance = _addr;
        emit MEVInsuranceSet(_addr);
    }

    function submitReport(address _accusedOracle, bytes calldata _proofs) external payable nonReentrant {
        require(msg.value >= reportDeposit, "Insufficient report deposit");

        (uint256 stake, DataTypes.OracleStatus oStatus,,,,,) = oracleRegistry.getOracleInfo(_accusedOracle);
        require(
            oStatus == DataTypes.OracleStatus.Active ||
            oStatus == DataTypes.OracleStatus.Watchlisted,
            "Oracle not active"
        );
        require(stake > 0, "Oracle has no stake");

        // Request nJury+2 candidates so after filtering the accused we still have nJury
        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.timestamp, block.prevrandao, msg.sender, reports.length
        )));
        address[] memory candidates = oracleRegistry.selectOracles(seed, nJury + 2);

        address[] memory selectedJury = new address[](nJury);
        uint256 picked = 0;
        for (uint256 i = 0; i < candidates.length && picked < nJury; i++) {
            if (candidates[i] != _accusedOracle && candidates[i] != msg.sender) {
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

        bytes32 expectedHash = keccak256(abi.encodePacked(_slashPercent, _salt));
        require(juryCommits[_reportId][msg.sender] == expectedHash, "Hash mismatch");

        juryHasRevealed[_reportId][msg.sender] = true;
        r.revealedVotes.push(_slashPercent);
        r.revealCount++;
        emit JuryVoteRevealed(_reportId, msg.sender, _slashPercent);
    }

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
            (uint256 oracleStake,,,,,,) = oracleRegistry.getOracleInfo(r.accusedOracle);
            uint256 slashAmount = (oracleStake * median) / 100;

            oracleRegistry.slashOracle(r.accusedOracle, slashAmount, address(this));

            uint256 totalJuryReward = juryReward * r.revealCount;
            uint256 residual = 0;
            if (slashAmount > totalJuryReward) {
                residual = slashAmount - totalJuryReward;
            } else {
                totalJuryReward = slashAmount;
            }

            uint256 reporterShare = (residual * 2500) / 10000;
            uint256 poolShare = residual - reporterShare;

            if (median > thetaExpulsion) {
                r.status = ReportStatus.Expelled;
                oracleRegistry.expelOracle(r.accusedOracle);
            } else {
                r.status = ReportStatus.Slashed;
            }

            totalSlashedToPoolCumulative += poolShare;

            uint256 reporterTotal = r.deposit + reporterShare;
            if (reporterTotal > 0) {
                (bool sentRep, ) = r.reporter.call{value: reporterTotal}("");
                require(sentRep, "Reporter payment failed");
            }

            _distributeJuryRewards(_reportId, totalJuryReward);

            if (poolShare > 0) {
                require(mevInsurance != address(0), "MEVInsurance not set");
                (bool sentPool, ) = mevInsurance.call{value: poolShare}("");
                require(sentPool, "Pool transfer failed");
            }

            emit SlashingFinalized(_reportId, r.status, median, slashAmount);
        } else {
            r.status = ReportStatus.Dismissed;
            _distributeJuryRewards(_reportId, r.deposit);
            emit ReportDismissed(_reportId);
            emit SlashingFinalized(_reportId, ReportStatus.Dismissed, 0, 0);
        }
    }

    function _isJuror(uint256 _reportId, address _juror) internal view returns (bool) {
        address[] storage jury = reports[_reportId].jury;
        for (uint256 i = 0; i < jury.length; i++) {
            if (jury[i] == _juror) return true;
        }
        return false;
    }

    function _distributeJuryRewards(uint256 _reportId, uint256 _totalReward) internal {
        Report storage r = reports[_reportId];
        if (r.revealCount == 0 || _totalReward == 0) return;

        uint256 perJuror = _totalReward / r.revealCount;
        if (perJuror == 0) return;

        for (uint256 i = 0; i < r.jury.length; i++) {
            if (juryHasRevealed[_reportId][r.jury[i]]) {
                (bool sent, ) = r.jury[i].call{value: perJuror}("");
                // Fallback: forward to pool if juror rejects ETH
                if (!sent && mevInsurance != address(0)) {
                    (bool fwd, ) = mevInsurance.call{value: perJuror}("");
                    if (fwd) totalSlashedToPoolCumulative += perJuror;
                }
            }
        }
    }

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

    receive() external payable {
        emit PoolFunded(msg.value);
    }
}
