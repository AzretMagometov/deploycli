"""Тесты блока меток Traefik для одного сервиса.

Golden-файлы в ``tests/fixtures/traefik_labels/*.txt`` написаны вручную по
ADR-003, до реализации: они фиксируют требуемый вывод, а не то, что
случайно выдал шаблон.
"""

from pathlib import Path

import pytest

from deploycli.traefik_labels import PublicRoute, service_labels
from deploycli.traefik_names import InvalidNameError

FIXTURES = Path(__file__).parent / "fixtures" / "traefik_labels"


def _golden(name: str) -> tuple[str, ...]:
    return tuple((FIXTURES / name).read_text().splitlines())


def test_single_domain_without_path_prefix() -> None:
    route = PublicRoute(domains=("example.com",), port=3000)

    assert service_labels("web", route) == _golden("single_domain.txt")


def test_apex_and_www_aliases_joined_with_or() -> None:
    route = PublicRoute(domains=("example.com", "www.example.com"), port=3000)

    assert service_labels("web", route) == _golden("apex_and_www.txt")


def test_domain_with_path_prefix() -> None:
    route = PublicRoute(domains=("example.com",), port=8080, path_prefix="/api")

    assert service_labels("api", route) == _golden("path_prefix.txt")


def test_two_services_same_domain_different_path_prefixes() -> None:
    route_a = PublicRoute(domains=("example.com",), port=3000, path_prefix="/a")
    route_b = PublicRoute(domains=("example.com",), port=4000, path_prefix="/b")

    assert service_labels("app-a", route_a) == _golden("two_services_same_domain_a.txt")
    assert service_labels("app-b", route_b) == _golden("two_services_same_domain_b.txt")


def test_aliases_combined_with_path_prefix_wrap_host_group() -> None:
    # `&&` в правиле Traefik связывает только последний Host(...), поэтому при
    # сочетании алиасов домена с префиксом пути группа алиасов оборачивается
    # скобками — иначе запрос по первому алиасу без префикса тоже совпал бы.
    route = PublicRoute(domains=("example.com", "www.example.com"), port=5000, path_prefix="/shop")

    assert service_labels("shop", route) == _golden("aliases_with_path_prefix.txt")


def test_not_public_service_gets_single_disable_label() -> None:
    assert service_labels("worker", None) == _golden("not_public.txt")


def test_not_public_service_name_is_not_validated() -> None:
    # Непубличный сервис не входит в пространство имён Traefik (ADR-003),
    # поэтому имя вне алфавита его не касается вовсе.
    assert service_labels("Not_In_Alphabet", None) == ("traefik.enable=false",)


def test_public_service_name_outside_alphabet_raises_from_traefik_names() -> None:
    route = PublicRoute(domains=("example.com",), port=3000)

    with pytest.raises(InvalidNameError):
        service_labels("Not_In_Alphabet", route)


def test_rendering_is_deterministic_across_repeated_calls() -> None:
    route = PublicRoute(domains=("example.com", "www.example.com"), port=3000, path_prefix="/shop")

    first = service_labels("shop", route)
    second = service_labels("shop", route)

    assert first == second
    assert "\n".join(first) == "\n".join(second)
