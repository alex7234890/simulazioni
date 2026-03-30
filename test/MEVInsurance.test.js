const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("MEVInsurance (Legacy Interface)", function () {
  let token;
  let insurance;
  let registry;
  let owner;
  let user1;
  let user2;

  const PREMIUM = ethers.parseEther("100");
  const CoverageLevel = { Low: 0, Medium: 1, High: 2 };

  beforeEach(async function () {
    [owner, user1, user2] = await ethers.getSigners();

    // Deploy token
    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();

    // Deploy oracle registry
    const OracleRegistry = await ethers.getContractFactory("OracleRegistry");
    registry = await OracleRegistry.deploy();
    await registry.waitForDeployment();

    // Deploy insurance (now takes token + oracle registry)
    const MEVInsurance = await ethers.getContractFactory("MEVInsurance");
    insurance = await MEVInsurance.deploy(
      await token.getAddress(),
      await registry.getAddress()
    );
    await insurance.waitForDeployment();

    // Give user1 tokens for premium and approve
    await token.transfer(user1.address, ethers.parseEther("500"));

    // Fund insurance contract for payouts
    await token.transfer(await insurance.getAddress(), ethers.parseEther("10000"));
  });

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
  });

  describe("Buy Policy", function () {
    beforeEach(async function () {
      await insurance.connect(user1).registerUser();
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
    });

    it("should allow registered user to buy a policy", async function () {
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const policy = await insurance.policies(user1.address);
      expect(policy.active).to.be.true;
    });

    it("should deduct premium from user", async function () {
      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.connect(user1).buyPolicy(CoverageLevel.High);
      const balanceAfter = await token.balanceOf(user1.address);
      expect(balanceBefore - balanceAfter).to.equal(PREMIUM);
    });

    it("should revert if not registered", async function () {
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
  });

  describe("View Functions", function () {
    it("should return claims count of 0 initially", async function () {
      expect(await insurance.getClaimsCount()).to.equal(0);
    });

    it("should return empty user claims initially", async function () {
      const claims = await insurance.getUserClaims(user1.address);
      expect(claims.length).to.equal(0);
    });
  });
});
