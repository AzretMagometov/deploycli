"""Тесты отказов генерации: четыре правила и их сообщения (ADR-002, ADR-003, dpc-tm3.3).

Golden-файлы в ``tests/fixtures/generation_refusals/*.txt`` фиксируют
дословный текст сообщений: что найдено, где именно (сервис) и что сделать
дальше.
"""

from pathlib import Path

from deploycli.compose_facts import ProjectFacts, ServiceFacts
from deploycli.generation_refusals import (
    GenerationRefused,
    ensure_generation_allowed,
    find_refusals,
)
from deploycli.traefik_labels import PublicRoute

FIXTURES = Path(__file__).parent / "fixtures" / "generation_refusals"


def _golden(name: str) -> tuple[str, ...]:
    return tuple((FIXTURES / name).read_text().splitlines())


def _service(
    name: str,
    *,
    published_ports: tuple[int, ...] = (),
    traefik_labels: dict[str, str] | None = None,
) -> ServiceFacts:
    return ServiceFacts(
        name=name,
        image="myapp:latest",
        networks=("default",),
        published_ports=published_ports,
        traefik_labels=traefik_labels or {},
    )


def test_no_violations_returns_empty_tuple_and_allows_generation() -> None:
    facts = ProjectFacts(services=(_service("web"),))
    public_routes = {"web": PublicRoute(domains=("example.com",), port=3000)}

    assert find_refusals(facts, public_routes) == ()
    ensure_generation_allowed(facts, public_routes)  # не поднимает исключение


# Правило 1: имя публичного сервиса вне алфавита ^[a-z][a-z0-9-]*$.
def test_public_service_name_outside_alphabet_is_refused() -> None:
    facts = ProjectFacts(services=(_service("Not_In_Alphabet"),))
    public_routes = {
        "Not_In_Alphabet": PublicRoute(domains=("example.com",), port=3000),
    }

    assert find_refusals(facts, public_routes) == _golden("invalid_name.txt")


# Правило 2, граничные случаи.
def test_same_domain_both_without_path_prefix_is_refused() -> None:
    facts = ProjectFacts(services=(_service("shop-a"), _service("shop-b")))
    public_routes = {
        "shop-a": PublicRoute(domains=("shop.com",), port=3000),
        "shop-b": PublicRoute(domains=("shop.com",), port=4000),
    }

    assert find_refusals(facts, public_routes) == _golden("domain_collision_no_path.txt")


def test_same_domain_one_without_path_other_with_path_is_not_refused() -> None:
    # Роутер без пути ловит остальное — это не отказ.
    facts = ProjectFacts(services=(_service("shop-a"), _service("shop-b")))
    public_routes = {
        "shop-a": PublicRoute(domains=("shop.com",), port=3000),
        "shop-b": PublicRoute(domains=("shop.com",), port=4000, path_prefix="/b"),
    }

    assert find_refusals(facts, public_routes) == ()


def test_overlapping_path_prefixes_on_same_domain_are_refused() -> None:
    facts = ProjectFacts(services=(_service("api"), _service("api-v2")))
    public_routes = {
        "api": PublicRoute(domains=("example.com",), port=3000, path_prefix="/api"),
        "api-v2": PublicRoute(domains=("example.com",), port=3001, path_prefix="/api/v2"),
    }

    assert find_refusals(facts, public_routes) == _golden("domain_collision_overlap.txt")


def test_non_overlapping_path_prefixes_on_same_domain_are_not_refused() -> None:
    facts = ProjectFacts(services=(_service("app-a"), _service("app-b")))
    public_routes = {
        "app-a": PublicRoute(domains=("example.com",), port=3000, path_prefix="/a"),
        "app-b": PublicRoute(domains=("example.com",), port=4000, path_prefix="/b"),
    }

    assert find_refusals(facts, public_routes) == ()


def test_different_domains_are_not_refused() -> None:
    facts = ProjectFacts(services=(_service("app-a"), _service("app-b")))
    public_routes = {
        "app-a": PublicRoute(domains=("a.example.com",), port=3000),
        "app-b": PublicRoute(domains=("b.example.com",), port=4000),
    }

    assert find_refusals(facts, public_routes) == ()


# Правило 3: любая метка traefik.* в базовом compose проекта, у любого сервиса.
def test_foreign_traefik_label_on_any_service_is_refused() -> None:
    facts = ProjectFacts(services=(_service("cache", traefik_labels={"traefik.enable": "true"}),))

    assert find_refusals(facts, {}) == _golden("foreign_traefik_label.txt")


# Правило 4: порт 80/443 либо любой ports: у прикладного сервиса.
def test_service_occupying_edge_port_is_refused() -> None:
    facts = ProjectFacts(services=(_service("web", published_ports=(80, 443)),))

    assert find_refusals(facts, {}) == _golden("edge_port_occupied.txt")


def test_service_with_non_edge_published_port_is_refused() -> None:
    facts = ProjectFacts(services=(_service("app", published_ports=(8080,)),))

    assert find_refusals(facts, {}) == _golden("ports_leak_non_edge.txt")


def test_service_with_edge_and_other_ports_lists_all_ports_in_one_run() -> None:
    # Сообщение обязано перечислить весь ports:, а не только 80/443: иначе
    # человек уберёт только этот порт, а второй прогон откажет снова на
    # оставшемся.
    facts = ProjectFacts(services=(_service("web", published_ports=(80, 8080)),))

    assert find_refusals(facts, {}) == _golden("edge_port_mixed_with_other.txt")


# Несколько одновременных нарушений: все попадают в вывод, а не только первое.
def test_multiple_simultaneous_violations_are_all_reported() -> None:
    facts = ProjectFacts(
        services=(
            _service("Not_In_Alphabet"),
            _service("shop-a"),
            _service("shop-b"),
            _service("cache", traefik_labels={"traefik.enable": "true"}),
            _service("web", published_ports=(80,)),
        )
    )
    public_routes = {
        "Not_In_Alphabet": PublicRoute(domains=("misc.com",), port=3000),
        "shop-a": PublicRoute(domains=("shop.com",), port=3001),
        "shop-b": PublicRoute(domains=("shop.com",), port=3002),
    }

    assert find_refusals(facts, public_routes) == _golden("multiple_violations.txt")


def test_ensure_generation_allowed_raises_with_all_messages() -> None:
    facts = ProjectFacts(services=(_service("web", published_ports=(80, 443)),))

    try:
        ensure_generation_allowed(facts, {})
    except GenerationRefused as exc:
        assert exc.messages == _golden("edge_port_occupied.txt")
    else:
        raise AssertionError("GenerationRefused ожидалось, но не поднялось")


def test_refusal_writes_nothing_to_disk(tmp_path: Path) -> None:
    # Настоящего шага записи файлов в репозитории ещё нет (dpc-tm3.25 — это
    # только правила отказа). Тест фиксирует контракт исключения:
    # ensure_generation_allowed поднимает, а не молча логирует или
    # возвращает управление, поэтому вызывающий код в форме
    # "проверка → запись" не дойдёт до записи при отказе.
    facts = ProjectFacts(services=(_service("web", published_ports=(80,)),))
    output_dir = tmp_path / "generated"
    output_dir.mkdir()

    def fake_generate() -> None:
        ensure_generation_allowed(facts, {})
        (output_dir / "docker-compose.prod.yml").write_text("never written")

    try:
        fake_generate()
    except GenerationRefused:
        pass
    else:
        raise AssertionError("GenerationRefused ожидалось, но не поднялось")

    assert list(output_dir.iterdir()) == []
