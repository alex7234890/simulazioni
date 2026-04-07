# MEV Insurance — Project Log

## Stack

| Component | Version |
|-----------|---------|
| Solidity  | 0.8.20  |
| Hardhat   | 2.28.6  |
| OpenZeppelin | 5.x |
| web3.py   | 6.x     |
| Python    | 3.10+   |

---

## Contracts (10 total)

| Contract | Description |
|----------|-------------|
| `MEVToken.sol` | ERC20 MEVI token (1M supply) |
| `MockUSDC.sol` | Mock USDC for AMM trading |
| `MockAMM.sol` | Minimal AMM (MEVI/USDC pool) |
| `SandwichBot.sol` | Executes frontrun/backrun on MockAMM |
| `OracleRegistry.sol` | Register, stake, activate/suspend oracles |
| `PremiumCalculator.sol` | Dynamic premium based on patt, tier, swap value |
| `MEVInsurance.sol` | Core policy/claim/payout logic |
| `DataTypes.sol` (lib) | Shared structs: Policy, Claim, etc. |
| `ClaimManager.sol` (lib) | Claim resolution logic |
| `MathUtils.sol` (lib) | Safe math helpers |

---

## Test Suite — 389 tests passing

| File | Tests | Coverage |
|------|-------|----------|
| `MEVToken.test.js` | 8 | Token basics |
| `MockAMM.test.js` | 12 | AMM swaps |
| `OracleRegistry.test.js` | 45 | Registration, staking, status |
| `PremiumCalculator.test.js` | 38 | Premium tiers, patt updates |
| `MEVInsurance.test.js` | 71 | Policy, swaps, claims, fraud scores |
| `ClaimManager.test.js` | 89 | Commit-reveal, finalize, CAPTCHA, C13 gas refund |
| `E2E.test.js` | 126 | Full lifecycle end-to-end |

---

## Protocol Parameters

| Parameter | Value |
|-----------|-------|
| θ_approve | 60 (fraud score ≤ 60 → auto approve) |
| θ_reject  | 80 (fraud score ≥ 80 → auto reject) |
| Dispersion threshold | 20 (triggers secondary review) |
| Gas refund amount | 0.01 MEVI per approved claim |
| Oracle stake minimum | 1000 MEVI |
| Policy duration | 30 days |
| Coverage levels | Bronze / Silver / Gold / Platinum |

### FraudScore Formula (PDF §1.3.11)

```
FraudScore = ScoreTier + ScoreClaimRate + ScoreNetwork + Variance

ScoreTier:      Bronze=50, Silver=30, Gold=15, Platinum=0
ScoreClaimRate: >30%→30, >20%→25, >10%→20, ≤10%→15, <6%→0
ScoreNetwork:   random(0, 15)
Variance:       random(-3, +3)
Range:          [0, 130]
```

---

## Corrections Applied

### C1–C10 (earlier phase)
Core contract logic: oracle commit-reveal pattern, tier promotion,
policy expiry, daily swap limits, premium calculation, blacklisting.

### C11 — selectOraclesExcluding
- **File:** `contracts/OracleRegistry.sol`
- Added `selectOraclesExcluding(seed, n, excludeList)` function
- Used in `finalizeClaim()` secondary review path to pick a *different*
  set of oracles from the first round (fixes "Already committed" error)

### C12 — getPremiumEstimate
- **File:** `contracts/MEVInsurance.sol`
- Added `getPremiumEstimate(uint256 swapValue, CoverageLevel level) external view returns (uint256)`
- Delegates to PremiumCalculator without requiring user address
- Used by `STIMA` command in user_cli.py

### C13 — Gas Refund on Claim Approval
- **Files:** `contracts/libraries/DataTypes.sol`, `contracts/MEVInsurance.sol`,
  `test/ClaimManager.test.js`, `test/E2E.test.js`
- Added `submitGasUsed` field to `Claim` struct
- Added `gasRefundAmount = 0.01 ether` state variable + `setGasRefundAmount()` setter
- Added `GasRefundIssued` event
- `submitClaim()`: measures gas via `gasleft()` before/after
- `finalizeClaim()` / `resolveCAPTCHA()`: approved payout = `payout + gasRefundAmount`
- All affected test assertions updated with `+ ethers.parseEther("0.01")`
- 8 new tests added in ClaimManager.test.js

---

## Simulation Scripts

### Architecture

```
scripts/
  deploy_all.js       Deploys all 10 contracts, saves config/deployed_addresses.json
  utils.py            Shared helpers (web3, logging, send_tx, advance_day, fraud score)
  setup_chain.py      One-time init: register oracles, fund bot/user, save actors.json
  user_cli.py         Interactive CLI — YOU are the trader
  bot_daemon.py       Background bot polling for sandwich attacks
  status.py           Live dashboard (pool, profile, oracles, recent claims)
  advance_day.py      Advance blockchain time by N days
```

### Workflow

```bash
# 1. Start Hardhat node
npx hardhat node

# 2. Deploy contracts
npx hardhat run scripts/deploy_all.js --network localhost

# 3. Initialize chain state (once)
python scripts/setup_chain.py

# 4. Optional: run background bot
python scripts/bot_daemon.py --rate 0.25 &

# 5. Play as trader
python scripts/user_cli.py
```

### CLI Commands (user_cli.py)

| Command | Description |
|---------|-------------|
| `SWAP <amount>` | Execute insured swap, pay premium |
| `CLAIM <swap_id> <loss>` | Submit claim; all 7 oracles vote automatically |
| `STIMA <amount>` | Estimate premium before swapping |
| `PROFILO` | Show your tier, balance, claims history |
| `POOL` | Show pool balance and global stats |
| `GIORNO [N]` | Advance blockchain by N days (default 1) |
| `RINNOVA` | Buy/renew insurance policy |
| `HELP` | List commands |
| `ESCI` | Exit |

### Account Allocation (Hardhat)

| Index | Role |
|-------|------|
| 0 | Deployer / owner |
| 1 | User (trader — you) |
| 6–12 | Oracles (7 total) |
| 19 | MEV Bot |

---

## Config Files

| File | Description |
|------|-------------|
| `config/deployed_addresses.json` | Contract addresses after deploy |
| `config/actors.json` | Oracle/bot/user addresses (written by setup_chain.py) |
| `logs/simulation.log` | Plain-text dual log (also printed to stdout with ANSI color) |
