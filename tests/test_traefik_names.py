"""Tests for Traefik name assembly, alphabet validation, and router priority."""

import pytest

from deploycli.traefik_names import (
    COMPOSE_PROJECT_NAME_LITERAL,
    InvalidNameError,
    hsts_handler_name,
    router_priority,
    router_service_name,
    validate_segment,
)

INVALID_SEGMENTS = [
    ("App", "заглавная буква"),
    ("app.v2", "точка"),
    ("app_v2", "подчёркивание"),
    ("app@x", "собака"),
    ("1app", "ведущая цифра"),
    ("", "пустое имя"),
    ("-app", "ведущий дефис"),
]

VALID_SEGMENTS = ["a", "my-service", "a-b-c", "app2"]


@pytest.mark.parametrize("name,_reason", INVALID_SEGMENTS)
def test_validate_segment_rejects_invalid_names(name: str, _reason: str) -> None:
    with pytest.raises(InvalidNameError) as excinfo:
        validate_segment(name)
    assert name in str(excinfo.value)
    assert "[a-z][a-z0-9-]*" in str(excinfo.value)


@pytest.mark.parametrize("name", VALID_SEGMENTS)
def test_validate_segment_accepts_valid_names(name: str) -> None:
    validate_segment(name)


@pytest.mark.parametrize("name,_reason", INVALID_SEGMENTS)
def test_router_service_name_rejects_invalid_service(name: str, _reason: str) -> None:
    with pytest.raises(InvalidNameError) as excinfo:
        router_service_name("ns", name)
    assert name in str(excinfo.value)


@pytest.mark.parametrize("name", VALID_SEGMENTS)
def test_router_service_name_builds_ns_dash_service(name: str) -> None:
    assert router_service_name("ns", name) == f"ns-{name}"


def test_router_service_name_accepts_compose_literal_prefix_unchecked() -> None:
    assert (
        router_service_name(COMPOSE_PROJECT_NAME_LITERAL, "app")
        == f"{COMPOSE_PROJECT_NAME_LITERAL}-app"
    )


def test_validate_segment_rejects_compose_literal_itself() -> None:
    """Литерал годится как префикс, но сам по алфавиту сегмента не проходит.

    Это тот же путь, которым позже воспользуется предполётная проверка
    (ADR-003) для настоящего значения префикса.
    """
    with pytest.raises(InvalidNameError):
        validate_segment(COMPOSE_PROJECT_NAME_LITERAL)


def test_hsts_handler_name_builds_ns_dash_hsts() -> None:
    assert hsts_handler_name("ns") == "ns-hsts"


def test_hsts_handler_name_accepts_compose_literal_prefix_unchecked() -> None:
    assert hsts_handler_name(COMPOSE_PROJECT_NAME_LITERAL) == f"{COMPOSE_PROJECT_NAME_LITERAL}-hsts"


def test_router_priority_without_path_is_one() -> None:
    assert router_priority() == 1
    assert router_priority("") == 1


def test_router_priority_with_path_is_100_plus_length() -> None:
    assert router_priority("/api") == 104
    assert router_priority("/api/v2") == 107


def test_router_priority_orders_longer_path_above_shorter_above_no_path() -> None:
    assert router_priority("/api/v2") > router_priority("/api") > router_priority("")
