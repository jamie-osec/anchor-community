#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def load_repos(path):
    with path.open("r", encoding="utf-8") as f:
        repos = json.load(f)
    if not isinstance(repos, list):
        raise ValueError(f"{path} must contain a JSON array")

    required = {"name", "source", "commit", "directory", "build_command", "ignored"}
    for index, repo in enumerate(repos):
        missing = sorted(required - set(repo))
        if missing:
            raise ValueError(f"repo at index {index} is missing: {', '.join(missing)}")
        validate_directory(repo["directory"])
        validate_ignored(repo["ignored"], repo["directory"])
        repo["ignored"] = normalize_ignored(repo["ignored"])
    return repos


def validate_directory(directory):
    path = Path(directory)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe repository directory: {directory}")


def validate_ignored(ignored, directory):
    if not isinstance(ignored, list):
        raise ValueError(f"{directory}: ignored must be an array")
    for ignored_path in ignored:
        if not isinstance(ignored_path, str):
            raise ValueError(f"{directory}: ignored path must be a string")
        path = Path(ignored_path)
        if (
            path.is_absolute()
            or not path.parts
            or ignored_path in {".", ""}
            or ".." in path.parts
        ):
            raise ValueError(f"{directory}: unsafe ignored path: {ignored_path}")


def normalize_ignored(ignored):
    normalized = []
    seen = set()
    for ignored_path in sorted(ignored, key=lambda path: (len(Path(path).parts), path)):
        parts = Path(ignored_path).parts
        if any(parts[: len(Path(parent).parts)] == Path(parent).parts for parent in normalized):
            continue
        if ignored_path not in seen:
            normalized.append(ignored_path)
            seen.add(ignored_path)
    return normalized


def validate_programs_dir(programs_dir):
    if programs_dir.exists() and programs_dir.is_symlink():
        raise ValueError(f"{programs_dir} exists but is a symlink")
    if programs_dir.exists() and not programs_dir.is_dir():
        raise ValueError(f"{programs_dir} exists but is not a directory")


def select_repos(repos, selectors):
    if not selectors:
        return repos

    selected = []
    exact_directories = {repo["directory"].lower(): repo for repo in repos}
    for selector in selectors:
        selector = selector.lower()
        if selector in exact_directories:
            repo = exact_directories[selector]
            if repo not in selected:
                selected.append(repo)
            continue

        for repo in repos:
            haystack = " ".join(
                [
                    repo["name"],
                    repo["source"],
                    repo["directory"],
                ]
            ).lower()
            if selector in haystack and repo not in selected:
                selected.append(repo)

    if not selected:
        raise ValueError(f"no repos matched: {', '.join(selectors)}")
    return selected


def run(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def short_commit(commit):
    return commit[:12]


def log_repo(repo, message):
    print(f"{repo['directory']}: {message}")


def clone_at_commit(repo, destination):
    log_repo(repo, f"cloning {repo['source']}")
    clone = run(["git", "clone", "--no-checkout", repo["source"], str(destination)])
    if clone.returncode != 0:
        raise RuntimeError(f"unable to clone {repo['source']}:\n{clone.stdout.rstrip()}")

    log_repo(repo, f"checking out {short_commit(repo['commit'])}")
    checkout = run(["git", "checkout", "--detach", repo["commit"]], cwd=destination)
    if checkout.returncode != 0:
        raise RuntimeError(
            f"unable to checkout {repo['commit']} for {repo['source']}:\n{checkout.stdout.rstrip()}"
        )

    git_dir = destination / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


def checked_ignore_path(root, ignored_path):
    root = root.absolute()
    rel = Path(ignored_path)
    validate_ignored([ignored_path], root.as_posix())

    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"ignored path traverses a symlink: {ignored_path}")

    target = root / rel
    target_abs = target.absolute()
    if os.path.commonpath([root.as_posix(), target_abs.as_posix()]) != root.as_posix():
        raise RuntimeError(f"ignored path escapes repository directory: {ignored_path}")
    return target


def remove_ignored(repo, root):
    removed = []
    for ignored_path in repo["ignored"]:
        target = checked_ignore_path(root, ignored_path)
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(ignored_path)

    if removed:
        log_repo(repo, f"removed ignored paths: {', '.join(removed)}")


