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

10. **Phase 7: SandwichBot.sol + MockAMM.sol + MockUSDC.sol**
   - SandwichBot: executeFrontrun/executeBackrun with profit tracking per attack
   - MockAMM: constant product AMM (x*y=k) with addLiquidity, swap, getPrice, getAmountOut
   - MockUSDC: simple ERC20 mock as quote token
   - 29 new tests (SandwichBot.test.js)
   - Total: 315/315 tests passing

11. **Phase 8: End-to-End Integration Test**
   - Full lifecycle: sandwich attack → claim → oracle commit-reveal → CAPTCHA → payout
   - Tier progression: Bronze → Silver → Gold → Platinum with requirements
   - Oracle slashing: report → jury commit-reveal → expulsion (median > 50%)
   - Premium calculator integration: dynamic premium on buyPolicy
   - Claim rejection: high fraud → blacklist, invalid pattern detection
   - CAPTCHA flow: Bronze users always get CAPTCHARequired → owner resolves
   - Multi-user concurrent claims with different outcomes
   - Oracle timeout handling with partial reveals
   - Complete protocol smoke test (all contracts wired)
   - 17 new tests (E2E.test.js)
   - Total: 332/332 tests passing

12. **Phase 9: PattUpdater.sol (Patt Update Mechanism)**
   - Oracle-driven periodic update of Patt (probability of attack) in PremiumCalculator
   - startPattUpdate(): selects Noracle_patt=5 oracles, starts commit phase
   - Commit-reveal: oracles estimate attack ratio in basis points (0-10000)
   - finalizePattUpdate(): median + safety margin ms (200 bps), writes to PremiumCalculator
   - Rclaim (0.002 ETH) reward per oracle that revealed
   - Update interval enforced (default 1 day)
   - Support for partial reveals after timeout
   - Cap at 10000 bps (100%)
   - Sequential rounds supported
   - PremiumCalculator ownership transferred to PattUpdater
   - 46 new tests (PattUpdater.test.js)
   - Total: 378/378 tests passing

## Current Architecture

