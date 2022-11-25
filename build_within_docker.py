import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from argparse import ArgumentParser
from hashlib import blake2b
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, Union
from zipfile import ZIP_DEFLATED, ZipFile

logger = logging.getLogger("build-within-docker")


HARDCODED_BUILD_DIRECTORY = Path("/tmp/contract")
HARDCODED_UNWRAP_DIRECTORY = Path("/tmp/unwrapped")
ONE_KB_IN_BYTES = 1024
MAX_SOURCE_CODE_ARCHIVE_SIZE = ONE_KB_IN_BYTES * 1024
# The output archive contains not only the *.wasm, but also *.wat, *.abi.json files etc.
MAX_OUTPUT_ARTIFACTS_ARCHIVE_SIZE = ONE_KB_IN_BYTES * 1024


class BuildArtifactsAccumulator:
    def __init__(self):
        self.contracts: Dict[str, Dict[str, str]] = dict()

    def gather_artifacts(self, contract_name: str, output_subdirectory: Path):
        with open(find_file_in_folder(output_subdirectory, "*.codehash.txt")) as file:
            code_hash = file.read()

        self.add_artifact(contract_name, "bytecode", find_file_in_folder(output_subdirectory, "*.wasm").name)
        self.add_artifact(contract_name, "text", find_file_in_folder(output_subdirectory, "*.wat").name)
        self.add_artifact(contract_name, "abi", find_file_in_folder(output_subdirectory, "*.abi.json").name)
        self.add_artifact(contract_name, "imports", find_file_in_folder(output_subdirectory, "*.imports.json").name)
        self.add_artifact(contract_name, "codehash", code_hash)
        self.add_artifact(contract_name, "srcPackage", find_file_in_folder(output_subdirectory, "*.source.json").name)
        self.add_artifact(contract_name, "srcArchive", find_file_in_folder(output_subdirectory, "*-src-*.zip").name)
        self.add_artifact(contract_name, "output", find_file_in_folder(output_subdirectory, "*-output-*.zip").name)

    def add_artifact(self, contract_name: str, kind: str, value: str):
        if contract_name not in self.contracts:
            self.contracts[contract_name] = dict()

        self.contracts[contract_name][kind] = value

    def dump_to_file(self, file: Path):
        with open(file, "w") as f:
            json.dump(self.contracts, f, indent=4)


class PackagedProjectEntry:
    def __init__(self, path: Path, content: bytes) -> None:
        self.path = path
        self.content = content

    @classmethod
    def from_dict(cls, dict: Dict[str, Any]) -> 'PackagedProjectEntry':
        path = Path(dict.get("path", ""))
        content = base64.b64decode(dict.get("content", ""))
        return PackagedProjectEntry(path, content)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "path": str(self.path),
            "content": base64.b64encode(self.content).decode()
        }

        return data


class PackagedProject:
    def __init__(self, name: str, version: str, entries: List[PackagedProjectEntry]) -> None:
        self.name = name
        self.version = version
        self.entries = entries

    @classmethod
    def from_file(cls, path: Path) -> 'PackagedProject':
        with open(path, "r") as f:
            data: Dict[str, Any] = json.load(f)

        return PackagedProject.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PackagedProject':
        name = data.get("name", "untitled")
        version = data.get("version", "0.0.0")
        entries_raw: List[Dict[str, Any]] = data.get("entries", [])
        entries = [PackagedProjectEntry.from_dict(entry) for entry in entries_raw]
        return PackagedProject(name, version, entries)

    @classmethod
    def from_folder(cls, folder: Path) -> 'PackagedProject':
        entries = cls._create_entries_from_folder(folder)
        name, version = get_contract_name_and_version(folder)
        return PackagedProject(name, version, entries)

    @classmethod
    def _create_entries_from_folder(cls, folder: Path) -> List[PackagedProjectEntry]:
        files = get_files_recursively(folder, is_source_code_file)
        entries: List[PackagedProjectEntry] = []

        for full_path in files:
            with open(full_path, "rb") as f:
                content = f.read()

            relative_path = full_path.relative_to(folder)
            entries.append(PackagedProjectEntry(relative_path, content))

        return entries

    def unwrap_to_folder(self, folder: Path):
        for entry in self.entries:
            full_path = folder / entry.path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(entry.content)

    def save_to_file(self, path: Path):
        data = self.to_dict()

        with open(path, "w") as f:
            json.dump(data, f, indent=4)

    def to_dict(self) -> Dict[str, Any]:
        entries = [entry.to_dict() for entry in self.entries]

        return {
            "name": self.name,
            "version": self.version,
            "entries": entries
        }


