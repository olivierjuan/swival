import os
import sys

import pytest

from swival import _env
from swival.agent import resolve_commands
from swival.tools import _run_argv_command, _run_shell_command


def _write_tool(path, text):
    path.write_text(f"#!/bin/sh\necho {text}\n")
    path.chmod(0o755)


@pytest.fixture
def homebrew_path(tmp_path, monkeypatch):
    prefix = tmp_path / "libexec"
    own_bin = prefix / "bin"
    user_bin = tmp_path / "user-bin"
    own_bin.mkdir(parents=True)
    user_bin.mkdir()
    _write_tool(own_bin / "demo-tool", "BUNDLED")
    _write_tool(user_bin / "demo-tool", "USER")

    monkeypatch.setattr(_env.sys, "prefix", str(prefix))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(own_bin), str(user_bin)]))
    return user_bin / "demo-tool"


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell scripts")
def test_unrestricted_argv_uses_sanitized_child_path(tmp_path, homebrew_path):
    result = _run_argv_command(
        ["demo-tool"],
        str(tmp_path),
        {},
        unrestricted=True,
    )

    assert "USER" in result
    assert "BUNDLED" not in result


@pytest.mark.skipif(sys.platform == "win32", reason="uses /bin/sh and POSIX scripts")
def test_shell_command_uses_sanitized_child_path(tmp_path, homebrew_path):
    result = _run_shell_command("demo-tool", str(tmp_path), timeout=30)

    assert "USER" in result
    assert "BUNDLED" not in result


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell scripts")
def test_resolve_commands_uses_sanitized_child_path(tmp_path, homebrew_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    resolved = resolve_commands(["demo-tool"], str(workspace))

    assert resolved["demo-tool"] == str(homebrew_path.resolve())
