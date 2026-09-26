// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {MockUSDC3009} from "../contracts/MockUSDC3009.sol";

interface Vm {
    function addr(uint256 privateKey) external returns (address);
    function prank(address sender) external;
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function warp(uint256 timestamp) external;
}

contract MockUSDC3009Test {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function testMintBurnAndTransfer() external {
        MockUSDC3009 token = new MockUSDC3009();
        address alice = vm.addr(1);
        address bob = vm.addr(2);

        token.mint(alice, 10_000_000);
        _eq(token.totalSupply(), 10_000_000, "mint supply");
        _eq(token.balanceOf(alice), 10_000_000, "mint balance");

        vm.prank(alice);
        token.approve(address(this), 1);
        token.transferFrom(alice, bob, 1);
        _eq(token.balanceOf(bob), 1, "transferFrom balance");
    }

    function testOpenMintCallerBurn() external {
        MockUSDC3009 token = new MockUSDC3009();
        token.mint(address(this), 10_000_000);
        token.burn(2_000_000);
        _eq(token.totalSupply(), 8_000_000, "burn supply");
        _eq(token.balanceOf(address(this)), 8_000_000, "burn balance");
    }

    function testTransferWithAuthorizationAndReplayProtection() external {
        MockUSDC3009 token = new MockUSDC3009();
        address signer = vm.addr(1);
        address recipient = vm.addr(2);
        bytes32 nonce = keccak256("demo-nonce");
        uint256 validAfter = 1_000;
        uint256 validBefore = 2_000;
        uint256 value = 5_000_000;

        token.mint(signer, value);
        vm.warp(validAfter + 1);
        bytes32 structHash = keccak256(
            abi.encode(
                token.TRANSFER_WITH_AUTHORIZATION_TYPEHASH(), signer, recipient, value, validAfter, validBefore, nonce
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(1, digest);

        token.transferWithAuthorization(signer, recipient, value, validAfter, validBefore, nonce, v, r, s);
        _eq(token.balanceOf(recipient), value, "authorized transfer");
        _true(token.authorizationState(signer, nonce), "nonce consumed");

        try token.transferWithAuthorization(signer, recipient, value, validAfter, validBefore, nonce, v, r, s) {
            revert("replay unexpectedly succeeded");
        } catch {}
    }

    function _eq(uint256 actual, uint256 expected, string memory message) private pure {
        require(actual == expected, message);
    }

    function _true(bool value, string memory message) private pure {
        require(value, message);
    }
}
