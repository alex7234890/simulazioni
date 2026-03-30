const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("MEVInsurance", function () {
  let token;
  let insurance;
  let owner;
  let user1;
  let user2;

  const PREMIUM = ethers.parseEther("100");
  const COVERAGE = ethers.parseEther("1000");

  beforeEach(async function () {
    [owner, user1, user2] = await ethers.getSigners();

    // Deploy token
    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();

    // Deploy insurance
    const MEVInsurance = await ethers.getContractFactory("MEVInsurance");
    insurance = await MEVInsurance.deploy(await token.getAddress());
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
      await insurance.connect(user1).buyPolicy();
      const policy = await insurance.policies(user1.address);
      expect(policy.active).to.be.true;
      expect(policy.coverageRemaining).to.equal(COVERAGE);
    });

    it("should deduct premium from user", async function () {
      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.connect(user1).buyPolicy();
      const balanceAfter = await token.balanceOf(user1.address);
      expect(balanceBefore - balanceAfter).to.equal(PREMIUM);
    });

    it("should revert if not registered", async function () {
      await token.connect(user2).approve(await insurance.getAddress(), PREMIUM);
      await expect(
        insurance.connect(user2).buyPolicy()
      ).to.be.revertedWith("Not registered");
    });

    it("should revert if policy already active", async function () {
      await insurance.connect(user1).buyPolicy();
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
      await expect(
        insurance.connect(user1).buyPolicy()
      ).to.be.revertedWith("Policy already active");
    });
  });

  describe("Submit Claim", function () {
    beforeEach(async function () {
      await insurance.connect(user1).registerUser();
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
      await insurance.connect(user1).buyPolicy();
    });

    it("should submit a valid claim", async function () {
      const amount = ethers.parseEther("50");
      await insurance.connect(user1).submitClaim(amount, "Sandwich attack on swap");

      expect(await insurance.getClaimsCount()).to.equal(1);
      const claim = await insurance.claims(0);
      expect(claim.claimant).to.equal(user1.address);
      expect(claim.amount).to.equal(amount);
    });

    it("should revert if no active policy", async function () {
      const amount = ethers.parseEther("50");
      await expect(
        insurance.connect(user2).submitClaim(amount, "Attack")
      ).to.be.revertedWith("No active policy");
    });

    it("should revert if claim exceeds coverage", async function () {
      const amount = ethers.parseEther("1001");
      await expect(
        insurance.connect(user1).submitClaim(amount, "Attack")
      ).to.be.revertedWith("Invalid claim amount");
    });

    it("should revert if policy expired", async function () {
      // Advance time past 30 days
      await time.increase(31 * 24 * 60 * 60);
      const amount = ethers.parseEther("50");
      await expect(
        insurance.connect(user1).submitClaim(amount, "Attack")
      ).to.be.revertedWith("Policy expired");
    });
  });

  describe("Approve / Reject Claims", function () {
    beforeEach(async function () {
      await insurance.connect(user1).registerUser();
      await token.connect(user1).approve(await insurance.getAddress(), PREMIUM);
      await insurance.connect(user1).buyPolicy();
      await insurance.connect(user1).submitClaim(ethers.parseEther("200"), "MEV sandwich");
    });

    it("should approve a claim and pay out", async function () {
      const balanceBefore = await token.balanceOf(user1.address);
      await insurance.approveClaim(0);
      const balanceAfter = await token.balanceOf(user1.address);

      expect(balanceAfter - balanceBefore).to.equal(ethers.parseEther("200"));

      const claim = await insurance.claims(0);
      expect(claim.status).to.equal(1); // Approved
    });

    it("should reduce coverage remaining after approval", async function () {
      await insurance.approveClaim(0);
      const policy = await insurance.policies(user1.address);
      expect(policy.coverageRemaining).to.equal(ethers.parseEther("800"));
    });

    it("should reject a claim", async function () {
      await insurance.rejectClaim(0);
      const claim = await insurance.claims(0);
      expect(claim.status).to.equal(2); // Rejected
    });

    it("should revert if non-owner tries to approve", async function () {
      await expect(
        insurance.connect(user1).approveClaim(0)
      ).to.be.revertedWithCustomError(insurance, "OwnableUnauthorizedAccount");
    });
  });
});
