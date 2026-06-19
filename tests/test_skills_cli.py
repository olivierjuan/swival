"""Tests for skills_cli.py — the `swival skills` add/delete/list CLI."""

import os
import types
from pathlib import Path

import pytest

from swival import skills_cli
from swival.skills_cli import run


def _write_skill(d: Path, name: str, desc: str = "A test skill.") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated project + global config dirs.

    Returns a namespace with project/global/library Paths. cwd is the project.
    """
    project = tmp_path / "project"
    project.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(project)
    return types.SimpleNamespace(
        project=project,
        project_skills=project / ".swival" / "skills",
        global_skills=xdg / "swival" / "skills",
        library=xdg / "swival" / "library" / "skills",
    )


def _stub_clone(monkeypatch, layout, *, ref_box=None, commit="deadbeef0001"):
    """Replace _clone_repo with a stub that materializes *layout* into dest.

    layout: dict of relpath -> (skill_name, description). Use relpath
    'skills/foo' for nested skills or 'SKILL.md'-bearing root via 'ROOT'.
    """

    def _clone(url, ref, dest):
        if ref_box is not None:
            ref_box.append(ref)
        dest = Path(dest)
        for relpath, info in layout.items():
            if relpath == "ROOT":
                _write_skill(dest, info[0], info[1])
            else:
                _write_skill(dest / relpath, info[0], info[1])
        return commit

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)


# --- URL detection ----------------------------------------------------------


@pytest.mark.parametrize(
    "arg,is_url",
    [
        ("https://github.com/o/r", True),
        ("http://example.com/o/r", True),
        ("git@github.com:o/r.git", True),
        ("ssh://git@host/o/r", True),
        ("git://host/o/r", True),
        ("github.com/o/r", True),
        ("ponytail/deploy", False),
        ("deploy", False),
        ("foo.bar", False),
    ],
)
def test_url_detection(arg, is_url):
    assert skills_cli._looks_like_url(arg) is is_url


def test_collection_name_from_url():
    assert (
        skills_cli._collection_name_from_url("https://github.com/Dietrich/ponytail")
        == "ponytail"
    )
    assert skills_cli._collection_name_from_url("git@github.com:o/repo.git") == "repo"
    assert skills_cli._collection_name_from_url("github.com/o/r/") == "r"


# --- add from URL -----------------------------------------------------------


def test_add_url_into_project(env, monkeypatch):
    _stub_clone(
        monkeypatch,
        {
            "skills/deploy": ("deploy", "Deploy it."),
            "skills/review": ("review", "Review."),
        },
    )
    assert run(["add", "git@github.com:o/ponytail.git"]) == 0
    assert (env.project_skills / "deploy" / "SKILL.md").is_file()
    assert (env.project_skills / "review" / "SKILL.md").is_file()
    # project install bypasses the library entirely
    assert not env.library.exists()


def test_add_url_global_stages_into_library(env, monkeypatch, capsys):
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "Deploy it.")})
    assert run(["add", "--global", "git@github.com:o/ponytail.git"]) == 0
    coll = env.library / "ponytail"
    assert (coll / "deploy" / "SKILL.md").is_file()
    assert (coll / ".origin.toml").is_file()
    # staging does NOT activate
    assert not env.global_skills.exists()
    out = capsys.readouterr().out
    assert "Staged" in out
    # the hint uses the real collection name, not a <name> placeholder
    assert "swival skills add ponytail" in out
    assert "<name>" not in out


def test_add_url_single_skill_at_repo_root(env, monkeypatch):
    _stub_clone(monkeypatch, {"ROOT": ("solo", "A solo skill.")})
    assert run(["add", "git@github.com:o/solo-repo.git"]) == 0
    assert (env.project_skills / "solo" / "SKILL.md").is_file()


def test_add_url_root_skill_dirname_differs_from_name(env, monkeypatch):
    # repo dir name is irrelevant; the skill is validated against its own name.
    _stub_clone(monkeypatch, {"ROOT": ("realname", "desc")})
    assert run(["add", "git@github.com:o/totally-different.git"]) == 0
    assert (env.project_skills / "realname").is_dir()


def test_add_url_no_skills_found(env, monkeypatch, capsys):
    def _clone(url, ref, dest):
        Path(dest, "README.md").write_text("nothing here", encoding="utf-8")
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    assert run(["add", "git@github.com:o/empty.git"]) == 1


def test_add_url_inline_ref(env, monkeypatch):
    ref_box: list = []
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d")}, ref_box=ref_box)
    assert run(["add", "git@github.com:o/r.git#dev"]) == 0
    assert ref_box == ["dev"]


def test_add_url_ref_flag(env, monkeypatch):
    ref_box: list = []
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d")}, ref_box=ref_box)
    assert run(["add", "--ref", "v1", "git@github.com:o/r.git"]) == 0
    assert ref_box == ["v1"]


# --- symlink / validation safety -------------------------------------------


def test_symlink_inside_skill_is_rejected(env, monkeypatch, capsys):
    secret = env.project / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    def _clone(url, ref, dest):
        d = _write_skill(Path(dest) / "skills" / "evil", "evil", "evil skill")
        os.symlink(secret, d / "leak.txt")
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    # the only skill is rejected → nothing valid → exit 1
    assert run(["add", "git@github.com:o/evil.git"]) == 1
    assert not (env.project_skills / "evil").exists()


def test_symlinked_skill_md_in_subdir_rejected(env, monkeypatch):
    secret = env.project / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    def _clone(url, ref, dest):
        d = Path(dest) / "skills" / "evil"
        d.mkdir(parents=True)
        os.symlink(secret, d / "SKILL.md")  # SKILL.md itself is a symlink
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    assert run(["add", "git@github.com:o/evil.git"]) == 1
    assert not (env.project_skills / "evil").exists()


def test_symlinked_skill_md_at_repo_root_rejected(env, monkeypatch):
    secret = env.project / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    def _clone(url, ref, dest):
        os.symlink(secret, Path(dest) / "SKILL.md")
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    assert run(["add", "git@github.com:o/evil.git"]) == 1


def test_symlinked_skills_dir_rejected(env, monkeypatch, tmp_path):
    # skills/ points outside the clone — must not be followed.
    outside = tmp_path / "outside"
    _write_skill(outside / "deploy", "deploy", "d")

    def _clone(url, ref, dest):
        os.symlink(outside, Path(dest) / "skills")
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    assert run(["add", "git@github.com:o/evil.git"]) == 1
    assert not (env.project_skills / "deploy").exists()


def test_invalid_skill_skipped_valid_installed(env, monkeypatch):
    def _clone(url, ref, dest):
        # name mismatch -> invalid; good one installs
        _write_skill(Path(dest) / "skills" / "mismatch", "other-name", "x")
        _write_skill(Path(dest) / "skills" / "good", "good", "ok")
        return "sha"

    monkeypatch.setattr(skills_cli, "_clone_repo", _clone)
    assert run(["add", "git@github.com:o/mix.git"]) == 0
    assert (env.project_skills / "good").is_dir()
    assert not (env.project_skills / "mismatch").exists()
    assert not (env.project_skills / "other-name").exists()


# --- add from name (library) -----------------------------------------------


def _seed_library(env, collection="ponytail", skills_=(("deploy", "Deploy."),)):
    for name, desc in skills_:
        _write_skill(env.library / collection / name, name, desc)


def test_add_name_collection_into_project(env):
    _seed_library(env, skills_=(("deploy", "Deploy."), ("review", "Review.")))
    assert run(["add", "ponytail"]) == 0
    assert (env.project_skills / "deploy").is_dir()
    assert (env.project_skills / "review").is_dir()


def test_add_name_single_skill_into_global(env):
    _seed_library(env, skills_=(("deploy", "Deploy."),))
    assert run(["add", "--global", "deploy"]) == 0
    assert (env.global_skills / "deploy").is_dir()


def test_add_name_explicit_collection_slash_skill(env):
    _seed_library(env, skills_=(("deploy", "Deploy."), ("review", "Review.")))
    assert run(["add", "ponytail/review"]) == 0
    assert (env.project_skills / "review").is_dir()
    assert not (env.project_skills / "deploy").exists()


def test_add_name_ambiguous_skill(env):
    _write_skill(env.library / "alpha" / "deploy", "deploy", "a")
    _write_skill(env.library / "beta" / "deploy", "deploy", "b")
    assert run(["add", "deploy"]) == 1


def test_add_name_not_found(env):
    _seed_library(env)
    assert run(["add", "nope"]) == 1


def test_add_name_empty_library(env):
    assert run(["add", "deploy"]) == 1


# --- conflict / force -------------------------------------------------------


def test_add_existing_without_force_skips(env, capsys):
    _seed_library(env)
    assert run(["add", "deploy"]) == 0
    # mark the installed copy so we can detect a (non-)overwrite
    (env.project_skills / "deploy" / "marker").write_text("v1", encoding="utf-8")
    assert run(["add", "deploy"]) == 0  # idempotent
    assert (env.project_skills / "deploy" / "marker").exists()


def test_add_force_replaces(env):
    _seed_library(env)
    run(["add", "deploy"])
    (env.project_skills / "deploy" / "marker").write_text("v1", encoding="utf-8")
    assert run(["add", "--force", "deploy"]) == 0
    assert not (env.project_skills / "deploy" / "marker").exists()


def test_atomic_replace_restores_original_on_rename_failure(env, monkeypatch, tmp_path):
    # An existing install must survive if the swap fails partway through.
    dest_root = env.project_skills
    _write_skill(dest_root / "deploy", "deploy", "old")
    (dest_root / "deploy" / "marker").write_text("ORIGINAL", encoding="utf-8")
    src = _write_skill(tmp_path / "src", "deploy", "new")

    real_replace = os.replace
    n = {"calls": 0}

    def flaky_replace(a, b):
        n["calls"] += 1
        if n["calls"] == 2:  # the tmp -> dest rename
            raise OSError("simulated failure")
        return real_replace(a, b)

    monkeypatch.setattr(skills_cli.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        skills_cli._atomic_install(src, dest_root, "deploy", force=True)

    # dest is still present and is the untouched original
    assert (dest_root / "deploy" / "marker").read_text() == "ORIGINAL"
    # no stray temp/old siblings left behind
    leftovers = [p.name for p in dest_root.iterdir() if p.name.startswith(".deploy.")]
    assert leftovers == []


def test_restage_collection_without_force_fails(env, monkeypatch):
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d")})
    assert run(["add", "--global", "git@github.com:o/ponytail.git"]) == 0
    assert run(["add", "--global", "git@github.com:o/ponytail.git"]) == 1


def test_restage_collection_force_removes_stale_skills(env, monkeypatch):
    _stub_clone(
        monkeypatch,
        {"skills/deploy": ("deploy", "d"), "skills/old": ("old", "stale")},
    )
    assert run(["add", "--global", "git@github.com:o/ponytail.git"]) == 0
    assert (env.library / "ponytail" / "old").is_dir()
    # re-stage with only deploy -> 'old' must be gone
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d2")})
    assert run(["add", "--force", "--global", "git@github.com:o/ponytail.git"]) == 0
    assert (env.library / "ponytail" / "deploy").is_dir()
    assert not (env.library / "ponytail" / "old").exists()


# --- --as scope -------------------------------------------------------------


def test_as_with_url_global_renames_collection(env, monkeypatch):
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d")})
    assert (
        run(["add", "--global", "--as", "pony", "git@github.com:o/ponytail.git"]) == 0
    )
    assert (env.library / "pony" / "deploy").is_dir()
    assert not (env.library / "ponytail").exists()


def test_as_rejected_with_name_source(env):
    _seed_library(env)
    assert run(["add", "--as", "foo", "deploy"]) == 1


def test_as_rejected_with_url_into_project(env, monkeypatch):
    _stub_clone(monkeypatch, {"skills/deploy": ("deploy", "d")})
    assert run(["add", "--as", "foo", "git@github.com:o/r.git"]) == 1


# --- delete -----------------------------------------------------------------


def test_delete_project_skill(env):
    _write_skill(env.project_skills / "deploy", "deploy", "d")
    assert run(["delete", "deploy"]) == 0
    assert not (env.project_skills / "deploy").exists()


def test_delete_global_skill(env):
    _write_skill(env.global_skills / "deploy", "deploy", "d")
    assert run(["delete", "--global", "deploy"]) == 0
    assert not (env.global_skills / "deploy").exists()


def test_delete_active_rejects_slash(env):
    assert run(["delete", "ponytail/deploy"]) == 1


def test_delete_library_collection(env):
    _seed_library(env, skills_=(("deploy", "d"), ("review", "r")))
    assert run(["delete", "--library", "ponytail"]) == 0
    assert not (env.library / "ponytail").exists()


def test_delete_library_collection_slash_skill(env):
    _seed_library(env, skills_=(("deploy", "d"), ("review", "r")))
    assert run(["delete", "--library", "ponytail/review"]) == 0
    assert not (env.library / "ponytail" / "review").exists()
    assert (env.library / "ponytail" / "deploy").is_dir()


def test_delete_missing_is_error(env):
    assert run(["delete", "ghost"]) == 1
    assert run(["delete", "--library", "ghost"]) == 1


def test_delete_traversal_rejected(env):
    _write_skill(env.project_skills / "deploy", "deploy", "d")
    assert run(["delete", "../deploy"]) == 1
    assert run(["delete", ".."]) == 1


# --- list -------------------------------------------------------------------


def test_list_active(env, capsys):
    _write_skill(env.project_skills / "deploy", "deploy", "Deploy it.")
    _write_skill(env.global_skills / "audit", "audit", "Audit it.")
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert "deploy" in out and "audit" in out


def test_list_has_no_global_flag(env):
    # plain `list` already shows global skills, so --global is not offered.
    with pytest.raises(SystemExit):
        run(["list", "--global"])


def test_list_library(env, capsys):
    _seed_library(env, skills_=(("deploy", "d"),))
    assert run(["list", "--library"]) == 0
    out = capsys.readouterr().out
    assert "ponytail" in out and "deploy" in out


# --- SSRF / clone hardening -------------------------------------------------


def test_http_safety_rejects_private(env, monkeypatch):
    import swival.fetch as fetch

    monkeypatch.setattr(
        fetch,
        "_check_url_safety",
        lambda url: (
            "error: url resolves to private/internal address (10.0.0.1), blocked for security"
        ),
    )
    # http URL triggers the safety check before any clone
    assert run(["add", "https://internal.example/o/r"]) == 1


def test_http_safety_message_not_double_prefixed(env, monkeypatch, capsys):
    import swival.fetch as fetch

    monkeypatch.setattr(fetch, "_check_url_safety", lambda url: "error: blocked thing")
    run(["add", "https://internal.example/o/r"])
    err = capsys.readouterr().err
    assert "error: error:" not in err.lower()


def test_clone_uses_hardened_flags_and_env(env, monkeypatch):
    calls: list = []

    import swival.fetch as fetch

    monkeypatch.setattr(fetch, "_check_url_safety", lambda url: None)
    monkeypatch.setattr(skills_cli.shutil, "which", lambda name: "/usr/bin/git")

    def fake_run(
        args, env=None, cwd=None, capture_output=None, text=None, timeout=None
    ):
        calls.append((args, env))
        if "clone" in args:
            dest = args[-1]
            _write_skill(Path(dest) / "skills" / "deploy", "deploy", "d")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in args:
            return types.SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(skills_cli.subprocess, "run", fake_run)
    assert run(["add", "https://github.com/o/r"]) == 0

    clone_args, clone_env = next(c for c in calls if "clone" in c[0])
    assert "-c" in clone_args
    assert "http.followRedirects=false" in clone_args
    assert "--depth" in clone_args and "1" in clone_args
    assert "--no-tags" in clone_args
    assert "--" in clone_args  # argument terminator before url
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"


def test_clone_missing_git(env, monkeypatch):
    monkeypatch.setattr(skills_cli.shutil, "which", lambda name: None)
    assert run(["add", "git@github.com:o/r.git"]) == 1


# --- no subcommand ----------------------------------------------------------


def test_no_subcommand_returns_nonzero(env):
    assert run([]) == 1


# --- discoverability --------------------------------------------------------


def test_skills_documented_in_main_help():
    from swival.agent import build_parser

    help_text = build_parser().format_help()
    assert "Management commands" in help_text
    assert "swival skills add" in help_text
