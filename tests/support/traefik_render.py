"""Разбор отрендеренного конфига Traefik в модель (ADR-003, dpc-tm3.26).

Проверка результата генерации меток читает ОТРЕНДЕРЕННЫЙ конфиг, а не факт
записи файла (ADR-003, раздел Rationale: «golden-файл подтверждает байты, а
не смысл»). Здесь — оснастка для этой проверки: рендер compose-проекта с
уже сгенерированными метками через ``docker compose config`` при заданном
``COMPOSE_PROJECT_NAME`` и разбор меток обратно в модель роутеров, сервисов
и обработчиков Traefik.

Рендер здесь идёт СВОИМ запуском ``docker compose config`` — с
интерполяцией и с подставленным ``COMPOSE_PROJECT_NAME``. Сканер проекта
читает чужой проект единственным режимом — безопасным, без интерполяции
(ADR-011), и в нём ``${COMPOSE_PROJECT_NAME}`` в имени роутера остался бы
литералом. Здесь же проверяется СГЕНЕРИРОВАННОЕ: метки должны дойти до
Traefik ровно в том виде, в каком их соберёт compose на хосте (ADR-003),
поэтому подстановка обязательна. Разбор ответа в факты переиспользован у
:func:`deploycli.compose_facts.facts_from_config`.
"""

import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml

from deploycli.compose_facts import ProjectFacts, facts_from_config

_ROUTER_ATTR = re.compile(r"^traefik\.http\.routers\.([^.]+)\.(.+)$")
_SERVICE_PORT = re.compile(r"^traefik\.http\.services\.([^.]+)\.loadbalancer\.server\.port$")
_MIDDLEWARE_ATTR = re.compile(r"^traefik\.http\.middlewares\.([^.]+)\.(.+)$")


@dataclass(frozen=True, slots=True)
class Router:
    """Роутер Traefik, разобранный из меток одного контейнера."""

    name: str
    rule: str
    entrypoint: str
    priority: int
    service_name: str
    middlewares: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Service:
    """Traefik-сервис (backend), разобранный из меток одного контейнера."""

    name: str
    port: int


@dataclass(frozen=True, slots=True)
class Middleware:
    """Промежуточный обработчик Traefik, разобранный из меток контейнера."""

    name: str
    params: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ComposeServiceView:
    """Что видно о сервисе compose-проекта со стороны пространства имён Traefik.

    ``traefik_enable`` хранит дословное значение метки ``traefik.enable``
    (``"true"``, ``"false"`` или ``None``, если метка отсутствует вовсе) —
    не булево «включён/выключен». ADR-003 требует явного
    ``traefik.enable=false`` на непубличных сервисах именно потому, что
    поведение по умолчанию при ОТСУТСТВИИ метки не проверяемо (зависит от
    `exposedByDefault` края, которого генератор не видит): отсутствие
    метки — это ровно тот отказ, который утверждение обязано ловить, а не
    молча считать эквивалентным явному ``false``.
    """

    compose_name: str
    traefik_enable: str | None
    router_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class NameConflict:
    """Одно имя Traefik встречено с более чем одним различающимся определением.

    См. ADR-003: роутеры с одним именем и разной конфигурацией Traefik
    молча удаляет оба, сервисы с разной конфигурацией молча сливает пул
    серверов — обе ошибки не имеют иного способа обнаружения, кроме
    сравнения определений по имени здесь.
    """

    kind: str
    name: str
    distinct_definitions: int


@dataclass(frozen=True, slots=True)
class TraefikModel:
    """Модель пространства имён Traefik, собранная со всех сервисов проекта."""

    routers: tuple[Router, ...] = field(default=())
    services: tuple[Service, ...] = field(default=())
    middlewares: tuple[Middleware, ...] = field(default=())
    compose_services: tuple[ComposeServiceView, ...] = field(default=())
    conflicts: tuple[NameConflict, ...] = field(default=())


