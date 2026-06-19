"""`swival skills` — add, delete, and list agent skills.

This drives a small management CLI that is dispatched before the main task
parser (see agent.main). Skills live in three locations:

  - project active:  <project>/.swival/skills/
  - global active:   ~/.config/swival/skills/
  - skills library:  ~/.config/swival/library/skills/   (staging, not discovered)

`add <URL>` fetches a git repository and installs the skills under its
`skills/` directory (or a single skill at the repo root); `add <name>` installs
a skill or collection that already lives in the library. The library is two
levels deep — `library/skills/<collection>/<skill>/SKILL.md`.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, fmt, skills

_GIT_MISSING = "git is required to fetch skills from a URL — install git and retry"
_GIT_TIMEOUT = 120


class SkillsCliError(Exception):
    """A user-facing error; run() prints it and returns a non-zero exit code."""


# --- argument parsing -------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swival skills",
        description="Add, delete, and list agent skills.",
    )
    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="Install a skill from a URL or the library.")
    add.add_argument(
        "target",
        help="A git/GitHub URL, or the name of a library skill or collection.",
    )
    add.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help=(
            "Act on global config instead of the project. With a URL, stages "
            "into the global skills library (does NOT activate — run "
            "'skills add <name>' afterward to install)."
        ),
    )
    add.add_argument(
        "--as",
        dest="as_name",
        metavar="NAME",
        help="Override the library collection name (only with 'add --global <URL>').",
    )
    add.add_argument(
        "--ref",
        metavar="REF",
        help="git branch, tag, or commit to fetch (default: repo default branch).",
    )
    add.add_argument(
        "--force",
        action="store_true",
        help="Replace existing skills/collection instead of skipping.",
    )

    delete = sub.add_parser("delete", help="Remove an installed skill.")
    delete.add_argument("name", help="Skill name (or collection/skill with --library).")
    delete.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help="Delete from the global active skills directory.",
    )
    delete.add_argument(
        "--library",
        dest="use_library",
        action="store_true",
        help="Delete from the skills library instead of an active location.",
    )

    lst = sub.add_parser("list", help="List installed or staged skills.")
    lst.add_argument(
        "--library",
        dest="use_library",
        action="store_true",
        help="List staged library collections instead of active skills.",
    )

    return parser


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "add":
            return _cmd_add(args)
        if args.cmd == "delete":
            return _cmd_delete(args)
        if args.cmd == "list":
            return _cmd_list(args)
        parser.print_help(sys.stderr)
        return 1
    except SkillsCliError as e:
        fmt.error(str(e))
        return 1


# --- shared helpers ---------------------------------------------------------


def _project_base() -> Path:
    return config.find_project_root(Path.cwd())


def _is_skill_dir(entry: Path) -> bool:
    """A skill directory is a real directory containing a SKILL.md file."""
    return entry.is_dir() and (entry / "SKILL.md").is_file()


def _find_skill_across_collections(name: str, lib: Path) -> list[tuple[str, Path]]:
    """Return (collection, dir) for every collection holding a skill *name*."""
    matches: list[tuple[str, Path]] = []
    if not lib.is_dir():
        return matches
    for coll in sorted(lib.iterdir()):
        if not coll.is_dir() or coll.name.startswith("."):
            continue
        entry = coll / name
        if _is_skill_dir(entry):
            matches.append((coll.name, entry))
    return matches


def _require_safe_component(name: str, what: str) -> None:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\0" in name
        or not re.match(r"^[A-Za-z0-9._-]+$", name)
    ):
        raise SkillsCliError(f"invalid {what}: {name!r}")


def _tree_has_symlink(root: Path) -> bool:
    """True if the skill directory itself, or anything inside it, is a symlink.

    `.git` is pruned so a cloned repo's internal links don't trip the check.
    """
    if root.is_symlink():
        return True
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for entry in list(dirnames) + filenames:
            if os.path.islink(os.path.join(dirpath, entry)):
                return True
    return False


def _validate_candidate(
    src: Path, expected_dir_name: str | None
) -> tuple[str | None, str | None]:
    """Validate a candidate skill directory.

    Returns (skill_name, None) on success or (None, error). When
    *expected_dir_name* is None (single-skill repo root), the frontmatter name
    is validated against itself — the normalized directory it will live in.
    """
    # Reject symlinks before touching any file, so a symlinked SKILL.md (or a
    # symlinked skill directory) can't be followed and read prior to the check.
    if _tree_has_symlink(src):
        return None, "contains a symlink, which is not allowed in a skill"
    skill_md = src / "SKILL.md"
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, f"cannot read SKILL.md: {e}"
    parsed = skills.parse_frontmatter(content)
    if isinstance(parsed, str):
        return None, f"invalid SKILL.md frontmatter: {parsed}"
    name = parsed["name"]
    dir_name = expected_dir_name if expected_dir_name is not None else name
    err = skills.validate_skill_name(name, dir_name)
    if err:
        return None, err
    return name, None


def _remove_path(p: Path) -> None:
    """Remove a file, symlink, or directory tree if it exists."""
    if p.is_symlink() or p.is_file():
        p.unlink(missing_ok=True)
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


def _atomic_replace(tmp: Path, dest: Path) -> bool:
    """Move *tmp* into place at *dest*, atomically replacing any existing dest.

    Returns True if an existing dest was replaced. The brief dest→old→new window
    is the cross-platform limit of directory replacement; each rename is atomic.
    If the second rename fails after the old dest was moved aside, the old dest
    is restored, so *dest* is never left missing.
    """
    if not dest.exists():
        os.replace(tmp, dest)
        return False
    old = dest.parent / f".{dest.name}.old-{os.getpid()}"
    _remove_path(old)
    os.replace(dest, old)
    try:
        os.replace(tmp, dest)
    except OSError:
        if old.exists() and not dest.exists():
            os.replace(old, dest)  # put the original back
        raise
    _remove_path(old)
    return True


def _atomic_install(src: Path, dest_root: Path, name: str, force: bool) -> str:
    """Install one skill dir into dest_root/name. Returns installed/replaced/skipped."""
    dest = dest_root / name
    if dest.exists() and not force:
        return "skipped"
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = dest_root / f".{name}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp, symlinks=False, ignore=shutil.ignore_patterns(".git"))
        return "replaced" if _atomic_replace(tmp, dest) else "installed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _notice_metaskill(skill_dir: Path, name: str) -> None:
    if (skill_dir / "SKILL.star").is_file():
        fmt.info(
            f"note: skill {name!r} ships a SKILL.star metaskill (executable code); "
            "it will not run unless you enable metaskills"
        )


def _install_candidates(
    candidates: list[tuple[str, Path]],
    dest_root: Path,
    force: bool,
    where: str,
) -> int:
    installed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for name, src in candidates:
        try:
            status = _atomic_install(src, dest_root, name, force)
        except OSError as e:
            fmt.warning(f"failed to install {name!r}: {e}")
            failed.append(name)
            continue
        if status == "skipped":
            fmt.warning(
                f"skill {name!r} already exists in {dest_root}; use --force to replace"
            )
            skipped.append(name)
        else:
            installed.append(name)
            _notice_metaskill(dest_root / name, name)

    if installed:
        print(
            f"Installed {len(installed)} skill(s) into {where} ({dest_root}): "
            f"{', '.join(installed)}"
        )
    if installed:
        return 0
    # Nothing installed: all-already-present (skipped, no failures) is still a
    # success; anything else (a failure, or no candidates at all) is an error.
    if skipped and not failed:
        return 0
    return 1


# --- URL handling -----------------------------------------------------------


def _looks_like_url(s: str) -> bool:
    """Conservatively decide whether an argument is a URL rather than a name."""
    if s.startswith(("http://", "https://", "ssh://", "git://")):
        return True
    if re.match(r"^[^/@]+@[^/:]+:", s):  # git@host:owner/repo
        return True
    if "://" in s:
        return True
    # host/owner/repo shorthand: only a URL if the first component is host-like
    if "/" in s and " " not in s:
        first = s.split("/", 1)[0]
        if "." in first:
            return True
    return False


def _normalize_url(url: str) -> str:
    if url.startswith(("http://", "https://", "ssh://", "git://")) or url.startswith(
        "git@"
    ):
        return url
    return "https://" + url


def _collection_name_from_url(url: str) -> str:
    if url.startswith("git@"):
        path = url.partition(":")[2]
    else:
        path = urllib.parse.urlsplit(url).path
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return base


def _check_http_safety(url: str) -> None:
    """Reject http(s) URLs that resolve to private/internal addresses (SSRF)."""
    if not url.startswith(("http://", "https://")):
        return
    from .fetch import _check_url_safety

    err = _check_url_safety(url)
    if err:
        if err.startswith("error: "):
            err = err[len("error: ") :]
        raise SkillsCliError(err)


def _clear_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _clone_repo(url: str, ref: str | None, dest: str) -> str:
    """Shallow-clone *url* (optionally at *ref*) into *dest*; return the commit SHA."""
    git = shutil.which("git")
    if git is None:
        raise SkillsCliError(_GIT_MISSING)
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:ssh:git",
    }
    common = ["-c", "http.followRedirects=false", "-c", "credential.helper="]

    def git_run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [git, *common, *args],
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except FileNotFoundError:
            raise SkillsCliError(_GIT_MISSING)
        except subprocess.TimeoutExpired:
            raise SkillsCliError(f"git timed out fetching {url}")

    clone_args = ["clone", "--depth", "1", "--no-tags"]
    if ref:
        clone_args += ["--branch", ref]
    clone_args += ["--", url, dest]
    proc = git_run(clone_args)

    if proc.returncode != 0:
        if ref:
            # --branch rejects commit SHAs; fall back to init + fetch + checkout.
            _clear_dir(Path(dest))
            steps = [
                (["init", "-q", dest], None),
                (["remote", "add", "origin", url], dest),
                (["fetch", "--depth", "1", "origin", ref], dest),
                (["checkout", "-q", "FETCH_HEAD"], dest),
            ]
            for args, cwd in steps:
                p = git_run(args, cwd=cwd)
                if p.returncode != 0:
                    msg = (p.stderr or p.stdout or "").strip()
                    raise SkillsCliError(f"git could not fetch ref {ref!r}: {msg}")
        else:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise SkillsCliError(f"git clone failed: {msg}")

    head = git_run(["rev-parse", "HEAD"], cwd=dest)
    return head.stdout.strip() if head.returncode == 0 else ""


def _collect_url_candidates(clone: Path) -> list[tuple[str, Path]]:
    skills_dir = clone / "skills"
    candidates: list[tuple[str, Path]] = []
    # A symlinked skills/ dir would resolve outside the clone — refuse it.
    if skills_dir.is_symlink():
        raise SkillsCliError("repository 'skills' is a symlink, which is not allowed")
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_symlink() or not _is_skill_dir(entry):
                continue
            name, err = _validate_candidate(entry, entry.name)
            if err:
                fmt.warning(f"skipping skill {entry.name!r}: {err}")
                continue
            candidates.append((name, entry))
    elif (clone / "SKILL.md").is_file():
        name, err = _validate_candidate(clone, None)
        if err:
            raise SkillsCliError(f"invalid skill at repository root: {err}")
        candidates.append((name, clone))
    else:
        raise SkillsCliError(
            "no 'skills/' directory or SKILL.md found in the repository"
        )
    return candidates


# --- TOML provenance --------------------------------------------------------


def _toml_str(s: str) -> str:
    return '"' + config._toml_escape(s) + '"'


def _write_origin(coll_dir: Path, url: str, ref: str | None, commit: str) -> None:
    lines = [f"url = {_toml_str(url)}"]
    if ref:
        lines.append(f"ref = {_toml_str(ref)}")
    if commit:
        lines.append(f"commit = {_toml_str(commit)}")
    lines.append(f"fetched_at = {_toml_str(datetime.now(timezone.utc).isoformat())}")
    (coll_dir / ".origin.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_origin(coll: Path) -> str | None:
    p = coll / ".origin.toml"
    if not p.is_file():
        return None
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    url = data.get("url")
    if not url:
        return None
    ref = data.get("ref")
    return f"{url} @ {ref}" if ref else url


# --- add --------------------------------------------------------------------


def _cmd_add(args) -> int:
    target = args.target
    ref = args.ref
    is_url = _looks_like_url(target)

    if is_url and not ref and "#" in target:
        target, _, ref = target.rpartition("#")

    if args.as_name and not (is_url and args.is_global):
        raise SkillsCliError(
            "--as is only valid with 'skills add --global <URL>' (it names the "
            "library collection)"
        )

    if is_url:
        return _add_from_url(target, ref, args.is_global, args.force, args.as_name)

    if ref:
        raise SkillsCliError("--ref is only valid when adding from a URL")
    return _add_from_name(target, args.is_global, args.force)


def _add_from_url(
    url: str, ref: str | None, is_global: bool, force: bool, as_name: str | None
) -> int:
    norm = _normalize_url(url)
    _check_http_safety(norm)

    collection = as_name or _collection_name_from_url(norm)
    _require_safe_component(collection, "collection name (use --as to override)")

    tmp_clone = tempfile.mkdtemp(prefix="swival-skills-")
    try:
        commit = _clone_repo(norm, ref, tmp_clone)
        candidates = _collect_url_candidates(Path(tmp_clone))
        if not candidates:
            raise SkillsCliError(f"no valid skills found in {url}")
        if is_global:
            return _stage_collection(collection, candidates, norm, ref, commit, force)
        dest = skills.project_skills_dir(_project_base())
        return _install_candidates(candidates, dest, force, "this project")
    finally:
        shutil.rmtree(tmp_clone, ignore_errors=True)


def _stage_collection(
    collection: str,
    candidates: list[tuple[str, Path]],
    url: str,
    ref: str | None,
    commit: str,
    force: bool,
) -> int:
    lib = skills.skills_library_dir()
    dest_coll = lib / collection
    if dest_coll.exists() and not force:
        raise SkillsCliError(
            f"collection {collection!r} is already staged in the library; "
            "use --force to replace it"
        )
    lib.mkdir(parents=True, exist_ok=True)
    tmp_coll = lib / f".{collection}.tmp-{os.getpid()}"
    shutil.rmtree(tmp_coll, ignore_errors=True)
    try:
        tmp_coll.mkdir()
        for name, src in candidates:
            shutil.copytree(
                src,
                tmp_coll / name,
                symlinks=False,
                ignore=shutil.ignore_patterns(".git"),
            )
        _write_origin(tmp_coll, url, ref, commit)
        _atomic_replace(tmp_coll, dest_coll)
    finally:
        shutil.rmtree(tmp_coll, ignore_errors=True)

    names = ", ".join(name for name, _ in candidates)
    print(f"Staged {len(candidates)} skill(s) into the library ({dest_coll}):")
    print(f"  {names}")
    print(f"Run 'swival skills add {collection}' to install them into this project,")
    print(f"or 'swival skills add --global {collection}' to install them globally.")
    return 0


def _add_from_name(name: str, is_global: bool, force: bool) -> int:
    lib = skills.skills_library_dir()
    if not lib.is_dir():
        raise SkillsCliError(
            f"the skills library is empty ({lib}); stage a collection first with "
            "'swival skills add --global <URL>'"
        )
    candidates = _resolve_library_skills(name, lib)
    if is_global:
        dest = skills.global_skills_dir()
        where = "global skills"
    else:
        dest = skills.project_skills_dir(_project_base())
        where = "this project"
    return _install_candidates(candidates, dest, force, where)


def _resolve_library_skills(name: str, lib: Path) -> list[tuple[str, Path]]:
    if "/" in name:
        coll, _, skill = name.partition("/")
        _require_safe_component(coll, "collection")
        _require_safe_component(skill, "skill name")
        src = lib / coll / skill
        if not _is_skill_dir(src):
            raise SkillsCliError(
                f"skill {skill!r} not found in library collection {coll!r}"
            )
        vname, err = _validate_candidate(src, skill)
        if err:
            raise SkillsCliError(f"invalid skill {name!r}: {err}")
        return [(vname, src)]

    _require_safe_component(name, "name")

    coll_dir = lib / name
    if coll_dir.is_dir():
        out: list[tuple[str, Path]] = []
        for entry in sorted(coll_dir.iterdir()):
            if not _is_skill_dir(entry):
                continue
            vname, err = _validate_candidate(entry, entry.name)
            if err:
                fmt.warning(f"skipping skill {entry.name!r}: {err}")
                continue
            out.append((vname, entry))
        if not out:
            raise SkillsCliError(f"collection {name!r} contains no valid skills")
        return out

    matches = _find_skill_across_collections(name, lib)
    if not matches:
        raise SkillsCliError(
            f"{name!r} not found in the skills library "
            "(neither a collection nor a skill)"
        )
    if len(matches) > 1:
        locs = ", ".join(f"{coll}/{name}" for coll, _ in matches)
        raise SkillsCliError(
            f"skill {name!r} is ambiguous across collections: {locs}; "
            "disambiguate with 'collection/skill'"
        )
    _, entry = matches[0]
    vname, err = _validate_candidate(entry, name)
    if err:
        raise SkillsCliError(f"invalid skill {name!r}: {err}")
    return [(vname, entry)]


# --- delete -----------------------------------------------------------------


def _safe_delete(target: Path, root: Path) -> None:
    if target.is_symlink():
        raise SkillsCliError(f"refusing to delete symlink {target}")
    if not target.is_dir():
        raise SkillsCliError(f"not found: {target}")
    root_r = root.resolve()
    target_r = target.resolve()
    if target_r == root_r or not target_r.is_relative_to(root_r):
        raise SkillsCliError(f"refusing to delete {target} (outside {root})")
    shutil.rmtree(target_r)


def _cmd_delete(args) -> int:
    name = args.name
    if args.use_library:
        return _delete_library(name)

    if "/" in name:
        raise SkillsCliError(
            "'collection/skill' paths require --library; active skills are "
            "addressed by name only"
        )
    _require_safe_component(name, "skill name")
    root = (
        skills.global_skills_dir()
        if args.is_global
        else skills.project_skills_dir(_project_base())
    )
    _safe_delete(root / name, root)
    print(f"Removed skill {name!r} from {root}")
    return 0


def _delete_library(name: str) -> int:
    lib = skills.skills_library_dir()
    if "/" in name:
        coll, _, skill = name.partition("/")
        _require_safe_component(coll, "collection")
        _require_safe_component(skill, "skill name")
        _safe_delete(lib / coll / skill, lib)
        print(f"Removed skill {coll}/{skill} from the library")
        return 0

    _require_safe_component(name, "name")
    coll_dir = lib / name
    if coll_dir.is_dir():
        _safe_delete(coll_dir, lib)
        print(f"Removed collection {name!r} from the library")
        return 0

    matches = _find_skill_across_collections(name, lib)
    if not matches:
        raise SkillsCliError(f"{name!r} not found in the skills library")
    if len(matches) > 1:
        locs = ", ".join(f"{coll}/{name}" for coll, _ in matches)
        raise SkillsCliError(
            f"{name!r} is ambiguous across collections: {locs}; "
            "disambiguate with 'collection/skill'"
        )
    coll_name, entry = matches[0]
    _safe_delete(entry, lib)
    print(f"Removed skill {coll_name}/{name} from the library")
    return 0


# --- list -------------------------------------------------------------------


def _short(desc: str) -> str:
    flat = " ".join(desc.split())
    return flat if len(flat) <= 70 else flat[:67] + "..."


def _scan_active(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not _is_skill_dir(entry):
            continue
        try:
            content = (entry / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = skills.parse_frontmatter(content)
        if isinstance(parsed, str):
            out.append((entry.name, "(invalid SKILL.md)"))
        else:
            out.append((parsed["name"], _short(parsed["description"])))
    return out


def _print_active(label: str, root: Path) -> None:
    print(f"{label} ({root}):")
    items = _scan_active(root)
    if not items:
        print("  (none)")
        return
    for name, desc in items:
        print(f"  {name} — {desc}")


def _cmd_list(args) -> int:
    if args.use_library:
        return _list_library()
    _print_active("project skills", skills.project_skills_dir(_project_base()))
    _print_active("global skills", skills.global_skills_dir())
    return 0


def _list_library() -> int:
    lib = skills.skills_library_dir()
    print(f"skills library ({lib}):")
    if not lib.is_dir():
        print("  (empty)")
        return 0
    colls = [
        d for d in sorted(lib.iterdir()) if d.is_dir() and not d.name.startswith(".")
    ]
    if not colls:
        print("  (empty)")
        return 0
    for coll in colls:
        origin = _read_origin(coll)
        suffix = f"  [{origin}]" if origin else ""
        print(f"  {coll.name}{suffix}")
        for entry in sorted(coll.iterdir()):
            if _is_skill_dir(entry):
                print(f"    {entry.name}")
    return 0
