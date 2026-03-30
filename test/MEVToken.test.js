const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("MEVToken", function () {
  let token;
  let deployer;
  let user1;
  let user2;

  beforeEach(async function () {
    [deployer, user1, user2] = await ethers.getSigners();
    const MEVToken = await ethers.getContractFactory("MEVToken");
    token = await MEVToken.deploy();
    await token.waitForDeployment();
  });

  describe("Deployment", function () {
    it("should have correct name", async function () {
      expect(await token.name()).to.equal("MEV Insurance Token");
    });

    it("should have correct symbol", async function () {
      expect(await token.symbol()).to.equal("MEVI");
    });

    it("should have 18 decimals", async function () {
      expect(await token.decimals()).to.equal(18);
    });

    it("should mint 1,000,000 tokens to deployer", async function () {
      const expected = ethers.parseEther("1000000");
      expect(await token.balanceOf(deployer.address)).to.equal(expected);
    });

    it("should set total supply to 1,000,000 tokens", async function () {
      const expected = ethers.parseEther("1000000");
      expect(await token.totalSupply()).to.equal(expected);
    });
  });

  describe("Transfers", function () {
    it("should transfer tokens between accounts", async function () {
      const amount = ethers.parseEther("100");
      await token.transfer(user1.address, amount);

      expect(await token.balanceOf(user1.address)).to.equal(amount);
      expect(await token.balanceOf(deployer.address)).to.equal(
        ethers.parseEther("999900")
      );
    });

    it("should fail when sender has insufficient balance", async function () {
      const amount = ethers.parseEther("1");
      await expect(
        token.connect(user1).transfer(deployer.address, amount)
      ).to.be.revertedWithCustomError(token, "ERC20InsufficientBalance");
    });
  });

  describe("Allowances", function () {
    it("should approve and transferFrom", async function () {
      const amount = ethers.parseEther("500");
      await token.approve(user1.address, amount);
      await token
        .connect(user1)
        .transferFrom(deployer.address, user2.address, amount);

      expect(await token.balanceOf(user2.address)).to.equal(amount);
    });
  });
});
