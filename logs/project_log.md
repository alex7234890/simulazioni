# MEV Insurance Simulation - Project Log

## Completed Tasks

1. **Project Initialization**
   - Initialized Hardhat 2 project with Solidity 0.8.20
   - Installed OpenZeppelin contracts
   - Installed Python dependencies (web3, eth-account)
   - Created project directory structure: contracts/, scripts/, tests/, logs/, docs/, config/

2. **ERC20 Token (MEVToken.sol)**
   - Created MEV Insurance Token (MEVI) with 1,000,000 supply
   - Mints full supply to deployer
   - All 8 unit tests passing (deployment, transfers, allowances)
   - Deployment script verified working

3. **MEVInsurance Contract**
   - Created MEVInsurance.sol with registerUser(), buyPolicy(), submitClaim()
   - Premium: 100 MEVI, Coverage: 1000 MEVI, Duration: 30 days
   - 15 unit tests passing (registration, policy purchase, claims, approvals)

## Current Architecture

```
contracts/
  MEVToken.sol          - ERC20 token (MEVI, 1M supply)
  MEVInsurance.sol      - Insurance contract (register, buy policy, submit claims)
scripts/
  deploy_token.js       - Deploy MEVToken to network
  trader.py             - Trader simulator (placeholder)
  mev_bot.py            - MEV bot simulator (placeholder)
  oracle.py             - Oracle service (placeholder)
test/
  MEVToken.test.js      - Token unit tests (8 tests)
  MEVInsurance.test.js  - Insurance unit tests
logs/
  project_log.md        - This file
```

## Tech Stack

- **Smart Contracts:** Solidity 0.8.20, OpenZeppelin ERC20
- **Framework:** Hardhat 2.28.6
- **Python:** web3.py, eth-account
- **Networks:** Hardhat local (chainId 31337), Sepolia (planned)

## Last Changes

- Initialized project with Hardhat 2 + OpenZeppelin
- Created and tested MEVToken.sol (8/8 tests passing)
- Created MEVInsurance.sol with registerUser, buyPolicy, submitClaim
- Created placeholder Python scripts (trader, mev_bot, oracle)

## Next Tasks

1. Create MockAMM contract
2. Implement trader simulator (Python)
3. Implement MEV bot simulator (Python)
4. Implement oracle service (Python)
5. Local blockchain simulation with all components
6. Sepolia testnet deployment

## How to Run

### Prerequisites
- Node.js installed
- Python installed with web3, eth-account

### Compile Contracts
```bash
npx hardhat compile
```

### Run Tests
```bash
npx hardhat test
```

### Deploy Token (local)
```bash
npx hardhat run scripts/deploy_token.js
```

### Deploy to Local Node
```bash
# Terminal 1: Start local node
npx hardhat node

# Terminal 2: Deploy
npx hardhat run scripts/deploy_token.js --network localhost
```
