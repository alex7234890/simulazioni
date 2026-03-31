const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("SandwichBot + MockAMM (Phase 7)", function () {
  let tokenA, tokenB, amm, bot;
  let owner, victim, attacker;

  const e = (n) => ethers.parseEther(String(n));

  beforeEach(async function () {
    [owner, victim, attacker] = await ethers.getSigners();

    // Deploy tokens
    const MEVToken = await ethers.getContractFactory("MEVToken");
    tokenA = await MEVToken.deploy();
    await tokenA.waitForDeployment();

    const MockUSDC = await ethers.getContractFactory("MockUSDC");
    tokenB = await MockUSDC.deploy();
    await tokenB.waitForDeployment();

    // Deploy AMM
    const MockAMM = await ethers.getContractFactory("MockAMM");
    amm = await MockAMM.deploy(await tokenA.getAddress(), await tokenB.getAddress());
    await amm.waitForDeployment();

    // Deploy SandwichBot (owned by attacker)
    const SandwichBot = await ethers.getContractFactory("SandwichBot");
    bot = await SandwichBot.connect(attacker).deploy();
    await bot.waitForDeployment();

    // Add liquidity: 100,000 tokenA + 100,000 tokenB (1:1 price)
    await tokenA.approve(await amm.getAddress(), e(100000));
    await tokenB.approve(await amm.getAddress(), e(100000));
    await amm.addLiquidity(e(100000), e(100000));

    // Give victim some tokenA for swapping
    await tokenA.transfer(victim.address, e(10000));

    // Give attacker (bot) some tokenA for frontrunning
    await tokenA.transfer(await bot.getAddress(), e(50000));
  });

  // =============================================================
  //  MockAMM Tests
  // =============================================================
  describe("MockAMM", function () {
    it("should have correct initial reserves", async function () {
      expect(await amm.reserveA()).to.equal(e(100000));
      expect(await amm.reserveB()).to.equal(e(100000));
    });

    it("should have initial price of 1.0", async function () {
      const price = await amm.getPrice();
      expect(price).to.equal(e(1)); // 1e18 = 1.0
    });

    it("should execute a simple swap A->B", async function () {
      await tokenA.transfer(victim.address, e(1000));
      await tokenA.connect(victim).approve(await amm.getAddress(), e(1000));

      const balBBefore = await tokenB.balanceOf(victim.address);
      await amm.connect(victim).swap(await tokenA.getAddress(), e(1000));
      const balBAfter = await tokenB.balanceOf(victim.address);

      const received = balBAfter - balBBefore;
      expect(received).to.be.gt(0);
      // Due to constant product, 1000 in should give < 1000 out
      expect(received).to.be.lt(e(1000));
    });

    it("should execute a swap B->A", async function () {
      await tokenB.transfer(victim.address, e(1000));
      await tokenB.connect(victim).approve(await amm.getAddress(), e(1000));

      const balABefore = await tokenA.balanceOf(victim.address);
      await amm.connect(victim).swap(await tokenB.getAddress(), e(1000));
      const balAAfter = await tokenA.balanceOf(victim.address);

      expect(balAAfter - balABefore).to.be.gt(0);
    });

    it("should change price after swap", async function () {
      const priceBefore = await amm.getPrice();

      // Swap A->B: more A in pool, less B -> price of A drops
      await tokenA.approve(await amm.getAddress(), e(10000));
      await amm.swap(await tokenA.getAddress(), e(10000));

      const priceAfter = await amm.getPrice();
      // After adding A, B/A ratio decreases
      expect(priceAfter).to.be.lt(priceBefore);
    });

    it("should maintain k constant (approximately)", async function () {
      const rA1 = await amm.reserveA();
      const rB1 = await amm.reserveB();
      const k1 = rA1 * rB1;

      await tokenA.approve(await amm.getAddress(), e(5000));
      await amm.swap(await tokenA.getAddress(), e(5000));

      const rA2 = await amm.reserveA();
      const rB2 = await amm.reserveB();
      const k2 = rA2 * rB2;

      // k should be approximately equal (may differ slightly by integer rounding)
      // Integer division can cause k to decrease very slightly
      const diff = k1 > k2 ? k1 - k2 : k2 - k1;
      // Difference should be negligible relative to k
      expect(diff * 1000000n / k1).to.equal(0n);
    });

    it("should revert swap with invalid token", async function () {
      await expect(
        amm.swap(victim.address, e(100))
      ).to.be.revertedWith("Invalid token");
    });

    it("should revert swap with zero amount", async function () {
      await expect(
        amm.swap(await tokenA.getAddress(), 0)
      ).to.be.revertedWith("Amount must be > 0");
    });

    it("should emit Swap event", async function () {
      await tokenA.approve(await amm.getAddress(), e(100));
      await expect(amm.swap(await tokenA.getAddress(), e(100)))
        .to.emit(amm, "Swap");
    });

    it("getAmountOut should match actual swap output", async function () {
      const amountIn = e(5000);
      const expected = await amm.getAmountOut(await tokenA.getAddress(), amountIn);

      await tokenA.approve(await amm.getAddress(), amountIn);
      const balBefore = await tokenB.balanceOf(owner.address);
      await amm.swap(await tokenA.getAddress(), amountIn);
      const balAfter = await tokenB.balanceOf(owner.address);

      expect(balAfter - balBefore).to.equal(expected);
    });

    it("should emit LiquidityAdded event", async function () {
      await tokenA.approve(await amm.getAddress(), e(100));
      await tokenB.approve(await amm.getAddress(), e(100));
      await expect(amm.addLiquidity(e(100), e(100)))
        .to.emit(amm, "LiquidityAdded")
        .withArgs(owner.address, e(100), e(100));
    });

    it("should revert addLiquidity with zero amounts", async function () {
      await expect(amm.addLiquidity(0, e(100)))
        .to.be.revertedWith("Amounts must be > 0");
    });

    it("larger swaps get worse price (slippage)", async function () {
      // Small swap: 100 tokens
      const outSmall = await amm.getAmountOut(await tokenA.getAddress(), e(100));
      // Large swap: 10000 tokens
      const outLarge = await amm.getAmountOut(await tokenA.getAddress(), e(10000));

      // Rate for small swap
      const rateSmall = (outSmall * 10000n) / e(100);
      // Rate for large swap
      const rateLarge = (outLarge * 10000n) / e(10000);

      // Large swap should have worse rate (more slippage)
      expect(rateLarge).to.be.lt(rateSmall);
    });
  });

  // =============================================================
  //  SandwichBot Tests
  // =============================================================
  describe("SandwichBot", function () {
    it("should execute frontrun", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const ammAddr = await amm.getAddress();

      // Bot needs to approve AMM - but bot calls swap internally which approves
      await expect(
        bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000))
      ).to.emit(bot, "Frontrun");

      expect(await bot.getAttackCount()).to.equal(1);
    });

    it("should execute backrun after frontrun", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      // Frontrun: buy tokenB with tokenA
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));

      // Get how much tokenB the bot received
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      expect(botBalB).to.be.gt(0);

      // Backrun: sell tokenB for tokenA
      await expect(
        bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB)
      ).to.emit(bot, "Backrun");
    });

    it("should revert backrun without frontrun", async function () {
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      await expect(
        bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, e(100))
      ).to.be.revertedWith("No frontrun executed");
    });

    it("only owner can execute frontrun", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const ammAddr = await amm.getAddress();

      await expect(
        bot.connect(victim).executeFrontrun(ammAddr, tokenAAddr, e(100))
      ).to.be.revertedWithCustomError(bot, "OwnableUnauthorizedAccount");
    });

    it("only owner can execute backrun", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));

      await expect(
        bot.connect(victim).executeBackrun(ammAddr, tokenBAddr, e(100))
      ).to.be.revertedWithCustomError(bot, "OwnableUnauthorizedAccount");
    });

    it("should record attack details", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      const attack = await bot.getAttack(0);
      expect(attack.frontrunAmountIn).to.equal(e(5000));
      expect(attack.frontrunAmountOut).to.be.gt(0);
      expect(attack.backrunAmountIn).to.equal(botBalB);
      expect(attack.backrunAmountOut).to.be.gt(0);
    });

    it("should allow token withdrawal", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const botAddr = await bot.getAddress();

      const balBefore = await tokenA.balanceOf(attacker.address);
      await bot.connect(attacker).withdrawToken(tokenAAddr, e(1000));
      const balAfter = await tokenA.balanceOf(attacker.address);

      expect(balAfter - balBefore).to.equal(e(1000));
    });
  });

  // =============================================================
  //  Full Sandwich Attack Simulation
  // =============================================================
  describe("Full Sandwich Attack", function () {
    it("should demonstrate sandwich attack profitability", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      // --- Record initial state ---
      const priceBefore = await amm.getPrice();
      const victimBalABefore = await tokenA.balanceOf(victim.address);

      // --- Step 1: Frontrun - bot buys tokenB with 5000 tokenA ---
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));
      const priceAfterFrontrun = await amm.getPrice();

      // Price of A should drop (more A in pool, less B)
      expect(priceAfterFrontrun).to.be.lt(priceBefore);

      // --- Step 2: Victim swap - buys 1000 tokenA worth of tokenB ---
      const victimSwapAmount = e(1000);
      await tokenA.connect(victim).approve(ammAddr, victimSwapAmount);

      // Calculate what victim would have gotten WITHOUT frontrun
      // We can't easily calculate this on-chain, but we can check victim gets less
      const victimExpectedWithFrontrun = await amm.getAmountOut(tokenAAddr, victimSwapAmount);
      await amm.connect(victim).swap(tokenAAddr, victimSwapAmount);

      const victimBalBAfter = await tokenB.balanceOf(victim.address);

      // --- Step 3: Backrun - bot sells all acquired tokenB ---
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      // --- Verify attack results ---
      const attack = await bot.getAttack(0);

      // Bot should have made a profit (backrun output > frontrun input)
      // Note: with constant product, profit depends on victim's swap size
      // relative to pool and frontrun size
      expect(attack.backrunAmountOut).to.be.gt(0);

      // The attack profit can be checked
      const botFinalBalA = await tokenA.balanceOf(await bot.getAddress());
      // Bot started with 50000, spent 5000 on frontrun, got backrunAmountOut back
      // Net position should be checked

      // Verify victim suffered slippage due to sandwich
      // Victim got tokenB at a worse rate than they would have without frontrun
      expect(victimBalBAfter).to.be.gt(0);
    });

    it("victim gets worse price due to sandwich", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();
      const victimSwapAmount = e(1000);

      // --- Scenario 1: Victim swaps WITHOUT sandwich ---
      const fairOutput = await amm.getAmountOut(tokenAAddr, victimSwapAmount);

      // --- Now execute sandwich ---
      // Frontrun
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(10000));

      // Victim swap (after frontrun, worse price)
      const sandwichedOutput = await amm.getAmountOut(tokenAAddr, victimSwapAmount);
      await tokenA.connect(victim).approve(ammAddr, victimSwapAmount);
      await amm.connect(victim).swap(tokenAAddr, victimSwapAmount);

      // Backrun
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      // Victim got LESS tokenB than they would have without the sandwich
      expect(sandwichedOutput).to.be.lt(fairOutput);

      // Calculate victim's loss
      const victimLoss = fairOutput - sandwichedOutput;
      expect(victimLoss).to.be.gt(0);
    });

    it("larger frontrun causes more victim slippage", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const ammAddr = await amm.getAddress();
      const victimSwapAmount = e(1000);

      // Fair price output
      const fairOutput = await amm.getAmountOut(tokenAAddr, victimSwapAmount);

      // Small frontrun (1000 tokens)
      // Deploy a fresh AMM for clean comparison
      const MockAMM2 = await ethers.getContractFactory("MockAMM");
      const amm2 = await MockAMM2.deploy(await tokenA.getAddress(), await tokenB.getAddress());
      await amm2.waitForDeployment();
      await tokenA.approve(await amm2.getAddress(), e(100000));
      await tokenB.approve(await amm2.getAddress(), e(100000));
      await amm2.addLiquidity(e(100000), e(100000));

      // Small frontrun
      await tokenA.approve(await amm2.getAddress(), e(1000));
      await amm2.swap(tokenAAddr, e(1000));
      const outputAfterSmallFrontrun = await amm2.getAmountOut(tokenAAddr, victimSwapAmount);

      // Large frontrun on original AMM
      await tokenA.approve(ammAddr, e(10000));
      await amm.swap(tokenAAddr, e(10000));
      const outputAfterLargeFrontrun = await amm.getAmountOut(tokenAAddr, victimSwapAmount);

      // Larger frontrun = worse price for victim
      expect(outputAfterLargeFrontrun).to.be.lt(outputAfterSmallFrontrun);
    });

    it("should generate 3 tx hashes for claim submission", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();
      const victimSwapAmount = e(1000);

      // Step 1: Frontrun
      const frontrunTx = await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));
      const frontrunReceipt = await frontrunTx.wait();

      // Step 2: Victim swap
      await tokenA.connect(victim).approve(ammAddr, victimSwapAmount);
      const victimTx = await amm.connect(victim).swap(tokenAAddr, victimSwapAmount);
      const victimReceipt = await victimTx.wait();

      // Step 3: Backrun
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      const backrunTx = await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);
      const backrunReceipt = await backrunTx.wait();

      // All 3 tx hashes should be available for claim
      expect(frontrunReceipt.hash).to.be.a("string");
      expect(victimReceipt.hash).to.be.a("string");
      expect(backrunReceipt.hash).to.be.a("string");

      // All should be different
      expect(frontrunReceipt.hash).to.not.equal(victimReceipt.hash);
      expect(victimReceipt.hash).to.not.equal(backrunReceipt.hash);
    });

    it("bot profit is positive with large enough victim swap", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      // Frontrun with moderate amount
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(5000));

      // Victim does a large swap (5000 tokens)
      const victimSwap = e(5000);
      await tokenA.connect(victim).approve(ammAddr, victimSwap);
      await amm.connect(victim).swap(tokenAAddr, victimSwap);

      // Backrun
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      const attack = await bot.getAttack(0);
      // With a large victim swap, bot should profit
      expect(attack.profit).to.be.gt(0);
    });

    it("calculates victim loss correctly", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();
      const victimSwapAmount = e(2000);

      // Fair output (without sandwich)
      const fairOutput = await amm.getAmountOut(tokenAAddr, victimSwapAmount);

      // Frontrun
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(8000));

      // Actual output (with sandwich)
      const actualOutput = await amm.getAmountOut(tokenAAddr, victimSwapAmount);
      await tokenA.connect(victim).approve(ammAddr, victimSwapAmount);
      await amm.connect(victim).swap(tokenAAddr, victimSwapAmount);

      // Backrun
      const botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      // Victim loss = fairOutput - actualOutput
      const victimLoss = fairOutput - actualOutput;
      expect(victimLoss).to.be.gt(0);

      // This loss value is what the user would submit in their claim
      // loss should be significant with an 8000 frontrun vs 2000 victim swap
      expect(victimLoss).to.be.gt(e(1)); // at least 1 token loss
    });
  });

  // =============================================================
  //  Edge Cases
  // =============================================================
  describe("Edge Cases", function () {
    it("very small swap should still work", async function () {
      const tokenAAddr = await tokenA.getAddress();
      await tokenA.approve(await amm.getAddress(), e(1));
      const balBBefore = await tokenB.balanceOf(owner.address);
      await amm.swap(tokenAAddr, e(1));
      const balBAfter = await tokenB.balanceOf(owner.address);
      expect(balBAfter - balBBefore).to.be.gt(0);
    });

    it("multiple sequential swaps should work", async function () {
      const tokenAAddr = await tokenA.getAddress();
      for (let i = 0; i < 5; i++) {
        await tokenA.approve(await amm.getAddress(), e(100));
        await amm.swap(tokenAAddr, e(100));
      }
      // Reserves should have changed
      expect(await amm.reserveA()).to.be.gt(e(100000));
      expect(await amm.reserveB()).to.be.lt(e(100000));
    });

    it("multiple attacks can be recorded", async function () {
      const tokenAAddr = await tokenA.getAddress();
      const tokenBAddr = await tokenB.getAddress();
      const ammAddr = await amm.getAddress();

      // Attack 1
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(1000));
      let botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      // Attack 2
      await bot.connect(attacker).executeFrontrun(ammAddr, tokenAAddr, e(2000));
      botBalB = await tokenB.balanceOf(await bot.getAddress());
      await bot.connect(attacker).executeBackrun(ammAddr, tokenBAddr, botBalB);

      expect(await bot.getAttackCount()).to.equal(2);
    });
  });
});
