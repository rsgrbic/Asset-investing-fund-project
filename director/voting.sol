// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Voting {
    address[] public voters;
    mapping(address => bool) public isVoter;
    mapping(address => bool) public hasVoted;

    uint256 public approveCount;
    uint256 public rejectCount;
    uint256 public quorum;

    bool public finalized;
    bool public approved;

    event VotedApprove(address indexed voter, uint256 total);
    event VotedReject(address indexed voter, uint256 total);
    event Finalized(bool approved);

    constructor(address[] memory _voters) {
        require(_voters.length > 0, "no voters");
        require(_voters.length % 2 == 1, "voters must be odd");
        for (uint256 i = 0; i < _voters.length; i++) {
            require(_voters[i] != address(0), "zero address");
            require(!isVoter[_voters[i]], "duplicate voter");
            isVoter[_voters[i]] = true;
            voters.push(_voters[i]);
        }
        quorum = _voters.length / 2 + 1;
    }

    modifier onlyVoter() {
        require(isVoter[msg.sender], "Invalid address.");
        _;
    }

    modifier onlyBeforeFinal() {
        require(!finalized, "Voting ended.");
        _;
    }

    function castApprove() external onlyVoter onlyBeforeFinal {
        require(!hasVoted[msg.sender], "already voted");
        hasVoted[msg.sender] = true;
        approveCount += 1;
        emit VotedApprove(msg.sender, approveCount);
        if (approveCount >= quorum) {
            finalized = true;
            approved = true;
            emit Finalized(true);
        }
    }

    function castReject() external onlyVoter onlyBeforeFinal {
        require(!hasVoted[msg.sender], "already voted");
        hasVoted[msg.sender] = true;
        rejectCount += 1;
        emit VotedReject(msg.sender, rejectCount);
        if (rejectCount >= quorum) {
            finalized = true;
            approved = false;
            emit Finalized(false);
        }
    }
}