def main(cli_args: List[str]):
    logging.basicConfig(level=logging.DEBUG)

    start_time = time.time()

    artifacts_accumulator = BuildArtifactsAccumulator()

    parser = ArgumentParser()
    parser.add_argument("--project", type=str, required=False, help="source code directory")
    parser.add_argument("--packaged-project", type=str, required=False, help="source code packaged in a JSON file")
    parser.add_argument("--contract", type=str, required=False, help="contract to build from within the source code directory; should be relative to the project path")
    parser.add_argument("--output", type=str, required=True)
    parser.add_argument("--no-wasm-opt", action="store_true", default=False, help="do not optimize wasm files after the build (default: %(default)s)")
    parser.add_argument("--cargo-target-dir", type=str, required=True, help="Cargo's target-dir")

    parsed_args = parser.parse_args(cli_args)
    project_path = Path(parsed_args.project).expanduser() if parsed_args.project else None
    packaged_project_path = Path(parsed_args.packaged_project).expanduser() if parsed_args.packaged_project else None
    parent_output_directory = Path(parsed_args.output)
    cargo_target_dir = parsed_args.cargo_target_dir
    no_wasm_opt = parsed_args.no_wasm_opt

    if not project_path:
        if not packaged_project_path:
            raise ErrKnown("One of the following must be provided: --project, --packaged-project")

        # We have to unwrap a packaged project (JSON)
        project_path = HARDCODED_UNWRAP_DIRECTORY
        packaged = PackagedProject.from_file(packaged_project_path)
        packaged.unwrap_to_folder(HARDCODED_UNWRAP_DIRECTORY)

    contracts_directories = get_contracts_directories(project_path)

    # We copy the whole project folder to the build path, to ensure that all local dependencies are available.
    project_within_build_directory = copy_project_directory_to_build_directory(project_path)

    for contract_directory in sorted(contracts_directories):
        contract_name, contract_version = get_contract_name_and_version(contract_directory)
        logger.info(f"Contract = {contract_name}, version = {contract_version}")

        output_subdirectory = parent_output_directory / f"{contract_name}"
        output_subdirectory.mkdir(parents=True, exist_ok=True)

        relative_contract_directory = contract_directory.relative_to(project_path)
        build_directory = project_within_build_directory / relative_contract_directory

        if parsed_args.contract and contract_name != parsed_args.contract:
            logger.info(f"Skipping {contract_name}.")
            continue

        # Clean directory - useful if it contains externally-generated build artifacts
        clean(build_directory)
        build(build_directory, output_subdirectory, cargo_target_dir, no_wasm_opt)

        # The archive will also include the "output" folder (useful for debugging)
        clean(build_directory, clean_output=False)

        promote_cargo_lock_to_contract_directory(build_directory, contract_directory)

        # The archives are created after build, so that Cargo.lock files are included, as well (useful for debugging)
        create_archives(contract_name, contract_version, build_directory, output_subdirectory)
        create_packaged_project(contract_name, contract_version, build_directory, output_subdirectory)

        artifacts_accumulator.gather_artifacts(contract_name, output_subdirectory)

    artifacts_accumulator.dump_to_file(parent_output_directory / "artifacts.json")

    end_time = time.time()
    time_elapsed = end_time - start_time
    logger.info(f"Built in {time_elapsed} seconds, as user = {os.getuid()}, group = {os.getgid()}")


