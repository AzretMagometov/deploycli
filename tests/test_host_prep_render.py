"""Сборка скрипта подготовки хоста: подстановка, проверки входа, чистота текста.

Скрипт уезжает на чужую машину и исполняется там под root, поэтому его
текст проверяется до прогона: синтаксис — самим bash, качество —
shellcheck, а значения, пришедшие подстановкой, — проверками алфавита из
ADR-003. Поведение того же текста проверяет ``tests/test_host_prep_run.py``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from deploycli.cli import main
from deploycli.host_edge import EDGE_IMAGE, InvalidAcmeEmailError, render_host_edge_compose
from deploycli.host_prep import (
    EDGE_COMPOSE_PATH,
    EDGE_PROJECT_DIR,
    STACK_ROOT,
    HostPrepRefused,
    render_prepare_host_script,
)
from deploycli.traefik_names import InvalidNameError
from support.host_stand import BASH

ACME_EMAIL = "ops@example.com"


def _script(*, stack: str = "shop", user: str = "deploy", with_edge: bool = False) -> str:
    return render_prepare_host_script(
        stack=stack,
        deploy_user=user,
        edge_acme_email=ACME_EMAIL if with_edge else None,
    )


def test_script_is_a_bash_script_without_unrendered_placeholders() -> None:
    # Фигурные скобки в скрипте есть, но только шаблонов вывода docker
    # (``--format '{{.Names}}'``); неотрендеренной подстановки Jinja быть
    # не должно ни одной.
    script = _script(with_edge=True)

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "{{ " not in script
    assert "{%" not in script


def test_script_carries_the_stack_and_the_deploy_user_it_was_rendered_for() -> None:
    script = _script(stack="shop", user="deploy")

    assert f"{STACK_ROOT}/shop" in script
    assert "deploy" in script


@pytest.mark.parametrize("stack", ["Shop", "1shop", "shop_prod", "shop prod", "", "shop;rm -rf /"])
def test_stack_name_outside_the_alphabet_stops_the_render(stack: str) -> None:
    with pytest.raises(InvalidNameError):
        _script(stack=stack)


@pytest.mark.parametrize("user", ["Deploy", "deploy user", "deploy'", ""])
def test_deploy_user_name_outside_the_alphabet_stops_the_render(user: str) -> None:
    with pytest.raises(InvalidNameError):
        _script(user=user)


def test_stack_named_like_the_edge_directory_stops_the_render() -> None:
    # Каталог края и каталог стека живут в одном /opt: совпадение имён
    # отдало бы каталог края деплой-пользователю.
    with pytest.raises(HostPrepRefused):
        _script(stack=Path(EDGE_PROJECT_DIR).name)


def test_unusable_acme_email_stops_the_render() -> None:
    with pytest.raises(InvalidAcmeEmailError):
        render_prepare_host_script(stack="shop", deploy_user="deploy", edge_acme_email="ops")


def test_edge_flag_embeds_exactly_the_shared_edge_template() -> None:
    script = _script(with_edge=True)

    assert render_host_edge_compose(ACME_EMAIL) in script
    assert EDGE_COMPOSE_PATH in script


def test_without_the_flag_the_script_knows_nothing_about_the_edge() -> None:
    script = _script()

    assert EDGE_IMAGE not in script
    assert "acme" not in script
    assert ACME_EMAIL not in script


def test_script_carries_no_unfinished_work_markers() -> None:
    script = _script(with_edge=True)

    assert not any(marker in script for marker in ("TODO", "FIXME", "XXX"))


def test_script_passes_the_bash_syntax_check(tmp_path: Path) -> None:
    path = tmp_path / "prepare-host.sh"
    path.write_text(_script(with_edge=True))

    checked = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True, check=False)

    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("with_edge", [False, True])
def test_script_passes_shellcheck(tmp_path: Path, with_edge: bool) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck не найден в PATH")
    path = tmp_path / "prepare-host.sh"
    path.write_text(_script(with_edge=with_edge))

    checked = subprocess.run(
        [shellcheck, "--severity=style", str(path)], capture_output=True, text=True, check=False
    )

    assert checked.returncode == 0, checked.stdout


def test_cli_prints_the_script_for_piping_into_ssh(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["prepare-host", "--stack", "shop", "--deploy-user", "deploy"])

    assert exit_code == 0
    assert capsys.readouterr().out == _script()


def test_cli_edge_flag_without_an_email_names_the_missing_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["prepare-host", "--stack", "shop", "--deploy-user", "deploy", "--with-edge"])

    assert excinfo.value.code != 0
    assert "--acme-email" in capsys.readouterr().err


def test_cli_reports_a_refused_name_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["prepare-host", "--stack", "Shop", "--deploy-user", "deploy"])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "Shop" in captured.err
    assert captured.out == ""
