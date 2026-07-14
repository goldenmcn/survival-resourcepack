"""
Templating module for the survival-resourcepack build system.
Handles fetching vanilla language files and processing Jinja2 templates.
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment

logger = logging.getLogger("build.templating")

# Configuration constants
MINECRAFT_VERSION: str = "26.2"
TARGET_LOCALES: List[str] = ["en_us", "ru_ru", "uk_ua"]

MANIFEST_URL: str = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCE_BASE_URL: str = "https://resources.download.minecraft.net"


def fetch_json(url: str) -> Dict[str, Any]:
    """
    Fetch and parse JSON from a given URL.

    Args:
        url: The URL to fetch.

    Returns:
        The parsed JSON dictionary.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(url: str, dest_path: Path) -> None:
    """
    Download a file from a URL to a local destination.

    Args:
        url: The URL to download.
        dest_path: The local path to save the file.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        with open(dest_path, "wb") as out_file:
            out_file.write(response.read())


def get_vanilla_lang_file(cache_dir: Path, locale: str) -> Dict[str, Any]:
    """
    Download or load a cached vanilla language file from Mojang.

    Args:
        cache_dir: The directory where cached language files are stored.
        locale: The locale code (e.g., 'en_us').

    Returns:
        A dictionary containing the language key-value pairs.
    """
    cache_path = cache_dir / f"{locale}.json"

    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    logger.info(
        f"Downloading vanilla language file for '{locale}' (version {MINECRAFT_VERSION})..."
    )

    try:
        manifest = fetch_json(MANIFEST_URL)
        version_meta_url: Optional[str] = None
        for version in manifest.get("versions", []):
            if version.get("id") == MINECRAFT_VERSION:
                version_meta_url = version.get("url")
                break

        if not version_meta_url:
            logger.error(f"Version {MINECRAFT_VERSION} not found in Mojang manifest.")
            return {}

        version_meta = fetch_json(version_meta_url)
        asset_index_url = version_meta.get("assetIndex", {}).get("url")

        if not asset_index_url:
            logger.error("No asset index URL found.")
            return {}

        asset_index = fetch_json(asset_index_url)
        objects = asset_index.get("objects", {})

        lang_path = f"minecraft/lang/{locale}.json"

        if lang_path not in objects:
            logger.warning(
                f"Locale '{locale}' not found in assets for {MINECRAFT_VERSION}."
            )
            return {}

        file_hash = objects[lang_path]["hash"]
        hash_prefix = file_hash[:2]

        download_url = f"{RESOURCE_BASE_URL}/{hash_prefix}/{file_hash}"
        download_file(download_url, cache_path)

        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        logger.error(f"Failed to fetch vanilla language file for {locale}: {e}")
        return {}


def process_workspace(workspace_dir: Path, build_dir: Path) -> None:
    """
    Scan the workspace for template.json files and process them with Jinja2.

    Args:
        workspace_dir: The root directory of the temporary workspace.
        build_dir: The build output directory.
    """
    cache_dir = build_dir / ".lang_cache"
    vanilla_langs: Dict[str, Dict[str, Any]] = {}

    templates: List[Path] = []
    for path in workspace_dir.rglob("template.json"):
        if "lang" in path.parts:
            templates.append(path)

    if not templates:
        return

    logger.info(f"Found {len(templates)} language templates. Processing with Jinja2...")
    env = Environment(autoescape=False)

    for template_path in templates:
        logger.info(
            f" -> Applying template: {template_path.relative_to(workspace_dir)}"
        )

        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_text = f.read()
            jinja_template = env.from_string(template_text)
        except Exception as e:
            logger.error(f"Failed to read/parse template {template_path}: {e}")
            continue

        for locale in TARGET_LOCALES:
            if locale not in vanilla_langs:
                vanilla_langs[locale] = get_vanilla_lang_file(cache_dir, locale)

            if "en_us" not in vanilla_langs:
                vanilla_langs["en_us"] = get_vanilla_lang_file(cache_dir, "en_us")

            vanilla_dict = vanilla_langs[locale]
            fallback_dict = vanilla_langs["en_us"]

            def get_translation(key: str, default: Optional[str] = None) -> str:
                """Get a translation falling back to en_us or default."""
                if key in vanilla_dict:
                    return vanilla_dict[key]
                if key in fallback_dict:
                    return fallback_dict[key]
                return default if default is not None else key

            context = {
                "locale": locale,
                "vanilla": vanilla_dict,
                "fallback": fallback_dict,
                "get_translation": get_translation,
            }

            try:
                rendered_text = jinja_template.render(context)
                rendered_data = json.loads(rendered_text)
            except Exception as e:
                logger.error(f"Failed to render or parse JSON for locale {locale}: {e}")
                continue

            locale_file_path = template_path.parent / f"{locale}.json"
            locale_data: Dict[str, Any] = {}
            if locale_file_path.exists():
                try:
                    with open(locale_file_path, "r", encoding="utf-8") as f:
                        locale_data = json.load(f)
                except Exception as e:
                    logger.error(f"Failed to read existing {locale}.json: {e}")

            locale_data.update(rendered_data)

            if locale_data:
                with open(locale_file_path, "w", encoding="utf-8") as f:
                    json.dump(locale_data, f, ensure_ascii=False, indent=2)

        template_path.unlink()
