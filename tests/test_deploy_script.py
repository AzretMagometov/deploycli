"""Тесты каркаса скрипта деплоя: замок, порядок шагов, обёртка compose.

Golden-файлы в ``tests/fixtures/deploy_script/*.sh`` написаны вручную по
ADR-008, ADR-009 и ADR-012, до реализации: они фиксируют требуемый вывод,
а не то, что случайно выдал шаблон.

Шаги, которые пишут другие тикеты, присутствуют здесь только заголовком
секции: заголовки и есть предмет этого тикета — последовательность шагов.
Утверждения про относительный порядок чужих шагов (копия до миграции,
гейт после миграции, строка журнала до ``up``) доводят тикеты-владельцы,
когда положат свои команды.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deploycli.deploy_script import DeployPlan, MigrationStep, render_deploy_script
from deploycli.traefik_names import InvalidNameError

FIXTURES = Path(__file__).parent / "fixtures" / "deploy_script"

_SECTION = re.compile(r"^# --- (?P<title>.+) ---$", re.MULTILINE)

NO_MIGRATION = DeployPlan(stack="blog")
MIGRATION = DeployPlan(stack="blog", migration=MigrationStep())
MIGRATION_AND_BACKUP = DeployPlan(stack="blog", migration=MigrationStep(backup=True))

ALL_PLANS = (NO_MIGRATION, MIGRATION, MIGRATION_AND_BACKUP)


def _golden(name: str) -> str:
    return (FIXTURES / name).read_text()


def _sections(script: str) -> tuple[str, ...]:
    return tuple(match.group("title") for match in _SECTION.finditer(script))


def test_plan_without_migration_matches_golden() -> None:
    assert render_deploy_script(NO_MIGRATION) == _golden("no_migration.sh")


def test_plan_with_migration_matches_golden() -> None:
    assert render_deploy_script(MIGRATION) == _golden("with_migration.sh")


def test_plan_with_migration_and_backup_matches_golden() -> None:
    assert render_deploy_script(MIGRATION_AND_BACKUP) == _golden("with_migration_and_backup.sh")


def test_steps_stand_in_the_decided_order() -> None:
    assert _sections(render_deploy_script(MIGRATION_AND_BACKUP)) == (
        "Замок",
        "Предполётная проверка",
        "Резервная копия",
        "Миграция",
        "Запуск новой версии: гейт готовности и сквозная проверка",
        "Запись деплоя и строка журнала",
        "Очистка образов",
    )


def test_step_without_answer_is_absent_entirely() -> None:
    # ADR-008: нет ответа про мигрирующий сервис — шага нет вовсе, не пустого,
    # а никакого; то же для шага копии.
    without_migration = render_deploy_script(NO_MIGRATION)
    assert "# --- Миграция ---" not in without_migration
    assert "# --- Резервная копия ---" not in without_migration
    assert _sections(without_migration) == (
        "Замок",
        "Предполётная проверка",
        "Запуск новой версии: гейт готовности и сквозная проверка",
        "Запись деплоя и строка журнала",
        "Очистка образов",
    )

    with_migration = render_deploy_script(MIGRATION)
    assert "# --- Резервная копия ---" not in with_migration
    assert "Резервная копия" not in _sections(with_migration)


def test_backup_stands_immediately_before_migration() -> None:
    sections = _sections(render_deploy_script(MIGRATION_AND_BACKUP))

    assert sections.index("Резервная копия") + 1 == sections.index("Миграция")


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_lock_is_taken_before_every_step(plan: DeployPlan) -> None:
    script = render_deploy_script(plan)

    assert _sections(script)[0] == "Замок"
    assert "flock -n 9" in script


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_lock_refuses_without_waiting_and_names_holder(plan: DeployPlan) -> None:
    script = render_deploy_script(plan)

    # Ожидания нет ни в какой форме: у flock это -n, а не -w с таймаутом.
    assert "flock -w" not in script
    # Отказ печатает строку держателя, а сам держатель пишет её сразу после
    # взятия: pid, контур, SHA и время взятия.
    assert "pid %s, контур '%s', SHA %s, взят %s" in script
    assert 'die "замок ${LOCK_FILE} занят, ждать не буду; держатель — ${holder}"' in script
    assert "date -u '+%Y-%m-%dT%H:%M:%SZ'" in script


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_lock_file_is_opened_without_truncation(plan: DeployPlan) -> None:
    # Открытие только на запись усекло бы файл держателя раньше, чем
    # отказавший запуск успел бы его прочитать.
    script = render_deploy_script(plan)

    assert 'exec 9<>"${LOCK_FILE}"' in script
    assert 'exec 9>"${LOCK_FILE}"' not in script


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_lock_path_travels_to_called_scripts(plan: DeployPlan) -> None:
    # deploy/rollback.sh берёт тот же замок и при вызове из деплоя повторно его
    # не берёт (ADR-009, dpc-tm3.36) — форма замка обязана это позволять.
    assert 'export DEPLOYCLI_LOCK_HELD="${LOCK_FILE}"' in render_deploy_script(plan)


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_compose_wrapper_carries_project_directory_and_release_files(
    plan: DeployPlan,
) -> None:
    script = render_deploy_script(plan)

    assert '--project-directory "${STACK_DIR}"' in script
    assert "readonly STACK_DIR='/opt/blog'" in script
    assert '-f "${RELEASE_DIR}/docker-compose.yml"' in script
    assert '-f "${RELEASE_DIR}/docker-compose.prod.yml"' in script
    assert 'readonly RELEASE_DIR="${STACK_DIR}/releases/${COMMIT_SHA}"' in script


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_every_compose_call_goes_through_the_wrapper(plan: DeployPlan) -> None:
    script = render_deploy_script(plan)
    calls = [
        line
        for line in script.splitlines()
        if "docker compose" in line and not line.lstrip().startswith("#")
    ]

    assert calls == ["  docker compose \\"]


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_registry_commands_are_absent(plan: DeployPlan) -> None:
    # Сессия реестра — отдельное подключение ssh (dpc-tm3.61, dpc-tm3.63): у
    # одного вызова ssh один stdin, и здесь он занят файлом окружения.
    script = render_deploy_script(plan)
    commands = [
        line
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
        and any(
            command in line
            for command in ("docker login", "docker pull", "docker logout", "compose pull")
        )
    ]

    assert commands == []


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_script_is_bash_with_strict_flags(plan: DeployPlan) -> None:
    # ADR-012: на проде /bin/sh — dash, а без pipefail конвейер теряет код ssh.
    lines = render_deploy_script(plan).splitlines()

    assert lines[0] == "#!/bin/bash"
    assert "set -euo pipefail" in lines


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_script_never_traces_execution(plan: DeployPlan) -> None:
    # ADR-012: set -x запрещён в блоках, работающих с содержимым окружения;
    # в каркасе его нет вовсе, а запрет назван комментарием заголовка.
    traced = [
        line
        for line in render_deploy_script(plan).splitlines()
        if "set -x" in line and not line.lstrip().startswith("#")
    ]

    assert traced == []


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_script_reads_no_forge_runner_variables(plan: DeployPlan) -> None:
    # ADR-007: ручной запуск на хосте обязан давать тот же результат, что
    # запуск через форжу, поэтому скрипт не читает переменных раннера.
    script = render_deploy_script(plan)

    for name in ("GITHUB_", "FORGEJO_", "GITEA_", "CI", "RUNNER_"):
        assert f"${{{name}" not in script


@pytest.mark.parametrize("plan", ALL_PLANS)
def test_rendered_script_passes_bash_syntax_check(plan: DeployPlan, tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash нужен для проверки синтаксиса сгенерированного скрипта"
    script_path = tmp_path / "deploy.sh"
    script_path.write_text(render_deploy_script(plan))

    result = subprocess.run([bash, "-n", str(script_path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_stack_name_outside_alphabet_is_refused() -> None:
    # Имя стека становится сегментом пути /opt/<имя стека> в каждом вызове
    # compose; алфавит тот же, что у имён в пространстве имён стека.
    with pytest.raises(InvalidNameError):
        render_deploy_script(DeployPlan(stack="Not_In_Alphabet"))


def test_rendering_is_deterministic_across_repeated_calls() -> None:
    assert render_deploy_script(MIGRATION_AND_BACKUP) == render_deploy_script(MIGRATION_AND_BACKUP)
