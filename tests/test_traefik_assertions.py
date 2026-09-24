"""Тесты утверждений над моделью Traefik (ADR-003, dpc-tm3.26).

Каждое утверждение проверено на успех и на искусственно испорченный вход:
искусственный вход собран из dataclass-ов модели напрямую, без разбора
меток и без docker — это тесты самих утверждений, а не разбора.
"""

from types import MappingProxyType

import pytest

from support.traefik_assertions import (
    assert_all_names_prefixed,
    assert_each_public_service_has_exactly_one_router,
    assert_no_name_conflicts,
    assert_non_public_services_disabled,
    assert_path_prefix_router_outranks_plain_on_shared_domain,
    assert_routers_reference_existing_services,
)
from support.traefik_render import (
    ComposeServiceView,
    Middleware,
    NameConflict,
    Router,
    Service,
    TraefikModel,
)


def _router(
    name: str,
    *,
    rule: str = "Host(`example.com`)",
    priority: int = 1,
    service_name: str | None = None,
) -> Router:
    return Router(
        name=name,
        rule=rule,
        entrypoint="websecure",
        priority=priority,
        service_name=service_name if service_name is not None else name,
        middlewares=(f"{name}-hsts",),
    )


def test_all_names_prefixed_passes_when_every_name_starts_with_prefix() -> None:
    model = TraefikModel(
        routers=(_router("myproj-web"),),
        services=(Service(name="myproj-web", port=3000),),
        middlewares=(Middleware(name="myproj-hsts", params=MappingProxyType({})),),
    )

    assert_all_names_prefixed(model, "myproj-")


def test_all_names_prefixed_fails_when_a_service_name_lacks_the_prefix() -> None:
    model = TraefikModel(
        routers=(_router("myproj-web"),),
        services=(Service(name="other-web", port=3000),),
    )

    with pytest.raises(AssertionError):
        assert_all_names_prefixed(model, "myproj-")


def test_no_name_conflicts_passes_when_model_has_no_conflicts() -> None:
    model = TraefikModel(conflicts=())

    assert_no_name_conflicts(model)


def test_no_name_conflicts_fails_when_model_reports_a_conflict() -> None:
    model = TraefikModel(
        conflicts=(NameConflict(kind="router", name="myproj-web", distinct_definitions=2),)
    )

    with pytest.raises(AssertionError):
        assert_no_name_conflicts(model)


def test_routers_reference_existing_services_passes_when_backend_exists() -> None:
    model = TraefikModel(
        routers=(_router("myproj-web", service_name="myproj-web"),),
        services=(Service(name="myproj-web", port=3000),),
    )

    assert_routers_reference_existing_services(model)


def test_routers_reference_existing_services_fails_on_dangling_reference() -> None:
    model = TraefikModel(
        routers=(_router("myproj-web", service_name="myproj-ghost"),),
        services=(Service(name="myproj-web", port=3000),),
    )

    with pytest.raises(AssertionError):
        assert_routers_reference_existing_services(model)


def test_each_public_service_has_exactly_one_router_passes() -> None:
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(
                compose_name="web",
                traefik_enable="true",
                router_names=frozenset({"myproj-web"}),
            ),
        )
    )

    assert_each_public_service_has_exactly_one_router(model, public_service_names=["web"])


def test_each_public_service_has_exactly_one_router_fails_when_router_missing() -> None:
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(compose_name="web", traefik_enable="true", router_names=frozenset()),
        )
    )

    with pytest.raises(AssertionError):
        assert_each_public_service_has_exactly_one_router(model, public_service_names=["web"])


def test_each_public_service_has_exactly_one_router_fails_when_two_routers() -> None:
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(
                compose_name="web",
                traefik_enable="true",
                router_names=frozenset({"myproj-web", "myproj-web-extra"}),
            ),
        )
    )

    with pytest.raises(AssertionError):
        assert_each_public_service_has_exactly_one_router(model, public_service_names=["web"])


def test_non_public_services_disabled_passes_when_traefik_explicitly_disabled() -> None:
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(
                compose_name="worker", traefik_enable="false", router_names=frozenset()
            ),
        )
    )

    assert_non_public_services_disabled(model, public_service_names=["web"])


def test_non_public_services_disabled_fails_when_non_public_service_is_enabled() -> None:
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(
                compose_name="worker", traefik_enable="true", router_names=frozenset()
            ),
        )
    )

    with pytest.raises(AssertionError):
        assert_non_public_services_disabled(model, public_service_names=["web"])


def test_non_public_services_disabled_fails_when_label_is_missing_entirely() -> None:
    # Порча входа именно того рода, ради которой заведено утверждение:
    # ADR-003 требует явного traefik.enable=false потому, что поведение при
    # ОТСУТСТВИИ метки зависит от exposedByDefault края и генератором не
    # проверяемо. Отсутствие — не то же самое, что явный false.
    model = TraefikModel(
        compose_services=(
            ComposeServiceView(
                compose_name="worker", traefik_enable=None, router_names=frozenset()
            ),
        )
    )

    with pytest.raises(AssertionError):
        assert_non_public_services_disabled(model, public_service_names=["web"])


def test_path_prefix_router_outranks_plain_on_shared_domain_passes() -> None:
    model = TraefikModel(
        routers=(
            _router("myproj-web", rule="Host(`example.com`)", priority=1),
            _router(
                "myproj-api",
                rule="Host(`example.com`) && PathPrefix(`/api`)",
                priority=104,
            ),
        )
    )

    assert_path_prefix_router_outranks_plain_on_shared_domain(model)


def test_path_prefix_router_outranks_plain_on_shared_domain_fails_on_equal_priority() -> None:
    # Порча входа: приоритет роутера с путём занижен до приоритета голого
    # роутера на том же домене — ровно коллизия, которую приоритет обязан
    # предотвращать (ADR-003).
    model = TraefikModel(
        routers=(
            _router("myproj-web", rule="Host(`example.com`)", priority=1),
            _router(
                "myproj-api",
                rule="Host(`example.com`) && PathPrefix(`/api`)",
                priority=1,
            ),
        )
    )

    with pytest.raises(AssertionError):
        assert_path_prefix_router_outranks_plain_on_shared_domain(model)


def test_path_prefix_router_outranks_plain_on_shared_domain_ignores_unrelated_domains() -> None:
    model = TraefikModel(
        routers=(
            _router("myproj-web", rule="Host(`example.com`)", priority=1),
            _router(
                "myproj-api", rule="Host(`other.example.com`) && PathPrefix(`/api`)", priority=1
            ),
        )
    )

    assert_path_prefix_router_outranks_plain_on_shared_domain(model)
