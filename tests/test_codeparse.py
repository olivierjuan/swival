"""Tests for swival.codeparse — shared lexical helpers."""

from swival.codeparse import (
    mask_noncode,
    redact_string_contents,
    strip_comments,
)

_SAMPLES = {
    "c_block_comment": (
        "#include <x.h>\n"
        "/* a long block comment\n"
        "   spanning lines with a } brace\n"
        "   and a fake call evil(1) */\n"
        "int helper(int a) {\n"
        "    return a;\n"
        "}\n"
    ),
    "js_mixed": (
        'import x from "./x";\n'
        "// line comment with danger()\n"
        "const re = /call(.*)/g;\n"
        'const s = "brace { and // slashes";\n'
        "function f() { return x(); }\n"
    ),
    "python_triple": (
        'DOC = """multi\nline { doc } with call(1)\n"""\ndef f():\n    return DOC\n'
    ),
    "zig": ('const std = @import("std");\n// comment\npub fn main() void {}\n'),
    "empty": "",
    "no_trailing_newline": "int f() { return 1; }",
}


class TestMaskNoncode:
    def test_core_invariants_on_varied_inputs(self):
        for name, original in _SAMPLES.items():
            masked = mask_noncode(original)
            assert len(masked) == len(original), name
            assert masked.count("\n") == original.count("\n"), name

    def test_block_comment_before_function_preserves_line_numbers(self):
        src = _SAMPLES["c_block_comment"]
        masked = mask_noncode(src)
        lines = masked.splitlines()
        # The declaration is on line 5 in the original and must stay there.
        assert src.splitlines()[4].startswith("int helper")
        assert lines[4].startswith("int helper")
        # The comment's brace and fake call are gone.
        assert "}" not in lines[1] + lines[2] + lines[3]
        assert "evil" not in masked

    def test_string_interiors_blanked_delimiters_kept(self):
        masked = mask_noncode('const s = "brace { and // slashes";\n')
        assert '"' in masked
        assert "{" not in masked
        assert "//" not in masked
        assert "brace" not in masked

    def test_line_comment_blanked(self):
        masked = mask_noncode("a();\n// danger()\nb();\n")
        assert "danger" not in masked
        assert "a();" in masked
        assert "b();" in masked

    def test_regex_literal_blanked(self):
        masked = mask_noncode("const re = /call(.*)/g;\n")
        assert "call" not in masked

    def test_triple_quoted_string_keeps_newlines(self):
        src = _SAMPLES["python_triple"]
        masked = mask_noncode(src)
        assert masked.count("\n") == src.count("\n")
        assert "doc" not in masked
        assert "def f():" in masked

    def test_hash_comments_left_alone(self):
        # `#` carries semantic weight in C/C++ (#include, #define), so it is
        # deliberately not treated as a comment marker.
        src = "#include <x.h>\n# call_in_comment()\n"
        assert mask_noncode(src) == src


class TestStripComments:
    def test_removes_block_and_line_comments(self):
        out = strip_comments("a; /* gone */ b; // also gone\nc;")
        assert "gone" not in out
        assert "a;" in out
        assert "b;" in out
        assert "c;" in out

    def test_keeps_markers_inside_strings(self):
        out = strip_comments("s = \"/* keep */\"; t = '// keep';")
        assert out.count("keep") == 2


class TestRedactStringContents:
    def test_interiors_become_spaces_offsets_stable(self):
        src = 'before "abc" after'
        out = redact_string_contents(src)
        assert len(out) == len(src)
        assert "abc" not in out
        assert out.startswith("before ")
        assert out.endswith(" after")
