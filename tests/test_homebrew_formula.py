import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "homebrew-tap" / "Formula" / "swival.rb"


def test_homebrew_installer_tools_are_build_only():
    if not FORMULA.exists():
        pytest.skip("Homebrew tap checkout is not included in the sdist")

    text = FORMULA.read_text()

    runtime_deps = set(re.findall(r'^\s*depends_on "([^"]+)"\s*$', text, re.M))
    build_deps = set(re.findall(r'^\s*depends_on "([^"]+)" => :build\s*$', text, re.M))

    assert {"go", "rust", "uv"} <= build_deps
    assert "rust" not in runtime_deps
    assert "uv" not in runtime_deps
