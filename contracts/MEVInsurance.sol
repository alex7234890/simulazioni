// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title MEVInsurance
 * @dev Insurance contract protecting traders against MEV extraction.
 *
 * Users register, purchase policies by paying a premium in MEVI tokens,
 * and submit claims when they experience MEV attacks. The contract owner
 * (or oracle) approves claims and pays out coverage.
 *
 * Policy Parameters:
 *   - Premium: 100 MEVI
 *   - Coverage: 1,000 MEVI
 *   - Duration: 30 days
 */
contract MEVInsurance is Ownable {
    IERC20 public token;

    uint256 public constant PREMIUM = 100 * 1e18;
    uint256 public constant COVERAGE = 1000 * 1e18;
    uint256 public constant POLICY_DURATION = 30 days;

    struct Policy {
        bool active;
        uint256 startTime;
        uint256 endTime;
        uint256 coverageRemaining;
    }

    enum ClaimStatus { Pending, Approved, Rejected }

    struct Claim {
        address claimant;
        uint256 amount;
        string description;
        ClaimStatus status;
    }

    /// @dev Registered users
    mapping(address => bool) public registeredUsers;

    /// @dev User policies
    mapping(address => Policy) public policies;

    /// @dev All submitted claims
    Claim[] public claims;

    /// @dev Mapping from user to their claim IDs
    mapping(address => uint256[]) public userClaims;

    event UserRegistered(address indexed user);
    event PolicyPurchased(address indexed user, uint256 startTime, uint256 endTime);
    event ClaimSubmitted(uint256 indexed claimId, address indexed claimant, uint256 amount);
    event ClaimApproved(uint256 indexed claimId, uint256 payout);
    event ClaimRejected(uint256 indexed claimId);

    constructor(address _token) Ownable(msg.sender) {
        token = IERC20(_token);
    }

    /**
     * @dev Register as a user in the insurance system.
     */
    function registerUser() external {
        require(!registeredUsers[msg.sender], "Already registered");
        registeredUsers[msg.sender] = true;
        emit UserRegistered(msg.sender);
    }

    /**
     * @dev Purchase an insurance policy by paying the premium in MEVI tokens.
     * Requires prior token approval for the premium amount.
     */
    function buyPolicy() external {
        require(registeredUsers[msg.sender], "Not registered");
        require(!policies[msg.sender].active, "Policy already active");

        // Transfer premium from user to contract
        require(
            token.transferFrom(msg.sender, address(this), PREMIUM),
            "Premium transfer failed"
        );

        uint256 start = block.timestamp;
        uint256 end = start + POLICY_DURATION;

        policies[msg.sender] = Policy({
            active: true,
            startTime: start,
            endTime: end,
            coverageRemaining: COVERAGE
        });

        emit PolicyPurchased(msg.sender, start, end);
    }

    /**
     * @dev Submit a claim for MEV losses. Requires an active policy.
     * @param _amount The claimed loss amount in MEVI tokens.
     * @param _description Description of the MEV attack.
     */
    function submitClaim(uint256 _amount, string calldata _description) external {
        Policy storage policy = policies[msg.sender];
        require(policy.active, "No active policy");
        require(block.timestamp <= policy.endTime, "Policy expired");
        require(_amount > 0 && _amount <= policy.coverageRemaining, "Invalid claim amount");

        uint256 claimId = claims.length;
        claims.push(Claim({
            claimant: msg.sender,
            amount: _amount,
            description: _description,
            status: ClaimStatus.Pending
        }));

        userClaims[msg.sender].push(claimId);
        emit ClaimSubmitted(claimId, msg.sender, _amount);
    }

    /**
     * @dev Approve a pending claim and pay out to the claimant. Owner only.
     * @param _claimId The ID of the claim to approve.
     */
    function approveClaim(uint256 _claimId) external onlyOwner {
        Claim storage claim = claims[_claimId];
        require(claim.status == ClaimStatus.Pending, "Claim not pending");

        Policy storage policy = policies[claim.claimant];
        require(policy.coverageRemaining >= claim.amount, "Insufficient coverage");

        claim.status = ClaimStatus.Approved;
        policy.coverageRemaining -= claim.amount;

        require(
            token.transfer(claim.claimant, claim.amount),
            "Payout transfer failed"
        );

        emit ClaimApproved(_claimId, claim.amount);
    }

    /**
     * @dev Reject a pending claim. Owner only.
     * @param _claimId The ID of the claim to reject.
     */
    function rejectClaim(uint256 _claimId) external onlyOwner {
        Claim storage claim = claims[_claimId];
        require(claim.status == ClaimStatus.Pending, "Claim not pending");

        claim.status = ClaimStatus.Rejected;
        emit ClaimRejected(_claimId);
    }

    /**
     * @dev Get the total number of claims submitted.
     */
    function getClaimsCount() external view returns (uint256) {
        return claims.length;
    }

    /**
     * @dev Get all claim IDs for a given user.
     */
    function getUserClaims(address _user) external view returns (uint256[] memory) {
        return userClaims[_user];
    }
}
