import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import update_manifest as sync


GUID = "0c8d1d43-0ad6-4d51-b5ac-b39b6f46fbcb"
SOURCE = {"repository": "reefside-ai-labs/animated-album-artwork", "guid": GUID}
ZIP = b"example plugin archive"


def fixture(version="0.1.0.0", release_id=1):
    base = f"https://github.com/{SOURCE['repository']}/releases/download/v{version}/"
    plugin = {
        "guid": GUID, "name": "Animated Album Art", "description": "Description",
        "overview": "Overview", "owner": "reefside-ai-labs", "category": "General",
        "versions": [{"version": version, "targetAbi": "12.1.0.0", "changelog": "Changes",
                      "timestamp": "2026-10-04T03:39:00Z", "sourceUrl": base + "plugin.zip",
                      "checksum": hashlib.md5(ZIP).hexdigest()}],
    }
    release = {
        "id": release_id, "tag_name": "v" + version, "draft": False, "prerelease": False,
        "published_at": f"2026-10-{release_id:02d}T03:39:00Z",
        "assets": [{"name": name, "browser_download_url": base + name}
                   for name in ("manifest.json", "plugin.zip")],
    }
    return plugin, release, {base + "manifest.json": json.dumps([plugin]).encode(), base + "plugin.zip": ZIP}


