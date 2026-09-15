#!/usr/bin/env python3

# Download DokuWiki to Static Site
# Author: Sascha Leib
# License: GPL3

# IMPORTS:

import os
import sys
import re
import html

from dataclasses import dataclass, field

# Try to import yaml support:
try:
    import yaml
except ImportError:
    sys.exit(
        "Missing dependency 'PyYAML'. Install it with:\n"
        "    pip install pyyaml"
    )

# Try to import requests support:
try:
    import requests
except ImportError:
    sys.exit(
        "Missing dependency 'requests'. Install it with:\n"
        "    pip install requests"
    )

# DATA CLASSES:

@dataclass
class TextReplacement:
    search: str
    replace: str
    regex: bool = False
    applies_to: list[str] | None = None

@dataclass
class AlternativeSpec:
    name: str
    pattern: str
    file_ext: str | None = None
    legacy_suffix: str | None = None

@dataclass
class Config:
    list_path: str
    list_is_url: bool
    alternatives: list[AlternativeSpec]
    target: str
    base: str
    add_file_ext: bool
    default_file_ext: str
    text_replacements: list[TextReplacement] = field(default_factory=list)

@dataclass
class FetchResult:
    ok: bool
    content: str | None = None
    warning: str | None = None

# FUNCTIONS:

# Load the config file:
def load_config(config_path: str) -> Config:
    if not os.path.isfile(config_path):
        sys.exit(f"ERROR: config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            sys.exit(f"ERROR: could not parse config file {config_path}: {exc}")

    if not isinstance(raw, dict):
        sys.exit(f"ERROR: config file {config_path} does not contain a YAML mapping")

    config_dir = os.path.dirname(os.path.abspath(config_path))

    # check for required key:
    def required(key):
        if key not in raw:
            sys.exit(f"ERROR: config is missing required key '{key}'")
        return raw[key]

    # "list" = source of URLs to load (can be URL or file path)
    list_value = required("list")
    list_is_url = list_value.startswith("http://") or list_value.startswith("https://")
    if list_is_url:
        list_path = list_value
    else:
        list_path = list_value
        if not os.path.isabs(list_path):
            list_path = os.path.join(config_dir, list_path)

    # "base" = base URL for downloads
    base = required("base")
    if not base.endswith("/"): # make sure base always ends with slash
        base += "/"

    # "target" = path where to save the downloaded pages
    target = required("target")

    # "alternatives" = list of alternative versions of a page
    #  must at least contain one entry with "" (empty string) in it.
    alternatives_raw = raw.get("alternatives", [""])
    if not isinstance(alternatives_raw, list) or not alternatives_raw:
        sys.exit("ERROR: 'alternatives' must be a non-empty list")

    alternatives: list[AlternativeSpec] = []
    seen_alt_names: set[str] = set()
    for i, item in enumerate(alternatives_raw):
        if isinstance(item, str):
            # Legacy form: the string is appended directly to base+slug to
            # build the fetch URL, and is also used as the identifier for
            # appliesTo and as the fallback for the addFileExt heuristic.
            spec = AlternativeSpec(
                name = item,
                pattern = "{base}{slug}" + item,
                file_ext = None,
                legacy_suffix = item,
            )
        elif isinstance(item, dict):
            missing = [k for k in ("name", "pattern", "fileExt") if k not in item]
            if missing:
                sys.exit(
                    f"ERROR: alternatives[{i}] is missing {missing} "
                    f"(needs 'name', 'pattern', and 'fileExt', or use a "
                    f"plain string for the legacy \"\"/\".md\"-style form)"
                )
            pattern = str(item["pattern"])
            if "{slug}" not in pattern:
                sys.exit(f"ERROR: alternatives[{i}] pattern {pattern!r} must contain '{{slug}}'")
            spec = AlternativeSpec(
                name = str(item["name"]),
                pattern = pattern,
                file_ext = str(item["fileExt"]),
            )
        else:
            sys.exit(f"ERROR: alternatives[{i}] must be a string or a mapping")

        # make sure no duplicates exist!
        if spec.name in seen_alt_names:
            print(
                f"WARNING: duplicate alternatives name {spec.name!r} "
                f"(appliesTo will match all entries sharing that name)"
            )
        seen_alt_names.add(spec.name)
        alternatives.append(spec)

    options = raw.get("options", {}) or {}
    add_file_ext = bool(options.get("addFileExt", False))
    default_file_ext = options.get("defaultFileExt", ".html")


    # "textReplacements" = list of text replacement patterns
    replacements_raw = raw.get("textReplacements", []) or []
    replacements: list[TextReplacement] = []
    for i, item in enumerate(replacements_raw):
        if "search" not in item or "replace" not in item:
            sys.exit(f"ERROR: textReplacements[{i}] needs 'search' and 'replace'")

        applies_to_raw = item.get("appliesTo")
        if applies_to_raw is None:
            applies_to = None
        elif isinstance(applies_to_raw, str):
            applies_to = [applies_to_raw]
        elif isinstance(applies_to_raw, list):
            applies_to = [str(a) for a in applies_to_raw]
        else:
            sys.exit(
                f"ERROR: textReplacements[{i}] 'appliesTo' must be a string "
                f"or a list of strings"
            )
        if applies_to is not None:
            for a in applies_to:
                if a not in seen_alt_names:
                    print(
                        f"WARNING: textReplacements[{i}] appliesTo value {a!r} "
                        f"is not among configured alternatives {sorted(seen_alt_names)!r}"
                    )

        replacements.append(
            TextReplacement(
                search = item["search"],
                replace = item["replace"],
                regex = bool(item.get("regex", False)),
                applies_to = applies_to,
            )
        )
        if replacements[-1].regex:
            try:
                re.compile(replacements[-1].search)
            except re.error as exc:
                sys.exit(f"ERROR: invalid regex in textReplacements[{i}]: {exc}")

    return Config(
        list_path = list_path,
        list_is_url = list_is_url,
        alternatives = alternatives,
        target = target,
        base = base,
        add_file_ext = add_file_ext,
        default_file_ext = default_file_ext,
        text_replacements = replacements,
    )

# Load the list of URLs directly from DokuWiki:
def load_url_list_from_web(list_url: str, base: str) -> list[str]:

    # try to load the page:
    result = fetch_url(list_url)
    if not result.ok:
        sys.exit(f"ERROR: could not download URL list from {list_url}: {result.warning}")

    # extract all links in the page:
    hrefs = re.findall(r'<a\b[^>]*\bhref="([^"]+)"', result.content, re.IGNORECASE)

    # only accept URLs that start with "base":
    urls = []
    seen = set()
    for raw_href in hrefs:
        href = html.unescape(raw_href)
        if href.startswith(base) and href not in seen:
            seen.add(href)
            urls.append(href)

    # found any URLs at all?
    if not urls:
        print(f"WARNING: no links starting with {base!r} found in {list_url}")

    return urls

# Fetch a single URL:
def fetch_url(url: str, timeout: float = 15.0) -> FetchResult:
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=False)
    except requests.exceptions.RequestException as exc:
        return FetchResult(ok=False, warning=f"network error fetching {url}: {exc}")

    # follow redirections:
    if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "?")
        return FetchResult(
            ok=False,
            warning=f"unexpected redirect for {url} -> {location} (skipped)",
        )

    # Status: not OK
    if response.status_code != 200:
        return FetchResult(
            ok=False,
            warning=f"HTTP {response.status_code} fetching {url} (skipped)",
        )

    # Make sure there is an encoding defined (default: UTF-8)
    if not response.encoding:
        response.encoding = "utf-8"

    return FetchResult(ok=True, content=response.text)

