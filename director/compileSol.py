import json
import os
from pathlib import Path

import solcx

HERE = Path(__file__).resolve().parent
SOL_PATH = HERE / "Voting.sol"
OUT_PATH = HERE / "Voting.json"


def main():
    solcx.install_solc("0.8.19")
    compiled = solcx.compile_source(
        SOL_PATH.read_text(),
        output_values=["abi", "bin"],
        solc_version="0.8.19",
    )
    # compile_source returns { 'submitted_filename:ContractName': { 'abi': ..., 'bin': ... } }
    contract_id, contract_interface = list(compiled.items())[0]
    artifact = {
        "contractName": "Voting",
        "abi": contract_interface["abi"],
        "bytecode": contract_interface["bin"],
    }
    OUT_PATH.write_text(json.dumps(artifact, indent=2))
    print(f"compiled {SOL_PATH.name} -> {OUT_PATH.name}")
    print(f"bytecode length: {len(artifact['bytecode'])} chars")
    print(f"abi entries: {len(artifact['abi'])}")


if __name__ == "__main__":
    main()