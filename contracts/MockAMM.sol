// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/**
 * @title MockAMM
 * @dev Simplified AMM with constant product formula (x * y = k).
 *
 * Simulates a Uniswap-like liquidity pool for two ERC20 tokens.
 * Used for testing sandwich attack scenarios:
 *   - Frontrunner buys tokenOut before victim, raising price
 *   - Victim buys at inflated price
 *   - Backrunner sells tokenOut after victim, profiting from slippage
 *
 * No LP tokens, no fees — pure x*y=k for simulation purposes.
 */
contract MockAMM {
    IERC20 public tokenA;
    IERC20 public tokenB;

    uint256 public reserveA;
    uint256 public reserveB;

    event LiquidityAdded(address indexed provider, uint256 amountA, uint256 amountB);
    event Swap(
        address indexed sender,
        address tokenIn,
        uint256 amountIn,
        address tokenOut,
        uint256 amountOut
    );

    constructor(address _tokenA, address _tokenB) {
        tokenA = IERC20(_tokenA);
        tokenB = IERC20(_tokenB);
    }

    /**
     * @dev Add liquidity to the pool. No LP token minting — just deposits.
     * @param _amountA Amount of tokenA to add
     * @param _amountB Amount of tokenB to add
     */
    function addLiquidity(uint256 _amountA, uint256 _amountB) external {
        require(_amountA > 0 && _amountB > 0, "Amounts must be > 0");

        require(tokenA.transferFrom(msg.sender, address(this), _amountA), "Transfer A failed");
        require(tokenB.transferFrom(msg.sender, address(this), _amountB), "Transfer B failed");

        reserveA += _amountA;
        reserveB += _amountB;

        emit LiquidityAdded(msg.sender, _amountA, _amountB);
    }

    /**
     * @dev Swap tokenIn for tokenOut using constant product formula.
     * Calculates output as: amountOut = reserveOut - k / (reserveIn + amountIn)
     * @param _tokenIn Address of the input token (must be tokenA or tokenB)
     * @param _amountIn Amount of input token
     * @return amountOut Amount of output token received
     */
    function swap(address _tokenIn, uint256 _amountIn) external returns (uint256 amountOut) {
        require(_amountIn > 0, "Amount must be > 0");
        require(
            _tokenIn == address(tokenA) || _tokenIn == address(tokenB),
            "Invalid token"
        );

        bool isAtoB = _tokenIn == address(tokenA);
        uint256 reserveIn = isAtoB ? reserveA : reserveB;
        uint256 reserveOut = isAtoB ? reserveB : reserveA;

        require(reserveIn > 0 && reserveOut > 0, "No liquidity");

        // Constant product: k = reserveIn * reserveOut
        // newReserveIn = reserveIn + amountIn
        // newReserveOut = k / newReserveIn
        // amountOut = reserveOut - newReserveOut
        uint256 k = reserveIn * reserveOut;
        uint256 newReserveIn = reserveIn + _amountIn;
        uint256 newReserveOut = k / newReserveIn;
        amountOut = reserveOut - newReserveOut;

        require(amountOut > 0, "Insufficient output");
        require(amountOut < reserveOut, "Output exceeds reserve");

        // Transfer input token in
        IERC20 inToken = isAtoB ? tokenA : tokenB;
        require(inToken.transferFrom(msg.sender, address(this), _amountIn), "Transfer in failed");

        // Transfer output token out
        IERC20 outToken = isAtoB ? tokenB : tokenA;
        require(outToken.transfer(msg.sender, amountOut), "Transfer out failed");

        // Update reserves
        if (isAtoB) {
            reserveA = newReserveIn;
            reserveB = newReserveOut;
        } else {
            reserveB = newReserveIn;
            reserveA = newReserveOut;
        }

        emit Swap(msg.sender, _tokenIn, _amountIn, address(outToken), amountOut);
    }

    /**
     * @dev Get the current price of tokenA in terms of tokenB.
     * Price = reserveB / reserveA (how many B per 1 A)
     */
    function getPrice() external view returns (uint256) {
        require(reserveA > 0, "No liquidity");
        return (reserveB * 1e18) / reserveA;
    }

    /**
     * @dev Get expected output for a swap without executing it.
     * @param _tokenIn Input token address
     * @param _amountIn Input amount
     * @return amountOut Expected output amount
     */
    function getAmountOut(address _tokenIn, uint256 _amountIn) external view returns (uint256 amountOut) {
        require(_tokenIn == address(tokenA) || _tokenIn == address(tokenB), "Invalid token");

        bool isAtoB = _tokenIn == address(tokenA);
        uint256 reserveIn = isAtoB ? reserveA : reserveB;
        uint256 reserveOut = isAtoB ? reserveB : reserveA;

        if (reserveIn == 0 || reserveOut == 0) return 0;

        uint256 k = reserveIn * reserveOut;
        uint256 newReserveIn = reserveIn + _amountIn;
        uint256 newReserveOut = k / newReserveIn;
        amountOut = reserveOut - newReserveOut;
    }
}
