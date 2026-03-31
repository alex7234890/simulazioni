// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "./MockAMM.sol";

/**
 * @title SandwichBot
 * @dev Simulates a MEV sandwich attacker on a MockAMM pool.
 *
 * A sandwich attack works in three steps (same block):
 *   1. Frontrun: bot buys tokenOut before the victim, raising the price
 *   2. Victim swap: victim buys at the inflated price
 *   3. Backrun: bot sells tokenOut after the victim, profiting from slippage
 *
 * This contract exposes executeFrontrun() and executeBackrun() for testing.
 * In tests, the sequence frontrun -> victim swap -> backrun is executed
 * in the same block to simulate a real sandwich attack.
 */
contract SandwichBot is Ownable {

    /// @dev Track profits per attack
    struct AttackResult {
        uint256 frontrunAmountIn;
        uint256 frontrunAmountOut;
        uint256 backrunAmountIn;
        uint256 backrunAmountOut;
        int256 profit; // Can be negative if attack fails
    }

    /// @dev History of attacks
    AttackResult[] public attacks;

    event Frontrun(address indexed amm, address tokenIn, uint256 amountIn, uint256 amountOut);
    event Backrun(address indexed amm, address tokenIn, uint256 amountIn, uint256 amountOut);
    event AttackCompleted(uint256 indexed attackId, int256 profit);

    constructor() Ownable(msg.sender) {}

    /**
     * @dev Execute the frontrun step: buy tokenOut before the victim.
     * @param _amm Address of the MockAMM pool
     * @param _tokenIn Token to spend (the one victim will also spend)
     * @param _amount Amount to spend on frontrun
     * @return amountOut Amount of tokenOut received
     */
    function executeFrontrun(
        address _amm,
        address _tokenIn,
        uint256 _amount
    ) external onlyOwner returns (uint256 amountOut) {
        MockAMM amm = MockAMM(_amm);

        // Approve AMM to spend our tokens
        IERC20(_tokenIn).approve(_amm, _amount);

        // Execute swap
        amountOut = amm.swap(_tokenIn, _amount);

        // Record frontrun data
        attacks.push(AttackResult({
            frontrunAmountIn: _amount,
            frontrunAmountOut: amountOut,
            backrunAmountIn: 0,
            backrunAmountOut: 0,
            profit: 0
        }));

        emit Frontrun(_amm, _tokenIn, _amount, amountOut);
    }

    /**
     * @dev Execute the backrun step: sell the tokenOut acquired during frontrun.
     * @param _amm Address of the MockAMM pool
     * @param _tokenIn Token to sell (the tokenOut from frontrun)
     * @param _amount Amount to sell
     * @return amountOut Amount received back
     */
    function executeBackrun(
        address _amm,
        address _tokenIn,
        uint256 _amount
    ) external onlyOwner returns (uint256 amountOut) {
        require(attacks.length > 0, "No frontrun executed");

        MockAMM amm = MockAMM(_amm);

        // Approve AMM to spend our tokens
        IERC20(_tokenIn).approve(_amm, _amount);

        // Execute swap
        amountOut = amm.swap(_tokenIn, _amount);

        // Update attack record
        uint256 attackId = attacks.length - 1;
        AttackResult storage attack = attacks[attackId];
        attack.backrunAmountIn = _amount;
        attack.backrunAmountOut = amountOut;

        // Calculate profit: backrunOut - frontrunIn (in the same token)
        // Frontrun: spent frontrunAmountIn of tokenA, got frontrunAmountOut of tokenB
        // Backrun: spent backrunAmountIn of tokenB (= frontrunAmountOut), got backrunAmountOut of tokenA
        // Profit = backrunAmountOut - frontrunAmountIn
        attack.profit = int256(amountOut) - int256(attack.frontrunAmountIn);

        emit Backrun(_amm, _tokenIn, _amount, amountOut);
        emit AttackCompleted(attackId, attack.profit);
    }

    /**
     * @dev Get total number of attacks executed.
     */
    function getAttackCount() external view returns (uint256) {
        return attacks.length;
    }

    /**
     * @dev Get details of a specific attack.
     */
    function getAttack(uint256 _id) external view returns (
        uint256 frontrunAmountIn,
        uint256 frontrunAmountOut,
        uint256 backrunAmountIn,
        uint256 backrunAmountOut,
        int256 profit
    ) {
        AttackResult storage a = attacks[_id];
        return (a.frontrunAmountIn, a.frontrunAmountOut, a.backrunAmountIn, a.backrunAmountOut, a.profit);
    }

    /**
     * @dev Withdraw tokens from this contract (in case of leftover).
     */
    function withdrawToken(address _token, uint256 _amount) external onlyOwner {
        IERC20(_token).transfer(msg.sender, _amount);
    }

    /// @dev Allow receiving ETH
    receive() external payable {}
}
