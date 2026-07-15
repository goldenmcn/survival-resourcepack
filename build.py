"""
Main build script for the survival-resourcepack.
Merges packs and runs PackSquash for optimization.
"""

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any, List

import templating

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build")

# --- Configuration ---
PROJECT_ROOT: Path = Path(__file__).parent.resolve()

BUILD_DIR: Path = PROJECT_ROOT / "build"
TEMP_WORKSPACE: Path = BUILD_DIR / "temp_workspace"

VENDOR_DIR: Path = PROJECT_ROOT / "vendor"
SRC_DIR: Path = PROJECT_ROOT / "packages"

TEMP_PACKSQUASH_CONFIG: Path = BUILD_DIR / "temp_packsquash.toml"
ORIGINAL_PACKSQUASH_CONFIG: Path = PROJECT_ROOT / "packsquash.toml"


def is_resource_pack(path: Path) -> bool:
    """
    Check if the given path (dir or zip) is a valid resource pack containing pack.mcmeta.

    Args:
        path: Path to check.

    Returns:
        True if the path contains a valid pack.mcmeta file, False otherwise.
    """
    if not path.exists():
        return False

    if path.is_dir():
        return (path / "pack.mcmeta").is_file()

    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                return "pack.mcmeta" in zf.namelist()
        except zipfile.BadZipFile:
            return False

    return False


def discover_sources() -> List[Path]:
    """
    Scan and return a list of resource packs in order of priority.

    Returns:
        A list of paths to resource packs, ordered by priority.
    """
    sources: List[Path] = []

    # 1. Scan vendor/ directory (lowest priority)
    vendor_packs: List[Path] = []
    if VENDOR_DIR.exists() and VENDOR_DIR.is_dir():
        for item in VENDOR_DIR.iterdir():
            if is_resource_pack(item):
                vendor_packs.append(item)
        vendor_packs.sort(key=lambda p: p.name.lower())
        sources.extend(vendor_packs)

    # 2. Scan packages/ directory (highest priority)
    src_packs: List[Path] = []
    if SRC_DIR.exists() and SRC_DIR.is_dir():
        if is_resource_pack(SRC_DIR):
            # The src directory itself is a single resource pack
            src_packs.append(SRC_DIR)
        else:
            # The src directory contains multiple resource pack subdirectories
            for item in SRC_DIR.iterdir():
                if item.is_dir() or is_resource_pack(item):
                    src_packs.append(item)
            src_packs.sort(key=lambda p: p.name.lower())
    sources.extend(src_packs)

    return sources


