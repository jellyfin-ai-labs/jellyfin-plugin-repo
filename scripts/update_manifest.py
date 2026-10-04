#!/usr/bin/env python3
"""Aggregate stable GitHub release manifests without executing plugin code."""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from uuid import UUID


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        raise ValueError(f"Expected a four-part numeric version, got {value!r}")
    return tuple(int(part) for part in value.split("."))


def get_releases(repository):
    # --slurp keeps page boundaries explicit, including repositories with >100 releases.
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{repository}/releases?per_page=100"],
        check=True, capture_output=True, text=True,
    )
    return [release for page in json.loads(result.stdout) for release in page]


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "jellyfin-plugin-repo"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def validate_plugin(plugin, guid, assets):
    if str(UUID(plugin["guid"])) != guid:
        raise ValueError(f"Unexpected plugin GUID: {plugin['guid']}")
    for field in ("name", "description", "overview", "owner", "category"):
        if not isinstance(plugin.get(field), str) or not plugin[field].strip():
            raise ValueError(f"Missing plugin metadata: {field}")
    if not isinstance(plugin.get("versions"), list) or not plugin["versions"]:
        raise ValueError("Release manifest has no versions")
    for version in plugin["versions"]:
        version_key(version["version"])
        version_key(version["targetAbi"])
        for field in ("changelog", "timestamp"):
            if not isinstance(version.get(field), str):
                raise ValueError(f"Missing version metadata: {field}")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", version.get("checksum", "")):
            raise ValueError("Invalid ZIP MD5 checksum")
        if version["sourceUrl"] not in assets or not assets[version["sourceUrl"]].endswith(".zip"):
            raise ValueError("Plugin ZIP must be an asset of the same release")


def synchronize(sources, existing):
    plugins = {str(UUID(plugin["guid"])): copy.deepcopy(plugin) for plugin in existing}
    seen = set()
    for source in sources:
        repository = source["repository"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"Invalid repository: {repository}")
        guid = str(UUID(source["guid"]))
        if guid in seen:
            raise ValueError(f"Duplicate configured GUID: {guid}")
        seen.add(guid)
        releases = [release for release in get_releases(repository)
                    if not release["draft"] and not release["prerelease"]]
        # Apply oldest first so the newest release supplies plugin descriptions.
        releases.sort(key=lambda release: (release["published_at"], release["id"]))
        for release in releases:
            assets = {asset["browser_download_url"]: asset["name"] for asset in release["assets"]}
            manifests = [url for url, name in assets.items() if name == "manifest.json"]
            if not manifests:
                # The publisher may still be building and uploading the assets.
                print(f"Waiting for manifest.json: {repository} {release['tag_name']}")
                continue
            data = json.loads(download(manifests[0]))
            if not isinstance(data, list) or len(data) != 1:
                raise ValueError(f"Expected one plugin in {repository} release manifest")
            plugin = data[0]
            validate_plugin(plugin, guid, assets)
            previous = {version["version"]: version for version in plugins.get(guid, {}).get("versions", [])}
            for version in plugin["versions"]:
                # Unchanged entries were verified when first imported. Avoid
                # repeatedly downloading every historical ZIP on each poll.
                if previous.get(version["version"]) != version:
                    checksum = hashlib.md5(download(version["sourceUrl"])).hexdigest()
                    if checksum.lower() != version["checksum"].lower():
                        raise ValueError(f"ZIP checksum mismatch: {version['sourceUrl']}")
                previous[version["version"]] = version
            plugin["versions"] = sorted(previous.values(), key=lambda item: version_key(item["version"]), reverse=True)
            plugins[guid] = plugin
    return sorted(plugins.values(), key=lambda plugin: (plugin["name"].casefold(), plugin["guid"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("plugins.json"))
    parser.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    args = parser.parse_args()
    existing = json.loads(args.manifest.read_text()) if args.manifest.exists() else []
    result = synchronize(json.loads(args.sources.read_text()), existing)
    content = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.manifest.exists() and args.manifest.read_text() == content:
        print("Manifest is already up to date.")
        return
    # Do not touch the published file unless every available manifest validated.
    temporary = args.manifest.with_suffix(".json.tmp")
    temporary.write_text(content)
    temporary.replace(args.manifest)
    print(f"Updated {args.manifest} with {len(result)} plugin(s).")


if __name__ == "__main__":
    main()