```
contracts/
  MEVToken.sol                  - ERC20 token (MEVI, 1M supply)
  MEVInsurance.sol              - Insurance contract (Phase 3: full ClaimManager)
  OracleRegistry.sol            - Oracle management (register, activate, select, slash)
  PremiumCalculator.sol          - Dynamic premium calculation (PDF formula)
  TierSystem.sol                 - User tier management (Bronze->Silver->Gold->Platinum)
  SlashingSystem.sol             - Oracle slashing disputes with jury commit-reveal
  SandwichBot.sol               - MEV sandwich attack simulator (frontrun/backrun)
  MockAMM.sol                   - Constant product AMM (x*y=k) for simulation
  MockUSDC.sol                  - ERC20 mock quote token
  PattUpdater.sol               - Periodic Patt update via oracle consensus
  libraries/
    DataTypes.sol               - Shared enums and structs
  test/
    DataTypesConsumer.sol       - Test helper for DataTypes
scripts/
  deploy_token.js               - Deploy MEVToken to network
  deploy_all.js                 - Full deployment + wiring + funding (all 10 contracts)
  utils.py                      - Shared Python utilities (web3, commit-reveal, helpers)
  simulation.py                 - Sequential orchestrator (20-cycle end-to-end demo)
  oracle.py                     - Standalone oracle node simulator
  trader.py                     - Standalone trader simulator
  mev_bot.py                    - MEV bot simulator (sandwich + direct)
config/
  deployed_addresses.json       - Auto-generated contract addresses from deploy_all.js
test/
  MEVToken.test.js              - Token unit tests (8 tests)
  MEVInsurance.test.js          - Insurance legacy tests (9 tests)
  ClaimManager.test.js          - ClaimManager full tests (56 tests)
  DataTypes.test.js             - DataTypes unit tests (12 tests)
  OracleRegistry.test.js        - Oracle registry tests (46 tests)
  PremiumCalculator.test.js      - Premium calculator tests (48 tests)
  TierSystem.test.js             - Tier system tests (59 tests)
  SlashingSystem.test.js         - Slashing system tests (46 tests)
  SandwichBot.test.js            - Sandwich attack + AMM tests (29 tests)
  E2E.test.js                    - End-to-end integration tests (17 tests)
  PattUpdater.test.js            - Patt update mechanism tests (46 tests)
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

- **Merged Corrections 2-6** from phase-3-import-fix branch
- **Correction 7: Secondary review for high dispersion**
  - finalizeClaim() now triggers secondary review when dispersione > threshold (20) on first evaluation
  - Claim is reset with fresh oracles for a second round of commit-reveal
  - Added `secondaryReview` flag to Claim struct, `SecondaryReviewTriggered` event
- **Correction 11: Twatchlist = 90 days minimum observation**
  - Watchlisted oracles must stay on watchlist for tWatchlist (90 days) before resetDeviationScore can restore them
  - Active (non-watchlisted) oracles only need tReset (30 days) for score reset
  - Added tWatchlist parameter + setTWatchlist() setter
- **Correction 12: Dataset commitment in PattUpdater** — already implemented in merged branch
  - commitPattEstimate uses keccak256(pattEstimate, datasetHash, salt)
  - revealPattEstimate accepts datasetHash, stores in datasetHashes mapping
- **Correction 9: MEV Bot Blacklist**
  - submitClaim() now accepts botAddress parameter
  - Tracks per-bot attackCount and totalDamage on approved claims
  - Auto-blacklists bot when attackCount >= botBlacklistThreshold (default 3)
  - Added botAttackCount, botTotalDamage, botBlacklisted mappings
  - Added BotBlacklisted event
- **Correction 10: Oracle Inactivity Penalty**
  - finalizeClaim() now penalizes oracles that didn't reveal (after timeout)
  - New penalizeInactivity() in OracleRegistry: deducts stake without changing status
  - Configurable inactivityPenalty (default 0.001 ETH)
  - Added OracleInactivityPenalized event
  - Added setUserTier() owner function for testing tier-dependent logic
- **Correction 8: Cumulative deviation score + watchlistStrikes**
  - recordDeviation() now accepts `uint256 _absoluteDeviation` parameter
  - deviationScore accumulates sum of absolute deviations (not just a counter)
  - Added separate `watchlistStrikes` counter: incremented only when deviation >= deltaWatchlist
  - Watchlist triggered by watchlistStrikes >= kWatchlist (not deviationScore)
  - resetDeviationScore() now also resets watchlistStrikes
  - Updated getOracleInfo() to return watchlistStrikes
- Total: 375/375 tests passing
- **Correction 13: Gas refund for approved claims + getPremiumEstimate**
  - submitClaim() now measures gas used (submitGasUsed field in Claim struct)
  - Configurable gasRefundAmount (default 0.01 MEVI) added to payout on claim approval
  - Gas refund applied in both finalizeClaim() and resolveCAPTCHA()
  - Added GasRefundIssued event, setGasRefundAmount() owner setter
  - Added getPremiumEstimate(swapValue, coverageLevel) view function for premium preview before insuredSwap()
  - 8 new tests (gas refund + premium estimate)
- Total: 389/389 tests passing

ALL 13 CORRECTIONS COMPLETE.

14. **CRITICO 1: RANDAO Randomness**
   - Oracle selection seed already uses `block.prevrandao` (RANDAO beacon post-merge)
   - Added clarifying comments: safe for simulation, production may use Chainlink VRF
   - No code change needed, only documentation

15. **CRITICO 2: Real Sandwich Pattern Verification**
   - Added `getClaimDetails()` view function to MEVInsurance.sol
     - Returns: user, txHash1-3, swapValue, loss, botAddress, secondaryReview
     - Allows oracles to fetch full claim data for off-chain verification
   - Rewrote `oracle.py` with real on-chain sandwich verification:
     - Fetches all 3 tx via web3 `get_transaction()`
     - 5-point verification: tx exist, same bot sender, same block, correct order, same pool
     - Heuristic fallback for synthetic tx (simulation): bot address, loss ratio, attack history
   - 389/389 tests still passing

16. **Operational Scripts (Deploy + Python Simulation Suite)**
   - **scripts/deploy_all.js** (174 lines): Full deployment pipeline
     - Deploys all 10 contracts in correct dependency order
     - Wiring: insurance↔calculator, insurance↔tierSystem, calculator↔pattUpdater, registry↔insurance, registry↔slashingSystem
     - Sets tActivation=0 for local testing (no oracle activation delay)
     - Funding: 500k MEVI to insurance pool, 100k+100k AMM liquidity, 10 ETH to registry, 5 ETH to slashingSystem
     - Saves all addresses to config/deployed_addresses.json
   - **scripts/utils.py** (100 lines): Shared Python utilities
     - Web3 connection, ABI loading, contract instantiation
     - Transaction helper: send_tx() with gas/nonce management
     - Commit-reveal: keccak256_commit() and keccak256_patt_commit() replicating Solidity encodePacked
     - Helpers: generate_salt(), to_wei(), from_wei(), increase_time(), log()
   - **scripts/simulation.py** (417 lines): Sequential orchestrator
     - 20-cycle end-to-end simulation: insuredSwap → submitClaim → oracle commit-reveal → finalize → handle outcome
     - Setup: funds trader/bot, registers 7 oracles, registers trader, buys High coverage policy
     - Oracle fraud analysis: tier-based scoring, claim rate analysis, per-oracle variance
     - Handles secondary review (re-does oracle round with converging scores)
     - Handles CAPTCHA resolution (80% auto-approve)
     - Comprehensive stats tracking + final report
   - **scripts/oracle.py** (~290 lines): Standalone oracle node simulator
     - OracleNode class: register, analyze claims, commit-reveal cycle
     - **Real sandwich pattern verification** (PDF §3.2):
       - Fetches txHash1/2/3 via getClaimDetails() getter
       - Verifies tx existence on-chain
       - Checks frontrun+backrun same sender (bot)
       - Checks same block, correct ordering (frontrun < victim < backrun)
       - Checks same pool/contract
       - Falls back to heuristic (bot address, loss ratio, history) for synthetic tx in simulation
     - Fraud analysis: tier adjustment, claim rate scoring, loss amount analysis, per-oracle variance
     - Poll mode: polls for new claims on interval, processes assigned ones
     - CLI args: --oracle-idx (0-6), --cycles, --interval
   - **scripts/trader.py** (193 lines): Standalone trader simulator
     - Trader class: setup, execute insured swaps, detect MEV, submit claims
     - Configurable claim rate, random swap values (50-500 MEVI)
     - Sandwich detection simulation (35% chance)
     - Session report with balance, profile, tier info
     - CLI args: --swaps, --claim-rate
   - **scripts/mev_bot.py** (213 lines): MEV bot simulator
     - MEVBot class: sandwich attacks via SandwichBot contract, direct AMM swaps
     - Funds bot account + SandwichBot contract with MEVI/USDC
     - Tracks attack success/failure, profit, blacklist status
     - Mixed mode: 60% sandwich, 40% direct
     - CLI args: --attacks, --mode (sandwich/direct/mixed)

## Next Tasks (Phases)

1. ~~Phase 1: Data structures and types~~ DONE
2. ~~Phase 2: OracleRegistry.sol~~ DONE
3. ~~Phase 3: ClaimManager (refactor MEVInsurance.sol)~~ DONE
4. ~~Phase 4: PremiumCalculator.sol~~ DONE
5. ~~Phase 5: TierSystem.sol~~ DONE
6. ~~Phase 6: SlashingSystem.sol~~ DONE
7. ~~Phase 7: SandwichBot.sol + MockAMM.sol~~ DONE
8. ~~Phase 8: End-to-end test~~ DONE
9. ~~Phase 9: Patt update mechanism~~ DONE

ALL 9 PHASES COMPLETE.

## Corrections (PDF Alignment)

1. ~~Correzione 1: Premium per-swap~~ DONE
2. ~~Correzione 2: Coverage percentages (Low=50%, Medium=70%, High=100%)~~ DONE
3. ~~Correzione 3: Separare Fcov (premium) da coverage% (rimborso)~~ DONE
4. ~~Correzione 4: FraudScore range 0-130, θapprove=60, θreject=80~~ DONE
5. ~~Correzione 5: Reward oracle in finalizeClaim~~ DONE
6. ~~Correzione 6: Margine ms variabile nel PattUpdater~~ DONE
7. ~~Correzione 7: Revisione secondaria per alta dispersione~~ DONE
8. ~~Correzione 8: Score scostamento come somma cumulativa~~ DONE
9. ~~Correzione 9: Blacklist bot MEV~~ DONE
10. ~~Correzione 10: Penalità inattività oracle~~ DONE
11. ~~Correzione 11: Twatchlist periodo minimo osservazione~~ DONE
12. ~~Correzione 12: Dataset commitment nel PattUpdater~~ DONE (merged branch)
13. ~~Correzione 13: Rimborso gas per claim approvati~~ DONE

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

### Deploy All Contracts (local node)
```bash
# Terminal 1: Start local node
npx hardhat node

# Terminal 2: Deploy all contracts
npx hardhat run scripts/deploy_all.js --network localhost
```

### Run Full Simulation
```bash
# Terminal 1: npx hardhat node
# Terminal 2: npx hardhat run scripts/deploy_all.js --network localhost
# Terminal 3:
python scripts/simulation.py          # 20-cycle orchestrated demo
```

### Run Individual Simulators
```bash
python scripts/trader.py --swaps 20 --claim-rate 0.6
python scripts/oracle.py --oracle-idx 0 --cycles 50 --interval 2
python scripts/mev_bot.py --attacks 10 --mode mixed
```
