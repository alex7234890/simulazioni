const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("PattUpdater", function () {
  let registry, calculator, pattUpdater;
  let owner, user1;
  let o1, o2, o3, o4, o5, o6, o7, o8;

  const e = (n) => ethers.parseEther(String(n));
  const BASE_STAKE = e("0.1");
  const T_ACTIVATION = 7 * 24 * 60 * 60;
  const COMMIT_TIMEOUT = 2 * 24 * 60 * 60;
  const REVEAL_TIMEOUT = 2 * 24 * 60 * 60;

  function computeCommitHash(pattEstimate, datasetHash, salt) {
    return ethers.solidityPackedKeccak256(
      ["uint16", "bytes32", "bytes32"],
      [pattEstimate, datasetHash, salt]
    );
  }

  const allOracles = () => [o1, o2, o3, o4, o5, o6, o7, o8];

  async function findSigner(address) {
    return allOracles().find(s => s.address === address);
  }

  beforeEach(async function () {
    [owner, user1, o1, o2, o3, o4, o5, o6, o7, o8] = await ethers.getSigners();

    // Deploy OracleRegistry
    const OracleRegistry = await ethers.getContractFactory("OracleRegistry");
    registry = await OracleRegistry.deploy();
    await registry.waitForDeployment();

    // Deploy PremiumCalculator
    const PremiumCalculator = await ethers.getContractFactory("PremiumCalculator");
    calculator = await PremiumCalculator.deploy();
    await calculator.waitForDeployment();

    // Deploy PattUpdater
    const PattUpdater = await ethers.getContractFactory("PattUpdater");
    pattUpdater = await PattUpdater.deploy(
      await registry.getAddress(),
      await calculator.getAddress()
    );
    await pattUpdater.waitForDeployment();

    // Authorize PattUpdater to call setPatt on calculator
    await calculator.setAuthorizedUpdater(await pattUpdater.getAddress());

    // Register and activate 8 oracles
    for (const o of allOracles()) {
      const stake = await registry.getMinimumStake();
      await registry.connect(o).registerOracle({ value: stake });
    }
    await time.increase(T_ACTIVATION + 1);
    for (const o of allOracles()) {
      await registry.connect(o).activateOracle();
    }
  });

  describe("Round Management", function () {
    it("should start a new round and select oracles", async function () {
      await pattUpdater.startRound();
      expect(await pattUpdater.getRoundsCount()).to.equal(1);

      const oracles = await pattUpdater.getRoundOracles(0);
      expect(oracles.length).to.equal(7); // nPattOracles default
    });

    it("should emit RoundStarted event", async function () {
      await expect(pattUpdater.startRound())
        .to.emit(pattUpdater, "RoundStarted");
    });

    it("only owner can start round", async function () {
      await expect(
        pattUpdater.connect(user1).startRound()
      ).to.be.revertedWithCustomError(pattUpdater, "OwnableUnauthorizedAccount");
    });
  });

  describe("Commit-Reveal", function () {
    let roundId, assignedOracles;

    beforeEach(async function () {
      await pattUpdater.startRound();
      roundId = 0;
      assignedOracles = await pattUpdater.getRoundOracles(roundId);
    });

    it("should allow assigned oracle to commit", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks_100_200"));
      const hash = computeCommitHash(500, datasetHash, salt);

      await expect(pattUpdater.connect(signer).commitPattEstimate(roundId, hash))
        .to.emit(pattUpdater, "PattCommitted")
        .withArgs(roundId, signer.address);
    });

    it("should revert commit from non-assigned oracle", async function () {
      // Find an oracle NOT in assignedOracles
      let nonAssigned;
      for (const o of allOracles()) {
        if (!assignedOracles.includes(o.address)) {
          nonAssigned = o;
          break;
        }
      }
      if (nonAssigned) {
        const hash = ethers.keccak256(ethers.toUtf8Bytes("dummy"));
        await expect(
          pattUpdater.connect(nonAssigned).commitPattEstimate(roundId, hash)
        ).to.be.revertedWith("Not assigned oracle");
      }
    });

    it("should revert double commit", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const hash = ethers.keccak256(ethers.toUtf8Bytes("dummy"));
      await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      await expect(
        pattUpdater.connect(signer).commitPattEstimate(roundId, hash)
      ).to.be.revertedWith("Already committed");
    });

    it("should revert commit after timeout", async function () {
      await time.increase(COMMIT_TIMEOUT + 1);
      const signer = await findSigner(assignedOracles[0]);
      const hash = ethers.keccak256(ethers.toUtf8Bytes("dummy"));
      await expect(
        pattUpdater.connect(signer).commitPattEstimate(roundId, hash)
      ).to.be.revertedWith("Commit timeout");
    });

    it("should allow reveal after commit", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks_100_200"));
      const pattEstimate = 500; // 5%
      const hash = computeCommitHash(pattEstimate, datasetHash, salt);

      await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      await expect(
        pattUpdater.connect(signer).revealPattEstimate(roundId, pattEstimate, datasetHash, salt)
      ).to.emit(pattUpdater, "PattRevealed")
        .withArgs(roundId, signer.address, pattEstimate, datasetHash);
    });

    it("should revert reveal with wrong hash", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks_100_200"));
      const hash = computeCommitHash(500, datasetHash, salt);

      await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      await expect(
        pattUpdater.connect(signer).revealPattEstimate(roundId, 600, datasetHash, salt)
      ).to.be.revertedWith("Hash mismatch");
    });

    it("should revert reveal without commit", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks"));
      await expect(
        pattUpdater.connect(signer).revealPattEstimate(roundId, 500, datasetHash, salt)
      ).to.be.revertedWith("Not committed");
    });

    it("should revert patt estimate > 10000", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks"));
      const hash = computeCommitHash(10001, datasetHash, salt);

      await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      await expect(
        pattUpdater.connect(signer).revealPattEstimate(roundId, 10001, datasetHash, salt)
      ).to.be.revertedWith("Patt must be 0-10000 bps");
    });

    it("should store dataset hash on reveal", async function () {
      const signer = await findSigner(assignedOracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("blocks_100_200"));
      const hash = computeCommitHash(500, datasetHash, salt);

      await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      await pattUpdater.connect(signer).revealPattEstimate(roundId, 500, datasetHash, salt);

      expect(await pattUpdater.datasetHashes(roundId, signer.address)).to.equal(datasetHash);
    });
  });

  describe("Finalization", function () {
    let roundId, assignedOracles;

    async function commitAndRevealAll(estimates) {
      for (let i = 0; i < assignedOracles.length; i++) {
        const signer = await findSigner(assignedOracles[i]);
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`salt_${i}`));
        const datasetHash = ethers.keccak256(ethers.toUtf8Bytes(`dataset_${i}`));
        const hash = computeCommitHash(estimates[i], datasetHash, salt);
        await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
      }
      for (let i = 0; i < assignedOracles.length; i++) {
        const signer = await findSigner(assignedOracles[i]);
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`salt_${i}`));
        const datasetHash = ethers.keccak256(ethers.toUtf8Bytes(`dataset_${i}`));
        await pattUpdater.connect(signer).revealPattEstimate(roundId, estimates[i], datasetHash, salt);
      }
    }

    beforeEach(async function () {
      await pattUpdater.startRound();
      roundId = 0;
      assignedOracles = await pattUpdater.getRoundOracles(roundId);
    });

    it("should finalize with ms=20% when volume < 1000 (default)", async function () {
      // All oracles estimate 500 bps (5%)
      const estimates = [500, 500, 500, 500, 500, 500, 500];
      await commitAndRevealAll(estimates);
      await pattUpdater.finalizePattUpdate(roundId);

      // finalPatt = 500 * (10000 + 2000) / 10000 = 500 * 1.2 = 600
      const round = await pattUpdater.rounds(roundId);
      expect(round.finalPatt).to.equal(600);
      expect(round.finalized).to.be.true;

      // PremiumCalculator should be updated
      expect(await calculator.patt()).to.equal(600);
    });

    it("should finalize with ms=10% when 1000 <= volume < 10000", async function () {
      await pattUpdater.updateVolumeSwaps(5000);
      const estimates = [500, 500, 500, 500, 500, 500, 500];
      await commitAndRevealAll(estimates);
      await pattUpdater.finalizePattUpdate(roundId);

      // finalPatt = 500 * (10000 + 1000) / 10000 = 500 * 1.1 = 550
      const round = await pattUpdater.rounds(roundId);
      expect(round.finalPatt).to.equal(550);
    });

    it("should finalize with ms=5% when volume >= 10000", async function () {
      await pattUpdater.updateVolumeSwaps(15000);
      const estimates = [500, 500, 500, 500, 500, 500, 500];
      await commitAndRevealAll(estimates);
      await pattUpdater.finalizePattUpdate(roundId);

      // finalPatt = 500 * (10000 + 500) / 10000 = 500 * 1.05 = 525
      const round = await pattUpdater.rounds(roundId);
      expect(round.finalPatt).to.equal(525);
    });

    it("should compute median correctly with varied estimates", async function () {
      // Estimates: [300, 400, 500, 600, 700, 800, 900] -> median = 600
      const estimates = [700, 300, 900, 500, 400, 800, 600];
      await commitAndRevealAll(estimates);

      // Default volume = 0 -> ms = 2000 (20%)
      await pattUpdater.finalizePattUpdate(roundId);

      // finalPatt = 600 * (10000 + 2000) / 10000 = 720
      const round = await pattUpdater.rounds(roundId);
      expect(round.finalPatt).to.equal(720);
    });

    it("should emit PattFinalized event", async function () {
      const estimates = [500, 500, 500, 500, 500, 500, 500];
      await commitAndRevealAll(estimates);

      await expect(pattUpdater.finalizePattUpdate(roundId))
        .to.emit(pattUpdater, "PattFinalized")
        .withArgs(roundId, 500, 2000, 600);
    });

    it("should revert if finalized twice", async function () {
      const estimates = [500, 500, 500, 500, 500, 500, 500];
      await commitAndRevealAll(estimates);
      await pattUpdater.finalizePattUpdate(roundId);

      await expect(
        pattUpdater.finalizePattUpdate(roundId)
      ).to.be.revertedWith("Already finalized");
    });

    it("should finalize after timeout with partial reveals", async function () {
      // Only 4 out of 7 oracles reveal
      for (let i = 0; i < 4; i++) {
        const signer = await findSigner(assignedOracles[i]);
        const salt = ethers.keccak256(ethers.toUtf8Bytes(`salt_${i}`));
        const datasetHash = ethers.keccak256(ethers.toUtf8Bytes(`dataset_${i}`));
        const hash = computeCommitHash(500, datasetHash, salt);
        await pattUpdater.connect(signer).commitPattEstimate(roundId, hash);
        await pattUpdater.connect(signer).revealPattEstimate(roundId, 500, datasetHash, salt);
      }

      // Can't finalize yet
      await expect(
        pattUpdater.finalizePattUpdate(roundId)
      ).to.be.revertedWith("Reveal phase not complete");

      // After timeout
      await time.increase(COMMIT_TIMEOUT + REVEAL_TIMEOUT + 1);
      await pattUpdater.finalizePattUpdate(roundId);

      const round = await pattUpdater.rounds(roundId);
      expect(round.finalized).to.be.true;
    });

    it("should revert with zero reveals after timeout", async function () {
      await time.increase(COMMIT_TIMEOUT + REVEAL_TIMEOUT + 1);
      await expect(
        pattUpdater.finalizePattUpdate(roundId)
      ).to.be.revertedWith("No reveals");
    });
  });

  describe("Safety Margin", function () {
    it("should return ms=2000 for volume 0", async function () {
      expect(await pattUpdater.getSafetyMargin()).to.equal(2000);
    });

    it("should return ms=2000 for volume 999", async function () {
      await pattUpdater.updateVolumeSwaps(999);
      expect(await pattUpdater.getSafetyMargin()).to.equal(2000);
    });

    it("should return ms=1000 for volume 1000", async function () {
      await pattUpdater.updateVolumeSwaps(1000);
      expect(await pattUpdater.getSafetyMargin()).to.equal(1000);
    });

    it("should return ms=1000 for volume 9999", async function () {
      await pattUpdater.updateVolumeSwaps(9999);
      expect(await pattUpdater.getSafetyMargin()).to.equal(1000);
    });

    it("should return ms=500 for volume 10000", async function () {
      await pattUpdater.updateVolumeSwaps(10000);
      expect(await pattUpdater.getSafetyMargin()).to.equal(500);
    });

    it("should return ms=500 for volume 50000", async function () {
      await pattUpdater.updateVolumeSwaps(50000);
      expect(await pattUpdater.getSafetyMargin()).to.equal(500);
    });
  });

  describe("Volume Updates", function () {
    it("should update volume", async function () {
      await pattUpdater.updateVolumeSwaps(12345);
      expect(await pattUpdater.volumeSwaps()).to.equal(12345);
    });

    it("should emit VolumeSwapsUpdated", async function () {
      await expect(pattUpdater.updateVolumeSwaps(5000))
        .to.emit(pattUpdater, "VolumeSwapsUpdated")
        .withArgs(5000);
    });

    it("only owner can update volume", async function () {
      await expect(
        pattUpdater.connect(user1).updateVolumeSwaps(100)
      ).to.be.revertedWithCustomError(pattUpdater, "OwnableUnauthorizedAccount");
    });
  });

  describe("Parameter Setters", function () {
    it("should update nPattOracles", async function () {
      await pattUpdater.setNPattOracles(5);
      expect(await pattUpdater.nPattOracles()).to.equal(5);
    });

    it("should update commit timeout", async function () {
      await pattUpdater.setCommitTimeout(86400);
      expect(await pattUpdater.commitTimeout()).to.equal(86400);
    });

    it("should update reveal timeout", async function () {
      await pattUpdater.setRevealTimeout(86400);
      expect(await pattUpdater.revealTimeout()).to.equal(86400);
    });

    it("should update safety margin values", async function () {
      await pattUpdater.setMsHigh(300);
      await pattUpdater.setMsMedium(800);
      await pattUpdater.setMsLow(1500);
      expect(await pattUpdater.msHigh()).to.equal(300);
      expect(await pattUpdater.msMedium()).to.equal(800);
      expect(await pattUpdater.msLow()).to.equal(1500);
    });

    it("should update volume thresholds", async function () {
      await pattUpdater.setVolumeHighThreshold(20000);
      await pattUpdater.setVolumeLowThreshold(2000);
      expect(await pattUpdater.volumeHighThreshold()).to.equal(20000);
      expect(await pattUpdater.volumeLowThreshold()).to.equal(2000);
    });

    it("non-owner cannot set parameters", async function () {
      await expect(
        pattUpdater.connect(user1).setNPattOracles(3)
      ).to.be.revertedWithCustomError(pattUpdater, "OwnableUnauthorizedAccount");
    });
  });

  describe("View Functions", function () {
    it("should return round count", async function () {
      expect(await pattUpdater.getRoundsCount()).to.equal(0);
      await pattUpdater.startRound();
      expect(await pattUpdater.getRoundsCount()).to.equal(1);
    });

    it("should return round estimates after reveals", async function () {
      await pattUpdater.startRound();
      const oracles = await pattUpdater.getRoundOracles(0);
      const signer = await findSigner(oracles[0]);
      const salt = ethers.keccak256(ethers.toUtf8Bytes("salt"));
      const datasetHash = ethers.keccak256(ethers.toUtf8Bytes("data"));
      const hash = computeCommitHash(500, datasetHash, salt);

      await pattUpdater.connect(signer).commitPattEstimate(0, hash);
      await pattUpdater.connect(signer).revealPattEstimate(0, 500, datasetHash, salt);

      const estimates = await pattUpdater.getRoundEstimates(0);
      expect(estimates.length).to.equal(1);
      expect(estimates[0]).to.equal(500);
    });
  });
});
