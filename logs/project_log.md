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
   - 8 unit tests passing (deployment, transfers, allowances)
   - Deployment script verified working

3. **MEVInsurance Contract (base)**
   - Created MEVInsurance.sol with registerUser(), buyPolicy(), submitClaim()
   - Premium: 100 MEVI, Coverage: 1000 MEVI, Duration: 30 days
   - 15 unit tests passing (registration, policy purchase, claims, approvals)

4. **Phase 1: Data Structures and Types (DataTypes.sol)**
   - Created contracts/libraries/DataTypes.sol with all shared types
   - Enums: Tier, CoverageLevel, ClaimStatus (6 states), OracleStatus (7 states)
   - Structs: UserProfile, OracleInfo, Claim, Policy
   - All numeric percentages in basis points (10000 = 100%)
   - 12 unit tests passing

5. **Phase 2: OracleRegistry.sol**
   - Full oracle lifecycle: register -> activate -> withdraw
   - Logarithmic stake scaling: baseStake * log2(1 + activeOracleCount)
   - Activation delay (7 days), withdrawal cooldown (30 days)
   - Deviation tracking & automatic watchlisting (after 2 deviations)
   - Periodic deviation score reset (every 30 days)
   - Pseudo-random oracle selection (Fisher-Yates), excludes watchlisted + withdrawing
   - Reward system with 50% penalty for watchlisted oracles
   - Slashing, expulsion, and reintegration support
   - All protocol parameters configurable by owner
   - 46 unit tests passing

## Current Architecture

```
contracts/
  MEVToken.sol                  - ERC20 token (MEVI, 1M supply)
  MEVInsurance.sol              - Insurance contract (base version)
  OracleRegistry.sol            - Oracle management (register, activate, select, slash)
  libraries/
    DataTypes.sol               - Shared enums and structs
  test/
    DataTypesConsumer.sol       - Test helper for DataTypes
scripts/
  deploy_token.js               - Deploy MEVToken to network
  trader.py                     - Trader simulator (placeholder)
  mev_bot.py                    - MEV bot simulator (placeholder)
  oracle.py                     - Oracle service (placeholder)
test/
  MEVToken.test.js              - Token unit tests (8 tests)
  MEVInsurance.test.js          - Insurance unit tests (15 tests)
  DataTypes.test.js             - DataTypes unit tests (12 tests)
  OracleRegistry.test.js        - Oracle registry tests (46 tests)
logs/
  project_log.md                - This file
```

## Tech Stack

- **Smart Contracts:** Solidity 0.8.20, OpenZeppelin ERC20 + Ownable + ReentrancyGuard
- **Compiler:** viaIR enabled, optimizer 200 runs
- **Framework:** Hardhat 2.28.6
- **Python:** web3.py, eth-account
- **Networks:** Hardhat local (chainId 31337), Sepolia (planned)

## Last Changes

- Created contracts/OracleRegistry.sol with full oracle lifecycle
- Created test/OracleRegistry.test.js with 46 tests
- Total: 81/81 tests passing

## Next Tasks (Phases)

1. ~~Phase 1: Data structures and types~~ DONE
2. ~~Phase 2: OracleRegistry.sol~~ DONE
3. Phase 3: ClaimManager (refactor MEVInsurance.sol)
4. Phase 4: PremiumCalculator.sol
5. Phase 5: TierSystem.sol
6. Phase 6: SlashingSystem.sol
7. Phase 7: SandwichBot.sol + MockAMM.sol
8. Phase 8: End-to-end test
9. Phase 9: Patt update mechanism

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
