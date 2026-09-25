"""Точка входа командной строки deploycli."""

import argparse

from deploycli.host_prep import add_prepare_host_parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploycli",
        description=(
            "Подключает деплой на VPS (Docker Compose + Traefik) к существующему проекту: "
            "сканирует проект, спрашивает недостающее и генерирует конфигурацию."
        ),
        epilog=(
            "Команды scan (сканирование проекта) и generate (генерация деплоя) "
            "появятся в следующих задачах."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")
    add_prepare_host_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    run = getattr(args, "run", None)
    if run is None:
        parser.print_help()
        return 0
    exit_code: int = run(args)
    return exit_code