# Extract the slug from a URL:
def slug_for(url: str, base: str) -> str | None:
    if not url.startswith(base):
        return None
    remainder = url[len(base):]
    if remainder == "":
        return None
    return remainder

# build the relative path for saving the file:
def build_relative_path(slug: str, alt: AlternativeSpec, add_file_ext: bool, default_ext: str) -> str:

    # use 'file_ext', if provided:
    if alt.file_ext is not None:
        return slug + alt.file_ext

    # alternatively, use the legacy suffix:
    combined = slug + (alt.legacy_suffix or "")

    # add an extension, if required:
    last_segment = posixpath.basename(combined)
    has_extension = "." in last_segment
    if add_file_ext and not has_extension:
        combined += default_ext

    return combined



# MAIN

# find the script path and config files
# (config file can be passed as parameter, otherwise use the default)
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(script_dir, "config.yaml")

print(f"- Loading config from: {config_path}");

# read the configuration file:
config = load_config(config_path)

# Where to load the list of files from?
if config.list_is_url:
    print(f"- Loading list of URLs from: {config.list_path}");
    urls = load_url_list_from_web(config.list_path, config.base)
else:
    print(f"- Reading URL list from file: {config.list_path}");
    urls = load_url_list(config.list_path)

print(f"- Found URLs to load: {str(len(urls))}");

# Where to save the downloaded pages?
target_path = os.path.normpath(config.target)
if os.path.islink(target_path):
    real_target = os.path.realpath(target_path)
    if not os.path.isdir(real_target):
        os.makedirs(real_target, exist_ok=True)
elif not os.path.isdir(target_path):
    os.makedirs(target_path, exist_ok=True)

# define stats variables:
counter = 0
total_attempted = 0
total_ok = 0
warnings: list[str] = []

# Loop over all URLs to download:
for url in urls:
    slug = slug_for(url, config.base)
    if slug is None:
        msg = f"URL does not match configured base, or is a bare root URL: {url} (skipped)"
        warnings.append(msg)
        print(f"WARNING: {msg}")
        continue

    # print a header for each URL:
    counter += 1
    print(f"#{counter}: {slug}")

    # loop all alternatives:
    for alt in config.alternatives:
        fetch_target = alt.pattern.format(base=config.base, slug=slug)
        total_attempted += 1

        print("  * " + fetch_target)

        # attempt to load the alt file:
        result = fetch_url(fetch_target)
        if not result.ok:
            warnings.append(result.warning)
            print(f"WARNING: {result.warning}")
            continue

        # extract the relative path, to re-create it for the saved file:
        rel_path = build_relative_path(slug, alt, config.add_file_ext, config.default_file_ext)
        print("    + Relative path: " + rel_path)


    # End of alternatives loop


# End of URL Loop

print()
print(f"Done. {total_ok}/{total_attempted} files downloaded successfully, "
      f"{len(warnings)} warning(s).")
