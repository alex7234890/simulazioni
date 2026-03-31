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

6. **Phase 3: ClaimManager (MEVInsurance.sol refactor)**
   - Complete rewrite of MEVInsurance.sol with commit-reveal oracle evaluation
   - Constructor now takes token + OracleRegistry addresses
   - User registration with Bronze tier default, registered users mapping
   - Policy purchase with CoverageLevel (Low/Medium/High) selection
   - submitClaim(): 3 tx hashes + swapValue + loss, auto-selects 7 oracles via OracleRegistry
   - Daily swap limits per tier (Bronze/Silver: 3, Gold: 4, Platinum: unlimited)
   - commitVerdict(): oracle submits keccak256(fraudScore, patternValid, salt) hash
   - revealVerdict(): oracle reveals score + pattern validity, verified against commit
   - finalizeClaim(): full decision logic:
     - Pattern invalidity check (>=70% invalid votes -> immediate rejection)
     - Median fraud score calculation (insertion sort)
     - Dispersione (max-min) tracking
     - Tier-based thresholds: Bronze/Silver (CAPTCHA or blacklist), Gold/Platinum (approve, CAPTCHA, or blacklist)
   - resolveCAPTCHA(): owner resolves CAPTCHA-required claims
   - Coverage payouts: Low=70%, Medium=90%, High=100% of claimed loss
   - Blacklist system with 20% penalty debt on rejected claims
   - Running average fraud score per user
   - Oracle timeout (3 days) allows finalization with partial reveals
   - All parameters configurable by owner
   - 56 new tests (ClaimManager.test.js) + 9 updated legacy tests
   - Total: 133/133 tests passing

7. **Phase 4: PremiumCalculator.sol**
   - Premium formula from PDF section 1.4.6:
     P = max(V * [(Patt * L%) + (Tint * E/(1-E))/Vbase + Coracle24h/Vbase] * (1+M) * Fcov, Pmin * V)
   - Three-component base rate: attack probability, fraud cost, oracle cost
   - Solvency ratio with adaptive margin:
     - SR >= 1.5x: mAdj = 0
     - 1.3x <= SR < 1.5x: mAdj = 5% (deltaMmed)
     - SR < 1.3x: mAdj = 10% (deltaMhigh)
   - Coverage factors: Low=70%, Medium=90%, High=100%
   - Minimum premium floor: 1.5% of swap value
   - Market data updates (tint, vbase, coracle24h) by owner
   - All parameters configurable (patt, lPercent, eFNR, mBase, pmin, fcov, SR thresholds)
   - Integrated with MEVInsurance.sol: buyPolicy uses calculator when set
   - 48 new tests (PremiumCalculator.test.js)
   - Total: 181/181 tests passing

8. **Phase 5: TierSystem.sol**
   - Tier upgrade requirements from PDF Table 8:
     - Bronze -> Silver: 18 swaps, 30 days membership, avg fraud score < 52
     - Silver -> Gold: 55 swaps, 60 days membership, avg fraud score < 35
   - checkAndUpgrade(): automatic tier promotion when requirements met
   - Platinum upgrade: requestPlatinum() with 20% stake + off-chain CAPTCHA verification
   - Full Platinum flow: stake deposit -> owner verifyCaptcha -> finalizePlatinum
   - Blacklist system: blacklistUser() adds 20% penalty debt on loss
   - payDebt(): partial/full debt payment, auto un-blacklist when debt = 0
   - Data sync from MEVInsurance (owner-triggered)
   - View helpers: canUpgradeToSilver(), canUpgradeToGold(), getMaxDailySwaps()
   - All parameters configurable by owner
   - Integrated reference in MEVInsurance.sol
   - 59 new tests (TierSystem.test.js) including full lifecycle test
   - Total: 240/240 tests passing

9. **Phase 6: SlashingSystem.sol**
   - Report submission: reporter deposits Creport = 0.014 ETH, jury of 7 selected
   - Jury selection excludes accused oracle (requests nJury+1, filters accused)
   - Commit-reveal jury voting: slash percentage 0-100%
   - Finalization based on median vote:
     - Median > 0: slash = stake * median / 100
       - Jury reward deducted from slash
       - Residual: 75% to pool, 25% to reporter
       - Reporter deposit refunded
       - Median > 50% (thetaExpulsion): oracle expelled permanently
       - Median <= 50%: oracle slashed, can reintegrate
     - Median = 0: reporter deposit confiscated, distributed as jury reward
   - Pool management: accumulated slashing funds, owner withdrawable
   - Commit/reveal timeouts (2 days each), partial reveal finalization
   - All parameters configurable by owner
   - 46 new tests (SlashingSystem.test.js)
   - Total: 286/286 tests passing

## Current Architecture

```
contracts/
  MEVToken.sol                  - ERC20 token (MEVI, 1M supply)
  MEVInsurance.sol              - Insurance contract (Phase 3: full ClaimManager)
  OracleRegistry.sol            - Oracle management (register, activate, select, slash)
  PremiumCalculator.sol          - Dynamic premium calculation (PDF formula)
  TierSystem.sol                 - User tier management (Bronze->Silver->Gold->Platinum)
  SlashingSystem.sol             - Oracle slashing disputes with jury commit-reveal
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
  MEVInsurance.test.js          - Insurance legacy tests (9 tests)
  ClaimManager.test.js          - ClaimManager full tests (56 tests)
  DataTypes.test.js             - DataTypes unit tests (12 tests)
  OracleRegistry.test.js        - Oracle registry tests (46 tests)
  PremiumCalculator.test.js      - Premium calculator tests (48 tests)
  TierSystem.test.js             - Tier system tests (59 tests)
  SlashingSystem.test.js         - Slashing system tests (46 tests)
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

- Created contracts/SlashingSystem.sol with full jury-based slashing
- Created test/SlashingSystem.test.js with 46 tests
- Total: 286/286 tests passing

## Next Tasks (Phases)

1. ~~Phase 1: Data structures and types~~ DONE
2. ~~Phase 2: OracleRegistry.sol~~ DONE
3. ~~Phase 3: ClaimManager (refactor MEVInsurance.sol)~~ DONE
4. ~~Phase 4: PremiumCalculator.sol~~ DONE
5. ~~Phase 5: TierSystem.sol~~ DONE
6. ~~Phase 6: SlashingSystem.sol~~ DONE
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
