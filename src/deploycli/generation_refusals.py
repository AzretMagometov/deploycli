"""Отказы генерации: четыре блокирующих правила и их сообщения.

См. ADR-002, ADR-003 в decisions.md и резолюцию dpc-tm3.3. Отказ — не
предупреждение: при срабатывании хотя бы одного правила генерация не
пишет ни одного файла. Все правила проверяются разом, а не до первого
срабатывания — человек должен узнать полный список нарушений за один
прогон, а не чинить по одному.

Правила:

1. Имя публичного сервиса вне алфавита ``^[a-z][a-z0-9-]*$`` — проверка
   переиспользована из :mod:`deploycli.traefik_names`.
2. Два публичных сервиса заявили один домен без непересекающихся
   префиксов пути. Роутер без пути ловит остальное, поэтому пара «без
   префикса» + «с префиксом» на одном домене отказом не является.
3. Любая метка ``traefik.*`` у любого сервиса в базовом compose проекта.
   Правило намеренно грубое: compose сливает метки базового файла и
   прод-override, а не заменяет их, поэтому чужая метка дожила бы до
   Traefik вне пространства имён (ADR-003) — публичность здесь ответ, а
   не свойство файла.
4. Сервис с непустым ``ports:`` в базовом compose проекта: порт 80 или
   443 занимает край хоста, которым владеет подготовка хоста, а не
   проект (ADR-002); любой другой порт всё равно доедет до
   прод-конфигурации в обход Traefik, потому что override не умеет
   удалять ключи, только переопределять (резолюция dpc-tm3.3).
"""

from collections.abc import Mapping
from itertools import combinations

from deploycli.compose_facts import ProjectFacts
from deploycli.traefik_labels import PublicRoute
from deploycli.traefik_names import InvalidNameError, validate_segment

_EDGE_PORTS = frozenset({80, 443})


class GenerationRefused(Exception):
    """Генерация отказана: несёт сообщения обо всех сработавших правилах.

    Поднимается только когда :func:`find_refusals` вернула хотя бы одно
    сообщение — пустой список отказов не порождает исключения.
    """

    def __init__(self, messages: tuple[str, ...]) -> None:
        super().__init__("\n".join(messages))
        self.messages = messages


def find_refusals(facts: ProjectFacts, public_routes: Mapping[str, PublicRoute]) -> tuple[str, ...]:
    """Проверяет все четыре правила и возвращает сообщения обо всех нарушениях.

    Пустой кортеж означает, что генерация разрешена. Порядок сообщений:
    правило 1, затем 2, 3, 4; внутри правила — по имени сервиса, чтобы
    вывод был детерминирован при нескольких одновременных нарушениях.
    """
    return (
        *_invalid_public_service_names(public_routes),
        *_colliding_domains(public_routes),
        *_foreign_traefik_labels(facts),
        *_occupied_ports(facts),
    )


def ensure_generation_allowed(
    facts: ProjectFacts, public_routes: Mapping[str, PublicRoute]
) -> None:
    """Поднимает :class:`GenerationRefused`, если сработало хоть одно правило.

    Генерация обязана вызвать эту проверку до того, как запишет хотя бы
    один файл (dpc-tm3.25): отказ — не предупреждение.
    """
    refusals = find_refusals(facts, public_routes)
    if refusals:
        raise GenerationRefused(refusals)


def _invalid_public_service_names(
    public_routes: Mapping[str, PublicRoute],
) -> tuple[str, ...]:
    messages = []
    for name in sorted(public_routes):
        try:
            validate_segment(name)
        except InvalidNameError:
            messages.append(
                f"Имя публичного сервиса '{name}' вне алфавита [a-z][a-z0-9-]*: "
                f"переименуйте сервис '{name}' в базовом compose проекта — только "
                "строчные латинские буквы, цифры и дефис, начиная с буквы."
            )
    return tuple(messages)


def _path_prefixes_overlap(a: str, b: str) -> bool:
    # Оба без пути — один и тот же катч-олл дважды: конфликт. Один без
    # пути, другой с путём — не конфликт: роутер без пути ловит остальное
    # (ADR-003, приоритет 1 у него ниже, чем 100+len у роутера с путём).
    # Оба с путём — конфликт, если один префикс — начало другого
    # (`/api` и `/api/v2`), ровно как Traefik сравнивает `PathPrefix`.
    if not a and not b:
        return True
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _prefix_label(prefix: str) -> str:
    return f"'{prefix}'" if prefix else "без префикса пути"


def _colliding_domains(public_routes: Mapping[str, PublicRoute]) -> tuple[str, ...]:
    messages = []
    for name_a, name_b in combinations(sorted(public_routes), 2):
        route_a, route_b = public_routes[name_a], public_routes[name_b]
        common_domains = sorted(set(route_a.domains) & set(route_b.domains))
        for domain in common_domains:
            if not _path_prefixes_overlap(route_a.path_prefix, route_b.path_prefix):
                continue
            messages.append(
                f"Публичные сервисы '{name_a}' и '{name_b}' заявили домен "
                f"'{domain}' без непересекающихся префиксов пути (у '{name_a}' — "
                f"{_prefix_label(route_a.path_prefix)}, у '{name_b}' — "
                f"{_prefix_label(route_b.path_prefix)}): задайте сервисам в ответах "
                "непересекающиеся префиксы пути или разные домены."
            )
    return tuple(messages)


def _foreign_traefik_labels(facts: ProjectFacts) -> tuple[str, ...]:
    messages = []
    for service in sorted(facts.services, key=lambda s: s.name):
        if not service.traefik_labels:
            continue
        labels = ", ".join(sorted(service.traefik_labels))
        messages.append(
            f"В базовом compose проекта у сервиса '{service.name}' есть метки "
            f"Traefik ({labels}): уберите их из базового compose — compose "
            "сливает метки базового файла и прод-override, а не заменяет их, "
            "поэтому чужая метка дожила бы до Traefik вне пространства имён стека."
        )
    return tuple(messages)


def _occupied_ports(facts: ProjectFacts) -> tuple[str, ...]:
    messages = []
    for service in sorted(facts.services, key=lambda s: s.name):
        if not service.published_ports:
            continue
        ports = ", ".join(str(port) for port in service.published_ports)
        edge_ports = sorted(port for port in service.published_ports if port in _EDGE_PORTS)
        if edge_ports:
            # Сообщение перечисляет ВСЕ published_ports сервиса, а не только
            # 80/443: убрав ключ наполовину, второй прогон отказал бы снова
            # на оставшемся порте — человек должен узнать всё за один раз.
            edge = ", ".join(str(port) for port in edge_ports)
            messages.append(
                f"Сервис '{service.name}' в базовом compose проекта занимает "
                f"порт {edge} через ports: ({ports}): край хоста принадлежит "
                "подготовке хоста, а не проекту (ADR-002). Уберите ports: у "
                f"сервиса '{service.name}' целиком и полагайтесь на маршрут "
                "через край хоста."
            )
        else:
            messages.append(
                f"У сервиса '{service.name}' в базовом compose проекта есть "
                f"ports: ({ports}): прод-override не умеет удалять ключи, только "
                "переопределять, поэтому проброс порта доедет до "
                f"прод-конфигурации в обход Traefik. Уберите ports: у сервиса "
                f"'{service.name}' в базовом compose проекта."
            )
    return tuple(messages)
