// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/**
 * @title MEVToken
 * @dev ERC20 token for the MEV Insurance simulation system.
 *
 * Token Details:
 *   - Name: MEV Insurance Token
 *   - Symbol: MEVI
 *   - Initial Supply: 1,000,000 tokens (minted to deployer)
 *
 * Used as the payment and settlement token across the insurance
 * contract, mock AMM, and trader/bot simulators.
 */
contract MEVToken is ERC20 {
    /**
     * @dev Mints the full initial supply to the deployer address.
     * Supply uses 18 decimals (ERC20 default).
     */
    constructor() ERC20("MEV Insurance Token", "MEVI") {
        _mint(msg.sender, 1_000_000 * 10 ** decimals());
    }
}
