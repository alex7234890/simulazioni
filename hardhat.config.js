require("@nomicfoundation/hardhat-toolbox");

// Legge l'intervallo di blocco da env var (ms). Default: 1500ms.
const BLOCK_INTERVAL_MS = parseInt(process.env.BLOCK_INTERVAL_MS || "1500");

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
    solidity: {
        version: "0.8.20",
        settings: {
            viaIR: true,
            optimizer: {
                enabled: true,
                runs: 200,
            },
        },
    },
    networks: {
        hardhat: {
            allowUnlimitedContractSize: true,
            chainId: 31337,
            accounts: {
                count: 25,
            },
            mining: {
                auto: false,
                interval: BLOCK_INTERVAL_MS
            }
        },
        localhost: {
            url: "http://127.0.0.1:8545",
        },
    },
};