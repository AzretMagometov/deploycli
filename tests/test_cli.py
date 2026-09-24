"""Tests for the deploycli entry point."""

import pytest

from deploycli.cli import main


def test_help_exits_zero_and_mentions_upcoming_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "scan" in output
    assert "generate" in output


def test_no_args_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "scan" in output
    assert "generate" in output
