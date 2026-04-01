const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PremiumCalculator", function () {
  let calculator;
  let owner, user1;

  const CoverageLevel = { Low: 0, Medium: 1, High: 2 };

  // Helper: parse ether shorthand
  const e = (n) => ethers.parseEther(String(n));

  beforeEach(async function () {
    [owner, user1] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("PremiumCalculator");
    calculator = await Factory.deploy();
    await calculator.waitForDeployment();
  });

  // =============================================================
  //  Default Parameters
  // =============================================================
  describe("Default Parameters", function () {
    it("should have correct default patt (5%)", async function () {
      expect(await calculator.patt()).to.equal(500);
    });

    it("should have correct default lPercent (25%)", async function () {
      expect(await calculator.lPercent()).to.equal(2500);
    });

    it("should have correct default eFNR (20%)", async function () {
      expect(await calculator.eFNR()).to.equal(2000);
    });

    it("should have correct default mBase (20%)", async function () {
      expect(await calculator.mBase()).to.equal(2000);
    });

    it("should have correct default pmin (1.5%)", async function () {
      expect(await calculator.pmin()).to.equal(150);
    });

    it("should have correct fcov values", async function () {
      expect(await calculator.fcov(CoverageLevel.Low)).to.equal(7000);
      expect(await calculator.fcov(CoverageLevel.Medium)).to.equal(9000);
      expect(await calculator.fcov(CoverageLevel.High)).to.equal(10000);
    });

    it("should have correct solvency thresholds", async function () {
      expect(await calculator.srSafe()).to.equal(15000);
      expect(await calculator.srCritical()).to.equal(13000);
    });

    it("should have zero initial mAdj", async function () {
      expect(await calculator.mAdj()).to.equal(0);
    });
  });

  // =============================================================
  //  Premium Calculation - Basic
  // =============================================================
  describe("Premium Calculation - Basic", function () {
    it("should return minimum premium when no market data set", async function () {
      // With no vbase/tint/coracle24h, comp2 and comp3 are 0
      // comp1 = patt * lPercent / 10000 = 500 * 2500 / 10000 = 125 bps
      // baseRate = 125
      // margin = 10000 + 2000 = 12000
      // Premium = V * 125/10000 * 12000/10000 * fcov/10000
      // For High (fcov=10000): V * 125/10000 * 12000/10000 * 10000/10000
      //   = V * 0.0125 * 1.2 * 1.0 = V * 0.015 = 1.5%
      // minPremium = V * 150/10000 = V * 1.5%
      // They should be equal (or calculated >= min)
      const swapValue = e(1000);
      const premium = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // 1000 * 1.5% = 15
      const expected = e(15);
      expect(premium).to.equal(expected);
    });

    it("should return minimum premium for Low coverage when formula gives less", async function () {
      // For Low (fcov=7000): V * 0.0125 * 1.2 * 0.7 = V * 0.0105 = 1.05%
      // minPremium = V * 1.5%
      // min wins
      const swapValue = e(1000);
      const premium = await calculator.calculatePremium(swapValue, CoverageLevel.Low);
      const minPremium = e(15); // 1000 * 1.5%
      expect(premium).to.equal(minPremium);
    });

    it("should return minimum premium for Medium coverage", async function () {
      // For Medium (fcov=9000): V * 0.0125 * 1.2 * 0.9 = V * 0.0135 = 1.35%
      // minPremium = 1.5% > 1.35%
      const swapValue = e(1000);
      const premium = await calculator.calculatePremium(swapValue, CoverageLevel.Medium);
      const minPremium = e(15);
      expect(premium).to.equal(minPremium);
    });

    it("should revert with zero swap value", async function () {
      await expect(
        calculator.calculatePremium(0, CoverageLevel.High)
      ).to.be.revertedWith("Swap value must be > 0");
    });

    it("should scale linearly with swap value", async function () {
      const p1 = await calculator.calculatePremium(e(1000), CoverageLevel.High);
      const p2 = await calculator.calculatePremium(e(2000), CoverageLevel.High);
      expect(p2).to.equal(p1 * 2n);
    });
  });

  // =============================================================
  //  Premium Calculation - With Market Data
  // =============================================================
  describe("Premium Calculation - With Market Data", function () {
    beforeEach(async function () {
      // Set realistic market data
      // tint = 10 ETH, vbase = 1000 ETH, coracle24h = 1 ETH
      await calculator.updateMarketData(e(10), e(1000), e(1));
    });

    it("should include fraud cost component (comp2)", async function () {
      // comp2 = (tint * eFNR / (10000 - eFNR)) * 10000 / vbase
      //       = (10 * 2000 / 8000) * 10000 / 1000
      //       = 2.5 * 10 = 25 bps
      // comp1 = 125 bps
      // comp3 = 1 * 10000 / 1000 = 10 bps
      // baseRate = 125 + 25 + 10 = 160 bps
      // Premium High = V * 160/10000 * 12000/10000 * 10000/10000
      //             = V * 0.016 * 1.2 = V * 0.0192 = 1.92%
      const swapValue = e(1000);
      const premium = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // 1000 * 1.92% = 19.2 MEVI
      const expected = e(19.2);
      // Allow for rounding (integer division)
      expect(premium).to.be.closeTo(expected, e(0.1));
    });

    it("should produce higher premium than minimum with market data", async function () {
      const swapValue = e(1000);
      const premium = await calculator.calculatePremium(swapValue, CoverageLevel.High);
      const minPremium = e(15); // 1.5%
      expect(premium).to.be.gt(minPremium);
    });

    it("Low coverage premium < High coverage premium", async function () {
      const swapValue = e(1000);
      const pLow = await calculator.calculatePremium(swapValue, CoverageLevel.Low);
      const pHigh = await calculator.calculatePremium(swapValue, CoverageLevel.High);
      expect(pLow).to.be.lte(pHigh);
    });

    it("Medium coverage premium between Low and High", async function () {
      const swapValue = e(1000);
      const pLow = await calculator.calculatePremium(swapValue, CoverageLevel.Low);
      const pMed = await calculator.calculatePremium(swapValue, CoverageLevel.Medium);
      const pHigh = await calculator.calculatePremium(swapValue, CoverageLevel.High);
      expect(pMed).to.be.gte(pLow);
      expect(pMed).to.be.lte(pHigh);
    });
  });

  // =============================================================
  //  Solvency Ratio & Adaptive Margin
  // =============================================================
  describe("Solvency Ratio & Adaptive Margin", function () {
    it("should return max uint when no liabilities", async function () {
      const sr = await calculator.getSolvencyRatio();
      expect(sr).to.equal(ethers.MaxUint256);
    });

    it("SR >= srSafe: mAdj = 0", async function () {
      // pool=1500, liabilities=500, expected=500 -> SR = 1500/1000 = 1.5x = 15000
      await calculator.updateSolvencyData(e(1500), e(500), e(500));
      expect(await calculator.mAdj()).to.equal(0);

      const sr = await calculator.getSolvencyRatio();
      expect(sr).to.equal(15000);
    });

    it("srCritical <= SR < srSafe: mAdj = deltaMmed (5%)", async function () {
      // pool=1400, liabilities=500, expected=500 -> SR = 1400/1000 = 1.4x = 14000
      await calculator.updateSolvencyData(e(1400), e(500), e(500));
      expect(await calculator.mAdj()).to.equal(500); // deltaMmed

      const sr = await calculator.getSolvencyRatio();
      expect(sr).to.equal(14000);
    });

    it("SR < srCritical: mAdj = deltaMhigh (10%)", async function () {
      // pool=1200, liabilities=500, expected=500 -> SR = 1200/1000 = 1.2x = 12000
      await calculator.updateSolvencyData(e(1200), e(500), e(500));
      expect(await calculator.mAdj()).to.equal(1000); // deltaMhigh

      const sr = await calculator.getSolvencyRatio();
      expect(sr).to.equal(12000);
    });

    it("mAdj increases premium", async function () {
      await calculator.updateMarketData(e(10), e(1000), e(1));

      const swapValue = e(1000);
      const premiumSafe = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      // Set critical solvency -> mAdj = 1000 (10%)
      await calculator.updateSolvencyData(e(1200), e(500), e(500));
      const premiumCritical = await calculator.calculatePremium(swapValue, CoverageLevel.High);

      expect(premiumCritical).to.be.gt(premiumSafe);
    });

    it("should emit SolvencyUpdated event", async function () {
      await expect(calculator.updateSolvencyData(e(1500), e(500), e(500)))
        .to.emit(calculator, "SolvencyUpdated");
    });
  });

  // =============================================================
  //  Market Data Updates
  // =============================================================
  describe("Market Data Updates", function () {
    it("should update tint, vbase, coracle24h", async function () {
      await calculator.updateMarketData(e(5), e(500), e(2));
      expect(await calculator.tint()).to.equal(e(5));
      expect(await calculator.vbase()).to.equal(e(500));
      expect(await calculator.coracle24h()).to.equal(e(2));
    });

    it("should emit MarketDataUpdated event", async function () {
      await expect(calculator.updateMarketData(e(5), e(500), e(2)))
        .to.emit(calculator, "MarketDataUpdated")
        .withArgs(e(5), e(500), e(2));
    });

    it("only owner can update market data", async function () {
      await expect(
        calculator.connect(user1).updateMarketData(e(5), e(500), e(2))
      ).to.be.revertedWithCustomError(calculator, "OwnableUnauthorizedAccount");
    });
  });

  // =============================================================
  //  Parameter Setters
  // =============================================================
  describe("Parameter Setters", function () {
    it("should update patt", async function () {
      await calculator.setPatt(1000);
      expect(await calculator.patt()).to.equal(1000);
    });

    it("should emit PattUpdated", async function () {
      await expect(calculator.setPatt(1000))
        .to.emit(calculator, "PattUpdated")
        .withArgs(1000);
    });

    it("should update lPercent", async function () {
      await calculator.setLPercent(3000);
      expect(await calculator.lPercent()).to.equal(3000);
    });

    it("should update eFNR", async function () {
      await calculator.setEFNR(3000);
      expect(await calculator.eFNR()).to.equal(3000);
    });

    it("should revert if eFNR >= 100%", async function () {
      await expect(calculator.setEFNR(10000))
        .to.be.revertedWith("eFNR must be < 100%");
    });

    it("should update mBase", async function () {
      await calculator.setMBase(3000);
      expect(await calculator.mBase()).to.equal(3000);
    });

    it("should update pmin", async function () {
      await calculator.setPmin(200);
      expect(await calculator.pmin()).to.equal(200);
    });

    it("should update fcov", async function () {
      await calculator.setFcov(CoverageLevel.Low, 8000);
      expect(await calculator.fcov(CoverageLevel.Low)).to.equal(8000);
    });

    it("should update srSafe", async function () {
      await calculator.setSRSafe(20000);
      expect(await calculator.srSafe()).to.equal(20000);
    });

    it("should update srCritical", async function () {
      await calculator.setSRCritical(12000);
      expect(await calculator.srCritical()).to.equal(12000);
    });

    it("should update deltaMmed", async function () {
      await calculator.setDeltaMmed(700);
      expect(await calculator.deltaMmed()).to.equal(700);
    });

    it("should update deltaMhigh", async function () {
      await calculator.setDeltaMhigh(1500);
      expect(await calculator.deltaMhigh()).to.equal(1500);
    });

    it("non-owner cannot set parameters", async function () {
      await expect(
        calculator.connect(user1).setPatt(1000)
      ).to.be.revertedWith("Not owner or authorized updater");
    });
  });

  // =============================================================
  //  Edge Cases
  // =============================================================
  describe("Edge Cases", function () {
    it("should handle zero vbase gracefully (comp2 and comp3 = 0)", async function () {
      await calculator.updateMarketData(e(10), 0, e(1));
      const premium = await calculator.calculatePremium(e(1000), CoverageLevel.High);
      // Falls back to comp1 only + margin, likely hits pmin
      expect(premium).to.be.gt(0);
    });

    it("should handle high patt correctly", async function () {
      await calculator.setPatt(5000); // 50% attack probability
      const premium = await calculator.calculatePremium(e(1000), CoverageLevel.High);
      // comp1 = 5000 * 2500 / 10000 = 1250 bps = 12.5%
      // With margin 1.2: 15%, * High: 15%
      // This is above pmin (1.5%)
      const minPremium = e(15);
      expect(premium).to.be.gt(minPremium);
    });

    it("should handle small swap values", async function () {
      const premium = await calculator.calculatePremium(e(1), CoverageLevel.High);
      // 1 MEVI * 1.5% = 0.015 MEVI
      expect(premium).to.be.gt(0);
    });

    it("should handle large swap values without overflow", async function () {
      const largeValue = e(1000000); // 1M
      const premium = await calculator.calculatePremium(largeValue, CoverageLevel.High);
      expect(premium).to.be.gt(0);
    });

    it("premium increases when patt increases", async function () {
      await calculator.updateMarketData(e(10), e(1000), e(1));
      const p1 = await calculator.calculatePremium(e(1000), CoverageLevel.High);

      await calculator.setPatt(1000); // 10% instead of 5%
      const p2 = await calculator.calculatePremium(e(1000), CoverageLevel.High);

      expect(p2).to.be.gt(p1);
    });

    it("premium increases when eFNR increases", async function () {
      await calculator.updateMarketData(e(10), e(1000), e(1));
      const p1 = await calculator.calculatePremium(e(1000), CoverageLevel.High);

      await calculator.setEFNR(4000); // 40% instead of 20%
      const p2 = await calculator.calculatePremium(e(1000), CoverageLevel.High);

      expect(p2).to.be.gt(p1);
    });
  });

  // =============================================================
  //  Integration with MEVInsurance
  // =============================================================
  describe("Integration with MEVInsurance", function () {
    let token, insurance, registry;

    beforeEach(async function () {
      const MEVToken = await ethers.getContractFactory("MEVToken");
      token = await MEVToken.deploy();
      await token.waitForDeployment();

      const OracleRegistry = await ethers.getContractFactory("OracleRegistry");
      registry = await OracleRegistry.deploy();
      await registry.waitForDeployment();

      const MEVInsurance = await ethers.getContractFactory("MEVInsurance");
      insurance = await MEVInsurance.deploy(
        await token.getAddress(),
        await registry.getAddress()
      );
      await insurance.waitForDeployment();

      // Fund insurance for payouts
      await token.transfer(await insurance.getAddress(), e(100000));
    });

    it("should use default premium when no calculator set", async function () {
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(500));
      await token.connect(user1).approve(await insurance.getAddress(), e(100));

      const balBefore = await token.balanceOf(user1.address);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const balAfter = await token.balanceOf(user1.address);

      expect(balBefore - balAfter).to.equal(e(100)); // defaultPremium
    });

    it("should use calculator premium when set", async function () {
      // Set calculator on insurance
      await insurance.setPremiumCalculator(await calculator.getAddress());

      // With default params (no market data), High coverage on defaultCoverage=1000:
      // premium = max(1000 * formula, 1000 * 1.5%) = max(15, 15) = 15 MEVI
      await insurance.connect(user1).registerUser();
      await token.transfer(user1.address, e(500));
      await token.connect(user1).approve(await insurance.getAddress(), e(500));

      const balBefore = await token.balanceOf(user1.address);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const balAfter = await token.balanceOf(user1.address);

      expect(balBefore - balAfter).to.equal(e(15)); // calculator premium
    });

    it("only owner can set premium calculator", async function () {
      await expect(
        insurance.connect(user1).setPremiumCalculator(await calculator.getAddress())
      ).to.be.revertedWithCustomError(insurance, "OwnableUnauthorizedAccount");
    });
  });
});
