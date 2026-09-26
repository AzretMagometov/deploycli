"""Край хоста: константы контракта и единственный шаблон его compose.

См. ADR-002, ADR-003 и ADR-006 в decisions.md. Край хоста — единственный
обратный прокси машины, занимающий порты 80 и 443. Он принадлежит
подготовке хоста, а не подключённому проекту: генератор про него ничего не
пишет и знает только имена контракта.

Имена контракта (сеть, точки входа, резолвер сертификатов) стоят здесь
константами, а в шаблон приходят подстановкой. У шаблона два потребителя:
подготовка хоста поднимает им край по своему флагу, документация комплекта
рендерит им же рабочий пример (ADR-014, dpc-tm3.65). Второй шаблон края в
репозитории запрещён: вторая правда расходится с подготовкой молча.

Прочие значения — не контракт, а выбор подготовки, и подключённый проект
их не видит: имя compose-проекта края, имя его сервиса, образ Traefik,
путь и том хранилища ACME. Имя compose-проекта отличает край хоста от
чужого прокси на тех же портах: контейнеры различают по метке
``com.docker.compose.project`` (ADR-003), а не по имени контейнера.

Версия образа — ``traefik:v3.6``: до 3.6.1 докер-провайдер Traefik был
жёстко пришит к Docker API 1.24 и падал на Docker Engine 29+ (замер в
dpc-tm3.27, ``tests/support/traefik_stand.py``).
"""

import re

from jinja2 import Environment, PackageLoader, StrictUndefined

EDGE_NETWORK = "proxy"
"""Внешняя сеть контракта: её создаёт подготовка на любой машине (ADR-002)."""

ENTRYPOINT_HTTP = "web"
"""Точка входа на порту 80; с неё же идёт редирект и проверка ACME."""

ENTRYPOINT_HTTPS = "websecure"
"""Точка входа на порту 443: на неё ссылаются метки роутеров проекта."""

CERT_RESOLVER = "letsencrypt"
"""Резолвер сертификатов контракта: имя стоит в метке ``tls.certresolver``."""

EDGE_PROJECT = "host-edge"
"""Имя compose-проекта края: по нему подготовка узнаёт свой край на портах."""

EDGE_SERVICE = "edge"
"""Имя единственного сервиса в compose края."""

EDGE_IMAGE = "traefik:v3.6"
"""Образ края хоста."""

ACME_VOLUME = "letsencrypt"
"""Именованный том под хранилище ACME: без него сертификаты перевыпускаются."""

ACME_STORAGE = "/letsencrypt/acme.json"
"""Путь хранилища ACME внутри контейнера края."""

_EDGE_TEMPLATE = "host_edge.compose.yml.j2"

_ENV = Environment(
    loader=PackageLoader("deploycli", "templates"),
    autoescape=False,  # вывод не HTML: это YAML и аргументы командной строки
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)

_EMAIL = re.compile(r"[^\s@'\"\\]+@[^\s@'\"\\]+\.[^\s@'\"\\]+")


class InvalidAcmeEmailError(ValueError):
    """Адрес для ACME непригоден к подстановке в конфигурацию края."""


def validate_acme_email(email: str) -> None:
    """Проверяет адрес регистрации ACME перед подстановкой.

    Адрес приходит аргументом запуска и попадает и в аргумент командной
    строки Traefik, и в скрипт подготовки. Пробел, кавычка или перевод
    строки в нём — не опечатка, а чужая строка в чужом файле, поэтому
    адрес не чинится, а останавливает рендер (тот же выбор, что и у имён
    в ADR-003).
    """
    if not _EMAIL.fullmatch(email):
        raise InvalidAcmeEmailError(
            f"Адрес '{email}' непригоден как адрес регистрации ACME: нужен адрес вида "
            "имя@домен без пробелов, кавычек и обратных слэшей."
        )


def render_host_edge_compose(acme_email: str) -> str:
    """Собирает compose края хоста с именами контракта и адресом ACME.

    Адрес регистрации обязателен: резолвер ACME без него Traefik не
    принимает (справочник Traefik, таблица ``certificatesResolvers.<name>
    .acme``), а выдумать адрес владельца машины инструмент не может.
    """
    validate_acme_email(acme_email)
    return _ENV.get_template(_EDGE_TEMPLATE).render(
        project=EDGE_PROJECT,
        service=EDGE_SERVICE,
        image=EDGE_IMAGE,
        network=EDGE_NETWORK,
        entrypoint_http=ENTRYPOINT_HTTP,
        entrypoint_https=ENTRYPOINT_HTTPS,
        resolver=CERT_RESOLVER,
        acme_email=acme_email,
        acme_volume=ACME_VOLUME,
        acme_storage=ACME_STORAGE,
        acme_storage_dir=ACME_STORAGE.rsplit("/", 1)[0],
    )
