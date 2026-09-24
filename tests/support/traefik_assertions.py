"""Утверждения над моделью Traefik, разобранной из отрендеренного конфига.

См. ADR-003 в decisions.md за перечнем и обоснованием каждого утверждения:
пространство имён стека, кратность роутера на публичный сервис,
`traefik.enable=false` на непубличных сервисах, приоритет роутера с
префиксом пути на общем домене.
"""

import re
from collections import defaultdict
from collections.abc import Iterable

from support.traefik_render import Router, TraefikModel

_HOST_DOMAIN = re.compile(r"Host\(`([^`]+)`\)")


def assert_all_names_prefixed(model: TraefikModel, prefix: str) -> None:
    """Все имена роутеров, сервисов и обработчиков начинаются с ``prefix``."""
    names = (
        [router.name for router in model.routers]
        + [service.name for service in model.services]
        + [middleware.name for middleware in model.middlewares]
    )
    unprefixed = sorted(name for name in names if not name.startswith(prefix))
    if unprefixed:
        raise AssertionError(f"Имена вне префикса пространства имён '{prefix}': {unprefixed}")


def assert_no_name_conflicts(model: TraefikModel) -> None:
    """Ни одно имя не встречается дважды с разной конфигурацией.

    Роутеры с одинаковым именем и разной конфигурацией Traefik молча
    удаляет оба; сервисы с разной конфигурацией молча сливает пул
    серверов (ADR-003) — обе ошибки не наблюдаемы иначе, кроме сравнения
    определений по имени.
    """
    if model.conflicts:
        details = "; ".join(
            f"{conflict.kind} '{conflict.name}': {conflict.distinct_definitions} разных определений"
            for conflict in model.conflicts
        )
        raise AssertionError(f"Имя встречается дважды с разной конфигурацией: {details}")


def assert_routers_reference_existing_services(model: TraefikModel) -> None:
    """Каждый роутер ссылается на существующий Traefik-сервис."""
    service_names = {service.name for service in model.services}
    dangling = sorted(
        f"{router.name} -> {router.service_name}"
        for router in model.routers
        if router.service_name not in service_names
    )
    if dangling:
        raise AssertionError(f"Роутеры ссылаются на несуществующий сервис: {dangling}")


def assert_each_public_service_has_exactly_one_router(
    model: TraefikModel, public_service_names: Iterable[str]
) -> None:
    """У каждого публичного сервиса ровно один роутер (ADR-003)."""
    by_name = {view.compose_name: view for view in model.compose_services}
    for name in public_service_names:
        view = by_name.get(name)
        if view is None:
            raise AssertionError(f"Публичный сервис '{name}' отсутствует среди сервисов проекта")
        if len(view.router_names) != 1:
            raise AssertionError(
                f"У публичного сервиса '{name}' роутеров: {len(view.router_names)} "
                f"({sorted(view.router_names)}), ожидался ровно 1"
            )


def assert_non_public_services_disabled(
    model: TraefikModel, public_service_names: Iterable[str]
) -> None:
    """У непубличных сервисов явно ``traefik.enable=false`` (ADR-003).

    Отсутствие метки — не то же самое, что явный ``false``: поведение при
    отсутствии зависит от ``exposedByDefault`` края, которого генератор не
    видит (ADR-003), поэтому здесь проверяется точное значение
    ``"false"``, а не то, что оно не ``"true"``.
    """
    public = set(public_service_names)
    not_explicitly_disabled = sorted(
        f"{view.compose_name} (traefik.enable={view.traefik_enable!r})"
        for view in model.compose_services
        if view.compose_name not in public and view.traefik_enable != "false"
    )
    if not_explicitly_disabled:
        raise AssertionError(
            f"Непубличные сервисы без явного traefik.enable=false: {not_explicitly_disabled}"
        )


def assert_path_prefix_router_outranks_plain_on_shared_domain(model: TraefikModel) -> None:
    """На общем домене роутер с префиксом пути имеет приоритет выше роутера без пути.

    Traefik ранжирует роутеры по длине правила сам, но этого не хватает
    ровно в сочетании алиасов домена и разделения по префиксу пути на том
    же домене (ADR-003) — приоритет пишется явно и проверяется здесь по
    числу, а не по структуре правила.
    """
    by_domain: dict[str, list[Router]] = defaultdict(list)
    for router in model.routers:
        for domain in _HOST_DOMAIN.findall(router.rule):
            by_domain[domain].append(router)

    for domain, routers in by_domain.items():
        with_prefix = [router for router in routers if "PathPrefix(" in router.rule]
        without_prefix = [router for router in routers if "PathPrefix(" not in router.rule]
        for prefixed in with_prefix:
            for plain in without_prefix:
                if not prefixed.priority > plain.priority:
                    raise AssertionError(
                        f"На домене '{domain}' роутер с путём '{prefixed.name}' "
                        f"(priority={prefixed.priority}) не выше роутера без пути "
                        f"'{plain.name}' (priority={plain.priority})"
                    )
