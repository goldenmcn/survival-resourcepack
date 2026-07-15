"""
Templating module for the survival-resourcepack build system.
Handles fetching vanilla language files and processing Jinja2 templates.
"""

import io
import json
import logging
import math
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment

logger = logging.getLogger("build.templating")

# Minecraft default font character widths (ASCII mostly)
DEFAULT_CHAR_WIDTH = 5
CHAR_WIDTHS: Dict[str, int] = {
    'i': 1, 'l': 2, 't': 3, 'k': 4, 'I': 3, 'f': 4, ' ': 3,
    '!': 1, '"': 3, "'": 1, '(': 3, ')': 3, '*': 3, ',': 1,
    '.': 1, ':': 1, ';': 1, '<': 4, '>': 4, '@': 6, '[': 3,
    ']': 3, '`': 2, '{': 3, '|': 1, '}': 3, '~': 6
}

def get_text_width(text: str) -> int:
    """Calculate the pixel width of a string in Minecraft default font."""
    if not text:
        return 0
    width = sum(CHAR_WIDTHS.get(char, DEFAULT_CHAR_WIDTH) for char in text)
    width += (len(text) - 1)  # 1 pixel space between characters
    return width

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
            logger.info(
                f"Locale '{locale}' not found in assets for {MINECRAFT_VERSION}. Attempting to extract from client jar..."
            )
            client_url = version_meta.get("downloads", {}).get("client", {}).get("url")
            if not client_url:
                logger.error("No client jar URL found.")
                return {}

            logger.info("Downloading client jar to extract language file...")
            req = urllib.request.Request(client_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as response:
                with zipfile.ZipFile(io.BytesIO(response.read())) as jar:
                    try:
                        with jar.open(f"assets/{lang_path}") as lang_file:
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(cache_path, "wb") as out_file:
                                out_file.write(lang_file.read())
                    except KeyError:
                        logger.error(f"Locale '{locale}' not found in client jar either.")
                        return {}

            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)

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

    lang_templates: List[Path] = []
    generic_templates: List[Path] = []
    
    for path in workspace_dir.rglob("template.json"):
        if "lang" in path.parts:
            lang_templates.append(path)

    for path in workspace_dir.rglob("*.j2"):
        generic_templates.append(path)

    if not lang_templates and not generic_templates:
        return

    spaces_map: Dict[str, str] = {}
    
    # Generate negative and positive spaces natively
    start_neg = 851850 # 0xCFF8A
    start_pos = 852000 # 0xD0020
    for i in range(1, 101):
        spaces_map[str(-i)] = chr(start_neg + i)
        spaces_map[str(i)] = chr(start_pos + i)
        
    logger.info(f"Loaded {len(spaces_map)} font spaces into Jinja context.")
    
    icons: Dict[str, Any] = {}
    icons_file = workspace_dir / "icons.json"
    if icons_file.exists():
        try:
            with open(icons_file, "r", encoding="utf-8") as f:
                icons = json.load(f)
            logger.info(f"Loaded {len(icons)} icons from icons.json")
            icons_file.unlink()
        except Exception as e:
            logger.error(f"Failed to load icons.json: {e}")

    def get_shift_str(amount: int) -> str:
        if amount == 0:
            return ""
        res = ""
        while amount > 100:
            res += spaces_map.get("100", "")
            amount -= 100
        while amount < -100:
            res += spaces_map.get("-100", "")
            amount += 100
        if amount != 0:
            res += spaces_map.get(str(amount), "")
        return res

    def center_text(text: str) -> str:
        w = get_text_width(text)
        h = math.ceil(w / 2.0)
        return get_shift_str(-h) + text + get_shift_str(-(w - h))

    def center_icon(icon_name: str) -> str:
        icon = icons.get(icon_name)
        if not icon:
            return ""
        w = icon.get("width", 0)
        char = icon.get("char", "")
        h = math.ceil(w / 2.0)
        return get_shift_str(-h) + char + get_shift_str(-(w - h))

    if lang_templates:
        logger.info(f"Found {len(lang_templates)} language templates. Processing with Jinja2...")
        env = Environment(autoescape=False)

        for template_path in lang_templates:
            logger.info(
                f" -> Applying language template: {template_path.relative_to(workspace_dir)}"
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
                    "spaces": spaces_map,
                    "icons": icons,
                    "center": center_text,
                    "center_icon": center_icon,
                    "shift": get_shift_str,
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

    if generic_templates:
        logger.info(f"Found {len(generic_templates)} generic Jinja2 templates. Processing...")
        env = Environment(autoescape=False)

        for template_path in generic_templates:
            logger.info(
                f" -> Applying generic template: {template_path.relative_to(workspace_dir)}"
            )
            try:
                with open(template_path, "r", encoding="utf-8") as f:
                    template_text = f.read()
                jinja_template = env.from_string(template_text)
                rendered_text = jinja_template.render({
                    "spaces": spaces_map,
                    "icons": icons,
                    "center": center_text,
                    "center_icon": center_icon,
                    "shift": get_shift_str,
                })
                
                # Check if it produces valid JSON
                rendered_data = json.loads(rendered_text)
            except Exception as e:
                logger.error(f"Failed to render generic template {template_path}: {e}")
                continue

            output_path = template_path.with_suffix("") # Remove .j2
            
            if output_path.exists():
                try:
                    with open(output_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    
                    if isinstance(existing_data, dict) and isinstance(rendered_data, dict):
                        if "providers" in existing_data and "providers" in rendered_data:
                            existing_data["providers"].extend(rendered_data["providers"])
                        else:
                            existing_data.update(rendered_data)
                        rendered_data = existing_data
                except Exception as e:
                    logger.error(f"Failed to merge generic template output into {output_path}: {e}")
            
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(rendered_data, f, ensure_ascii=False, indent=2)
                
            template_path.unlink()

    if icons:
        logger.info("Generating native icon font providers from icons.json...")
        icon_providers = []
        for name, icon in icons.items():
            if "file" in icon:
                provider = {
                    "type": "bitmap",
                    "file": icon["file"],
                    "ascent": icon.get("ascent", 0)
                }
                if "height" in icon:
                    provider["height"] = icon["height"]
                if "chars" in icon:
                    provider["chars"] = icon["chars"]
                else:
                    provider["chars"] = [icon.get("char", "")]
                icon_providers.append(provider)
        
        if icon_providers:
            default_font_path = workspace_dir / "assets/minecraft/font/default.json"
            default_font_path.parent.mkdir(parents=True, exist_ok=True)
            font_data = {"providers": []}
            if default_font_path.exists():
                try:
                    with open(default_font_path, "r", encoding="utf-8") as f:
                        font_data = json.load(f)
                except Exception as e:
                    logger.error(f"Failed to read {default_font_path}: {e}")
            
            font_data.setdefault("providers", []).extend(icon_providers)
            
            with open(default_font_path, "w", encoding="utf-8") as f:
                json.dump(font_data, f, ensure_ascii=False, indent=2)
