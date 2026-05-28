"""Tests for the barsoom example."""

import sys

import pytest

from examples.barsoom import BOOKS, confirm_overwrite, parse_args


class TestModuleImportSideEffects:
    """Test that module import doesn't have global side effects."""

    def test_import_does_not_install_urllib3_hook(self) -> None:
        """Importing barsoom should not install the urllib3 cleanup hook."""
        # Check that the current hook is not the urllib3 suppression hook
        # (pytest may have its own hook installed, which is fine)
        hook_name = getattr(sys.unraisablehook, "__name__", "")
        assert "urllib3" not in hook_name.lower()
        assert "suppress" not in hook_name.lower()


class TestArgumentParsing:
    """Test CLI argument parsing."""

    def test_parse_args_default(self) -> None:
        """Default args have setup=False and no verbose flag."""
        args = parse_args([])
        assert args.setup is False

    def test_parse_args_setup_flag(self) -> None:
        """--setup flag sets setup=True."""
        args = parse_args(["--setup"])
        assert args.setup is True

    def test_parse_args_verify_default_none(self) -> None:
        """--verify defaults to None so config file settings are preserved."""
        args = parse_args([])
        assert args.verify is None

    def test_parse_args_verify_flag(self) -> None:
        """--verify flag sets verify=True."""
        args = parse_args(["--verify"])
        assert args.verify is True

    def test_parse_args_model_default_none(self) -> None:
        """--model defaults to None so env var is used."""
        args = parse_args([])
        assert args.model is None

    def test_parse_args_model_flag(self) -> None:
        """--model flag sets model name."""
        args = parse_args(["--model", "gpt-4o"])
        assert args.model == "gpt-4o"

    def test_parse_args_no_verbose_flag(self) -> None:
        """--verbose flag has been removed (TUI info bar replaces it)."""
        assert not hasattr(parse_args([]), "verbose")

    def test_parse_args_force_default(self) -> None:
        """Default args have force=False."""
        args = parse_args([])
        assert args.force is False

    def test_parse_args_force_flag(self) -> None:
        """--force flag sets force=True."""
        args = parse_args(["--force"])
        assert args.force is True


class TestConfirmOverwrite:
    """Tests for the --setup overwrite confirmation gate."""

    def test_force_returns_true_without_prompting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--force skips the prompt entirely and returns True."""

        def fail_input(_prompt: str) -> str:
            raise AssertionError("input() must not be called when force=True")

        monkeypatch.setattr("builtins.input", fail_input)
        assert confirm_overwrite("barsoom", force=True, interactive=True) is True

    def test_non_interactive_without_force_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-interactive run (e.g. --prompt) without --force must exit, not hang."""
        with pytest.raises(SystemExit) as exc_info:
            confirm_overwrite("barsoom", force=False, interactive=False)
        assert exc_info.value.code != 0
        # The message must name the project and point at --force as the escape hatch.
        captured = capsys.readouterr()
        all_output = captured.out + captured.err
        assert "barsoom" in all_output
        assert "--force" in all_output

    def test_interactive_yes_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Typing 'y' at the prompt returns True."""
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        assert confirm_overwrite("barsoom", force=False, interactive=True) is True

    def test_interactive_yes_uppercase_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Typing 'Y' (uppercase) returns True."""
        monkeypatch.setattr("builtins.input", lambda _prompt: "Y")
        assert confirm_overwrite("barsoom", force=False, interactive=True) is True

    def test_interactive_empty_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty response (bare Enter) defaults to No, matching the [y/N] hint."""
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        assert confirm_overwrite("barsoom", force=False, interactive=True) is False

    def test_interactive_no_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Typing 'n' returns False."""
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert confirm_overwrite("barsoom", force=False, interactive=True) is False


class TestBooksMapping:
    """Test the BOOKS constant."""

    def test_books_has_seven_entries(self) -> None:
        """BOOKS maps 7 filenames to titles."""
        assert len(BOOKS) == 7

    def test_books_filenames_match_pattern(self) -> None:
        """All filenames follow barsoom-N.txt pattern."""
        for filename in BOOKS:
            assert filename.startswith("barsoom-")
            assert filename.endswith(".txt")

    def test_books_has_princess_of_mars(self) -> None:
        """First book is A Princess of Mars."""
        assert BOOKS["barsoom-1.txt"] == "A Princess of Mars"