class SynchronizationTests(unittest.TestCase):
    def run_sync(self, releases, downloads, existing=None):
        with patch.object(sync, "get_releases", return_value=releases), patch.object(
            sync, "download", side_effect=downloads.__getitem__
        ):
            return sync.synchronize([SOURCE], existing or [])

    def test_merges_history_sorts_numerically_and_preserves_other_plugins(self):
        old, old_release, old_files = fixture("0.2.0.0", 1)
        new, new_release, new_files = fixture("0.10.0.0", 2)
        new["description"] = "Newest description"
        new_files[new_release["assets"][0]["browser_download_url"]] = json.dumps([new]).encode()
        other = {"guid": "11111111-2222-3333-4444-555555555555", "name": "Other", "versions": []}
        result = self.run_sync([new_release, old_release], old_files | new_files, [other])
        self.assertEqual([v["version"] for v in result[0]["versions"]], ["0.10.0.0", "0.2.0.0"])
        self.assertEqual(result[0]["description"], "Newest description")
        self.assertEqual(result[1], other)
        self.assertEqual(self.run_sync([old_release], old_files, [old])[0], old)

    def test_skips_prereleases_drafts_and_pending_assets(self):
        plugin, stable, files = fixture()
        prerelease = dict(stable, prerelease=True, id=2)
        draft = dict(stable, draft=True, id=3)
        pending = dict(stable, assets=[], id=4)
        self.assertEqual(self.run_sync([stable, prerelease, draft, pending], files), [plugin])

    def test_unchanged_versions_do_not_download_zip(self):
        plugin, release, files = fixture()
        del files[plugin["versions"][0]["sourceUrl"]]
        self.assertEqual(self.run_sync([release], files, [plugin]), [plugin])

    def test_replaced_manifest_uses_current_asset_id(self):
        plugin, release, files = fixture()
        asset = release["assets"][0]
        asset["id"] = 42
        url = asset["browser_download_url"]
        files[url + "?asset_id=42"] = files.pop(url)
        self.assertEqual(self.run_sync([release], files), [plugin])

    def test_accepts_renamed_repository_and_uses_current_asset_url(self):
        plugin, release, files = fixture()
        expected = copy.deepcopy(plugin)
        release["html_url"] = (
            "https://github.com/transferred-owner/animated-album-artwork/releases/tag/"
            + release["tag_name"]
        )
        for asset in release["assets"]:
            old_url = asset["browser_download_url"]
            asset["browser_download_url"] = old_url.replace("reefside-ai-labs", "transferred-owner")
            files[asset["browser_download_url"]] = files.pop(old_url)
        expected["versions"][0]["sourceUrl"] = release["assets"][1]["browser_download_url"]
        self.assertEqual(self.run_sync([release], files), [expected])

    def test_repository_redirect_does_not_allow_other_release_or_asset(self):
        for path in ("v9.0.0.0/plugin.zip", "v0.1.0.0/other.zip"):
            with self.subTest(path=path):
                plugin, release, files = fixture()
                release["html_url"] = (
                    "https://github.com/transferred-owner/animated-album-artwork/releases/tag/"
                    + release["tag_name"]
                )
                for asset in release["assets"]:
                    old_url = asset["browser_download_url"]
                    asset["browser_download_url"] = old_url.replace("reefside-ai-labs", "transferred-owner")
                    files[asset["browser_download_url"]] = files.pop(old_url)
                plugin["versions"][0]["sourceUrl"] = (
                    f"https://github.com/{SOURCE['repository']}/releases/download/{path}"
                )
                files[release["assets"][0]["browser_download_url"]] = json.dumps([plugin]).encode()
                with self.assertRaisesRegex(ValueError, "same release"):
                    self.run_sync([release], files)

    def test_rejects_wrong_identity_abi_url_and_checksum(self):
        for mutation in ("guid", "targetAbi", "sourceUrl", "checksum"):
            with self.subTest(mutation=mutation):
                plugin, release, files = fixture()
                if mutation == "guid":
                    plugin["guid"] = "11111111-2222-3333-4444-555555555555"
                else:
                    plugin["versions"][0][mutation] = {
                        "targetAbi": "12.1", "sourceUrl": "https://example.com/plugin.zip",
                        "checksum": "0" * 32,
                    }[mutation]
                files[release["assets"][0]["browser_download_url"]] = json.dumps([plugin]).encode()
                with self.assertRaises(ValueError):
                    self.run_sync([release], files)

    def test_failure_leaves_published_manifest_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            sources = Path(directory) / "plugins.json"
            manifest.write_text("[]\n")
            sources.write_text(json.dumps([SOURCE]))
            with patch("sys.argv", ["update_manifest", "--sources", str(sources), "--manifest", str(manifest)]), patch.object(
                sync, "get_releases", side_effect=RuntimeError("API unavailable")
            ):
                with self.assertRaises(RuntimeError):
                    sync.main()
            self.assertEqual(manifest.read_text(), "[]\n")

    def test_transferred_repository_imports_old_manifest_urls(self):
        plugin, release, files = fixture()
        release["html_url"] = "https://github.com/transferred-owner/animated-album-artwork/releases/tag/" + release["tag_name"]
        renamed_files = {}
        for asset in release["assets"]:
            old_url = asset["browser_download_url"]
            asset["browser_download_url"] = old_url.replace("reefside-ai-labs", "transferred-owner")
            renamed_files[asset["browser_download_url"]] = files[old_url]
        result = self.run_sync([release], renamed_files, [plugin])
        self.assertEqual(result[0]["versions"][0]["sourceUrl"], release["assets"][1]["browser_download_url"])

        for url in (
            plugin["versions"][0]["sourceUrl"].replace("v0.1.0.0", "v9.0.0.0"),
            plugin["versions"][0]["sourceUrl"].replace("reefside-ai-labs", "unrelated-owner"),
        ):
            with self.subTest(url=url):
                invalid = copy.deepcopy(plugin)
                invalid["versions"][0]["sourceUrl"] = url
                renamed_files[release["assets"][0]["browser_download_url"]] = json.dumps([invalid]).encode()
                with self.assertRaises(ValueError):
                    self.run_sync([release], renamed_files)

    def test_syncs_multiple_configured_plugins(self):
        plugin, release, files = fixture()
        other = copy.deepcopy(plugin)
        other["guid"] = "11111111-2222-3333-4444-555555555555"
        other["name"] = "Second Plugin"
        other_release = copy.deepcopy(release)
        other_files = {}
        for asset in other_release["assets"]:
            asset["browser_download_url"] = asset["browser_download_url"].replace("animated-album-artwork", "second-plugin")
        other["versions"][0]["sourceUrl"] = other_release["assets"][1]["browser_download_url"]
        other_files[other_release["assets"][0]["browser_download_url"]] = json.dumps([other]).encode()
        other_files[other["versions"][0]["sourceUrl"]] = ZIP
        sources = [SOURCE, {"repository": "reefside-ai-labs/second-plugin", "guid": other["guid"]}]
        with patch.object(sync, "get_releases", side_effect=[[release], [other_release]]), patch.object(
            sync, "download", side_effect=(files | other_files).__getitem__
        ):
            self.assertEqual(sync.synchronize(sources, []), [plugin, other])

    def test_paginates_release_api(self):
        with patch.object(sync.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([[{"id": 1}], [{"id": 2}]]), stderr=""
        )) as run:
            self.assertEqual(sync.get_releases(SOURCE["repository"]), [{"id": 1}, {"id": 2}])
            self.assertIn("--paginate", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