def get_contracts_directories(project_path: Path) -> List[Path]:
    directories = [elrond_json.parent for elrond_json in project_path.glob("**/elrond.json")]
    return sorted(directories)


def get_contract_name_and_version(contract_directory: Path) -> Tuple[str, str]:
    # For simplicity and less dependencies installed in the Docker image, we do not rely on an external library
    # to parse the metadata from Cargo.toml.
    with open(contract_directory / "Cargo.toml") as file:
        lines = file.readlines()

    line_with_name = next((line for line in lines if line.startswith("name = ")), 'name = "untitled"')
    line_with_version = next((line for line in lines if line.startswith("version = ")), 'version = "0.0.0"')

    name = line_with_name.split("=")[1].strip().strip('"')
    version = line_with_version.split("=")[1].strip().strip('"')
    return name, version


def copy_project_directory_to_build_directory(project_directory: Path):
    shutil.rmtree(HARDCODED_BUILD_DIRECTORY, ignore_errors=True)
    HARDCODED_BUILD_DIRECTORY.mkdir()
    shutil.copytree(project_directory, HARDCODED_BUILD_DIRECTORY, dirs_exist_ok=True)
    return HARDCODED_BUILD_DIRECTORY


def clean(directory: Path, clean_output: bool = True):
    logger.info(f"Cleaning: {directory}")

    # On a best-effort basis, remove directories that (usually) hold build artifacts
    shutil.rmtree(directory / "wasm" / "target", ignore_errors=True)
    shutil.rmtree(directory / "meta" / "target", ignore_errors=True)

    if clean_output:
        shutil.rmtree(directory / "output", ignore_errors=True)


def build(build_directory: Path, output_directory: Path, cargo_target_dir: Path, no_wasm_opt: bool):
    cargo_output_directory = build_directory / "output"
    meta_directory = build_directory / "meta"
    cargo_lock = build_directory / "wasm" / "Cargo.lock"

    args = ["cargo", "run", "build"]
    args.extend(["--target-dir", str(cargo_target_dir)])
    args.extend(["--no-wasm-opt"] if no_wasm_opt else [])
    # If the lock file is missing, or it needs to be updated, Cargo will exit with an error.
    # See: https://doc.rust-lang.org/cargo/commands/cargo-build.html
    args.extend(["--locked"] if cargo_lock.exists() else [])

    logger.info(f"Building: {args}")
    return_code = subprocess.run(args, cwd=meta_directory).returncode
    if return_code != 0:
        exit(return_code)

    wasm_file = find_file_in_folder(cargo_output_directory, "*.wasm")
    generate_wabt_artifacts(wasm_file)
    generate_code_hash_artifact(wasm_file)

    shutil.copytree(cargo_output_directory, output_directory, dirs_exist_ok=True)


def promote_cargo_lock_to_contract_directory(build_directory: Path, contract_directory: Path):
    from_path = build_directory / "wasm" / "Cargo.lock"
    to_path = contract_directory / "wasm" / "Cargo.lock"
    shutil.copy(from_path, to_path)


def generate_wabt_artifacts(wasm_file: Path):
    wat_file = wasm_file.with_suffix(".wat")
    imports_file = wasm_file.with_suffix(".imports.json")

    logger.info(f"Convert WASM to WAT: {wasm_file}")
    subprocess.check_output(["wasm2wat", str(wasm_file), "-o", str(wat_file)], shell=False, universal_newlines=True, stderr=subprocess.STDOUT)

    logger.info(f"Extract imports: {wasm_file}")
    imports_text = subprocess.check_output(["wasm-objdump", str(wasm_file), "--details", "--section", "Import"], shell=False, universal_newlines=True, stderr=subprocess.STDOUT)

    imports = _parse_imports_text(imports_text)

    with open(imports_file, "w") as f:
        json.dump(imports, f, indent=4)


