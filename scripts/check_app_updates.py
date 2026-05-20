#!/usr/bin/env python3
"""
Incremental update checker for Morphe AutoBuilds.

Strategy:
1. Read patch-config.json + arch-config.json -> full expected matrix.
2. Fetch existing 'latest' release manifest (manifest.json asset, if present).
3. For each (app, source, arch):
   - Determine current configured app version (from apps/<platform>/<app>.json).
   - Determine current patch-source signature (latest GitHub release tag(s) of
     repos listed in sources/<source>.json).
   - Compare to manifest.json -> if changed OR APK missing -> needs build.
4. Output:
   - GitHub Actions outputs: build_matrix (JSON), has_updates, total/update counts.
   - File: build_matrix.json    (matrix entries that need rebuild).
   - File: carry_over.json      (existing APK names to re-upload unchanged).
   - File: new_manifest.json    (manifest to upload with the new release).

Force full rebuild: env FORCE_FULL_REBUILD=true (also: any app missing from the
old manifest is rebuilt automatically).

Fail-safe: any unexpected error -> full rebuild matrix is emitted (preserves the
previous always-build behavior so nothing breaks).
"""
import os
import sys
import json
import logging
import subprocess
import traceback
from pathlib import Path
from typing import Dict, List, Tuple, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_CONFIG = REPO_ROOT / "patch-config.json"
ARCH_CONFIG = REPO_ROOT / "arch-config.json"
SOURCES_DIR = REPO_ROOT / "sources"
APPS_DIR = REPO_ROOT / "apps"

MANIFEST_NAME = "manifest.json"
RELEASE_TAG = "latest"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORCE_FULL = os.environ.get("FORCE_FULL_REBUILD", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def write_gh_output(key: str, value: str) -> None:
    """Append key=value (multiline-safe) to GITHUB_OUTPUT, or print locally."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        preview = value if len(value) < 200 else value[:200] + "..."
        print(f"[gh-output] {key}={preview}")
        return
    with open(out, "a", encoding="utf-8") as f:
        if "\n" in value:
            f.write(f"{key}<<EOF_GH\n{value}\nEOF_GH\n")
        else:
            f.write(f"{key}={value}\n")


def run_gh(args: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run `gh ...`; returns (rc, stdout, stderr). Never raises."""
    env = os.environ.copy()
    if GITHUB_TOKEN and "GH_TOKEN" not in env:
        env["GH_TOKEN"] = GITHUB_TOKEN
    try:
        p = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, env=env, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "gh CLI not found"
    except Exception as e:
        return 1, "", f"{e}"


def load_patch_config() -> List[dict]:
    with PATCH_CONFIG.open("r", encoding="utf-8") as f:
        return json.load(f).get("patch_list", [])


def load_arch_config() -> Dict[Tuple[str, str], List[str]]:
    if not ARCH_CONFIG.exists():
        return {}
    with ARCH_CONFIG.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        (e["app_name"], e["source"]): e.get("arches", ["universal"])
        for e in data
    }


def load_app_config_version(app_name: str) -> str:
    """Return the configured 'version' field from the first matching app config,
    or '' if none is pinned (means 'latest at build time')."""
    for platform in ("apkmirror", "apkpure", "uptodown", "aptoide"):
        fp = APPS_DIR / platform / f"{app_name}.json"
        if fp.exists():
            try:
                with fp.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return (data.get("version") or "").strip()
            except Exception:
                continue
    return ""


# ---------------------------------------------------------------------------
# Source-signature: detect when patch repos publish new releases
# ---------------------------------------------------------------------------
_repo_sig_cache: Dict[Tuple[str, str, str], str] = {}


