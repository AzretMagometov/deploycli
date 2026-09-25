"""Подготовка хоста: сборка скрипта, который доводит машину до деплоя.

См. ADR-006 в decisions.md. Состав подготовки: docker с плагином compose,
внешняя сеть края хоста, деплой-пользователь без sudo в группе docker,
каталог ``/opt/<имя стека>`` в его собственности и — по флагу — сам край
хоста. Гигиена машины (брандмауэр, fail2ban, доступ по SSH,
автообновления) остаётся снаружи: подготовка её читает и печатает отчёт,
не трогая ни одного правила.

Подготовка — не владеемый путь в подключённом проекте (ADR-001): скрипт
принадлежит инструменту, живёт ровно один прогон и не хранится в чужом
репозитории. Поэтому он собирается на каждый запуск и печатается в
stdout, а до машины доезжает тем же каналом, что и файл окружения
(ADR-012): ``deploycli prepare-host ... | ssh <хост> sudo bash -s``.

Значения подстановки проверяются до сборки: имя стека и имя деплой-
пользователя — тем же алфавитом ``[a-z][a-z0-9-]*``, которым ADR-003
проверяет сегменты имён Traefik. Проверка здесь не косметическая: обе
строки попадают в текст, который исполняется на чужой машине от root.

Политика по демону docker: подготовка включает и запускает его сразу
после собственной установки пакетов, но остановленный демон на уже
настроенной машине не поднимает — его мог остановить владелец, и молча
вернуть машину в другое состояние подготовка права не имеет; такой прогон
отказывается и называет причину.
"""

import shlex
import sys
from argparse import ArgumentParser, Namespace, _SubParsersAction
from functools import partial
from pathlib import PurePosixPath

from jinja2 import Environment, PackageLoader, StrictUndefined

from deploycli.host_edge import (
    EDGE_NETWORK,
    EDGE_PROJECT,
    InvalidAcmeEmailError,
    render_host_edge_compose,
)
from deploycli.traefik_names import InvalidNameError, validate_segment

STACK_ROOT = "/opt"
"""Корень каталогов стеков на хосте: каталог стека — ``/opt/<имя стека>``."""

EDGE_PROJECT_DIR = f"{STACK_ROOT}/{EDGE_PROJECT}"
"""Каталог края хоста: его compose принадлежит подготовке, а не проекту."""

EDGE_COMPOSE_PATH = f"{EDGE_PROJECT_DIR}/docker-compose.yml"

_SCRIPT_TEMPLATE = "prepare_host.sh.j2"

_ENV = Environment(
    loader=PackageLoader("deploycli", "templates"),
    autoescape=False,  # вывод не HTML, а текст скрипта для bash
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["sh"] = shlex.quote


class HostPrepRefused(ValueError):
    """Подготовку нельзя собрать с такими значениями."""


def stack_dir(stack: str) -> str:
    """Каталог стека на хосте, владельцем которого станет деплой-пользователь."""
    return f"{STACK_ROOT}/{stack}"


def render_prepare_host_script(
    *, stack: str, deploy_user: str, edge_acme_email: str | None = None
) -> str:
    """Собирает скрипт подготовки хоста под один стек.

    ``edge_acme_email`` и есть флаг края хоста: адрес задан — скрипт
    поднимает край, адрес не задан — края в скрипте нет вовсе. Разделить
    их нечем: резолвер ACME без адреса регистрации Traefik не принимает,
    а выдумать адрес владельца машины инструмент не может.
    """
    validate_segment(stack)
    validate_segment(deploy_user)
    if stack == PurePosixPath(EDGE_PROJECT_DIR).name:
        raise HostPrepRefused(
            f"Имя стека '{stack}' занято каталогом края хоста '{EDGE_PROJECT_DIR}': "
            "переименуйте стек — иначе каталог края достался бы деплой-пользователю."
        )
    return _ENV.get_template(_SCRIPT_TEMPLATE).render(
        stack=stack,
        deploy_user=deploy_user,
        stack_dir=stack_dir(stack),
        edge_network=EDGE_NETWORK,
        edge_project=EDGE_PROJECT,
        edge_project_dir=EDGE_PROJECT_DIR,
        edge_compose_path=EDGE_COMPOSE_PATH,
        with_edge=edge_acme_email is not None,
        edge_compose=render_host_edge_compose(edge_acme_email) if edge_acme_email else "",
    )


def add_prepare_host_parser(subparsers: _SubParsersAction[ArgumentParser]) -> None:
    """Заводит команду ``prepare-host`` в разборщике командной строки."""
    parser = subparsers.add_parser(
        "prepare-host",
        help="собрать скрипт подготовки хоста",
        description=(
            "Печатает скрипт подготовки хоста в stdout. Подайте его на машину: "
            "deploycli prepare-host --stack <стек> --deploy-user <имя> "
            "| ssh <хост> sudo bash -s"
        ),
    )
    parser.add_argument("--stack", required=True, help="имя стека: каталог /opt/<имя стека>")
    parser.add_argument(
        "--deploy-user", required=True, help="имя деплой-пользователя: без sudo, в группе docker"
    )
    parser.add_argument(
        "--with-edge", action="store_true", help="поднять на машине край хоста (Traefik)"
    )
    parser.add_argument(
        "--acme-email",
        help="адрес регистрации ACME для резолвера сертификатов; обязателен с --with-edge",
    )
    parser.set_defaults(run=partial(_run, parser))


def _run(parser: ArgumentParser, args: Namespace) -> int:
    if args.with_edge and not args.acme_email:
        parser.error(
            "--with-edge требует --acme-email: резолвер сертификатов не регистрируется "
            "без адреса, а выдумать его deploycli не может"
        )
    if args.acme_email and not args.with_edge:
        parser.error("--acme-email имеет смысл только вместе с --with-edge")
    try:
        script = render_prepare_host_script(
            stack=args.stack,
            deploy_user=args.deploy_user,
            edge_acme_email=args.acme_email if args.with_edge else None,
        )
    except (InvalidNameError, InvalidAcmeEmailError, HostPrepRefused) as refusal:
        print(refusal, file=sys.stderr)
        return 1
    sys.stdout.write(script)
    return 0
