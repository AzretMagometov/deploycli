"""Замок деплоя на настоящем Linux: взятие, строка держателя, отказ без ожидания.

Golden-файл подтверждает текст скрипта, но не поведение ``flock`` (тот же
довод, которым ``tests/test_traefik_live_stand.py`` поднимает настоящий
Traefik). Здесь сгенерированный скрипт исполняется в контейнере Ubuntu
24.04 — дистрибутива прода по инвентаризации ADR-012.

Стенд занимает замок тем же ``flock``, которым его берёт скрипт, и не
переписывает файл: отказавший запуск обязан назвать держателя, которого
записал настоящий прогон деплоя, а не подставленную стендом строку.
"""

import re

import pytest

from deploycli.deploy_script import DeployPlan, render_deploy_script
from support.linux_host import SCRIPT_PATH, run_on_linux

pytestmark = pytest.mark.docker

PLAN = DeployPlan(stack="blog")
STACK_DIR = "/opt/blog"
LOCK_FILE = f"{STACK_DIR}/.deploy.lock"
CONTOUR = "prod"
SHA = "0123456789abcdef0123456789abcdef01234567"

HOLDER_LINE = re.compile(
    rf"pid \d+, контур 'prod', SHA {SHA}, взят \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z"
)


def test_run_takes_the_lock_and_names_itself_in_it() -> None:
    stand = "\n".join(
        (
            "set +e",
            f"{SCRIPT_PATH} {CONTOUR} {SHA}",
            'echo "деплой вышел с кодом $?"',
            f"cat {LOCK_FILE}",
        )
    )

    result = run_on_linux(render_deploy_script(PLAN), stand, STACK_DIR)

    assert "деплой вышел с кодом 0" in result.stdout.splitlines(), result.stderr
    assert HOLDER_LINE.search(result.stdout) is not None, result.stdout


def test_busy_lock_refuses_naming_holder_and_time_without_waiting() -> None:
    stand = "\n".join(
        (
            "set +e",
            # Настоящий прогон деплоя: берёт замок, называет себя в файле и
            # отпускает замок, выйдя.
            f"{SCRIPT_PATH} {CONTOUR} {SHA}",
            # Стенд занимает замок вместо идущего деплоя, файл не трогая.
            f"exec 9<>{LOCK_FILE}",
            'flock -n 9 || { echo "стенд не смог занять замок" >&2; exit 90; }',
            # timeout ловит ожидание: отказ обязан прийти сразу, а не через
            # окно (ADR-009).
            f"timeout 5 {SCRIPT_PATH} {CONTOUR} {SHA}",
            'echo "второй запуск вышел с кодом $?"',
        )
    )

    result = run_on_linux(render_deploy_script(PLAN), stand, STACK_DIR)

    assert "стенд не смог занять замок" not in result.stderr
    # Код 124 означал бы, что второй запуск ждал освобождения замка и его снял
    # timeout, а не что он отказал сам.
    assert "второй запуск вышел с кодом 1" in result.stdout.splitlines(), result.stdout
    assert f"замок {LOCK_FILE} занят, ждать не буду; держатель — " in result.stderr
    assert HOLDER_LINE.search(result.stderr) is not None, result.stderr
