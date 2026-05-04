// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract MockAMM {
    IERC20 public tokenA;
    IERC20 public tokenB;

    uint256 public reserveA;
    uint256 public reserveB;

    address public owner;
    uint256 public initReserveA;
    uint256 public initReserveB;
    bool private _initSet;

    event LiquidityAdded(address indexed provider, uint256 amountA, uint256 amountB);
    event Swap(
        address indexed sender,
        address tokenIn,
        uint256 amountIn,
        address tokenOut,
        uint256 amountOut
    );
    event Rebalanced(uint256 newReserveA, uint256 newReserveB);

    constructor(address _tokenA, address _tokenB) {
        tokenA = IERC20(_tokenA);
        tokenB = IERC20(_tokenB);
        owner = msg.sender;
    }

    function addLiquidity(uint256 _amountA, uint256 _amountB) external {
        require(_amountA > 0 && _amountB > 0, "Amounts must be > 0");

        require(tokenA.transferFrom(msg.sender, address(this), _amountA), "Transfer A failed");
        require(tokenB.transferFrom(msg.sender, address(this), _amountB), "Transfer B failed");

        reserveA += _amountA;
        reserveB += _amountB;

        if (!_initSet) {
            initReserveA = reserveA;
            initReserveB = reserveB;
            _initSet = true;
        }

        emit LiquidityAdded(msg.sender, _amountA, _amountB);
    }

    // Restore reserves to initial ratio; owner must have approved this contract for deficit pulls.
    // Simulates LP rebalancing between sandwich attacks.
    function rebalance() external {
        require(msg.sender == owner, "Not owner");
        require(_initSet, "Pool not initialized");

        if (reserveA > initReserveA) {
            tokenA.transfer(owner, reserveA - initReserveA);
        } else if (reserveA < initReserveA) {
            require(
                tokenA.transferFrom(owner, address(this), initReserveA - reserveA),
                "Rebalance: transferFrom A failed"
            );
        }

        if (reserveB > initReserveB) {
            tokenB.transfer(owner, reserveB - initReserveB);
        } else if (reserveB < initReserveB) {
            require(
                tokenB.transferFrom(owner, address(this), initReserveB - reserveB),
                "Rebalance: transferFrom B failed"
            );
        }

        reserveA = initReserveA;
        reserveB = initReserveB;

        emit Rebalanced(initReserveA, initReserveB);
    }

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

        // Constant product: newReserveOut = k / (reserveIn + amountIn)
        // amountOut = reserveOut - newReserveOut
        uint256 k = reserveIn * reserveOut;
        uint256 newReserveIn = reserveIn + _amountIn;
        uint256 newReserveOut = k / newReserveIn;
        amountOut = reserveOut - newReserveOut;

        require(amountOut > 0, "Insufficient output");
        require(amountOut < reserveOut, "Output exceeds reserve");

        IERC20 inToken = isAtoB ? tokenA : tokenB;
        require(inToken.transferFrom(msg.sender, address(this), _amountIn), "Transfer in failed");

        IERC20 outToken = isAtoB ? tokenB : tokenA;
        require(outToken.transfer(msg.sender, amountOut), "Transfer out failed");

        if (isAtoB) {
            reserveA = newReserveIn;
            reserveB = newReserveOut;
        } else {
            reserveB = newReserveIn;
            reserveA = newReserveOut;
        }

        emit Swap(msg.sender, _tokenIn, _amountIn, address(outToken), amountOut);
    }

    function getPrice() external view returns (uint256) {
        require(reserveA > 0, "No liquidity");
        return (reserveB * 1e18) / reserveA;
    }

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
