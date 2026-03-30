const { ethers } = require("hardhat");

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log("Deploying MEVToken with account:", deployer.address);

  const MEVToken = await ethers.getContractFactory("MEVToken");
  const token = await MEVToken.deploy();
  await token.waitForDeployment();

  const address = await token.getAddress();
  console.log("MEVToken deployed to:", address);

  const balance = await token.balanceOf(deployer.address);
  console.log("Deployer balance:", ethers.formatEther(balance), "MEVI");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
