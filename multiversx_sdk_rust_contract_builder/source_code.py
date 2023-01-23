
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from multiversx_sdk_rust_contract_builder.constants import (
    CONTRACT_CONFIG_FILENAME, OLD_CONTRACT_CONFIG_FILENAME)
from multiversx_sdk_rust_contract_builder.errors import ErrKnown


def is_source_code_file(path: Path) -> bool:
    if path.suffix == ".rs":
        return True
    if path.parent.name == "meta" and path.name == "Cargo.lock":
        return False
    if path.name in ["Cargo.toml", "Cargo.lock", CONTRACT_CONFIG_FILENAME, OLD_CONTRACT_CONFIG_FILENAME]:
        return True
    return False


def get_local_dependencies(contract_folder: Path, contract_name: str) -> List[Path]:
    args = ["cargo", "metadata", "--format-version=1"]
    logging.info(f"get_local_dependencies(), running: {args} in folder {contract_folder}")
    metadata_json = subprocess.check_output(args, cwd=contract_folder, shell=False, universal_newlines=True)
    metadata = json.loads(metadata_json)

    paths = _get_local_dependencies_recursively(metadata, contract_name, [], 0)

    # Remove duplicates
    paths = list(set(paths))
    return paths


def _get_local_dependencies_recursively(cargo_metadata: Dict[str, Any], package_name: str, visited: List[str], indentation: int) -> List[Path]:
    if package_name in visited or _is_mock_package(package_name):
        return []

    logging.debug(f"{indentation * 4 * ' '} _get_local_dependencies_recursively({package_name})")

    visited.append(package_name)

    packages = cargo_metadata.get("packages", [])
    package = next((package for package in packages if package["name"] == package_name), None)
    if not package:
        print(json.dumps(cargo_metadata))
        raise ErrKnown(f"Could not find package {package_name} in project metadata.")

    dependencies = package.get("dependencies", [])
    local_dependencies = [dependency for dependency in dependencies if _is_local_dependency(dependency)]
    paths = [Path(dependency["path"]) for dependency in local_dependencies]

    for dependency in local_dependencies:
        paths += _get_local_dependencies_recursively(cargo_metadata, dependency["name"], visited, indentation + 1)

    return paths


def _is_mock_package(package_name: str) -> bool:
    return "mock" in package_name


def _is_local_dependency(dependency: Dict[str, Any]) -> bool:
    return "path" in dependency
