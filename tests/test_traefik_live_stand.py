"""Живой стенд Traefik: настоящая семантика слияния и приоритета (dpc-tm3.27).

ADR-003 обосновывает пространство имён и явный приоритет роутера настоящим
поведением Traefik при слиянии одноимённых определений — не нашим
представлением о нём. ``tests/test_traefik_render_check.py`` (dpc-tm3.26)
проверяет только отрендеренный конфиг: golden-файл подтверждает байты, а
не смысл. Здесь — единственная проверка, которая поднимает настоящий
Traefik и настоящие backend-контейнеры и смотрит на настоящие HTTP-ответы:

- Сценарий 1: два стека с ОДИНАКОВЫМИ именами сервисов и разными доменами
  за одним Traefik — каждый домен обязан отвечать своим стеком. Без
  префикса пространства имён Traefik складывает списки серверов
  одноимённых сервисов в общий пул (ADR-003) — эта проверка эмпирически
  подтверждена вручную в ходе разработки (см. отчёт задачи dpc-tm3.27),
  а не встроена сюда постоянным тестом: у генератора нет и не должно быть
  режима «без пространства имён» — поломку нечем было бы воспроизвести
  без временного вмешательства в сборку меток.
- Сценарий 2: один стек, сервис с алиасами apex/www и сервис с префиксом
  пути ``/api`` на apex — запрос на ``apex/api`` обязан прийти во второй
  сервис, на корень apex и на www — в первый. Ловит и явный приоритет
  роутера, и скобки вокруг группы алиасов (ADR-003).

Стенд поднимается и гасится каждым тестом сам (:class:`TraefikStand`),
после прогона на хосте не остаётся ни контейнеров, ни сетей, ни томов.
"""

from pathlib import Path

import pytest

from deploycli.traefik_labels import PublicRoute, service_labels
from support.traefik_stand import BACKEND_PORT, LiveService, TraefikStand

pytestmark = pytest.mark.docker


def test_two_stacks_same_service_name_different_domains_stay_isolated(tmp_path: Path) -> None:
    with TraefikStand(tmp_path) as stand:
        stand.up_stack(
            "alpha",
            {
                "web": LiveService(
                    labels=service_labels(
                        "web", PublicRoute(domains=("alpha.dpc-e2e.test",), port=BACKEND_PORT)
                    ),
                    response_text="alpha-web",
                )
            },
        )
        stand.up_stack(
            "beta",
            {
                "web": LiveService(
                    labels=service_labels(
                        "web", PublicRoute(domains=("beta.dpc-e2e.test",), port=BACKEND_PORT)
                    ),
                    response_text="beta-web",
                )
            },
        )

        stand.wait_for_body("alpha.dpc-e2e.test", "/", "alpha-web")
        stand.wait_for_body("beta.dpc-e2e.test", "/", "beta-web")

        # Не одно совпадение, а устойчивость на серии запросов — страховка
        # от неопределённого порядка выдачи Traefik при живом провайдере.
        for _ in range(10):
            assert stand.https_get("alpha.dpc-e2e.test", "/").body.strip() == "alpha-web"
            assert stand.https_get("beta.dpc-e2e.test", "/").body.strip() == "beta-web"


def test_alias_and_path_prefix_router_on_shared_domain_route_correctly(tmp_path: Path) -> None:
    with TraefikStand(tmp_path) as stand:
        stand.up_stack(
            "shop",
            {
                "web": LiveService(
                    labels=service_labels(
                        "web",
                        PublicRoute(
                            domains=("apex.dpc-e2e.test", "www.apex.dpc-e2e.test"),
                            port=BACKEND_PORT,
                        ),
                    ),
                    response_text="shop-web",
                ),
                "api": LiveService(
                    labels=service_labels(
                        "api",
                        PublicRoute(
                            domains=("apex.dpc-e2e.test",),
                            port=BACKEND_PORT,
                            path_prefix="/api",
                        ),
                    ),
                    response_text="shop-api",
                ),
            },
        )

        stand.wait_for_body("apex.dpc-e2e.test", "/api", "shop-api")

        assert stand.https_get("apex.dpc-e2e.test", "/").body.strip() == "shop-web"
        assert stand.https_get("www.apex.dpc-e2e.test", "/").body.strip() == "shop-web"
        assert stand.https_get("apex.dpc-e2e.test", "/api").body.strip() == "shop-api"