def parse_traefik_model(project: ProjectFacts) -> TraefikModel:
    """Разбирает метки ``traefik.*`` всех сервисов проекта в :class:`TraefikModel`.

    Один и тот же обработчик HSTS обязан повторяться одинаковым на каждом
    публичном сервисе стека (ADR-003) — это не конфликт: конфликтом
    считается только различие в определении при совпадении имени.
    """
    router_defs: dict[str, list[Router]] = defaultdict(list)
    service_defs: dict[str, list[Service]] = defaultdict(list)
    middleware_defs: dict[str, list[Middleware]] = defaultdict(list)
    compose_services: list[ComposeServiceView] = []

    for compose_service in project.services:
        labels = compose_service.traefik_labels
        own_router_names: set[str] = set()

        for name, attrs in _group_by_name(labels, _ROUTER_ATTR).items():
            router_defs[name].append(_router_from_attrs(name, attrs))
            own_router_names.add(name)

        for name, port in _service_ports(labels).items():
            service_defs[name].append(Service(name=name, port=port))

        for name, attrs in _group_by_name(labels, _MIDDLEWARE_ATTR).items():
            middleware_defs[name].append(
                Middleware(name=name, params=MappingProxyType(dict(attrs)))
            )

        compose_services.append(
            ComposeServiceView(
                compose_name=compose_service.name,
                traefik_enable=labels.get("traefik.enable"),
                router_names=frozenset(own_router_names),
            )
        )

    conflicts = (
        *_conflicts("router", router_defs),
        *_conflicts("service", service_defs),
        *_conflicts("middleware", middleware_defs),
    )
    return TraefikModel(
        routers=tuple(_first_definition(router_defs)),
        services=tuple(_first_definition(service_defs)),
        middlewares=tuple(_first_definition(middleware_defs)),
        compose_services=tuple(compose_services),
        conflicts=conflicts,
    )


def render_traefik_model(project_dir: Path, project_name: str) -> TraefikModel:
    """Рендерит проект в ``project_dir`` через ``docker compose config`` и разбирает
    результат в :class:`TraefikModel`.

    ``project_name`` подставляется как ``COMPOSE_PROJECT_NAME`` — то же имя
    переменной, которым в реальном контуре разводит стеки ``.env``
    (ADR-003).
    """
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROJECT_NAME": project_name},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"docker compose config не отрендерил проект: {result.stderr}")
    return parse_traefik_model(facts_from_config(json.loads(result.stdout)))


def write_compose_project(project_dir: Path, services: Mapping[str, Sequence[str]]) -> None:
    """Пишет ``docker-compose.yml`` с сервисами и их метками в форме списка строк.

    ``services`` — имя сервиса compose -> кортеж строк меток
    (``"ключ=значение"``), дословный вывод
    :func:`deploycli.traefik_labels.service_labels`. Списочная форма
    обязательна (ADR-003): в форме отображения compose не подставляет
    переменную в ключе метки, и ``${COMPOSE_PROJECT_NAME}`` остался бы
    литералом в имени роутера.
    """
    compose = {
        "services": {
            name: {"image": "scratch", "labels": list(labels)} for name, labels in services.items()
        }
    }
    (project_dir / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))


def _group_by_name(
    labels: Mapping[str, str], pattern: re.Pattern[str]
) -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for key, value in labels.items():
        match = pattern.match(key)
        if match is not None:
            name, attr = match.group(1), match.group(2)
            grouped[name][attr] = value
    return grouped


def _service_ports(labels: Mapping[str, str]) -> dict[str, int]:
    ports: dict[str, int] = {}
    for key, value in labels.items():
        match = _SERVICE_PORT.match(key)
        if match is not None:
            ports[match.group(1)] = int(value)
    return ports


def _router_from_attrs(name: str, attrs: Mapping[str, str]) -> Router:
    middlewares_raw = attrs.get("middlewares", "")
    middlewares = tuple(middlewares_raw.split(",")) if middlewares_raw else ()
    return Router(
        name=name,
        rule=attrs["rule"],
        entrypoint=attrs["entrypoints"],
        priority=int(attrs["priority"]),
        service_name=attrs["service"],
        middlewares=middlewares,
    )


def _dedup[T](items: list[T]) -> list[T]:
    result: list[T] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _first_definition[T](defs: Mapping[str, list[T]]) -> list[T]:
    return [items[0] for items in defs.values()]


def _conflicts[T](kind: str, defs: Mapping[str, list[T]]) -> list[NameConflict]:
    conflicts: list[NameConflict] = []
    for name, items in defs.items():
        distinct = _dedup(items)
        if len(distinct) > 1:
            conflicts.append(NameConflict(kind=kind, name=name, distinct_definitions=len(distinct)))
    return conflicts