def generate_code_hash_artifact(wasm_file: Path):
    code_hash = compute_code_hash(wasm_file)
    with open(wasm_file.with_suffix(".codehash.txt"), "w") as f:
        f.write(code_hash)
    logger.info(f"Code hash of {wasm_file}: {code_hash}")


def _parse_imports_text(text: str) -> List[str]:
    lines = [line for line in text.splitlines() if "func" in line and "env" in line]
    imports = [line.split(".")[-1] for line in lines]
    return imports


def compute_code_hash(wasm_file: Path):
    with open(wasm_file, "rb") as bytecode_file:
        code = bytecode_file.read()

    h = blake2b(digest_size=32)
    h.update(code)
    return h.hexdigest()


def find_file_in_folder(folder: Path, pattern: str) -> Path:
    files = list(folder.rglob(pattern))

    if len(files) == 0:
        raise Exception(f"No file matches pattern [{pattern}] in folder {folder}")
    if len(files) > 1:
        logger.warning(f"More files match pattern [{pattern}] in folder {folder}. Will pick first:\n{files}")

    file = folder / files[0]
    return Path(file).resolve()


def create_archives(contract_name: str, contract_version: str, input_directory: Path, output_directory: Path):
    source_code_archive_file = output_directory / f"{contract_name}-src-{contract_version}.zip"
    output_artifacts_archive_file = output_directory / f"{contract_name}-output-{contract_version}.zip"

    archive_directory(source_code_archive_file, input_directory, is_source_code_file)
    archive_directory(output_artifacts_archive_file, input_directory / "output")

    size_of_source_code_archive = source_code_archive_file.stat().st_size
    size_of_output_artifacts_archive = output_artifacts_archive_file.stat().st_size

    if size_of_source_code_archive > MAX_SOURCE_CODE_ARCHIVE_SIZE:
        warn_file_too_large(source_code_archive_file, size_of_source_code_archive, MAX_SOURCE_CODE_ARCHIVE_SIZE)
    if size_of_output_artifacts_archive > MAX_OUTPUT_ARTIFACTS_ARCHIVE_SIZE:
        warn_file_too_large(output_artifacts_archive_file, size_of_output_artifacts_archive, MAX_OUTPUT_ARTIFACTS_ARCHIVE_SIZE)


def warn_file_too_large(path: Path, size: int, max_size: int):
    logger.warning(f"""File is too large (this might cause issues with using downstream applications, such as the contract build verification services): 
file = {path}, size = {size}, maximum size = {max_size}""")


def archive_directory(archive_file: Path, directory: Path, should_include_file: Union[Callable[[Path], bool], None] = None):
    files = get_files_recursively(directory, should_include_file)

    with ZipFile(archive_file, "w", ZIP_DEFLATED) as archive:
        for full_path in files:
            archive.write(full_path, full_path.relative_to(directory))

    logger.info(f"Created archive: file = {archive_file}, with size = {archive_file.stat().st_size} bytes")


def get_files_recursively(directory: Path, should_include_file: Union[Callable[[Path], bool], None] = None):
    should_include_file = should_include_file or (lambda _: True)
    paths: List[Path] = []

    for root, _, files in os.walk(directory):
        root_path = Path(root)
        for file in files:
            file_path = Path(file)
            full_path = root_path / file_path

            if file_path.is_dir():
                continue
            if not should_include_file(file_path):
                continue

            paths.append(full_path)

    return paths


def is_source_code_file(path: Path):
    if path.suffix == ".rs":
        return True
    if path.name in ["Cargo.toml", "Cargo.lock", "elrond.json"]:
        return True
    return False


def create_packaged_project(contract_name: str, contract_version: str, input_directory: Path, output_directory: Path):
    package = PackagedProject.from_folder(input_directory)
    package_path = output_directory / f"{contract_name}-{contract_version}.source.json"
    package.save_to_file(package_path)


class ErrKnown(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ErrKnown as err:
        print("An error occurred.")
        print(err)
