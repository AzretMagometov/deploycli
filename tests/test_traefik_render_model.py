"""Тесты разбора отрендеренных меток Traefik в модель (ADR-003, dpc-tm3.26).

Эти тесты не запускают docker: они строят :class:`ServiceFacts` вручную —
так же, как их строит :func:`deploycli.compose_facts.scan_project` из
вывода ``docker compose config`` — и проверяют только разбор меток в
модель. Метки — дословный вывод :func:`deploycli.traefik_labels.service_labels`
с подставленным вместо ``${COMPOSE_PROJECT_NAME}`` конкретным префиксом:
подстановку в реальном рендере делает compose, здесь она сделана строкой,
чтобы не поднимать docker ради проверки одного только разбора.
"""

from types import MappingProxyType

from deploycli.compose_facts import ProjectFacts, ServiceFacts
from deploycli.traefik_labels import PublicRoute, service_labels
from support.traefik_render import parse_traefik_model


def _labels_for(service: str, route: PublicRoute | None, prefix: str) -> dict[str, str]:
    lines = service_labels(service, route)
    substituted = (line.replace("${COMPOSE_PROJECT_NAME}", prefix) for line in lines)
    return dict(pair.split("=", 1) for pair in substituted)


def _service_facts(name: str, labels: dict[str, str]) -> ServiceFacts:
    return ServiceFacts(
        name=name,
        image="scratch",
        networks=("default",),
        published_ports=(),
        traefik_labels=MappingProxyType(labels),
    )


def test_parses_single_public_service_into_router_service_and_middleware() -> None:
    route = PublicRoute(domains=("example.com",), port=3000)
    project = ProjectFacts(
        name="myproj", services=(_service_facts("web", _labels_for("web", route, "myproj")),)
    )

    model = parse_traefik_model(project)

    assert [r.name for r in model.routers] == ["myproj-web"]
    router = model.routers[0]
    assert router.rule == "Host(`example.com`)"
    assert router.entrypoint == "websecure"
    assert router.priority == 1
    assert router.service_name == "myproj-web"
    assert router.middlewares == ("myproj-hsts",)

    assert [s.name for s in model.services] == ["myproj-web"]
    assert model.services[0].port == 3000

    assert [m.name for m in model.middlewares] == ["myproj-hsts"]
    assert dict(model.middlewares[0].params) == {"headers.stsSeconds": "31536000"}

    [view] = model.compose_services
    assert view.compose_name == "web"


def test_public_service_compose_view_is_enabled_with_its_own_router() -> None:
    route = PublicRoute(domains=("example.com",), port=3000)
    project = ProjectFacts(
        name="myproj", services=(_service_facts("web", _labels_for("web", route, "myproj")),)
    )

    model = parse_traefik_model(project)

    [view] = model.compose_services
    assert view.compose_name == "web"
    assert view.traefik_enable == "true"
    assert view.router_names == frozenset({"myproj-web"})


def test_non_public_service_has_no_router_and_is_disabled() -> None:
    project = ProjectFacts(
        name="myproj", services=(_service_facts("worker", _labels_for("worker", None, "myproj")),)
    )

    model = parse_traefik_model(project)

    assert model.routers == ()
    assert model.services == ()
    assert model.middlewares == ()
    [view] = model.compose_services
    assert view.compose_name == "worker"
    assert view.traefik_enable == "false"
    assert view.router_names == frozenset()


def test_hsts_middleware_repeated_identically_across_services_is_not_a_conflict() -> None:
    route_a = PublicRoute(domains=("a.example.com",), port=3000)
    route_b = PublicRoute(domains=("b.example.com",), port=4000)
    project = ProjectFacts(
        name="myproj",
        services=(
            _service_facts("app-a", _labels_for("app-a", route_a, "myproj")),
            _service_facts("app-b", _labels_for("app-b", route_b, "myproj")),
        ),
    )

    model = parse_traefik_model(project)

    assert [m.name for m in model.middlewares] == ["myproj-hsts"]
    assert model.conflicts == ()


def test_two_public_services_on_shared_domain_with_and_without_path_prefix() -> None:
    plain = PublicRoute(domains=("example.com",), port=3000)
    prefixed = PublicRoute(domains=("example.com",), port=4000, path_prefix="/api")
    project = ProjectFacts(
        name="myproj",
        services=(
            _service_facts("web", _labels_for("web", plain, "myproj")),
            _service_facts("api", _labels_for("api", prefixed, "myproj")),
        ),
    )

    model = parse_traefik_model(project)

    by_name = {r.name: r for r in model.routers}
    assert by_name["myproj-web"].priority == 1
    assert by_name["myproj-api"].priority == 104


def test_missing_enable_label_is_parsed_as_none_not_as_disabled() -> None:
    # Сервис без единой метки traefik.* вовсе (compose_facts фильтрует
    # namespace traefik.* целиком, а не эту метку конкретно) — realистичный
    # случай, если базовый compose проекта не тронут генератором вообще.
    project = ProjectFacts(name="myproj", services=(_service_facts("cache", {}),))

    model = parse_traefik_model(project)

    [view] = model.compose_services
    assert view.traefik_enable is None


def test_conflicting_router_definitions_for_same_name_are_recorded() -> None:
    labels_a = _labels_for("web", PublicRoute(domains=("a.example.com",), port=3000), "myproj")
    labels_b = _labels_for("web", PublicRoute(domains=("b.example.com",), port=3000), "myproj")
    project = ProjectFacts(
        name="myproj",
        services=(
            _service_facts("first", labels_a),
            _service_facts("second", labels_b),
        ),
    )

    model = parse_traefik_model(project)

    assert len(model.conflicts) == 1
    conflict = model.conflicts[0]
    assert conflict.kind == "router"
    assert conflict.name == "myproj-web"
    assert conflict.distinct_definitions == 2
