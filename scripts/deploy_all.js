const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
    const [deployer] = await hre.ethers.getSigners();
    const e = (n) => hre.ethers.parseEther(String(n));

    let deployParams = { amm_mevi: 100000, amm_usdc: 100000, pool_mevi: 500000, pool_eth: 5000 };
    const deployParamsPath = path.join(__dirname, "..", "config", "deploy_params.json");
    try {
        const raw = fs.readFileSync(deployParamsPath, "utf8");
        deployParams = { ...deployParams, ...JSON.parse(raw) };
    } catch (e) { /* usa default */ }
    console.log(`\n── Riserve iniziali ──`);
    console.log(`  MockAMM:      ${deployParams.amm_mevi.toLocaleString()} MEVI + ${deployParams.amm_usdc.toLocaleString()} USDC`);
    console.log(`  Pool MEVI:    ${deployParams.pool_mevi.toLocaleString()} MEVI`);
    console.log(`  Pool ETH:     ${deployParams.pool_eth.toLocaleString()} ETH`);

    console.log("⚡ Velocizzando il mining per il deploy...");
    await hre.network.provider.send("evm_setAutomine", [true]);
    await hre.network.provider.send("evm_setIntervalMining", [0]);

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

    // OracleRegistry -> authorize MEVInsurance (for rewards, penalties)
    await registry.setAuthorizedCaller(await insurance.getAddress(), true);
    console.log("OracleRegistry.authorize(MEVInsurance) done");

    // OracleRegistry -> authorize SlashingSystem
    await registry.setAuthorizedCaller(await slashingSystem.getAddress(), true);
    console.log("OracleRegistry.authorize(SlashingSystem) done");

    // SlashingSystem -> MEVInsurance (per inviare quota pool dopo slash)
    await slashingSystem.setMEVInsurance(await insurance.getAddress());
    console.log("SlashingSystem.setMEVInsurance done");

    // OracleRegistry -> set tActivation to 0 for local testing (no delay)
    await registry.setTActivation(0);
    console.log("OracleRegistry.setTActivation(0) done");

    // Whitelist contratti legittimi per BFS (evita falsi positivi nel network score)
    await insurance.addWhitelist([
        await amm.getAddress(),
        await token.getAddress(),
        await usdc.getAddress()
    ]);
    console.log("Whitelist AMM/Token/USDC aggiunta per BFS");

    // ══════════════════════════════════════════════════════════
    //  Fund contracts
    // ══════════════════════════════════════════════════════════
    console.log("\n── Funding ──");

    await token.transfer(await insurance.getAddress(), e(deployParams.pool_mevi));
    console.log(`MEVInsurance funded with ${deployParams.pool_mevi.toLocaleString()} MEVI`);

    await token.approve(await amm.getAddress(), e(deployParams.amm_mevi));
    await usdc.approve(await amm.getAddress(), e(deployParams.amm_usdc));
    await amm.addLiquidity(e(deployParams.amm_mevi), e(deployParams.amm_usdc));
    console.log(`MockAMM: ${deployParams.amm_mevi.toLocaleString()} MEVI + ${deployParams.amm_usdc.toLocaleString()} USDC liquidity added`);

    // Large standing approval so rebalance() can pull tokens from deployer
    const rebalanceAllowance = Math.max(deployParams.amm_mevi, deployParams.amm_usdc) * 100;
    await token.approve(await amm.getAddress(), e(rebalanceAllowance));
    await usdc.approve(await amm.getAddress(), e(rebalanceAllowance));
    console.log(`MockAMM: deployer approved ${rebalanceAllowance.toLocaleString()} MEVI + USDC for rebalancer`);

    await deployer.sendTransaction({
        to: await insurance.getAddress(),
        value: e(deployParams.pool_eth)
    });
    console.log(`MEVInsurance funded with ${deployParams.pool_eth.toLocaleString()} ETH for oracle rewards`);

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
    };

    const configDir = path.join(__dirname, "..", "config");
    if (!fs.existsSync(configDir)) fs.mkdirSync(configDir, { recursive: true });
    const configPath = path.join(configDir, "deployed_addresses.json");
    fs.writeFileSync(configPath, JSON.stringify(addresses, null, 2));

    const blockIntervalMs = parseInt(process.env.BLOCK_INTERVAL_MS || "3000");
    console.log(`\n🐢 Ripristino interval mining a ${blockIntervalMs}ms...`);
    await hre.network.provider.send("evm_setAutomine", [false]);
    await hre.network.provider.send("evm_setIntervalMining", [blockIntervalMs]);

    console.log("✅ Sistema pronto per i test!");

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