def merge_json_files(src: str, dst: str) -> bool:
    """
    Attempt to merge src JSON into dst JSON.

    Args:
        src: Source JSON file path.
        dst: Destination JSON file path.

    Returns:
        True if successful, False otherwise.
    """
    try:
        with open(dst, "r", encoding="utf-8") as f:
            dest_data: Any = json.load(f)
        with open(src, "r", encoding="utf-8") as f:
            src_data: Any = json.load(f)

        path_parts = Path(dst).parts

        # Merge fonts (providers array)
        if (
            "font" in path_parts
            and isinstance(dest_data, dict)
            and "providers" in dest_data
            and "providers" in src_data
        ):
            dest_data["providers"].extend(src_data["providers"])

        # Merge dicts (lang, sounds.json, etc.)
        elif isinstance(dest_data, dict) and isinstance(src_data, dict):
            dest_data.update(src_data)

        # Merge arrays (e.g. some tags)
        elif isinstance(dest_data, list) and isinstance(src_data, list):
            dest_data.extend(src_data)

        else:
            return False  # Overwrite normally

        with open(dst, "w", encoding="utf-8") as f:
            json.dump(dest_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.debug(f"Could not merge JSON {Path(dst).name}: {e}")
        return False


def smart_copy(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    """
    Custom copy function that intelligently merges JSON files instead of overwriting.

    Args:
        src: Source path.
        dst: Destination path.
        follow_symlinks: Whether to follow symlinks.

    Returns:
        The destination path.
    """
    if os.path.exists(dst) and src.endswith(".json"):
        if merge_json_files(src, dst):
            return dst

    # Fallback to normal copy (overwrites if exists)
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def merge_packs(sources: List[Path]) -> None:
    """
    Merge all source packs into the temporary workspace.

    Args:
        sources: A list of resource pack paths.
    """
    if TEMP_WORKSPACE.exists():
        shutil.rmtree(TEMP_WORKSPACE, onexc=remove_readonly)
    TEMP_WORKSPACE.mkdir(parents=True, exist_ok=True)

    temp_unzip_dir = BUILD_DIR / "temp_unzip"

    import fnmatch

    def get_ignore_func(src_root_dir: Path, exclusions: List[str]):
        all_exclusions = list(exclusions) if exclusions else []
        # Always ignore common OS garbage files
        all_exclusions.extend([".DS_Store", "__MACOSX", "Thumbs.db", "desktop.ini"])
        
        def ignore_func(dir_path: str, names: List[str]) -> List[str]:
            ignored = []
            rel_dir = os.path.relpath(dir_path, src_root_dir).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
            
            for name in names:
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                
                for ex in all_exclusions:
                    ex = ex.replace("\\", "/")
                    
                    # Remove trailing slash as we don't strictly differentiate dirs/files here
                    if ex.endswith("/"):
                        ex = ex[:-1]
                        
                    match_from_root = False
                    if ex.startswith("/"):
                        ex = ex[1:]
                        match_from_root = True
                        
                    # If there's a slash anywhere, it must be evaluated from the root of the pack
                    if "/" in ex:
                        match_from_root = True
                        
                    if match_from_root:
                        if fnmatch.fnmatch(rel_path, ex):
                            ignored.append(name)
                            break
                    else:
                        # Matches anywhere in the tree (e.g. "*.txt", "__MACOSX")
                        if fnmatch.fnmatch(name, ex):
                            ignored.append(name)
                            break
                            
            return ignored
        return ignore_func

    logger.info("Merging resource packs into workspace...")
    for source in sources:
        logger.info(f" -> Applying: {source.relative_to(PROJECT_ROOT)}")
        
        pack_exclusions = []
        config_path = source.with_suffix(".json")
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if isinstance(config, list):
                        pack_exclusions = config
                    elif isinstance(config, dict):
                        pack_exclusions = config.get("exclusions", [])
            except Exception as e:
                logger.error(f"Failed to load {config_path.name}: {e}")
                
        if source.is_dir():
            shutil.copytree(
                source, TEMP_WORKSPACE, dirs_exist_ok=True, copy_function=smart_copy,
                ignore=get_ignore_func(source, pack_exclusions)
            )
        elif source.is_file() and source.suffix.lower() == ".zip":
            if temp_unzip_dir.exists():
                shutil.rmtree(temp_unzip_dir, onexc=remove_readonly)
            temp_unzip_dir.mkdir(parents=True)
            with zipfile.ZipFile(source, "r") as zf:
                zf.extractall(temp_unzip_dir)
            shutil.copytree(
                temp_unzip_dir,
                TEMP_WORKSPACE,
                dirs_exist_ok=True,
                copy_function=smart_copy,
                ignore=get_ignore_func(temp_unzip_dir, pack_exclusions)
            )

    if temp_unzip_dir.exists():
        shutil.rmtree(temp_unzip_dir, onexc=remove_readonly)


def prepare_packsquash_config() -> bool:
    """
    Modify packsquash config to use temp workspace.

    Returns:
        True if configuration is prepared successfully, False otherwise.
    """
    logger.info("Preparing PackSquash configuration...")
    if not ORIGINAL_PACKSQUASH_CONFIG.exists():
        logger.error("packsquash.toml not found in project root.")
        return False

    config_content = ORIGINAL_PACKSQUASH_CONFIG.read_text(encoding="utf-8")
    rel_workspace = TEMP_WORKSPACE.relative_to(PROJECT_ROOT).as_posix()

    new_content = re.sub(
        r"pack_directory\s*=\s*['\"].*?['\"]",
        f"pack_directory = '{rel_workspace}'",
        config_content,
    )

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_PACKSQUASH_CONFIG.write_text(new_content, encoding="utf-8")
    return True


def run_packsquash() -> bool:
    """
    Execute PackSquash with the modified config.

    Returns:
        True if PackSquash runs successfully, False otherwise.
    """
    if not prepare_packsquash_config():
        return False

    logger.info("Starting PackSquash execution...")
    try:
        try:
            subprocess.run(
                ["packsquash", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(
                ["packsquash.exe", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )

        result = subprocess.run(
            ["packsquash", str(TEMP_PACKSQUASH_CONFIG.relative_to(PROJECT_ROOT))],
            cwd=PROJECT_ROOT,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.error(
            "PackSquash command not found. Ensure it is installed and added to your system's PATH."
        )
        return False


def remove_readonly(func: Any, path: str, excinfo: Any) -> None:
    """
    Callback to handle read-only file removal during cleanup.

    Args:
        func: The function that failed.
        path: The path that failed.
        excinfo: Exception information.
    """
    os.chmod(path, stat.S_IWRITE)
    try:
        func(path)
    except Exception:
        pass


def cleanup() -> None:
    """Remove temporary files."""
    logger.info("Cleaning up temporary workspace...")
    if TEMP_WORKSPACE.exists():
        shutil.rmtree(TEMP_WORKSPACE, onexc=remove_readonly)
    if TEMP_PACKSQUASH_CONFIG.exists():
        TEMP_PACKSQUASH_CONFIG.unlink()


def main() -> None:
    """Main execution function for the build script."""
    parser = argparse.ArgumentParser(description="Survival Resourcepack Build Script")
    parser.add_argument("--prepare", action="store_true", help="Prepare workspace without running PackSquash")
    args = parser.parse_args()

    logger.info("Initializing resource pack build process.")
    sources = discover_sources()

    if not sources:
        logger.warning("No valid resource packs found in 'vendor/' or 'packages/'!")
        logger.info("Ensure there is at least one folder with a pack.mcmeta file.")
        return

    logger.info("Discovered packs (in priority order):")
    for i, s in enumerate(sources, 1):
        logger.info(f"  {i}. {s.name}")

    try:
        merge_packs(sources)
        templating.process_workspace(TEMP_WORKSPACE, BUILD_DIR)
        
        if args.prepare:
            if prepare_packsquash_config():
                logger.info("Workspace prepared. PackSquash execution skipped due to --prepare.")
            else:
                logger.error("Failed to prepare PackSquash configuration.")
        else:
            success = run_packsquash()

            if success:
                logger.info("Build completed successfully.")
            else:
                logger.error("Build failed during PackSquash execution.")

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        if not args.prepare:
            cleanup()


if __name__ == "__main__":
    main()