def fetch_repo_signature(user: str, repo: str, tag: str) -> str:
    """Get a stable identifier for the current state of a github repo's release.
    Returns 'tag_name@published_at' on success, else a short error sentinel."""
    key = (user, repo, tag)
    if key in _repo_sig_cache:
        return _repo_sig_cache[key]

    if tag == "latest":
        api = f"repos/{user}/{repo}/releases/latest"
    elif tag in ("", "dev", "prerelease"):
        api = f"repos/{user}/{repo}/releases?per_page=10"
    else:
        api = f"repos/{user}/{repo}/releases/tags/{tag}"

    rc, out, err = run_gh(["api", api])
    if rc != 0:
        sig = f"err:{tag}"
        _repo_sig_cache[key] = sig
        logging.warning(f"  gh api {api} -> rc={rc} ({err.strip()[:80]})")
        return sig

    try:
        data = json.loads(out)
    except Exception:
        sig = f"badjson:{tag}"
        _repo_sig_cache[key] = sig
        return sig

    # If list (per_page query), filter & pick newest
    if isinstance(data, list):
        if tag == "dev":
            data = [r for r in data if "dev" in (r.get("tag_name") or "").lower()]
        elif tag == "prerelease":
            data = [r for r in data if r.get("prerelease")]
        if not data:
            sig = f"none:{tag}"
            _repo_sig_cache[key] = sig
            return sig
        data.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        rel = data[0]
    else:
        rel = data

    tag_name = rel.get("tag_name") or rel.get("name") or "?"
    published = rel.get("published_at") or rel.get("created_at") or "?"
    sig = f"{tag_name}@{published}"
    _repo_sig_cache[key] = sig
    return sig


_source_sig_cache: Dict[str, str] = {}


def get_source_signature(source: str) -> str:
    """Combine release signatures of every repo declared in sources/<source>.json
    into a single deterministic string."""
    if source in _source_sig_cache:
        return _source_sig_cache[source]

    src_file = SOURCES_DIR / f"{source}.json"
    if not src_file.exists():
        # Case-insensitive fallback
        for f in SOURCES_DIR.glob("*.json"):
            if f.stem.lower() == source.lower():
                src_file = f
                break
    if not src_file.exists():
        sig = f"missing-source:{source}"
        _source_sig_cache[source] = sig
        return sig

    try:
        with src_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sig = f"unparseable:{e}"
        _source_sig_cache[source] = sig
        return sig

    if isinstance(data, dict) and "bundle_url" in data:
        sig = f"bundle:{data['bundle_url']}"
        _source_sig_cache[source] = sig
        return sig

    parts: List[str] = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user")
            repo = entry.get("repo")
            tag = entry.get("tag", "latest")
            if user and repo:
                parts.append(f"{user}/{repo}@{fetch_repo_signature(user, repo, tag)}")

    sig = ";".join(parts) if parts else f"empty:{source}"
    _source_sig_cache[source] = sig
    return sig


# ---------------------------------------------------------------------------
# Existing release manifest + assets
# ---------------------------------------------------------------------------
def fetch_existing_manifest() -> Optional[dict]:
    rc, out, err = run_gh(["release", "view", RELEASE_TAG, "--json", "assets"])
    if rc != 0:
        logging.info(f"No existing '{RELEASE_TAG}' release ({err.strip()[:100]})")
        return None
    try:
        assets = [a.get("name", "") for a in json.loads(out).get("assets", [])]
    except Exception as e:
        logging.warning(f"Cannot parse assets JSON: {e}")
        return None

    if MANIFEST_NAME not in assets:
        logging.info(f"Existing release has no '{MANIFEST_NAME}' (first incremental run)")
        return None

    rc, _, err = run_gh(["release", "download", RELEASE_TAG,
                         "--pattern", MANIFEST_NAME, "--clobber"])
    if rc != 0:
        logging.warning(f"Failed to download manifest: {err.strip()[:120]}")
        return None
    try:
        with open(MANIFEST_NAME, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Bad manifest.json: {e}")
        return None


def fetch_existing_apk_names() -> List[str]:
    rc, out, _ = run_gh(["release", "view", RELEASE_TAG, "--json", "assets"])
    if rc != 0:
        return []
    try:
        return [
            a.get("name", "")
            for a in json.loads(out).get("assets", [])
            if a.get("name", "").endswith(".apk")
        ]
    except Exception:
        return []
