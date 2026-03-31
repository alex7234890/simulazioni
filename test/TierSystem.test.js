const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("TierSystem", function () {
  let token, tierSystem;
  let owner, user1, user2;

  const Tier = { Bronze: 0, Silver: 1, Gold: 2, Platinum: 3 };
  const e = (n) => ethers.parseEther(String(n));

  const SILVER_MIN_SWAPS = 18;
  const SILVER_MIN_DAYS = 30 * 24 * 60 * 60;
  const SILVER_MAX_FRAUD = 52;
  const GOLD_MIN_SWAPS = 55;
  const GOLD_MIN_DAYS = 60 * 24 * 60 * 60;
  const GOLD_MAX_FRAUD = 35;

  beforeEach(async function () {
    [owner, user1, user2] = await ethers.getSigners();

    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();

    const TierSystem = await ethers.getContractFactory("TierSystem");
    tierSystem = await TierSystem.deploy(await token.getAddress());
    await tierSystem.waitForDeployment();
  });

  // Helper: register + sync user for upgrades
  async function registerAndSync(user, swaps, fraudScore) {
    await tierSystem.registerUser(user.address);
    await tierSystem.syncUserData(user.address, swaps, fraudScore);
  }

  // =============================================================
  //  Registration
  // =============================================================
  describe("Registration", function () {
    it("should register user as Bronze", async function () {
      await tierSystem.registerUser(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Bronze);
      expect(await tierSystem.isRegistered(user1.address)).to.be.true;
    });

    it("should emit UserTierRegistered event", async function () {
      await expect(tierSystem.registerUser(user1.address))
        .to.emit(tierSystem, "UserTierRegistered")
        .withArgs(user1.address, Tier.Bronze);
    });

    it("should revert if already registered", async function () {
      await tierSystem.registerUser(user1.address);
      await expect(
        tierSystem.registerUser(user1.address)
      ).to.be.revertedWith("Already registered");
    });

    it("only owner can register users", async function () {
      await expect(
        tierSystem.connect(user1).registerUser(user1.address)
      ).to.be.revertedWithCustomError(tierSystem, "OwnableUnauthorizedAccount");
    });

    it("should set registration time", async function () {
      await tierSystem.registerUser(user1.address);
      const regTime = await tierSystem.registrationTime(user1.address);
      expect(regTime).to.be.gt(0);
    });
  });

  // =============================================================
  //  Data Sync
  // =============================================================
  describe("Data Sync", function () {
    it("should sync user swap and fraud data", async function () {
      await tierSystem.registerUser(user1.address);
      await tierSystem.syncUserData(user1.address, 20, 30);

      expect(await tierSystem.userTotalSwaps(user1.address)).to.equal(20);
      expect(await tierSystem.userAvgFraudScore(user1.address)).to.equal(30);
    });

    it("should emit UserDataSynced event", async function () {
      await tierSystem.registerUser(user1.address);
      await expect(tierSystem.syncUserData(user1.address, 20, 30))
        .to.emit(tierSystem, "UserDataSynced")
        .withArgs(user1.address, 20, 30);
    });

    it("should revert for unregistered user", async function () {
      await expect(
        tierSystem.syncUserData(user1.address, 20, 30)
      ).to.be.revertedWith("User not registered");
    });

    it("only owner can sync data", async function () {
      await tierSystem.registerUser(user1.address);
      await expect(
        tierSystem.connect(user1).syncUserData(user1.address, 20, 30)
      ).to.be.revertedWithCustomError(tierSystem, "OwnableUnauthorizedAccount");
    });
  });

  // =============================================================
  //  Bronze -> Silver Upgrade
  // =============================================================
  describe("Bronze -> Silver Upgrade", function () {
    beforeEach(async function () {
      await registerAndSync(user1, SILVER_MIN_SWAPS, 30); // 30 < 52
    });

    it("should upgrade after meeting all requirements", async function () {
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Silver);
    });

    it("should emit TierUpgraded event", async function () {
      await time.increase(SILVER_MIN_DAYS);
      await expect(tierSystem.checkAndUpgrade(user1.address))
        .to.emit(tierSystem, "TierUpgraded")
        .withArgs(user1.address, Tier.Bronze, Tier.Silver);
    });

    it("should revert if not enough swaps", async function () {
      await tierSystem.syncUserData(user1.address, 10, 30); // only 10 swaps
      await time.increase(SILVER_MIN_DAYS);
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Not enough swaps for Silver");
    });

    it("should revert if not enough time", async function () {
      // No time increase
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Not enough time for Silver");
    });

    it("should revert if fraud score too high", async function () {
      await tierSystem.syncUserData(user1.address, SILVER_MIN_SWAPS, 55); // 55 >= 52
      await time.increase(SILVER_MIN_DAYS);
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Fraud score too high for Silver");
    });

    it("fraud score of exactly 52 should fail (< 52 required)", async function () {
      await tierSystem.syncUserData(user1.address, SILVER_MIN_SWAPS, 52);
      await time.increase(SILVER_MIN_DAYS);
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Fraud score too high for Silver");
    });

    it("fraud score of 51 should pass", async function () {
      await tierSystem.syncUserData(user1.address, SILVER_MIN_SWAPS, 51);
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Silver);
    });
  });

  // =============================================================
  //  Silver -> Gold Upgrade
  // =============================================================
  describe("Silver -> Gold Upgrade", function () {
    beforeEach(async function () {
      // First get to Silver
      await registerAndSync(user1, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);
      // Now sync Gold-eligible data
      await tierSystem.syncUserData(user1.address, GOLD_MIN_SWAPS, 20); // 20 < 35
    });

    it("should upgrade to Gold after meeting requirements", async function () {
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS); // additional time needed
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Gold);
    });

    it("should emit TierUpgraded Silver -> Gold", async function () {
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);
      await expect(tierSystem.checkAndUpgrade(user1.address))
        .to.emit(tierSystem, "TierUpgraded")
        .withArgs(user1.address, Tier.Silver, Tier.Gold);
    });

    it("should revert if not enough swaps for Gold", async function () {
      await tierSystem.syncUserData(user1.address, 40, 20); // 40 < 55
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Not enough swaps for Gold");
    });

    it("should revert if fraud score too high for Gold", async function () {
      await tierSystem.syncUserData(user1.address, GOLD_MIN_SWAPS, 40); // 40 >= 35
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Fraud score too high for Gold");
    });

    it("should revert if not enough time for Gold", async function () {
      // No additional time
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("Not enough time for Gold");
    });
  });

  // =============================================================
  //  Gold -> Platinum (CAPTCHA + Stake)
  // =============================================================
  describe("Platinum Upgrade", function () {
    beforeEach(async function () {
      // Get user to Gold
      await registerAndSync(user1, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);
      await tierSystem.syncUserData(user1.address, GOLD_MIN_SWAPS, 20);
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);

      // Give user tokens for stake
      await token.transfer(user1.address, e(1000));
    });

    it("should request Platinum with stake deposit", async function () {
      const maxInsurable = e(1000);
      const stakeAmount = e(200); // 20% of 1000
      await token.connect(user1).approve(await tierSystem.getAddress(), stakeAmount);

      await expect(tierSystem.connect(user1).requestPlatinum(maxInsurable))
        .to.emit(tierSystem, "PlatinumRequested")
        .withArgs(user1.address, stakeAmount);

      expect(await tierSystem.platinumStakes(user1.address)).to.equal(stakeAmount);
    });

    it("should transfer stake tokens to contract", async function () {
      const maxInsurable = e(1000);
      const stakeAmount = e(200);
      await token.connect(user1).approve(await tierSystem.getAddress(), stakeAmount);

      const balBefore = await token.balanceOf(user1.address);
      await tierSystem.connect(user1).requestPlatinum(maxInsurable);
      const balAfter = await token.balanceOf(user1.address);

      expect(balBefore - balAfter).to.equal(stakeAmount);
    });

    it("should revert if not Gold tier", async function () {
      // user2 is Bronze
      await tierSystem.registerUser(user2.address);
      await expect(
        tierSystem.connect(user2).requestPlatinum(e(1000))
      ).to.be.revertedWith("Must be Gold tier");
    });

    it("should revert if already requested", async function () {
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await tierSystem.connect(user1).requestPlatinum(e(1000));
      await expect(
        tierSystem.connect(user1).requestPlatinum(e(1000))
      ).to.be.revertedWith("Already requested");
    });

    it("should verify CAPTCHA by owner", async function () {
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await tierSystem.connect(user1).requestPlatinum(e(1000));

      await expect(tierSystem.verifyCaptcha(user1.address))
        .to.emit(tierSystem, "PlatinumCaptchaVerified")
        .withArgs(user1.address);

      expect(await tierSystem.platinumCaptchaVerified(user1.address)).to.be.true;
    });

    it("only owner can verify CAPTCHA", async function () {
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await tierSystem.connect(user1).requestPlatinum(e(1000));

      await expect(
        tierSystem.connect(user1).verifyCaptcha(user1.address)
      ).to.be.revertedWithCustomError(tierSystem, "OwnableUnauthorizedAccount");
    });

    it("should finalize Platinum after CAPTCHA", async function () {
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await tierSystem.connect(user1).requestPlatinum(e(1000));
      await tierSystem.verifyCaptcha(user1.address);

      await expect(tierSystem.finalizePlatinum(user1.address))
        .to.emit(tierSystem, "PlatinumGranted")
        .withArgs(user1.address);

      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Platinum);
    });

    it("should revert finalize without CAPTCHA", async function () {
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await tierSystem.connect(user1).requestPlatinum(e(1000));

      await expect(
        tierSystem.finalizePlatinum(user1.address)
      ).to.be.revertedWith("CAPTCHA not verified");
    });

    it("should revert finalize without stake", async function () {
      await expect(
        tierSystem.finalizePlatinum(user1.address)
      ).to.be.revertedWith("No stake deposited");
    });

    it("Gold checkAndUpgrade should revert (no auto upgrade to Platinum)", async function () {
      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("No automatic upgrade available");
    });
  });

  // =============================================================
  //  Blacklist & Debt
  // =============================================================
  describe("Blacklist & Debt", function () {
    beforeEach(async function () {
      await tierSystem.registerUser(user1.address);
      await token.transfer(user1.address, e(1000));
    });

    it("should blacklist user with debt", async function () {
      const loss = e(500);
      await tierSystem.blacklistUser(user1.address, loss);

      expect(await tierSystem.isBlacklisted(user1.address)).to.be.true;
      // debt = 500 * 20% = 100
      expect(await tierSystem.userDebt(user1.address)).to.equal(e(100));
    });

    it("should emit UserBlacklisted event", async function () {
      await expect(tierSystem.blacklistUser(user1.address, e(500)))
        .to.emit(tierSystem, "UserBlacklisted")
        .withArgs(user1.address, e(100));
    });

    it("blacklisted user cannot upgrade", async function () {
      await tierSystem.syncUserData(user1.address, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.blacklistUser(user1.address, e(500));

      await expect(
        tierSystem.checkAndUpgrade(user1.address)
      ).to.be.revertedWith("User is blacklisted");
    });

    it("should allow partial debt payment", async function () {
      await tierSystem.blacklistUser(user1.address, e(500)); // debt = 100

      await token.connect(user1).approve(await tierSystem.getAddress(), e(50));
      await tierSystem.connect(user1).payDebt(e(50));

      expect(await tierSystem.userDebt(user1.address)).to.equal(e(50));
      expect(await tierSystem.isBlacklisted(user1.address)).to.be.true; // still blacklisted
    });

    it("should un-blacklist when debt fully paid", async function () {
      await tierSystem.blacklistUser(user1.address, e(500)); // debt = 100

      await token.connect(user1).approve(await tierSystem.getAddress(), e(100));
      await tierSystem.connect(user1).payDebt(e(100));

      expect(await tierSystem.userDebt(user1.address)).to.equal(0);
      expect(await tierSystem.isBlacklisted(user1.address)).to.be.false;
    });

    it("should emit DebtPaid and UserUnblacklisted events", async function () {
      await tierSystem.blacklistUser(user1.address, e(500));

      await token.connect(user1).approve(await tierSystem.getAddress(), e(100));
      await expect(tierSystem.connect(user1).payDebt(e(100)))
        .to.emit(tierSystem, "DebtPaid")
        .withArgs(user1.address, e(100), 0)
        .and.to.emit(tierSystem, "UserUnblacklisted")
        .withArgs(user1.address);
    });

    it("should revert if not blacklisted", async function () {
      await expect(
        tierSystem.connect(user1).payDebt(e(10))
      ).to.be.revertedWith("Not blacklisted");
    });

    it("should revert if amount exceeds debt", async function () {
      await tierSystem.blacklistUser(user1.address, e(500)); // debt = 100
      await token.connect(user1).approve(await tierSystem.getAddress(), e(200));
      await expect(
        tierSystem.connect(user1).payDebt(e(200))
      ).to.be.revertedWith("Amount exceeds debt");
    });

    it("should revert if amount is zero", async function () {
      await tierSystem.blacklistUser(user1.address, e(500));
      await expect(
        tierSystem.connect(user1).payDebt(0)
      ).to.be.revertedWith("Amount must be > 0");
    });

    it("should accumulate debt from multiple blacklistings", async function () {
      await tierSystem.blacklistUser(user1.address, e(500)); // debt = 100
      await tierSystem.blacklistUser(user1.address, e(300)); // debt += 60
      expect(await tierSystem.userDebt(user1.address)).to.equal(e(160));
    });

    it("only owner can blacklist", async function () {
      await expect(
        tierSystem.connect(user1).blacklistUser(user1.address, e(500))
      ).to.be.revertedWithCustomError(tierSystem, "OwnableUnauthorizedAccount");
    });
  });

  // =============================================================
  //  View Functions
  // =============================================================
  describe("View Functions", function () {
    it("canUpgradeToSilver returns true when eligible", async function () {
      await registerAndSync(user1, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);
      expect(await tierSystem.canUpgradeToSilver(user1.address)).to.be.true;
    });

    it("canUpgradeToSilver returns false when not enough swaps", async function () {
      await registerAndSync(user1, 10, 30);
      await time.increase(SILVER_MIN_DAYS);
      expect(await tierSystem.canUpgradeToSilver(user1.address)).to.be.false;
    });

    it("canUpgradeToSilver returns false for unregistered user", async function () {
      expect(await tierSystem.canUpgradeToSilver(user1.address)).to.be.false;
    });

    it("canUpgradeToGold returns true when eligible", async function () {
      await registerAndSync(user1, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);
      await tierSystem.checkAndUpgrade(user1.address);
      await tierSystem.syncUserData(user1.address, GOLD_MIN_SWAPS, 20);
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);
      expect(await tierSystem.canUpgradeToGold(user1.address)).to.be.true;
    });

    it("canUpgradeToGold returns false for Bronze user", async function () {
      await registerAndSync(user1, GOLD_MIN_SWAPS, 20);
      await time.increase(GOLD_MIN_DAYS);
      expect(await tierSystem.canUpgradeToGold(user1.address)).to.be.false;
    });

    it("getMaxDailySwaps returns correct values", async function () {
      expect(await tierSystem.getMaxDailySwaps(Tier.Bronze)).to.equal(3);
      expect(await tierSystem.getMaxDailySwaps(Tier.Silver)).to.equal(3);
      expect(await tierSystem.getMaxDailySwaps(Tier.Gold)).to.equal(4);
      expect(await tierSystem.getMaxDailySwaps(Tier.Platinum)).to.equal(ethers.MaxUint256);
    });
  });

  // =============================================================
  //  Parameter Setters
  // =============================================================
  describe("Parameter Setters", function () {
    it("should update silverMinSwaps", async function () {
      await tierSystem.setSilverMinSwaps(25);
      expect(await tierSystem.silverMinSwaps()).to.equal(25);
    });

    it("should update silverMinDays", async function () {
      await tierSystem.setSilverMinDays(45 * 24 * 60 * 60);
      expect(await tierSystem.silverMinDays()).to.equal(45 * 24 * 60 * 60);
    });

    it("should update silverMaxFraudScore", async function () {
      await tierSystem.setSilverMaxFraudScore(60);
      expect(await tierSystem.silverMaxFraudScore()).to.equal(60);
    });

    it("should update goldMinSwaps", async function () {
      await tierSystem.setGoldMinSwaps(70);
      expect(await tierSystem.goldMinSwaps()).to.equal(70);
    });

    it("should update goldMinDays", async function () {
      await tierSystem.setGoldMinDays(90 * 24 * 60 * 60);
      expect(await tierSystem.goldMinDays()).to.equal(90 * 24 * 60 * 60);
    });

    it("should update goldMaxFraudScore", async function () {
      await tierSystem.setGoldMaxFraudScore(40);
      expect(await tierSystem.goldMaxFraudScore()).to.equal(40);
    });

    it("should update platinumStakeBps", async function () {
      await tierSystem.setPlatinumStakeBps(3000);
      expect(await tierSystem.platinumStakeBps()).to.equal(3000);
    });

    it("should update blacklistPenaltyBps", async function () {
      await tierSystem.setBlacklistPenaltyBps(3000);
      expect(await tierSystem.blacklistPenaltyBps()).to.equal(3000);
    });

    it("should set insurance contract address", async function () {
      await tierSystem.setInsuranceContract(user1.address);
      expect(await tierSystem.insuranceContract()).to.equal(user1.address);
    });

    it("non-owner cannot set parameters", async function () {
      await expect(
        tierSystem.connect(user1).setSilverMinSwaps(25)
      ).to.be.revertedWithCustomError(tierSystem, "OwnableUnauthorizedAccount");
    });
  });

  // =============================================================
  //  Full Lifecycle Test
  // =============================================================
  describe("Full Lifecycle: Bronze -> Silver -> Gold -> Platinum", function () {
    it("should complete full tier progression", async function () {
      // Register as Bronze
      await tierSystem.registerUser(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Bronze);

      // Sync data for Silver
      await tierSystem.syncUserData(user1.address, SILVER_MIN_SWAPS, 30);
      await time.increase(SILVER_MIN_DAYS);

      // Upgrade to Silver
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Silver);

      // Sync data for Gold
      await tierSystem.syncUserData(user1.address, GOLD_MIN_SWAPS, 20);
      await time.increase(GOLD_MIN_DAYS - SILVER_MIN_DAYS);

      // Upgrade to Gold
      await tierSystem.checkAndUpgrade(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Gold);

      // Request Platinum
      await token.transfer(user1.address, e(1000));
      const maxInsurable = e(1000);
      const stakeAmount = e(200); // 20%
      await token.connect(user1).approve(await tierSystem.getAddress(), stakeAmount);
      await tierSystem.connect(user1).requestPlatinum(maxInsurable);

      // Verify CAPTCHA
      await tierSystem.verifyCaptcha(user1.address);

      // Finalize Platinum
      await tierSystem.finalizePlatinum(user1.address);
      expect(await tierSystem.getTier(user1.address)).to.equal(Tier.Platinum);
    });
  });
});
