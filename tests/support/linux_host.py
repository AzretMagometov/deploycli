"""Прогон сгенерированного скрипта на настоящем Linux: контейнер Ubuntu 24.04.

Замок деплоя стоит на ``flock`` (ADR-008), а его на машине разработчика
под macOS нет вовсе: сравнение байтов golden-файлом подтверждает текст
скрипта, но не то, что этот текст на хосте работает. Поэтому скрипт
исполняется в контейнере того же дистрибутива, что и прод
(инвентаризация ADR-012: Ubuntu 24.04, bash 5.2, ``flock`` из
util-linux), а не под подменённым ``flock`` в PATH.

Скрипт и сценарий стенда едут в контейнер через stdin, а не томом:
каталоги pytest лежат в ``/private/var/folders``, и их доступность
Docker Desktop — настройка машины, от которой тест зависеть не должен.
"""

import base64
import subprocess

IMAGE = "ubuntu:24.04"
SCRIPT_PATH = "/srv/deploy.sh"


class LinuxHostError(RuntimeError):
    """Контейнер не запустился: docker недоступен или образ не скачался."""


def run_on_linux(script: str, stand: str, stack_dir: str) -> subprocess.CompletedProcess[str]:
    """Кладёт ``script`` в контейнер и выполняет в нём сценарий ``stand``.

    ``stack_dir`` создаётся заранее: его заводит подготовка хоста
    (ADR-006), а не деплой. Сценарий стенда отвечает за коды возврата
    сам — обёртка их не перехватывает и не трактует.
    """
    payload = base64.b64encode(script.encode()).decode()
    program = "\n".join(
        (
            "set -eu",
            f"mkdir -p {stack_dir}",
            f"printf '%s' '{payload}' | base64 -d >{SCRIPT_PATH}",
            f"chmod 755 {SCRIPT_PATH}",
            stand,
        )
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", IMAGE, "bash", "-s"],
        input=program,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 125:
        raise LinuxHostError(result.stderr.strip())
    return result
