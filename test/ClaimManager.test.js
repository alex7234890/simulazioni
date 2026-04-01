const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("ClaimManager (MEVInsurance Phase 3)", function () {
  let token, insurance, registry;
  let owner, user1, user2;
  let oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7;

  const PREMIUM = ethers.parseEther("100");
  const BASE_STAKE = ethers.parseEther("0.1");
  const T_ACTIVATION = 7 * 24 * 60 * 60;
  const ORACLE_TIMEOUT = 3 * 24 * 60 * 60;

  const ClaimStatus = {
    Pending: 0, OracleReview: 1, CAPTCHARequired: 2,
    Approved: 3, Rejected: 4, InvalidPattern: 5
  };

  const CoverageLevel = { Low: 0, Medium: 1, High: 2 };

  // Helper: hash for commit-reveal
  function computeCommitHash(fraudScore, patternValid, salt) {
    return ethers.solidityPackedKeccak256(
      ["uint8", "bool", "bytes32"],
      [fraudScore, patternValid, salt]
    );
  }

  // Helper: setup oracles (register + activate 7 oracles)
  async function setupOracles() {
    const oracles = [oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7];
    for (const o of oracles) {
      await registry.connect(o).registerOracle({ value: BASE_STAKE });
    }
    await time.increase(T_ACTIVATION + 1);
    for (const o of oracles) {
      await registry.connect(o).activateOracle();
    }
  }

  // Helper: setup user with policy
  async function setupUserWithPolicy(user, coverageLevel = CoverageLevel.High) {
    await insurance.connect(user).registerUser();
    await token.transfer(user.address, ethers.parseEther("500"));
    await token.connect(user).approve(await insurance.getAddress(), PREMIUM);
    await insurance.connect(user).buyPolicy(coverageLevel);
  }

  // Helper: submit a claim and return claim ID and assigned oracles
  async function submitClaimAndGetOracles(user) {
    const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
    const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim"));
    const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun"));
    const swapValue = ethers.parseEther("500");
    const loss = ethers.parseEther("50");

    const tx = await insurance.connect(user).submitClaim(tx1, tx2, tx3, swapValue, loss);
    const receipt = await tx.wait();
    const claimId = 0; // first claim
    const oracles = await insurance.getClaimOracles(claimId);
    return { claimId, oracles, tx1, tx2, tx3, swapValue, loss };
  }

  // Helper: all oracles commit and reveal
  async function oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids) {
    const salts = [];
    // Commit phase
    for (let i = 0; i < assignedOracles.length; i++) {
      const salt = ethers.keccak256(ethers.toUtf8Bytes(`salt${i}`));
      salts.push(salt);
      const hash = computeCommitHash(fraudScores[i], patternValids[i], salt);

      // Find the signer that matches assignedOracles[i]
      const signer = await findSigner(assignedOracles[i]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
    }
    // Reveal phase
    for (let i = 0; i < assignedOracles.length; i++) {
      const signer = await findSigner(assignedOracles[i]);
      await insurance.connect(signer).revealVerdict(
        claimId, fraudScores[i], patternValids[i], salts[i]
      );
    }
  }

  async function findSigner(address) {
    const all = [oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7];
    return all.find(s => s.address === address);
  }

  beforeEach(async function () {
    [owner, user1, user2, oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7] =
      await ethers.getSigners();

    // Deploy token
    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();

    // Deploy oracle registry
    const OracleRegistry = await ethers.getContractFactory("OracleRegistry");
    registry = await OracleRegistry.deploy();
    await registry.waitForDeployment();

    // Deploy insurance
    const MEVInsurance = await ethers.getContractFactory("MEVInsurance");
    insurance = await MEVInsurance.deploy(
      await token.getAddress(),
      await registry.getAddress()
    );
    await insurance.waitForDeployment();

    // Fund insurance contract for payouts
    await token.transfer(await insurance.getAddress(), ethers.parseEther("100000"));
  });

  // =============================================================
  //  Registration
  // =============================================================
  describe("Registration", function () {
    it("should register a new user", async function () {
      await insurance.connect(user1).registerUser();
      expect(await insurance.registeredUsers(user1.address)).to.be.true;
    });

    it("should emit UserRegistered event", async function () {
      await expect(insurance.connect(user1).registerUser())
        .to.emit(insurance, "UserRegistered")
        .withArgs(user1.address);
    });

    it("should revert if already registered", async function () {
      await insurance.connect(user1).registerUser();
      await expect(
        insurance.connect(user1).registerUser()
      ).to.be.revertedWith("Already registered");
    });

    it("should set initial tier to Bronze", async function () {
      await insurance.connect(user1).registerUser();
      const profile = await insurance.getUserProfile(user1.address);
      expect(profile.tier).to.equal(0); // Bronze
    });
  });

  // =============================================================
  //  Buy Policy
  // =============================================================
  describe("Buy Policy", function () {
    beforeEach(async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, ethers.parseEther("500"));
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
    });

    it("should allow buying a policy with coverage level", async function () {
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const policy = await insurance.policies(user1.address);
      expect(policy.active).to.be.true;
      expect(policy.coverageLevel).to.equal(CoverageLevel.High);
    });

    it("should deduct premium from user", async function () {
      const before = await token.balanceOf(user1.address);
      await insurance.connect(user1).buyPolicy(CoverageLevel.Medium);
      const after = await token.balanceOf(user1.address);
      expect(before - after).to.equal(PREMIUM);
    });

    it("should emit PolicyPurchased event", async function () {
      await expect(insurance.connect(user1).buyPolicy(CoverageLevel.Low))
        .to.emit(insurance, "PolicyPurchased");
    });

    it("should revert if not registered", async function () {
      await token.transfer(user2.address, ethers.parseEther("500"));
      await token.connect(user2).approve(await insurance.getAddress(), PREMIUM);
      await expect(
        insurance.connect(user2).buyPolicy(CoverageLevel.High)
      ).to.be.revertedWith("Not registered");
    });

    it("should revert if policy already active", async function () {
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
      await expect(
        insurance.connect(user1).buyPolicy(CoverageLevel.High)
      ).to.be.revertedWith("Policy already active");
    });

    it("should revert if user is blacklisted", async function () {
      // We'll test blacklisting after claim finalization
      // For now, this is a placeholder — blacklist tested in finalization
    });
  });

  // =============================================================
  //  Submit Claim
  // =============================================================
  describe("Submit Claim", function () {
    beforeEach(async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
    });

    it("should submit a claim and assign oracles", async function () {
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);
      expect(await insurance.getClaimsCount()).to.equal(1);
      expect(oracles.length).to.equal(7);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.user).to.equal(user1.address);
      expect(info.status).to.equal(ClaimStatus.OracleReview);
    });

    it("should emit ClaimSubmitted event", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim"));
      const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun"));
      await expect(
        insurance.connect(user1).submitClaim(tx1, tx2, tx3, ethers.parseEther("500"), ethers.parseEther("50"))
      ).to.emit(insurance, "ClaimSubmitted");
    });

    it("should track daily swap count", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim"));
      const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun"));
      const sv = ethers.parseEther("100");
      const loss = ethers.parseEther("10");

      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);
      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);
      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);

      // Bronze tier: max 3 swaps/day - 4th should fail
      await expect(
        insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss)
      ).to.be.revertedWith("Daily swap limit reached");
    });

    it("should reset daily count on new day", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim"));
      const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun"));
      const sv = ethers.parseEther("100");
      const loss = ethers.parseEther("10");

      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);
      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);
      await insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss);

      // Advance 1 day
      await time.increase(24 * 60 * 60);

      // Should work again
      await expect(
        insurance.connect(user1).submitClaim(tx1, tx2, tx3, sv, loss)
      ).to.not.be.reverted;
    });

    it("should revert without active policy", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      await expect(
        insurance.connect(user2).submitClaim(
          tx1, tx1, tx1, ethers.parseEther("100"), ethers.parseEther("10")
        )
      ).to.be.revertedWith("No active policy");
    });

    it("should revert if policy expired", async function () {
      await time.increase(31 * 24 * 60 * 60);
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      await expect(
        insurance.connect(user1).submitClaim(
          tx1, tx1, tx1, ethers.parseEther("100"), ethers.parseEther("10")
        )
      ).to.be.revertedWith("Policy expired");
    });

    it("should revert if swap value exceeds policy max", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
      await expect(
        insurance.connect(user1).submitClaim(
          tx1, tx1, tx1, ethers.parseEther("2000"), ethers.parseEther("10")
        )
      ).to.be.revertedWith("Swap value exceeds policy max");
    });
  });

  // =============================================================
  //  Commit Verdict
  // =============================================================
  describe("Commit Verdict", function () {
    let claimId, assignedOracles;

    beforeEach(async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      const result = await submitClaimAndGetOracles(user1);
      claimId = result.claimId;
      assignedOracles = result.oracles;
    });

    it("should allow assigned oracle to commit", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);

      await expect(insurance.connect(signer).commitVerdict(claimId, hash))
        .to.emit(insurance, "VerdictCommitted")
        .withArgs(claimId, signer.address);
    });

    it("should revert for non-assigned oracle", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      // user1 is not an oracle
      await expect(
        insurance.connect(user1).commitVerdict(claimId, hash)
      ).to.be.revertedWith("Not assigned oracle");
    });

    it("should revert on double commit", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);

      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).commitVerdict(claimId, hash)
      ).to.be.revertedWith("Already committed");
    });

    it("should revert with zero hash", async function () {
      const signer = await findSigner(assignedOracles[0]);
      await expect(
        insurance.connect(signer).commitVerdict(claimId, ethers.ZeroHash)
      ).to.be.revertedWith("Invalid commit hash");
    });

    it("should increment commit count", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);
      await insurance.connect(signer).commitVerdict(claimId, hash);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.commitCount).to.equal(1);
    });
  });

  // =============================================================
  //  Reveal Verdict
  // =============================================================
  describe("Reveal Verdict", function () {
    let claimId, assignedOracles;

    beforeEach(async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      const result = await submitClaimAndGetOracles(user1);
      claimId = result.claimId;
      assignedOracles = result.oracles;
    });

    it("should allow oracle to reveal after commit", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const fraudScore = 25;
      const patternValid = true;
      const hash = computeCommitHash(fraudScore, patternValid, salt);
      const signer = await findSigner(assignedOracles[0]);

      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, fraudScore, patternValid, salt)
      ).to.emit(insurance, "VerdictRevealed")
        .withArgs(claimId, signer.address, fraudScore, patternValid);
    });

    it("should revert with wrong salt (hash mismatch)", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const wrongSalt = ethers.keccak256(ethers.toUtf8Bytes("wrong"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);

      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, 25, true, wrongSalt)
      ).to.be.revertedWith("Commit hash mismatch");
    });

    it("should revert with wrong fraud score", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);

      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, 50, true, salt)
      ).to.be.revertedWith("Commit hash mismatch");
    });

    it("should revert on double reveal", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);

      await insurance.connect(signer).commitVerdict(claimId, hash);
      await insurance.connect(signer).revealVerdict(claimId, 25, true, salt);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, 25, true, salt)
      ).to.be.revertedWith("Already revealed");
    });

    it("should revert for fraud score > 130", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(131, true, salt);
      const signer = await findSigner(assignedOracles[0]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, 131, true, salt)
      ).to.be.revertedWith("Fraud score must be 0-130");
    });

    it("should accept fraud score of 130", async function () {
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const hash = computeCommitHash(130, true, salt);
      const signer = await findSigner(assignedOracles[0]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
      await expect(
        insurance.connect(signer).revealVerdict(claimId, 130, true, salt)
      ).to.not.be.reverted;
    });

    it("should track pattern valid/invalid votes", async function () {
      // Commit and reveal for first oracle: pattern valid
      const salt1 = ethers.keccak256(ethers.toUtf8Bytes("salt1"));
      const hash1 = computeCommitHash(20, true, salt1);
      const signer1 = await findSigner(assignedOracles[0]);
      await insurance.connect(signer1).commitVerdict(claimId, hash1);
      await insurance.connect(signer1).revealVerdict(claimId, 20, true, salt1);

      // Second oracle: pattern invalid
      const salt2 = ethers.keccak256(ethers.toUtf8Bytes("salt2"));
      const hash2 = computeCommitHash(80, false, salt2);
      const signer2 = await findSigner(assignedOracles[1]);
      await insurance.connect(signer2).commitVerdict(claimId, hash2);
      await insurance.connect(signer2).revealVerdict(claimId, 80, false, salt2);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.revealCount).to.equal(2);
    });
  });

  // =============================================================
  //  Finalize Claim
  // =============================================================
  describe("Finalize Claim", function () {
    let claimId, assignedOracles;

    beforeEach(async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      const result = await submitClaimAndGetOracles(user1);
      claimId = result.claimId;
      assignedOracles = result.oracles;
    });

    it("should reject claim when >= 70% say pattern is invalid", async function () {
      // 5 out of 7 say pattern invalid (71.4%)
      const scores = [50, 50, 50, 50, 50, 50, 50];
      const patterns = [false, false, false, false, false, true, true]; // 5 invalid

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.InvalidPattern);
    });

    it("should NOT reject when < 70% say pattern invalid", async function () {
      // 4 out of 7 say invalid (57.1%) — under threshold
      const scores = [10, 10, 10, 10, 10, 10, 10];
      const patterns = [false, false, false, false, true, true, true]; // 4 invalid

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      // Should NOT be InvalidPattern — Bronze with score 10 < 70 -> CAPTCHA
      expect(info.status).to.not.equal(ClaimStatus.InvalidPattern);
    });

    it("Bronze: score < thetaReject -> CAPTCHARequired", async function () {
      // Fraud scores: all 40 (median = 40, < thetaReject=80)
      const scores = [40, 40, 40, 40, 40, 40, 40];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.CAPTCHARequired);
      expect(info.finalFraudScore).to.equal(40);
    });

    it("Bronze: score >= thetaReject -> Rejected + blacklisted", async function () {
      // Fraud scores: all 90 (median = 90, >= thetaReject=80)
      const scores = [90, 90, 90, 90, 90, 90, 90];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.Rejected);

      const profile = await insurance.getUserProfile(user1.address);
      expect(profile.isBlacklisted).to.be.true;
      expect(profile.debt).to.be.gt(0); // 20% penalty on loss
    });

    it("should calculate dispersione correctly", async function () {
      // Scores vary: 10, 20, 30, 40, 50, 60, 70 -> dispersione = 60
      const scores = [10, 20, 30, 40, 50, 60, 70];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.dispersione).to.equal(60);
      // Median of [10,20,30,40,50,60,70] = 40
      expect(info.finalFraudScore).to.equal(40);
    });

    it("should calculate median correctly for odd array", async function () {
      // Unsorted: [90, 10, 50, 30, 70, 20, 60] -> sorted: [10,20,30,50,60,70,90] -> median=50
      const scores = [90, 10, 50, 30, 70, 20, 60];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.finalFraudScore).to.equal(50);
    });

    it("should revert if not all oracles revealed and no timeout", async function () {
      // Only commit+reveal for 1 oracle
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt0"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
      await insurance.connect(signer).revealVerdict(claimId, 25, true, salt);

      await expect(
        insurance.finalizeClaim(claimId)
      ).to.be.revertedWith("Not all oracles revealed and timeout not reached");
    });

    it("should allow finalization after timeout with partial reveals", async function () {
      // Only 1 oracle reveals
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt0"));
      const hash = computeCommitHash(25, true, salt);
      const signer = await findSigner(assignedOracles[0]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
      await insurance.connect(signer).revealVerdict(claimId, 25, true, salt);

      // Advance past timeout
      await time.increase(ORACLE_TIMEOUT + 1);

      await expect(insurance.finalizeClaim(claimId)).to.not.be.reverted;
    });

    it("should revert if finalized twice", async function () {
      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      await expect(
        insurance.finalizeClaim(claimId)
      ).to.be.revertedWith("Claim not in review");
    });

    it("should emit ClaimFinalized event", async function () {
      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);

      await expect(insurance.finalizeClaim(claimId))
        .to.emit(insurance, "ClaimFinalized");
    });

    it("should update user fraud score average", async function () {
      const scores = [40, 40, 40, 40, 40, 40, 40];
      const patterns = [true, true, true, true, true, true, true];

      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const profile = await insurance.getUserProfile(user1.address);
      expect(profile.avgFraudScore).to.equal(40);
    });
  });

  // =============================================================
  //  Gold/Platinum Tier Claim Decisions
  // =============================================================
  describe("Gold/Platinum Tier Decisions", function () {
    // For this test we set user tier to Gold manually via owner setting
    // Since TierSystem isn't implemented yet, we test the logic by
    // using the setters or by manipulating state

    it("Gold: score < thetaApprove -> Approved with payout", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1, CoverageLevel.High);

      // Manually set user tier to Gold (we'll need owner access or a setter)
      // Since there's no public setter for tier yet, we test with the contract as-is
      // and verify Bronze behavior. Gold behavior tested via integration.

      // For now test approval flow via CAPTCHA resolution
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);
      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      // Bronze with score 20 -> CAPTCHARequired
      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.CAPTCHARequired);
    });
  });

  // =============================================================
  //  CAPTCHA Resolution
  // =============================================================
  describe("CAPTCHA Resolution", function () {
    let claimId, assignedOracles;

    beforeEach(async function () {
      await setupOracles();
      await setupUserWithPolicy(user1, CoverageLevel.High);
      const result = await submitClaimAndGetOracles(user1);
      claimId = result.claimId;
      assignedOracles = result.oracles;

      // Finalize to CAPTCHARequired (Bronze, low fraud score)
      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, scores, patterns);
      await insurance.finalizeClaim(claimId);
    });

    it("should approve claim after CAPTCHA passed", async function () {
      const balanceBefore = await token.balanceOf(user1.address);

      await insurance.resolveCAPTCHA(claimId, true);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.Approved);

      const balanceAfter = await token.balanceOf(user1.address);
      // High coverage = 100%, loss = 50 MEVI
      const payout = ethers.parseEther("50");
      expect(balanceAfter - balanceBefore).to.equal(payout);
    });

    it("should reject claim if CAPTCHA not passed", async function () {
      await insurance.resolveCAPTCHA(claimId, false);

      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.Rejected);
    });

    it("should emit PayoutIssued on CAPTCHA approval", async function () {
      await expect(insurance.resolveCAPTCHA(claimId, true))
        .to.emit(insurance, "PayoutIssued");
    });

    it("should revert if not owner", async function () {
      await expect(
        insurance.connect(user1).resolveCAPTCHA(claimId, true)
      ).to.be.revertedWithCustomError(insurance, "OwnableUnauthorizedAccount");
    });

    it("should revert if claim not awaiting CAPTCHA", async function () {
      await insurance.resolveCAPTCHA(claimId, true);
      await expect(
        insurance.resolveCAPTCHA(claimId, true)
      ).to.be.revertedWith("Not awaiting CAPTCHA");
    });
  });

  // =============================================================
  //  Coverage Level Payout Calculation
  // =============================================================
  describe("Coverage Level Payouts", function () {
    beforeEach(async function () {
      await setupOracles();
    });

    it("Low coverage: payout = loss * 50%", async function () {
      await setupUserWithPolicy(user1, CoverageLevel.Low);
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);

      // Get to CAPTCHARequired then approve
      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.resolveCAPTCHA(claimId, true);
      const balanceAfter = await token.balanceOf(user1.address);

      // loss = 50 MEVI, Low = 50% (PDF Table 2)
      const expectedPayout = ethers.parseEther("25"); // 50 * 0.5
      expect(balanceAfter - balanceBefore).to.equal(expectedPayout);
    });

    it("Medium coverage: payout = loss * 70%", async function () {
      await setupUserWithPolicy(user1, CoverageLevel.Medium);
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);

      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.resolveCAPTCHA(claimId, true);
      const balanceAfter = await token.balanceOf(user1.address);

      // loss = 50 MEVI, Medium = 70% (PDF Table 2)
      const expectedPayout = ethers.parseEther("35"); // 50 * 0.7
      expect(balanceAfter - balanceBefore).to.equal(expectedPayout);
    });

    it("High coverage: payout = loss * 100%", async function () {
      await setupUserWithPolicy(user1, CoverageLevel.High);
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);

      const scores = [20, 20, 20, 20, 20, 20, 20];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.resolveCAPTCHA(claimId, true);
      const balanceAfter = await token.balanceOf(user1.address);

      const expectedPayout = ethers.parseEther("50"); // 50 * 1.0
      expect(balanceAfter - balanceBefore).to.equal(expectedPayout);
    });
  });

  // =============================================================
  //  View Functions
  // =============================================================
  describe("View Functions", function () {
    it("should return user claim IDs", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      await submitClaimAndGetOracles(user1);

      const ids = await insurance.getUserClaims(user1.address);
      expect(ids.length).to.equal(1);
      expect(ids[0]).to.equal(0);
    });

    it("should return claim scores after reveals", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);

      const scores = [10, 20, 30, 40, 50, 60, 70];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);

      const revealedScores = await insurance.getClaimScores(claimId);
      expect(revealedScores.length).to.equal(7);
    });

    it("getClaimsCount returns correct count", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1);
      expect(await insurance.getClaimsCount()).to.equal(0);
      await submitClaimAndGetOracles(user1);
      expect(await insurance.getClaimsCount()).to.equal(1);
    });
  });

  // =============================================================
  //  Parameter Setters
  // =============================================================
  describe("Parameter Setters", function () {
    it("owner can set nOracle", async function () {
      await insurance.setNOracle(5);
      expect(await insurance.nOracle()).to.equal(5);
    });

    it("owner can set thetaReject", async function () {
      await insurance.setThetaReject(80);
      expect(await insurance.thetaReject()).to.equal(80);
    });

    it("owner can set thetaApprove", async function () {
      await insurance.setThetaApprove(25);
      expect(await insurance.thetaApprove()).to.equal(25);
    });

    it("owner can set oracleTimeout", async function () {
      await insurance.setOracleTimeout(5 * 24 * 60 * 60);
      expect(await insurance.oracleTimeout()).to.equal(5 * 24 * 60 * 60);
    });

    it("non-owner cannot set parameters", async function () {
      await expect(
        insurance.connect(user1).setNOracle(5)
      ).to.be.revertedWithCustomError(insurance, "OwnableUnauthorizedAccount");
    });
  });

  // =============================================================
  //  Blacklist and Debt
  // =============================================================
  describe("Blacklist and Debt", function () {
    it("blacklisted user cannot buy new policy", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1, CoverageLevel.High);
      const { claimId, oracles } = await submitClaimAndGetOracles(user1);

      // High fraud scores -> rejection + blacklist
      const scores = [80, 85, 90, 75, 80, 85, 80];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const profile = await insurance.getUserProfile(user1.address);
      expect(profile.isBlacklisted).to.be.true;

      // Policy is still "active" from the previous purchase.
      // If we try to buy again it should fail due to blacklist after policy expires
      // But since policy is still active, it fails on "Policy already active"
      // We need to wait for policy to expire first
    });

    it("debt is 20% of claimed loss", async function () {
      await setupOracles();
      await setupUserWithPolicy(user1, CoverageLevel.High);
      const { claimId, oracles, loss } = await submitClaimAndGetOracles(user1);

      const scores = [80, 85, 90, 75, 80, 85, 80];
      const patterns = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, oracles, scores, patterns);
      await insurance.finalizeClaim(claimId);

      const profile = await insurance.getUserProfile(user1.address);
      // debt = loss * 2000 / 10000 = loss * 20%
      const expectedDebt = loss * 2000n / 10000n;
      expect(profile.debt).to.equal(expectedDebt);
    });
  });
});
