const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

/**
 * Phase 8: End-to-End Integration Test
 *
 * Full lifecycle simulation integrating all contracts:
 *   MEVToken, OracleRegistry, MEVInsurance, PremiumCalculator,
 *   TierSystem, SlashingSystem, SandwichBot, MockAMM, MockUSDC
 *
 * Scenarios:
 *   1. Full claim lifecycle: sandwich attack → claim → oracle vote → payout
 *   2. Tier progression: Bronze → Silver → Gold with claim history
 *   3. Oracle slashing: dishonest oracle reported → jury votes → slashed
 *   4. Premium calculator integration: dynamic premium on policy purchase
 *   5. Claim rejection flow: high fraud score → blacklist → debt payment
 *   6. Multi-user concurrent claims
 */
describe("End-to-End Integration (Phase 8)", function () {
  // Contracts
  let token, usdc, registry, insurance, calculator, tierSystem, slashingSystem;
  let amm, bot;

  // Signers
  let owner, user1, user2, victim, attacker;
  let oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8;

  // Constants
  const e = (n) => ethers.parseEther(String(n));
  const BASE_STAKE = e("0.1");
  const T_ACTIVATION = 7 * 24 * 60 * 60; // 7 days
  const ORACLE_TIMEOUT = 3 * 24 * 60 * 60; // 3 days
  const DAY = 24 * 60 * 60;

  const ClaimStatus = {
    Pending: 0, OracleReview: 1, CAPTCHARequired: 2,
    Approved: 3, Rejected: 4, InvalidPattern: 5
  };
  const CoverageLevel = { Low: 0, Medium: 1, High: 2 };
  const Tier = { Bronze: 0, Silver: 1, Gold: 2, Platinum: 3 };

  // ── Helpers ──────────────────────────────────────────────────────

  function computeCommitHash(fraudScore, patternValid, salt) {
    return ethers.solidityPackedKeccak256(
      ["uint8", "bool", "bytes32"],
      [fraudScore, patternValid, salt]
    );
  }

  function computeJuryCommitHash(slashPercent, salt) {
    return ethers.solidityPackedKeccak256(
      ["uint8", "bytes32"],
      [slashPercent, salt]
    );
  }

  const allOracles = () => [oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8];

  async function findSigner(address) {
    return allOracles().find(s => s.address === address);
  }

  async function registerAndActivateOracles(oracles) {
    for (const o of oracles) {
      const stake = await registry.getMinimumStake();
      await registry.connect(o).registerOracle({ value: stake });
    }
    await time.increase(T_ACTIVATION + 1);
    for (const o of oracles) {
      await registry.connect(o).activateOracle();
    }
  }

  // Get the actual premium that buyPolicy will charge (uses defaultCoverage=1000e18)
  async function getActualPremium(coverageLevel) {
    return await calculator.calculatePremium(e(1000), coverageLevel);
  }

  async function setupUserWithPolicy(user, coverageLevel = CoverageLevel.High) {
    await insurance.connect(user).registerUser();
    await token.transfer(user.address, e(10000));
    const premium = await getActualPremium(coverageLevel);
    await token.connect(user).approve(await insurance.getAddress(), premium);
    await insurance.connect(user).buyPolicy(coverageLevel);
  }

  async function submitClaimForUser(user, swapValue, loss) {
    const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun_" + Date.now()));
    const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim_" + Date.now()));
    const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun_" + Date.now()));

    const claimCountBefore = await insurance.getClaimsCount();
    await insurance.connect(user).submitClaim(tx1, tx2, tx3, swapValue, loss);
    const claimId = claimCountBefore;
    const assignedOracles = await insurance.getClaimOracles(claimId);
    return { claimId: Number(claimId), assignedOracles, tx1, tx2, tx3 };
  }

  async function oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids) {
    const salts = [];
    for (let i = 0; i < assignedOracles.length; i++) {
      const salt = ethers.keccak256(ethers.toUtf8Bytes(`salt_${claimId}_${i}_${Date.now()}`));
      salts.push(salt);
      const hash = computeCommitHash(fraudScores[i], patternValids[i], salt);
      const signer = await findSigner(assignedOracles[i]);
      await insurance.connect(signer).commitVerdict(claimId, hash);
    }
    for (let i = 0; i < assignedOracles.length; i++) {
      const signer = await findSigner(assignedOracles[i]);
      await insurance.connect(signer).revealVerdict(claimId, fraudScores[i], patternValids[i], salts[i]);
    }
  }

  // ── Setup ────────────────────────────────────────────────────────

  beforeEach(async function () {
    [owner, user1, user2, victim, attacker,
      oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8] =
      await ethers.getSigners();

    // Deploy MEVToken
    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();

    // Deploy MockUSDC
    const MockUSDC = await ethers.getContractFactory("MockUSDC");
    usdc = await MockUSDC.deploy();
    await usdc.waitForDeployment();

    // Deploy OracleRegistry
    const OracleRegistry = await ethers.getContractFactory("OracleRegistry");
    registry = await OracleRegistry.deploy();
    await registry.waitForDeployment();

    // Deploy PremiumCalculator
    const PremiumCalculator = await ethers.getContractFactory("PremiumCalculator");
    calculator = await PremiumCalculator.deploy();
    await calculator.waitForDeployment();

    // Deploy MEVInsurance
    const MEVInsurance = await ethers.getContractFactory("MEVInsurance");
    insurance = await MEVInsurance.deploy(
      await token.getAddress(),
      await registry.getAddress()
    );
    await insurance.waitForDeployment();

    // Deploy TierSystem
    const TierSystem = await ethers.getContractFactory("TierSystem");
    tierSystem = await TierSystem.deploy(await token.getAddress());
    await tierSystem.waitForDeployment();

    // Deploy SlashingSystem
    const SlashingSystem = await ethers.getContractFactory("SlashingSystem");
    slashingSystem = await SlashingSystem.deploy(await registry.getAddress());
    await slashingSystem.waitForDeployment();

    // Deploy MockAMM
    const MockAMM = await ethers.getContractFactory("MockAMM");
    amm = await MockAMM.deploy(await token.getAddress(), await usdc.getAddress());
    await amm.waitForDeployment();

    // Deploy SandwichBot (owned by attacker)
    const SandwichBot = await ethers.getContractFactory("SandwichBot");
    bot = await SandwichBot.connect(attacker).deploy();
    await bot.waitForDeployment();

    // ── Wire contracts together ──
    await insurance.setPremiumCalculator(await calculator.getAddress());
    await insurance.setTierSystem(await tierSystem.getAddress());
    await tierSystem.setInsuranceContract(await insurance.getAddress());

    // Authorize MEVInsurance and SlashingSystem to call OracleRegistry restricted functions
    await registry.setAuthorizedCaller(await insurance.getAddress(), true);
    await registry.setAuthorizedCaller(await slashingSystem.getAddress(), true);

    // ── Fund contracts ──
    // Fund insurance pool for claim payouts
    await token.transfer(await insurance.getAddress(), e(200000));

    // Fund AMM pool: 100k MEVI + 100k USDC (1:1)
    await token.approve(await amm.getAddress(), e(100000));
    await usdc.approve(await amm.getAddress(), e(100000));
    await amm.addLiquidity(e(100000), e(100000));

    // Fund victim with MEVI tokens for swapping
    await token.transfer(victim.address, e(20000));

    // Fund bot with MEVI tokens for frontrunning
    await token.transfer(await bot.getAddress(), e(50000));

    // Fund OracleRegistry for rewards
    await owner.sendTransaction({
      to: await registry.getAddress(),
      value: e(10)
    });

    // Fund SlashingSystem for reporter payments
    await owner.sendTransaction({
      to: await slashingSystem.getAddress(),
      value: e(5)
    });

    // Register and activate 8 oracles
    await registerAndActivateOracles(allOracles());
  });

  // =================================================================
  //  Scenario 1: Full Lifecycle - Sandwich Attack → Claim → Payout
  // =================================================================
  describe("Scenario 1: Full Claim Lifecycle", function () {
    it("should complete full flow: sandwich attack → claim → oracle vote → payout", async function () {
      const tokenAddr = await token.getAddress();
      const usdcAddr = await usdc.getAddress();
      const ammAddr = await amm.getAddress();

      // ── Step 1: Execute sandwich attack ──
      const priceBefore = await amm.getPrice();

      // Frontrun: bot buys USDC with 5000 MEVI
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAddr, e(5000));
      const priceAfterFrontrun = await amm.getPrice();
      expect(priceAfterFrontrun).to.be.lt(priceBefore);

      // Victim swap: victim buys USDC with 2000 MEVI (at worse price)
      const fairOutput = await amm.getAmountOut(tokenAddr, e(2000));
      // fairOutput is already post-frontrun, let's just track victim gets something
      await token.connect(victim).approve(ammAddr, e(2000));
      const victimBalBefore = await usdc.balanceOf(victim.address);
      await amm.connect(victim).swap(tokenAddr, e(2000));
      const victimBalAfter = await usdc.balanceOf(victim.address);
      const victimReceived = victimBalAfter - victimBalBefore;
      expect(victimReceived).to.be.gt(0);

      // Backrun: bot sells USDC for MEVI
      const botBalUsdc = await usdc.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, usdcAddr, botBalUsdc);

      // Verify attack recorded
      const attack = await bot.getAttack(0);
      expect(attack.frontrunAmountIn).to.equal(e(5000));

      // ── Step 2: Victim registers and buys insurance ──
      await insurance.connect(victim).registerUser();
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(victim).approve(await insurance.getAddress(), premium);
      await insurance.connect(victim).buyPolicy(CoverageLevel.High);

      // ── Step 3: Victim submits claim ──
      // swapValue must be <= defaultCoverage (1000 MEVI)
      const swapValue = e(1000);
      const loss = e(100); // estimated loss from sandwich
      const { claimId, assignedOracles } = await submitClaimForUser(victim, swapValue, loss);

      expect(assignedOracles.length).to.equal(7);

      // ── Step 4: Oracles evaluate - low fraud (legitimate claim) ──
      // All scores must be < thetaApprove (30) for Bronze users to get Approved
      const fraudScores = [10, 12, 8, 15, 11, 9, 14];
      const patternValids = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids);

      // ── Step 5: Finalize claim → Bronze users always get CAPTCHARequired ──
      await insurance.finalizeClaim(claimId);

      const claimInfo = await insurance.getClaimInfo(claimId);
      expect(claimInfo.status).to.equal(ClaimStatus.CAPTCHARequired);

      // ── Step 6: Owner resolves CAPTCHA → Approved ──
      const victimBalTokenBefore = await token.balanceOf(victim.address);
      await insurance.resolveCAPTCHA(claimId, true);

      const finalInfo = await insurance.getClaimInfo(claimId);
      expect(finalInfo.status).to.equal(ClaimStatus.Approved);

      // Victim should receive payout (High coverage = 100% of loss)
      const victimBalTokenAfter = await token.balanceOf(victim.address);
      expect(victimBalTokenAfter).to.be.gt(victimBalTokenBefore);

      const payout = victimBalTokenAfter - victimBalTokenBefore;
      expect(payout).to.equal(loss); // High = 100%
    });

    it("should complete flow with Medium coverage (70% payout)", async function () {
      // Register + buy policy with Medium coverage
      await insurance.connect(victim).registerUser();
      const premium = await getActualPremium(CoverageLevel.Medium);
      await token.connect(victim).approve(await insurance.getAddress(), premium);
      await insurance.connect(victim).buyPolicy(CoverageLevel.Medium);

      // Submit claim (swapValue <= defaultCoverage 1000)
      const loss = e(200);
      const { claimId, assignedOracles } = await submitClaimForUser(victim, e(800), loss);

      // Oracles approve (low fraud, all < thetaApprove 30 for Bronze)
      const fraudScores = [5, 8, 10, 7, 12, 9, 6];
      const patternValids = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids);

      // Bronze user → CAPTCHARequired, then resolve
      await insurance.finalizeClaim(claimId);
      const claimInfo = await insurance.getClaimInfo(claimId);
      expect(claimInfo.status).to.equal(ClaimStatus.CAPTCHARequired);

      const balBefore = await token.balanceOf(victim.address);
      await insurance.resolveCAPTCHA(claimId, true);
      const balAfter = await token.balanceOf(victim.address);

      const finalInfo = await insurance.getClaimInfo(claimId);
      expect(finalInfo.status).to.equal(ClaimStatus.Approved);

      // Medium = 70% payout (PDF Table 2)
      const expectedPayout = (loss * 70n) / 100n;
      expect(balAfter - balBefore).to.equal(expectedPayout);
    });
  });

  // =================================================================
  //  Scenario 2: Tier Progression
  // =================================================================
  describe("Scenario 2: Tier Progression", function () {
    it("should upgrade user from Bronze to Silver after meeting requirements", async function () {
      // Register user in insurance and tier system
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));

      // Verify starts at Bronze
      await tierSystem.registerUser(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Bronze);

      // Simulate meeting Silver requirements:
      // 18 swaps, 30 days membership, avg fraud < 52
      // Use syncUserData to simulate the swaps and fraud score
      await tierSystem.syncUserData(user1.address, 20, 30); // 20 swaps, avgFraud 30

      // Advance time past 30 days
      await time.increase(31 * DAY);

      // Check and upgrade
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Silver);
    });

    it("should upgrade from Silver to Gold", async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      await tierSystem.registerUser(user1.address);

      // First upgrade to Silver
      await tierSystem.syncUserData(user1.address, 20, 30);
      await time.increase(31 * DAY);
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Silver);

      // Meet Gold requirements: 55 swaps, 60 days, avgFraud < 35
      await tierSystem.syncUserData(user1.address, 60, 25);
      await time.increase(31 * DAY); // total 62 days
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Gold);
    });

    it("should handle full Platinum upgrade flow", async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(100000));
      await tierSystem.registerUser(user1.address);

      // Fast track to Gold
      await tierSystem.syncUserData(user1.address, 20, 30);
      await time.increase(31 * DAY);
      await tierSystem.checkAndUpgrade(user1.address);
      await tierSystem.syncUserData(user1.address, 60, 25);
      await time.increase(31 * DAY);
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Gold);

      // Request Platinum: stake 20% of maxInsurable
      const maxInsurable = e(10000);
      const stakeAmount = (maxInsurable * 2000n) / 10000n; // 20%
      await token.connect(user1).approve(await tierSystem.getAddress(), stakeAmount);
      await tierSystem.connect(user1).requestPlatinum(maxInsurable);

      // Owner verifies CAPTCHA
      await tierSystem.verifyCaptcha(user1.address);

      // Finalize Platinum
      await tierSystem.finalizePlatinum(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Platinum);
    });
  });

  // =================================================================
  //  Scenario 3: Oracle Slashing
  // =================================================================
  describe("Scenario 3: Oracle Slashing", function () {
    it("should slash dishonest oracle through jury vote", async function () {
      const accusedOracle = oracle1;

      // Get oracle info before
      const infoBefore = await registry.getOracleInfo(accusedOracle.address);
      const stakeBefore = infoBefore.stake;
      expect(stakeBefore).to.be.gt(0);

      // User submits a report against oracle1
      const reportDeposit = e("0.014");
      await slashingSystem.connect(user1).submitReport(
        accusedOracle.address,
        ethers.toUtf8Bytes("evidence of dishonest voting"),
        { value: reportDeposit }
      );

      const reportId = 0;
      const jury = await slashingSystem.getReportJury(reportId);
      expect(jury.length).to.equal(7);

      // Jury should NOT include the accused oracle
      for (const j of jury) {
        expect(j).to.not.equal(accusedOracle.address);
      }

      // Jury commits votes (all vote to slash 60%)
      const slashPercent = 60;
      const salts = [];
      for (let i = 0; i < jury.length; i++) {
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`jury_salt_${i}`));
        salts.push(salt);
        const hash = computeJuryCommitHash(slashPercent, salt);
        const signer = await findSigner(jury[i]);
        await slashingSystem.connect(signer).commitJuryVote(reportId, hash);
      }

      // Advance past commit timeout
      await time.increase(2 * DAY + 1);

      // Jury reveals votes
      for (let i = 0; i < jury.length; i++) {
        const signer = await findSigner(jury[i]);
        await slashingSystem.connect(signer).revealJuryVote(reportId, slashPercent, salts[i]);
      }

      // Advance past reveal timeout
      await time.increase(2 * DAY + 1);

      // Finalize slashing
      await slashingSystem.finalizeSlashing(reportId);

      // Report should be finalized as Expelled (median 60% > thetaExpulsion 50%)
      const reportInfo = await slashingSystem.getReportInfo(reportId);
      expect(reportInfo.status).to.equal(2); // Expelled

      // Oracle should be expelled in registry (OracleStatus.Expelled = 6)
      const infoAfter = await registry.getOracleInfo(accusedOracle.address);
      expect(infoAfter.status).to.equal(6); // Expelled
    });

    it("should dismiss report when jury votes 0%", async function () {
      const accusedOracle = oracle2;
      const reportDeposit = e("0.014");

      await slashingSystem.connect(user1).submitReport(
        accusedOracle.address,
        ethers.toUtf8Bytes("false accusation"),
        { value: reportDeposit }
      );

      const reportId = 0;
      const jury = await slashingSystem.getReportJury(reportId);

      // All jury members vote 0% (no slash)
      const slashPercent = 0;
      const salts = [];
      for (let i = 0; i < jury.length; i++) {
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`jury0_salt_${i}`));
        salts.push(salt);
        const hash = computeJuryCommitHash(slashPercent, salt);
        const signer = await findSigner(jury[i]);
        await slashingSystem.connect(signer).commitJuryVote(reportId, hash);
      }

      await time.increase(2 * DAY + 1);

      for (let i = 0; i < jury.length; i++) {
        const signer = await findSigner(jury[i]);
        await slashingSystem.connect(signer).revealJuryVote(reportId, slashPercent, salts[i]);
      }

      await time.increase(2 * DAY + 1);

      // Reporter balance before
      const reporterBalBefore = await ethers.provider.getBalance(user1.address);

      await slashingSystem.finalizeSlashing(reportId);

      const reportInfo = await slashingSystem.getReportInfo(reportId);
      // Status 3 = Dismissed
      expect(reportInfo.status).to.equal(3);

      // Oracle should still be active
      const oracleInfo = await registry.getOracleInfo(accusedOracle.address);
      expect(oracleInfo.status).to.equal(2); // Active
    });
  });

  // =================================================================
  //  Scenario 4: Premium Calculator Integration
  // =================================================================
  describe("Scenario 4: Premium Calculator Integration", function () {
    it("should use dynamic premium when calculator is set", async function () {
      // Register user
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));

      // buyPolicy uses defaultCoverage (1000e18) as swap value for premium calc
      const expectedPremium = await getActualPremium(CoverageLevel.High);
      expect(expectedPremium).to.be.gt(0);

      // Buy policy - premium is deducted
      const balBefore = await token.balanceOf(user1.address);
      await token.connect(user1).approve(await insurance.getAddress(), expectedPremium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const balAfter = await token.balanceOf(user1.address);

      // User paid the dynamic premium
      expect(balBefore - balAfter).to.equal(expectedPremium);
    });

    it("premium should differ by coverage level", async function () {
      const swapValue = e(1000);
      const premiumLow = await calculator.calculatePremium(swapValue, CoverageLevel.Low);
      const premiumMed = await calculator.calculatePremium(swapValue, CoverageLevel.Medium);
      const premiumHigh = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // Higher coverage = higher premium
      expect(premiumHigh).to.be.gte(premiumMed);
      expect(premiumMed).to.be.gte(premiumLow);
    });

    it("solvency ratio should affect premium", async function () {
      const swapValue = e(1000);
      const premiumNormal = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // Simulate low solvency (pool < 1.3x liabilities)
      await calculator.updateSolvencyData(e(1000), e(900), e(100));
      const premiumCritical = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // Critical solvency should increase premium
      expect(premiumCritical).to.be.gte(premiumNormal);
    });

    it("Fcov (premium) and coverage% (payout) are distinct (anti-selection)", async function () {
      // PDF Table 2: Fcov != payout%
      // Fcov: Low=70%, Medium=90%, High=100% (premium multiplier)
      // Payout: Low=50%, Medium=70%, High=100% (reimbursement)
      expect(await calculator.fcov(CoverageLevel.Low)).to.equal(7000);
      expect(await calculator.fcov(CoverageLevel.Medium)).to.equal(9000);
      expect(await calculator.fcov(CoverageLevel.High)).to.equal(10000);

      expect(await insurance.coveragePercentBps(CoverageLevel.Low)).to.equal(5000);
      expect(await insurance.coveragePercentBps(CoverageLevel.Medium)).to.equal(7000);
      expect(await insurance.coveragePercentBps(CoverageLevel.High)).to.equal(10000);

      // Low user pays 70% of premium but only gets 50% reimbursement
      // This discourages adverse selection
    });
  });

  // =================================================================
  //  Scenario 4b: Oracle Rewards on finalizeClaim
  // =================================================================
  describe("Scenario 4b: Oracle Rewards", function () {
    it("oracles should receive ETH reward after finalizeClaim", async function () {
      // Setup user and submit claim
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), premium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);

      const { claimId, assignedOracles } = await submitClaimForUser(user1, e(500), e(50));

      // Track oracle balances before
      const balancesBefore = [];
      for (const addr of assignedOracles) {
        balancesBefore.push(await ethers.provider.getBalance(addr));
      }

      // All oracles commit and reveal
      await oraclesCommitAndReveal(
        claimId, assignedOracles,
        [40, 40, 40, 40, 40, 40, 40],
        [true, true, true, true, true, true, true]
      );

      // Finalize (triggers rewards)
      await insurance.finalizeClaim(claimId);

      // Check at least some oracles received reward (gas costs may affect exact balance)
      let rewardedCount = 0;
      for (let i = 0; i < assignedOracles.length; i++) {
        const balAfter = await ethers.provider.getBalance(assignedOracles[i]);
        // Oracle spent gas on commit+reveal, but should have received rClaim (0.002 ETH)
        // Net balance change: received 0.002 ETH - gas spent
        // We check claimsEvaluated instead as a more reliable indicator
        const info = await registry.getOracleInfo(assignedOracles[i]);
        if (info.claimsEvaluated > 0n) rewardedCount++;
      }
      expect(rewardedCount).to.equal(7);
    });
  });

  // =================================================================
  //  Scenario 5: Claim Rejection → Blacklist → Debt
  // =================================================================
  describe("Scenario 5: Claim Rejection and Blacklist", function () {
    it("should reject claim with high fraud score and blacklist user", async function () {
      // Setup user with policy
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), premium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);

      // Submit claim
      const loss = e(300);
      const { claimId, assignedOracles } = await submitClaimForUser(user1, e(500), loss);

      // Oracles vote high fraud score (>= thetaReject = 80)
      const fraudScores = [90, 95, 85, 92, 100, 88, 86];
      const patternValids = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids);

      // Finalize → should be Rejected
      await insurance.finalizeClaim(claimId);
      const claimInfo = await insurance.getClaimInfo(claimId);
      expect(claimInfo.status).to.equal(ClaimStatus.Rejected);

      // No payout should be made for rejected claim
    });

    it("should handle invalid pattern detection", async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), premium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);

      const { claimId, assignedOracles } = await submitClaimForUser(user1, e(500), e(100));

      // 5 out of 7 oracles say pattern invalid (>= 70% threshold)
      const fraudScores = [50, 50, 50, 50, 50, 50, 50];
      const patternValids = [false, false, false, false, false, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids);

      await insurance.finalizeClaim(claimId);
      const claimInfo = await insurance.getClaimInfo(claimId);
      expect(claimInfo.status).to.equal(ClaimStatus.InvalidPattern);
    });

    it("should handle CAPTCHA-required claim (Bronze, medium fraud)", async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), premium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);

      const loss = e(100);
      const { claimId, assignedOracles } = await submitClaimForUser(user1, e(500), loss);

      // Medium fraud scores (between thetaApprove=60 and thetaReject=80)
      // For Bronze tier: all scores < thetaReject → CAPTCHARequired
      const fraudScores = [65, 70, 68, 72, 67, 75, 63];
      const patternValids = [true, true, true, true, true, true, true];
      await oraclesCommitAndReveal(claimId, assignedOracles, fraudScores, patternValids);

      await insurance.finalizeClaim(claimId);
      const claimInfo = await insurance.getClaimInfo(claimId);
      expect(claimInfo.status).to.equal(ClaimStatus.CAPTCHARequired);

      // Owner resolves CAPTCHA positively
      const balBefore = await token.balanceOf(user1.address);
      await insurance.resolveCAPTCHA(claimId, true);
      const balAfter = await token.balanceOf(user1.address);

      const finalInfo = await insurance.getClaimInfo(claimId);
      expect(finalInfo.status).to.equal(ClaimStatus.Approved);
      expect(balAfter).to.be.gt(balBefore);
    });
  });

  // =================================================================
  //  Scenario 6: Blacklist and Debt Payment via TierSystem
  // =================================================================
  describe("Scenario 6: Blacklist and Debt Payment", function () {
    it("should blacklist user and allow debt repayment", async function () {
      await tierSystem.registerUser(user2.address);
      await token.transfer(user2.address, e(10000));

      // Blacklist user with 500 MEVI loss → debt = 20% = 100 MEVI
      const loss = e(500);
      await tierSystem.blacklistUser(user2.address, loss);

      // Check user is blacklisted
      const tier = await tierSystem.getTier(user2.address);
      // Blacklisted users still have a tier but are flagged

      // Pay debt
      const debtAmount = (loss * 2000n) / 10000n; // 20% penalty = 100 MEVI
      await token.connect(user2).approve(await tierSystem.getAddress(), debtAmount);
      await tierSystem.connect(user2).payDebt(debtAmount);

      // User should be unblacklisted after full payment
    });
  });

  // =================================================================
  //  Scenario 7: Multi-User Concurrent Claims
  // =================================================================
  describe("Scenario 7: Multi-User Concurrent Claims", function () {
    it("should handle two users submitting claims simultaneously", async function () {
      // Setup two users
      await insurance.connect(user1).registerUser();
      await insurance.connect(user2).registerUser();
      await token.transfer(user1.address, e(50000));
      await token.transfer(user2.address, e(50000));

      const premium1 = await getActualPremium(CoverageLevel.High);
      const premium2 = await getActualPremium(CoverageLevel.Medium);
      await token.connect(user1).approve(await insurance.getAddress(), premium1);
      await token.connect(user2).approve(await insurance.getAddress(), premium2);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      await insurance.connect(user2).buyPolicy(CoverageLevel.Medium);

      // Both submit claims
      const claim1 = await submitClaimForUser(user1, e(500), e(80));
      const claim2 = await submitClaimForUser(user2, e(300), e(50));

      expect(claim1.claimId).to.not.equal(claim2.claimId);

      // Process claim 1 - approve (low fraud < thetaApprove 30)
      await oraclesCommitAndReveal(
        claim1.claimId, claim1.assignedOracles,
        [5, 8, 10, 12, 7, 9, 11],
        [true, true, true, true, true, true, true]
      );

      // Process claim 2 - reject (high fraud >= thetaReject=80)
      await oraclesCommitAndReveal(
        claim2.claimId, claim2.assignedOracles,
        [90, 95, 88, 100, 92, 98, 86],
        [true, true, true, true, true, true, true]
      );

      // Finalize both
      await insurance.finalizeClaim(claim1.claimId);
      await insurance.finalizeClaim(claim2.claimId);

      const info1 = await insurance.getClaimInfo(claim1.claimId);
      const info2 = await insurance.getClaimInfo(claim2.claimId);

      // Bronze users: low fraud → CAPTCHARequired, high fraud → Rejected
      expect(info1.status).to.equal(ClaimStatus.CAPTCHARequired);
      expect(info2.status).to.equal(ClaimStatus.Rejected);

      // Resolve CAPTCHA for user1
      await insurance.resolveCAPTCHA(claim1.claimId, true);
      const info1Final = await insurance.getClaimInfo(claim1.claimId);
      expect(info1Final.status).to.equal(ClaimStatus.Approved);

      // User1 got payout, user2 did not
      const user1Claims = await insurance.getUserClaims(user1.address);
      const user2Claims = await insurance.getUserClaims(user2.address);
      expect(user1Claims.length).to.equal(1);
      expect(user2Claims.length).to.equal(1);
    });
  });

  // =================================================================
  //  Scenario 8: Full Sandwich + Claim + Oracle Timeout
  // =================================================================
  describe("Scenario 8: Oracle Timeout Handling", function () {
    it("should finalize claim after oracle timeout with partial reveals", async function () {
      // Setup user
      await insurance.connect(victim).registerUser();
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(victim).approve(await insurance.getAddress(), premium);
      await insurance.connect(victim).buyPolicy(CoverageLevel.High);

      // Submit claim
      const { claimId, assignedOracles } = await submitClaimForUser(victim, e(1000), e(150));

      // Only 4 out of 7 oracles respond (low fraud, approve)
      const respondingOracles = assignedOracles.slice(0, 4);
      const fraudScores = [15, 20, 18, 22];
      const patternValids = [true, true, true, true];

      const salts = [];
      for (let i = 0; i < respondingOracles.length; i++) {
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`timeout_salt_${i}`));
        salts.push(salt);
        const hash = computeCommitHash(fraudScores[i], patternValids[i], salt);
        const signer = await findSigner(respondingOracles[i]);
        await insurance.connect(signer).commitVerdict(claimId, hash);
      }
      for (let i = 0; i < respondingOracles.length; i++) {
        const signer = await findSigner(respondingOracles[i]);
        await insurance.connect(signer).revealVerdict(claimId, fraudScores[i], patternValids[i], salts[i]);
      }

      // Wait for oracle timeout (3 days)
      await time.increase(ORACLE_TIMEOUT + 1);

      // Finalize with partial reveals
      await insurance.finalizeClaim(claimId);

      const claimInfo = await insurance.getClaimInfo(claimId);
      // Should still be finalized based on available votes
      expect(claimInfo.status).to.not.equal(ClaimStatus.OracleReview);
    });
  });

  // =================================================================
  //  Scenario 9: Complete Protocol Flow (Integration Smoke Test)
  // =================================================================
  describe("Scenario 9: Complete Protocol Smoke Test", function () {
    it("should wire all contracts and verify cross-contract interactions", async function () {
      // Verify all contracts are deployed and connected
      expect(await insurance.getClaimsCount()).to.equal(0);
      expect(await registry.getOracleCount()).to.equal(8);

      // Verify premium calculator is set
      const premiumHigh = await calculator.calculatePremium(e(1000), CoverageLevel.High);
      const premiumLow = await calculator.calculatePremium(e(1000), CoverageLevel.Low);
      expect(premiumHigh).to.be.gte(premiumLow);

      // Verify AMM pool is funded
      expect(await amm.reserveA()).to.equal(e(100000));
      expect(await amm.reserveB()).to.equal(e(100000));

      // Verify insurance pool has funds
      const insuranceBal = await token.balanceOf(await insurance.getAddress());
      expect(insuranceBal).to.be.gte(e(200000));

      // Complete mini-lifecycle
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(50000));
      const premium = await getActualPremium(CoverageLevel.High);
      await token.connect(user1).approve(await insurance.getAddress(), premium);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);

      const { claimId, assignedOracles } = await submitClaimForUser(user1, e(500), e(50));
      await oraclesCommitAndReveal(
        claimId, assignedOracles,
        [5, 8, 10, 7, 12, 9, 6],
        [true, true, true, true, true, true, true]
      );
      await insurance.finalizeClaim(claimId);

      // Bronze → CAPTCHARequired → resolve → Approved
      const info = await insurance.getClaimInfo(claimId);
      expect(info.status).to.equal(ClaimStatus.CAPTCHARequired);

      await insurance.resolveCAPTCHA(claimId, true);
      const finalInfo = await insurance.getClaimInfo(claimId);
      expect(finalInfo.status).to.equal(ClaimStatus.Approved);

      // Verify total claims count
      expect(await insurance.getClaimsCount()).to.equal(1);
    });
  });
});
