"""Точка входа командной строки deploycli."""

import argparse


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