def prune_empty_dirs(root):
    root = root.absolute()
    for current, dirnames, _ in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        if current_path == root:
            continue

        if current_path.is_symlink():
            continue

        try:
            next(current_path.iterdir())
        except StopIteration:
            current_path.rmdir()


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_folder_recursive(root):
    root = root.absolute()
    entries = {}

    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames.sort()
        filenames.sort()

        for dirname in dirnames:
            path = current_path / dirname
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[rel] = {
                    "type": "symlink",
                    "target": os.readlink(path),
                    "mode": path.lstat().st_mode & 0o777,
                }
            else:
                entries[rel] = {
                    "type": "directory",
                    "mode": path.lstat().st_mode & 0o777,
                }

        for filename in filenames:
            path = current_path / filename
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[rel] = {
                    "type": "symlink",
                    "target": os.readlink(path),
                    "mode": path.lstat().st_mode & 0o777,
                }
            else:
                entries[rel] = {
                    "type": "file",
                    "mode": path.lstat().st_mode & 0o777,
                    "sha256": hash_file(path),
                }

    digest = hashlib.sha256()
    for rel, metadata in sorted(entries.items()):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
        digest.update(b"\0")

    return digest.hexdigest(), entries


def first_difference(left_entries, right_entries):
    left_keys = set(left_entries)
    right_keys = set(right_entries)
    only_left = sorted(left_keys - right_keys)
    if only_left:
        return f"only in existing tree: {only_left[0]}"

    only_right = sorted(right_keys - left_keys)
    if only_right:
        return f"only in freshly cloned tree: {only_right[0]}"

    for key in sorted(left_keys):
        if left_entries[key] != right_entries[key]:
            return f"different metadata or hash: {key}"

    return "unknown difference"


def vendor_repo(repo, programs_dir):
    validate_programs_dir(programs_dir)
    target = programs_dir / repo["directory"]
    print(f"\n==> {repo['name']}")
    log_repo(repo, f"target directory: {target}")
    log_repo(repo, f"pinned commit: {repo['commit']}")

    with tempfile.TemporaryDirectory(prefix="anchor-community-") as tmp:
        clone_path = Path(tmp) / "repo"
        clone_at_commit(repo, clone_path)
        remove_ignored(repo, clone_path)
        prune_empty_dirs(clone_path)

        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(clone_path), str(target))
            log_repo(repo, f"vendored new tree at {short_commit(repo['commit'])}")
            return target

        if target.is_symlink():
            raise RuntimeError(f"{target} exists but is a symlink")

        if not target.is_dir():
            raise RuntimeError(f"{target} exists but is not a directory")

        remove_ignored(repo, target)
        prune_empty_dirs(target)
        log_repo(repo, "existing tree found; verifying recursive file hashes")
        target_hash, target_entries = hash_folder_recursive(target)
        clone_hash, clone_entries = hash_folder_recursive(clone_path)
        if target_hash != clone_hash:
            difference = first_difference(target_entries, clone_entries)
            raise RuntimeError(
                f"{repo['directory']} already exists but does not match {repo['commit']}: {difference}"
            )

        log_repo(repo, f"verified existing tree matches {short_commit(repo['commit'])}")
        return target


def build_repo(repo, programs_dir):
    target = vendor_repo(repo, programs_dir)
    command = repo["build_command"]
    log_repo(repo, f"running build command: {command}")
    subprocess.run(command, cwd=target, shell=True, check=True)


def list_repos(repos):
    for repo in repos:
        print(f"{repo['directory']}")
        print(f"  name: {repo['name']}")
        print(f"  source: {repo['source']}")
        print(f"  commit: {repo['commit']}")
        print(f"  build_command: {repo['build_command']}")
        print(f"  ignored: {', '.join(repo['ignored']) if repo['ignored'] else '(none)'}")


def run_many(repos, action):
    failed = False
    for repo in repos:
        try:
            action(repo)
        except Exception as exc:
            failed = True
            print(f"error: {repo['directory']}: {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


def parse_args():
    parser = argparse.ArgumentParser(description="Vendor and build Anchor repositories from repos.json")
    parser.add_argument("command", choices=["list", "fetch", "build"])
    parser.add_argument("--repos-json", default="repos.json", type=Path)
    parser.add_argument("--programs-dir", default="programs", type=Path)
    parser.add_argument("--repo", action="append", default=[], help="directory, name, or source substring to select")
    return parser.parse_args()


def main():
    args = parse_args()
    repos = select_repos(load_repos(args.repos_json), args.repo)

    if args.command == "list":
        list_repos(repos)
        return

    if args.command == "fetch":
        run_many(repos, lambda repo: vendor_repo(repo, args.programs_dir))
        return

    run_many(repos, lambda repo: build_repo(repo, args.programs_dir))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        sys.exit(130)
