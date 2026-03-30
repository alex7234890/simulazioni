const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("DataTypes", function () {
  let consumer;
  let deployer;
  let user1;
  let oracle1;

  // Enum values
  const Tier = { Bronze: 0, Silver: 1, Gold: 2, Platinum: 3 };
  const CoverageLevel = { Low: 0, Medium: 1, High: 2 };
  const ClaimStatus = {
    Pending: 0, OracleReview: 1, CAPTCHARequired: 2,
    Approved: 3, Rejected: 4, InvalidPattern: 5
  };
  const OracleStatus = {
    Inactive: 0, Pending: 1, Active: 2, Watchlisted: 3,
    Contested: 4, Slashed: 5, Expelled: 6
  };

  beforeEach(async function () {
    [deployer, user1, oracle1] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("DataTypesConsumer");
    consumer = await Factory.deploy();
    await consumer.waitForDeployment();
  });

  describe("Tier Enum", function () {
    it("should default to Bronze (0)", async function () {
      expect(await consumer.getUserTier(user1.address)).to.equal(Tier.Bronze);
    });

    it("should set and get all tier values", async function () {
      for (const [name, value] of Object.entries(Tier)) {
        await consumer.setUserTier(user1.address, value);
        expect(await consumer.getUserTier(user1.address)).to.equal(value);
      }
    });
  });

  describe("CoverageLevel Enum", function () {
    it("should set and get all coverage levels", async function () {
      for (const [name, value] of Object.entries(CoverageLevel)) {
        await consumer.setCoverageLevel(user1.address, value);
        expect(await consumer.getCoverageLevel(user1.address)).to.equal(value);
      }
    });
  });

  describe("ClaimStatus Enum", function () {
    const tx1 = ethers.keccak256(ethers.toUtf8Bytes("frontrun"));
    const tx2 = ethers.keccak256(ethers.toUtf8Bytes("victim"));
    const tx3 = ethers.keccak256(ethers.toUtf8Bytes("backrun"));

    it("should create claim with Pending status", async function () {
      await consumer.createClaim(user1.address, tx1, tx2, tx3);
      expect(await consumer.getClaimStatus(0)).to.equal(ClaimStatus.Pending);
    });

    it("should transition through all claim statuses", async function () {
      await consumer.createClaim(user1.address, tx1, tx2, tx3);
      for (const [name, value] of Object.entries(ClaimStatus)) {
        await consumer.setClaimStatus(0, value);
        expect(await consumer.getClaimStatus(0)).to.equal(value);
      }
    });
  });

  describe("OracleStatus Enum", function () {
    it("should default to Inactive (0)", async function () {
      expect(await consumer.getOracleStatus(oracle1.address)).to.equal(OracleStatus.Inactive);
    });

    it("should transition through all oracle statuses", async function () {
      for (const [name, value] of Object.entries(OracleStatus)) {
        await consumer.setOracleStatus(oracle1.address, value);
        expect(await consumer.getOracleStatus(oracle1.address)).to.equal(value);
      }
    });
  });

  describe("UserProfile Struct", function () {
    it("should store and retrieve user profile fields", async function () {
      await consumer.setUserProfile(user1.address, 42, 5, 30, false, 0);
      const [tier, totalSwaps, totalClaims, avgFraudScore, isBlacklisted, debt] =
        await consumer.getUserProfile(user1.address);

      expect(tier).to.equal(Tier.Bronze);
      expect(totalSwaps).to.equal(42);
      expect(totalClaims).to.equal(5);
      expect(avgFraudScore).to.equal(30);
      expect(isBlacklisted).to.be.false;
      expect(debt).to.equal(0);
    });

    it("should store blacklisted user with debt", async function () {
      const debtAmount = ethers.parseEther("500");
      await consumer.setUserProfile(user1.address, 10, 3, 80, true, debtAmount);
      const [, , , avgFraudScore, isBlacklisted, debt] =
        await consumer.getUserProfile(user1.address);

      expect(avgFraudScore).to.equal(80);
      expect(isBlacklisted).to.be.true;
      expect(debt).to.equal(debtAmount);
    });
  });

  describe("Policy Struct", function () {
    it("should create and retrieve a policy", async function () {
      const premium = ethers.parseEther("100");
      const maxSwap = ethers.parseEther("5000");

      await consumer.createPolicy(user1.address, CoverageLevel.High, premium, maxSwap);
      const [holder, cov, prem, active] = await consumer.getPolicy(0);

      expect(holder).to.equal(user1.address);
      expect(cov).to.equal(CoverageLevel.High);
      expect(prem).to.equal(premium);
      expect(active).to.be.true;
    });

    it("should create multiple policies", async function () {
      await consumer.createPolicy(user1.address, CoverageLevel.Low, 100, 1000);
      await consumer.createPolicy(deployer.address, CoverageLevel.Medium, 200, 2000);
      expect(await consumer.getPoliciesCount()).to.equal(2);
    });
  });

  describe("Claim Struct", function () {
    it("should track claim count", async function () {
      const tx1 = ethers.keccak256(ethers.toUtf8Bytes("tx1"));
      const tx2 = ethers.keccak256(ethers.toUtf8Bytes("tx2"));
      const tx3 = ethers.keccak256(ethers.toUtf8Bytes("tx3"));

      await consumer.createClaim(user1.address, tx1, tx2, tx3);
      await consumer.createClaim(deployer.address, tx1, tx2, tx3);
      expect(await consumer.getClaimsCount()).to.equal(2);
    });
  });
});
