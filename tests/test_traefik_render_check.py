"""Проверка отрендеренного конфига через настоящий docker compose config (dpc-tm3.26).

ADR-003: golden-файл подтверждает байты, а не смысл — эти тесты рендерят
compose-проект с уже сгенерированными метками (дословный вывод
:func:`deploycli.traefik_labels.service_labels`) через ``docker compose
config`` при заданном ``COMPOSE_PROJECT_NAME`` и проверяют утверждения
ADR-003 поверх модели, разобранной из отрендеренного результата.

Дифференциальный тест дополнительно рендерит тот же самый compose-файл с
двумя разными значениями ``COMPOSE_PROJECT_NAME`` и проверяет, что
пространства имён не пересекаются — это единственный тест здесь, которому
для смысла обязательно требуется настоящий рендер (а не собранная вручную
модель), поэтому он и живёт под маркером ``docker``.
"""

from pathlib import Path

import pytest

from deploycli.traefik_labels import PublicRoute, service_labels
from support.traefik_assertions import (
    assert_all_names_prefixed,
    assert_each_public_service_has_exactly_one_router,
    assert_no_name_conflicts,
    assert_non_public_services_disabled,
    assert_path_prefix_router_outranks_plain_on_shared_domain,
    assert_routers_reference_existing_services,
)
from support.traefik_render import TraefikModel, render_traefik_model, write_compose_project

pytestmark = pytest.mark.docker


def _write_stack(project_dir: Path) -> None:
    """Стек из трёх сервисов: два публичных на общем домене (алиас и путь) и один непубличный."""
    write_compose_project(
        project_dir,
        services={
            "web": service_labels(
                "web", PublicRoute(domains=("example.com", "www.example.com"), port=3000)
            ),
            "api": service_labels(
                "api",
                PublicRoute(domains=("example.com",), port=8080, path_prefix="/api"),
            ),
            "worker": service_labels("worker", None),
        },
    )


def test_rendered_stack_satisfies_all_adr003_assertions(tmp_path: Path) -> None:
    _write_stack(tmp_path)

    model = render_traefik_model(tmp_path, "myproj")

    assert_all_names_prefixed(model, "myproj-")
    assert_no_name_conflicts(model)
    assert_routers_reference_existing_services(model)
    assert_each_public_service_has_exactly_one_router(model, public_service_names=["web", "api"])
    assert_non_public_services_disabled(model, public_service_names=["web", "api"])
    assert_path_prefix_router_outranks_plain_on_shared_domain(model)


def test_two_project_names_yield_disjoint_traefik_namespaces(tmp_path: Path) -> None:
    _write_stack(tmp_path)

    first = render_traefik_model(tmp_path, "first")
    second = render_traefik_model(tmp_path, "second")

    def _all_names(model: TraefikModel) -> set[str]:
        return (
            {router.name for router in model.routers}
            | {service.name for service in model.services}
            | {middleware.name for middleware in model.middlewares}
        )

    names_first = _all_names(first)
    names_second = _all_names(second)

    assert names_first, "рендер не дал ни одного имени — проверять нечего"
    assert names_first.isdisjoint(names_second)


def test_two_project_names_disagree_on_which_service_owns_a_router(tmp_path: Path) -> None:
    # Тот же дифференциальный факт с другой стороны: не просто "имена разные",
    # а конкретный роутер публичного сервиса "web" называется по-разному в
    # каждом контуре — то, что и защищает от коллизии между контурами на
    # одном хосте (ADR-003).
    _write_stack(tmp_path)

    first = render_traefik_model(tmp_path, "first")
    second = render_traefik_model(tmp_path, "second")

    first_web_router = next(
        view.router_names for view in first.compose_services if view.compose_name == "web"
    )
    second_web_router = next(
        view.router_names for view in second.compose_services if view.compose_name == "web"
    )

    assert first_web_router == {"first-web"}
    assert second_web_router == {"second-web"}
