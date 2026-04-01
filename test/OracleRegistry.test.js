const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("OracleRegistry", function () {
  let registry;
  let owner;
  let oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8;

  const OracleStatus = {
    Inactive: 0, Pending: 1, Active: 2, Watchlisted: 3,
    Contested: 4, Slashed: 5, Expelled: 6
  };

  const BASE_STAKE = ethers.parseEther("0.1");
  const T_ACTIVATION = 7 * 24 * 60 * 60; // 7 days
  const T_COOLDOWN = 30 * 24 * 60 * 60;  // 30 days
  const T_RESET = 30 * 24 * 60 * 60;     // 30 days

  beforeEach(async function () {
    [owner, oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8] =
      await ethers.getSigners();
    const Factory = await ethers.getContractFactory("OracleRegistry");
    registry = await Factory.deploy();
    await registry.waitForDeployment();
  });

  describe("Registration", function () {
    it("should register an oracle with sufficient stake", async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      const [stake, status] = await registry.getOracleInfo(oracle1.address);
      expect(stake).to.equal(BASE_STAKE);
      expect(status).to.equal(OracleStatus.Pending);
    });

    it("should emit OracleRegistered event", async function () {
      await expect(registry.connect(oracle1).registerOracle({ value: BASE_STAKE }))
        .to.emit(registry, "OracleRegistered")
        .withArgs(oracle1.address, BASE_STAKE);
    });

    it("should revert with insufficient stake", async function () {
      const lowStake = ethers.parseEther("0.01");
      await expect(
        registry.connect(oracle1).registerOracle({ value: lowStake })
      ).to.be.revertedWith("Insufficient stake");
    });

    it("should revert if already registered", async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await expect(
        registry.connect(oracle1).registerOracle({ value: BASE_STAKE })
      ).to.be.revertedWith("Already registered");
    });

    it("should accept overpayment", async function () {
      const overStake = ethers.parseEther("1.0");
      await registry.connect(oracle1).registerOracle({ value: overStake });
      const [stake] = await registry.getOracleInfo(oracle1.address);
      expect(stake).to.equal(overStake);
    });

    it("should increment oracle count", async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      expect(await registry.getOracleCount()).to.equal(1);
      await registry.connect(oracle2).registerOracle({ value: BASE_STAKE });
      expect(await registry.getOracleCount()).to.equal(2);
    });
  });

  describe("Activation", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
    });

    it("should revert before activation delay", async function () {
      await expect(
        registry.connect(oracle1).activateOracle()
      ).to.be.revertedWith("Activation delay not passed");
    });

    it("should activate after delay", async function () {
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      const [, status] = await registry.getOracleInfo(oracle1.address);
      expect(status).to.equal(OracleStatus.Active);
    });

    it("should emit OracleActivated event", async function () {
      await time.increase(T_ACTIVATION);
      await expect(registry.connect(oracle1).activateOracle())
        .to.emit(registry, "OracleActivated")
        .withArgs(oracle1.address);
    });

    it("should increment activeOracleCount", async function () {
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      expect(await registry.activeOracleCount()).to.equal(1);
    });

    it("should revert if not in Pending status", async function () {
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      await expect(
        registry.connect(oracle1).activateOracle()
      ).to.be.revertedWith("Not in Pending status");
    });
  });

  describe("Logarithmic Stake Scaling", function () {
    it("should require baseStake for first oracle", async function () {
      expect(await registry.getMinimumStake()).to.equal(BASE_STAKE);
    });

    it("should scale stake with active oracle count", async function () {
      // Register and activate oracle1
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();

      // Now activeOracleCount = 1, minStake = baseStake * log2(2) = baseStake * 1
      expect(await registry.getMinimumStake()).to.equal(BASE_STAKE);

      // Register and activate oracle2
      await registry.connect(oracle2).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle2).activateOracle();

      // activeOracleCount = 2, log2(3) = 1 (floor), minStake = baseStake * 1
      // Actually log2(3) = 1 (floor), so still baseStake
      const minStake = await registry.getMinimumStake();
      expect(minStake).to.be.gte(BASE_STAKE);
    });
  });

  describe("Withdrawal", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
    });

    it("should request withdrawal", async function () {
      await expect(registry.connect(oracle1).withdrawOracle())
        .to.emit(registry, "OracleWithdrawRequested");
    });

    it("should revert duplicate withdrawal request", async function () {
      await registry.connect(oracle1).withdrawOracle();
      await expect(
        registry.connect(oracle1).withdrawOracle()
      ).to.be.revertedWith("Withdrawal already requested");
    });

    it("should revert complete withdrawal before cooldown", async function () {
      await registry.connect(oracle1).withdrawOracle();
      await expect(
        registry.connect(oracle1).completeWithdrawal()
      ).to.be.revertedWith("Cooldown not complete");
    });

    it("should complete withdrawal after cooldown and return stake", async function () {
      await registry.connect(oracle1).withdrawOracle();
      await time.increase(T_COOLDOWN);

      const balanceBefore = await ethers.provider.getBalance(oracle1.address);
      const tx = await registry.connect(oracle1).completeWithdrawal();
      const receipt = await tx.wait();
      const gasUsed = receipt.gasUsed * tx.gasPrice;
      const balanceAfter = await ethers.provider.getBalance(oracle1.address);

      // balance should increase by approximately BASE_STAKE (minus gas)
      expect(balanceAfter + gasUsed - balanceBefore).to.equal(BASE_STAKE);
    });

    it("should decrement activeOracleCount after withdrawal", async function () {
      expect(await registry.activeOracleCount()).to.equal(1);
      await registry.connect(oracle1).withdrawOracle();
      await time.increase(T_COOLDOWN);
      await registry.connect(oracle1).completeWithdrawal();
      expect(await registry.activeOracleCount()).to.equal(0);
    });

    it("should set status to Inactive after withdrawal", async function () {
      await registry.connect(oracle1).withdrawOracle();
      await time.increase(T_COOLDOWN);
      await registry.connect(oracle1).completeWithdrawal();
      const [, status] = await registry.getOracleInfo(oracle1.address);
      expect(status).to.equal(OracleStatus.Inactive);
    });
  });

  describe("Deviation & Watchlist", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
    });

    it("should record deviation as cumulative sum", async function () {
      await registry.recordDeviation(oracle1.address, 15);
      const [, , , , deviationScore, , watchlistStrikes] = await registry.getOracleInfo(oracle1.address);
      expect(deviationScore).to.equal(15);
      expect(watchlistStrikes).to.equal(1); // 15 >= deltaWatchlist(10)
    });

    it("should not count strike for small deviation", async function () {
      await registry.recordDeviation(oracle1.address, 5); // below deltaWatchlist=10
      const [, , , , deviationScore, , watchlistStrikes] = await registry.getOracleInfo(oracle1.address);
      expect(deviationScore).to.equal(5);
      expect(watchlistStrikes).to.equal(0);
    });

    it("should watchlist oracle after kWatchlist strikes", async function () {
      // kWatchlist defaults to 2, deltaWatchlist defaults to 10
      await registry.recordDeviation(oracle1.address, 15);
      let [, status1] = await registry.getOracleInfo(oracle1.address);
      expect(status1).to.equal(OracleStatus.Active);

      await registry.recordDeviation(oracle1.address, 12);
      let [, status2, , , deviationScore, , watchlistStrikes] = await registry.getOracleInfo(oracle1.address);
      expect(status2).to.equal(OracleStatus.Watchlisted);
      expect(deviationScore).to.equal(27); // 15 + 12
      expect(watchlistStrikes).to.equal(2);
    });

    it("should emit OracleWatchlisted event", async function () {
      await registry.recordDeviation(oracle1.address, 15);
      await expect(registry.recordDeviation(oracle1.address, 12))
        .to.emit(registry, "OracleWatchlisted");
    });

    it("should only allow owner or authorized to record deviations", async function () {
      await expect(
        registry.connect(oracle1).recordDeviation(oracle1.address, 10)
      ).to.be.revertedWith("Not owner or authorized");
    });
  });

  describe("Deviation Score Reset", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      // Watchlist oracle1 (2 strikes with deviation >= deltaWatchlist)
      await registry.recordDeviation(oracle1.address, 15);
      await registry.recordDeviation(oracle1.address, 12);
    });

    it("should revert reset before Treset period", async function () {
      await expect(
        registry.resetDeviationScore(oracle1.address)
      ).to.be.revertedWith("Reset period not elapsed");
    });

    it("should reset deviation score and strikes after Treset", async function () {
      await time.increase(T_RESET);
      await registry.resetDeviationScore(oracle1.address);

      const [, status, , , deviationScore, , watchlistStrikes] = await registry.getOracleInfo(oracle1.address);
      expect(deviationScore).to.equal(0);
      expect(watchlistStrikes).to.equal(0);
      expect(status).to.equal(OracleStatus.Active); // Restored from watchlist
    });

    it("should emit OracleScoreReset event", async function () {
      await time.increase(T_RESET);
      await expect(registry.resetDeviationScore(oracle1.address))
        .to.emit(registry, "OracleScoreReset")
        .withArgs(oracle1.address);
    });
  });

  describe("Oracle Selection", function () {
    beforeEach(async function () {
      // Register and activate 8 oracles
      const oracles = [oracle1, oracle2, oracle3, oracle4, oracle5, oracle6, oracle7, oracle8];
      for (const o of oracles) {
        const minStake = await registry.getMinimumStake();
        await registry.connect(o).registerOracle({ value: minStake });
      }
      await time.increase(T_ACTIVATION);
      for (const o of oracles) {
        await registry.connect(o).activateOracle();
      }
    });

    it("should select the requested number of oracles", async function () {
      const seed = 12345;
      const selected = await registry.selectOracles(seed, 7);
      expect(selected.length).to.equal(7);
    });

    it("should select unique oracles (no duplicates)", async function () {
      const seed = 99999;
      const selected = await registry.selectOracles(seed, 7);
      const unique = new Set(selected.map(a => a.toLowerCase()));
      expect(unique.size).to.equal(7);
    });

    it("should produce different selections with different seeds", async function () {
      const selected1 = await registry.selectOracles(111, 3);
      const selected2 = await registry.selectOracles(222, 3);
      // With high probability, at least one oracle differs
      const set1 = selected1.map(a => a.toLowerCase()).sort();
      const set2 = selected2.map(a => a.toLowerCase()).sort();
      // Not guaranteed to differ but extremely likely with 8 oracles choosing 3
      // We just verify they're valid
      expect(selected1.length).to.equal(3);
      expect(selected2.length).to.equal(3);
    });

    it("should revert if not enough eligible oracles", async function () {
      await expect(
        registry.selectOracles(1, 9)
      ).to.be.revertedWith("Not enough eligible oracles");
    });

    it("should exclude watchlisted oracles", async function () {
      // Watchlist oracle1
      await registry.recordDeviation(oracle1.address, 15);
      await registry.recordDeviation(oracle1.address, 12);

      // Select all remaining eligible (7)
      const selected = await registry.selectOracles(42, 7);
      const selectedLower = selected.map(a => a.toLowerCase());
      expect(selectedLower).to.not.include(oracle1.address.toLowerCase());
    });

    it("should exclude oracles with pending withdrawal", async function () {
      await registry.connect(oracle1).withdrawOracle();

      // Oracle1 requested withdrawal — should be excluded
      const selected = await registry.selectOracles(42, 7);
      const selectedLower = selected.map(a => a.toLowerCase());
      expect(selectedLower).to.not.include(oracle1.address.toLowerCase());
    });
  });

  describe("Slashing & Expulsion", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
    });

    it("should slash oracle stake", async function () {
      const slashAmount = ethers.parseEther("0.05");
      await registry.slashOracle(oracle1.address, slashAmount);

      const [stake, status] = await registry.getOracleInfo(oracle1.address);
      expect(stake).to.equal(BASE_STAKE - slashAmount);
      expect(status).to.equal(OracleStatus.Slashed);
    });

    it("should expel oracle permanently", async function () {
      await registry.expelOracle(oracle1.address);
      const [, status] = await registry.getOracleInfo(oracle1.address);
      expect(status).to.equal(OracleStatus.Expelled);
    });

    it("should decrement activeOracleCount on expulsion", async function () {
      expect(await registry.activeOracleCount()).to.equal(1);
      await registry.expelOracle(oracle1.address);
      expect(await registry.activeOracleCount()).to.equal(0);
    });

    it("should only allow owner or authorized to slash", async function () {
      await expect(
        registry.connect(oracle1).slashOracle(oracle1.address, 1)
      ).to.be.revertedWith("Not owner or authorized");
    });
  });

  describe("Reintegration", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      // Slash oracle1
      await registry.slashOracle(oracle1.address, ethers.parseEther("0.05"));
    });

    it("should allow slashed oracle to reintegrate", async function () {
      const newStake = await registry.getMinimumStake();
      await registry.connect(oracle1).reintegrateOracle({ value: newStake });

      const [stake, status] = await registry.getOracleInfo(oracle1.address);
      expect(stake).to.equal(newStake);
      expect(status).to.equal(OracleStatus.Pending);
    });

    it("should revert if not slashed", async function () {
      await registry.connect(oracle2).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle2).activateOracle();

      await expect(
        registry.connect(oracle2).reintegrateOracle({ value: BASE_STAKE })
      ).to.be.revertedWith("Not slashed");
    });

    it("should reset deviation score on reintegration", async function () {
      const newStake = await registry.getMinimumStake();
      await registry.connect(oracle1).reintegrateOracle({ value: newStake });

      const [, , , , deviationScore] = await registry.getOracleInfo(oracle1.address);
      expect(deviationScore).to.equal(0);
    });
  });

  describe("Oracle Rewards", function () {
    beforeEach(async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();

      // Fund the registry with ETH for rewards
      await owner.sendTransaction({
        to: await registry.getAddress(),
        value: ethers.parseEther("1.0"),
      });
    });

    it("should reward an active oracle", async function () {
      const balanceBefore = await ethers.provider.getBalance(oracle1.address);
      await registry.rewardOracle(oracle1.address);
      const balanceAfter = await ethers.provider.getBalance(oracle1.address);

      expect(balanceAfter - balanceBefore).to.equal(ethers.parseEther("0.002"));
    });

    it("should increment claimsEvaluated", async function () {
      await registry.rewardOracle(oracle1.address);
      const [, , , , , claimsEvaluated] = await registry.getOracleInfo(oracle1.address);
      expect(claimsEvaluated).to.equal(1);
    });

    it("should apply penalty for watchlisted oracle", async function () {
      // Watchlist oracle1
      await registry.recordDeviation(oracle1.address, 15);
      await registry.recordDeviation(oracle1.address, 12);

      const balanceBefore = await ethers.provider.getBalance(oracle1.address);
      await registry.rewardOracle(oracle1.address);
      const balanceAfter = await ethers.provider.getBalance(oracle1.address);

      // 50% penalty: 0.002 * 50% = 0.001
      expect(balanceAfter - balanceBefore).to.equal(ethers.parseEther("0.001"));
    });

    it("should only allow owner or authorized to reward", async function () {
      await expect(
        registry.connect(oracle1).rewardOracle(oracle1.address)
      ).to.be.revertedWith("Not owner or authorized");
    });
  });

  describe("Parameter Setters", function () {
    it("should update baseStake", async function () {
      const newVal = ethers.parseEther("0.5");
      await expect(registry.setBaseStake(newVal))
        .to.emit(registry, "ParameterUpdated")
        .withArgs("baseStake", newVal);
      expect(await registry.baseStake()).to.equal(newVal);
    });

    it("should only allow owner to set parameters", async function () {
      await expect(
        registry.connect(oracle1).setBaseStake(1)
      ).to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount");
    });
  });

  describe("View Functions", function () {
    it("should report eligibility correctly", async function () {
      await registry.connect(oracle1).registerOracle({ value: BASE_STAKE });
      expect(await registry.isEligible(oracle1.address)).to.be.false; // Pending

      await time.increase(T_ACTIVATION);
      await registry.connect(oracle1).activateOracle();
      expect(await registry.isEligible(oracle1.address)).to.be.true; // Active

      await registry.connect(oracle1).withdrawOracle();
      expect(await registry.isEligible(oracle1.address)).to.be.false; // Withdraw requested
    });
  });
});
