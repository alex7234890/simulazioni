const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  const e = (n) => hre.ethers.parseEther(String(n));

  console.log("Deploying with account:", deployer.address);
  console.log("Balance:", hre.ethers.formatEther(await hre.ethers.provider.getBalance(deployer.address)), "ETH\n");

  // ── 1. MEVToken ──
  const MEVToken = await hre.ethers.getContractFactory("MEVToken");
  const token = await MEVToken.deploy();
  await token.waitForDeployment();
  console.log("MEVToken:", await token.getAddress());

  // ── 2. OracleRegistry ──
  const OracleRegistry = await hre.ethers.getContractFactory("OracleRegistry");
  const registry = await OracleRegistry.deploy();
  await registry.waitForDeployment();
  console.log("OracleRegistry:", await registry.getAddress());

  // ── 3. PremiumCalculator ──
  const PremiumCalculator = await hre.ethers.getContractFactory("PremiumCalculator");
  const calculator = await PremiumCalculator.deploy();
  await calculator.waitForDeployment();
  console.log("PremiumCalculator:", await calculator.getAddress());

  // ── 4. MEVInsurance ──
  const MEVInsurance = await hre.ethers.getContractFactory("MEVInsurance");
  const insurance = await MEVInsurance.deploy(
    await token.getAddress(),
    await registry.getAddress()
  );
  await insurance.waitForDeployment();
  console.log("MEVInsurance:", await insurance.getAddress());

  // ── 5. TierSystem ──
  const TierSystem = await hre.ethers.getContractFactory("TierSystem");
  const tierSystem = await TierSystem.deploy(await token.getAddress());
  await tierSystem.waitForDeployment();
  console.log("TierSystem:", await tierSystem.getAddress());

  // ── 6. SlashingSystem ──
  const SlashingSystem = await hre.ethers.getContractFactory("SlashingSystem");
  const slashingSystem = await SlashingSystem.deploy(await registry.getAddress());
  await slashingSystem.waitForDeployment();
  console.log("SlashingSystem:", await slashingSystem.getAddress());

  // ── 7. MockUSDC ──
  const MockUSDC = await hre.ethers.getContractFactory("MockUSDC");
  const usdc = await MockUSDC.deploy();
  await usdc.waitForDeployment();
  console.log("MockUSDC:", await usdc.getAddress());

  // ── 8. MockAMM ──
  const MockAMM = await hre.ethers.getContractFactory("MockAMM");
  const amm = await MockAMM.deploy(await token.getAddress(), await usdc.getAddress());
  await amm.waitForDeployment();
  console.log("MockAMM:", await amm.getAddress());

  // ── 9. SandwichBot ──
  const SandwichBot = await hre.ethers.getContractFactory("SandwichBot");
  const bot = await SandwichBot.deploy();
  await bot.waitForDeployment();
  console.log("SandwichBot:", await bot.getAddress());

  // ── 10. PattUpdater ──
  const PattUpdater = await hre.ethers.getContractFactory("PattUpdater");
  const pattUpdater = await PattUpdater.deploy(
    await registry.getAddress(),
    await calculator.getAddress()
  );
  await pattUpdater.waitForDeployment();
  console.log("PattUpdater:", await pattUpdater.getAddress());

  // ══════════════════════════════════════════════════════════
  //  Wiring
  // ══════════════════════════════════════════════════════════
  console.log("\n── Wiring contracts ──");

  // MEVInsurance -> PremiumCalculator
  await insurance.setPremiumCalculator(await calculator.getAddress());
  console.log("MEVInsurance.setPremiumCalculator done");

  // MEVInsurance -> TierSystem
  await insurance.setTierSystem(await tierSystem.getAddress());
  console.log("MEVInsurance.setTierSystem done");

  // TierSystem -> MEVInsurance
  await tierSystem.setInsuranceContract(await insurance.getAddress());
  console.log("TierSystem.setInsuranceContract done");

  // PremiumCalculator -> PattUpdater authorized
  await calculator.setAuthorizedUpdater(await pattUpdater.getAddress());
  console.log("PremiumCalculator.setAuthorizedUpdater(PattUpdater) done");

  // OracleRegistry -> authorize MEVInsurance (for rewards, penalties)
  await registry.setAuthorizedCaller(await insurance.getAddress(), true);
  console.log("OracleRegistry.authorize(MEVInsurance) done");

  // OracleRegistry -> authorize SlashingSystem
  await registry.setAuthorizedCaller(await slashingSystem.getAddress(), true);
  console.log("OracleRegistry.authorize(SlashingSystem) done");

  // OracleRegistry -> set tActivation to 0 for local testing (no delay)
  await registry.setTActivation(0);
  console.log("OracleRegistry.setTActivation(0) done");

  // ══════════════════════════════════════════════════════════
  //  Fund contracts
  // ══════════════════════════════════════════════════════════
  console.log("\n── Funding ──");

  // Fund MEVInsurance pool: 500k MEVI
  await token.transfer(await insurance.getAddress(), e(500000));
  console.log("MEVInsurance funded with 500,000 MEVI");

  // Add AMM liquidity: 100k MEVI + 100k USDC
  await token.approve(await amm.getAddress(), e(100000));
  await usdc.approve(await amm.getAddress(), e(100000));
  await amm.addLiquidity(e(100000), e(100000));
  console.log("MockAMM: 100k MEVI + 100k USDC liquidity added");

  // Fund OracleRegistry for oracle rewards
  await deployer.sendTransaction({
    to: await registry.getAddress(),
    value: e(10)
  });
  console.log("OracleRegistry funded with 10 ETH for rewards");

  // Fund SlashingSystem for reporter payments
  await deployer.sendTransaction({
    to: await slashingSystem.getAddress(),
    value: e(5)
  });
  console.log("SlashingSystem funded with 5 ETH");

  // ══════════════════════════════════════════════════════════
  //  Save addresses
  // ══════════════════════════════════════════════════════════
  const addresses = {
    MEVToken: await token.getAddress(),
    OracleRegistry: await registry.getAddress(),
    PremiumCalculator: await calculator.getAddress(),
    MEVInsurance: await insurance.getAddress(),
    TierSystem: await tierSystem.getAddress(),
    SlashingSystem: await slashingSystem.getAddress(),
    MockUSDC: await usdc.getAddress(),
    MockAMM: await amm.getAddress(),
    SandwichBot: await bot.getAddress(),
    PattUpdater: await pattUpdater.getAddress()
  };

  const configDir = path.join(__dirname, "..", "config");
  if (!fs.existsSync(configDir)) fs.mkdirSync(configDir, { recursive: true });
  const configPath = path.join(configDir, "deployed_addresses.json");
  fs.writeFileSync(configPath, JSON.stringify(addresses, null, 2));

  console.log("\n══════════════════════════════════════");
  console.log("  All contracts deployed and wired!");
  console.log("══════════════════════════════════════");
  console.log("Addresses saved to:", configPath);
  console.log(JSON.stringify(addresses, null, 2));
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
