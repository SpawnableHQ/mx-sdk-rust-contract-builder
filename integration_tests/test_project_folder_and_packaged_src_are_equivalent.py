import sys
from typing import List
from integration_tests.config import PARENT_OUTPUT_FOLDER
from integration_tests.shared import download_project_repository, run_docker

from multiversx_sdk_rust_contract_builder.main import main


def main(cli_args: List[str]):
    project_path = download_project_repository("https://github.com/multiversx/mx-exchange-sc/archive/refs/heads/main.zip", "mx-exchange-sc-main")
    output_using_project = PARENT_OUTPUT_FOLDER / "using-project"
    output_using_packaged_src = PARENT_OUTPUT_FOLDER / "using-packaged-src"

    output_using_project.mkdir(parents=True, exist_ok=True)
    output_using_packaged_src.mkdir(parents=True, exist_ok=True)

    contracts = ['distribution', 'energy-factory', 'energy-update', 'factory', 'farm', 'farm-staking', 'farm-staking-proxy', 'farm-with-locked-rewards', 'fees-collector', 'governance', 'governance-v2', 'lkmex-transfer', 'locked-token-wrapper', 'metabonding-staking', 'pair', 'pause-all', 'price-discovery', 'proxy-deployer', 'proxy_dex', 'router', 'simple-lock', 'simple-lock-whitelist', 'token-unstake']
    contracts = ['farm-staking']

    for contract in contracts:
        run_docker(
            project_path=project_path,
            packaged_src_path=None,
            contract_name=contract,
            image="sdk-rust-contract-builder:next",
            output_folder=output_using_project
        )

        code_hash_using_project = (output_using_project / f"{contract}/{contract}.codehash.txt").read_text().strip()

        packaged_src_path = output_using_project / f"{contract}/{contract}-0.0.0.source.json"

        run_docker(
            project_path=None,
            packaged_src_path=packaged_src_path,
            contract_name=contract,
            image="sdk-rust-contract-builder:next",
            output_folder=output_using_packaged_src
        )

        code_hash_using_packaged_src = (output_using_packaged_src / f"{contract}/{contract}.codehash.txt").read_text().strip()

        assert code_hash_using_project == code_hash_using_packaged_src


if __name__ == "__main__":
    main(sys.argv[1:])
