"""Факты о подключённом проекте, полученные от Docker Compose.

deploycli не разбирает базовый compose проекта сам. Он поддерживает якоря,
merge-ключи, `extends`, `include`, профили и интерполяцию с формами `:-` и
`:?`, а короткие формы записи портов и healthcheck — источник расхождений
даже без всего перечисленного. Собственный разборщик обязан повторить всю
эту семантику и разойдётся с docker в первом же нестандартном проекте —
тогда сканер прочитает не тот проект, который потом поедет (ADR-004).

Поэтому факты о сервисах проекта всегда получены запуском
``docker compose config --format json`` в каталоге проекта: эта команда
уже прогнала якоря, merge-ключи, `extends`, `include`, профили и
интерполяцию и отдаёт полностью разрешённую модель.

Вывод этой команды — источник фактов, а НЕ основа генерации: в нём уже
подставлены локальные значения переменных окружения автора (ADR-004). Этот
модуль намеренно не возвращает и не хранит сырой JSON целиком — только
узкий набор полей ниже, — чтобы его нельзя было случайно использовать как
источник содержимого генерируемых файлов.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

_EDGE_PORTS = frozenset({80, 443})


class ComposeScanError(RuntimeError):
    """Docker недоступен или ``docker compose config`` завершилась с ошибкой.

    Текст исключения — дословный stderr команды (или дословный текст
    ошибки операционной системы, если docker не найден в PATH), без
    попытки разобрать или переформулировать причину: пользователю нужно
    искать в интернете именно этот текст, а не его пересказ.
    """


@dataclass(frozen=True, slots=True)
class ServiceFacts:
    """Факты об одном сервисе подключённого проекта."""

    name: str
    image: str | None
    networks: tuple[str, ...]
    published_ports: tuple[int, ...]
    traefik_labels: Mapping[str, str]

    @property
    def publishes_edge_port(self) -> bool:
        """Сервис публикует наружу порт 80 или 443 — то же, что занимает край хоста."""
        return any(port in _EDGE_PORTS for port in self.published_ports)


@dataclass(frozen=True, slots=True)
class ProjectFacts:
    """Факты о подключённом проекте: список его сервисов."""

    services: tuple[ServiceFacts, ...] = field(default=())


def scan_project(project_dir: Path) -> ProjectFacts:
    """Прочитать факты о проекте в ``project_dir`` через ``docker compose config``.

    Поднимает :class:`ComposeScanError`, если docker не найден в PATH или
    команда завершилась ненулевым кодом возврата.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ComposeScanError(str(exc)) from exc

    if result.returncode != 0:
        raise ComposeScanError(result.stderr)

    payload = json.loads(result.stdout)
    return _project_facts_from_payload(payload)


def _project_facts_from_payload(payload: Mapping[str, Any]) -> ProjectFacts:
    services_payload: Mapping[str, Any] = payload.get("services") or {}
    services = tuple(
        _service_facts(name, definition) for name, definition in services_payload.items()
    )
    return ProjectFacts(services=services)


def _service_facts(name: str, definition: Mapping[str, Any]) -> ServiceFacts:
    networks = tuple(definition.get("networks") or {})
    published_ports = tuple(sorted(_published_ports(definition)))
    traefik_labels = MappingProxyType(
        {
            key: value
            for key, value in (definition.get("labels") or {}).items()
            if key.startswith("traefik.")
        }
    )
    return ServiceFacts(
        name=name,
        image=definition.get("image"),
        networks=networks,
        published_ports=published_ports,
        traefik_labels=traefik_labels,
    )


def _published_ports(definition: Mapping[str, Any]) -> set[int]:
    # `docker compose config` уже разворачивает короткие формы записи портов,
    # включая диапазоны ("8000-8010:8000-8010"), в отдельные записи с
    # единственным числом в `published`; сервис без публикации порта наружу
    # (`- "80"`, только target) вовсе не несёт ключ `published`. Значение вне
    # этой формы — расхождение с моделью compose, а не факт для угадывания.
    return {int(port["published"]) for port in definition.get("ports") or () if "published" in port}
