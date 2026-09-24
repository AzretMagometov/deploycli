"""Блок меток Traefik для одного сервиса подключённого проекта.

См. ADR-003 в decisions.md. Этот модуль строит метки ОДНОГО сервиса, не
весь прод-override: состав остальных частей файла (healthcheck, ссылка на
образ, файл окружения) картой ещё не решён.

Имена ресурсов и приоритет роутера переиспользуются из
:mod:`deploycli.traefik_names`, а не собираются заново. Пространство имён
стека — всегда литерал ``${COMPOSE_PROJECT_NAME}`` (ADR-003: префикс
подставляется compose из ``.env`` контура, а не зашивается генератором в
файл), поэтому оно не входит во вход этого модуля.

Блок меток собирается шаблоном Jinja2 (ADR-004), а не склейкой строк:
шаблон отвечает за раскладку строк, значения (имена, приоритет, порт)
вычисляет и передаёт этот модуль.
"""

from dataclasses import dataclass

from jinja2 import Environment, PackageLoader, StrictUndefined

from deploycli.traefik_names import (
    COMPOSE_PROJECT_NAME_LITERAL,
    hsts_handler_name,
    router_priority,
    router_service_name,
)

_ENV = Environment(
    loader=PackageLoader("deploycli", "templates"),
    autoescape=False,  # вывод не HTML: обратные кавычки и `${}` должны пройти как есть
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
_TEMPLATE = _ENV.get_template("service_labels.j2")

_NOT_PUBLIC_LABELS: tuple[str, ...] = ("traefik.enable=false",)


@dataclass(frozen=True, slots=True)
class PublicRoute:
    """Маршрут публичного сервиса: домены, внутренний порт, префикс пути.

    ``path_prefix`` пустой означает отсутствие префикса — роутер получает
    приоритет ``1`` (см. :func:`deploycli.traefik_names.router_priority`).
    """

    domains: tuple[str, ...]
    port: int
    path_prefix: str = ""


def service_labels(service: str, route: PublicRoute | None) -> tuple[str, ...]:
    """Строит упорядоченный блок меток Traefik для одного сервиса.

    ``route is None`` — сервис непубличный, единственная метка вывода —
    ``traefik.enable=false``.

    Иначе строится полный блок из ADR-003: сеть, правило роутера (алиасы
    доменов через ``||``, скобки вокруг группы алиасов при сочетании с
    префиксом пути — иначе ``&&`` в правиле Traefik связал бы только
    последний домен), точка входа, TLS одной меткой, явный приоритет,
    ссылка на сервис и единственный в v1 обработчик HSTS.

    Имя сервиса проверяется по алфавиту (:mod:`deploycli.traefik_names`)
    только для публичного сервиса: непубличный не входит в пространство
    имён Traefik (ADR-003), и его имя в вывод не попадает.
    """
    if route is None:
        return _NOT_PUBLIC_LABELS

    router = router_service_name(COMPOSE_PROJECT_NAME_LITERAL, service)
    hsts = hsts_handler_name(COMPOSE_PROJECT_NAME_LITERAL)
    rendered = _TEMPLATE.render(
        router=router,
        hsts=hsts,
        domains=route.domains,
        path_prefix=route.path_prefix,
        priority=router_priority(route.path_prefix),
        port=route.port,
    )
    return tuple(rendered.splitlines())
