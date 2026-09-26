"""Прогон подготовки хоста на стенде: состав, идемпотентность, отказы (ADR-006).

Скрипт исполняется настоящим bash, а машина под ним заглушена
(``tests/support/host_stand.py``). Проверяется и то, что скрипт СДЕЛАЛ
(журнал вызовов), и то, что он СКАЗАЛ (код возврата и вывод): отчёт —
часть обещания ADR-006 ровно так же, как установка пакетов.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from deploycli.host_edge import EDGE_NETWORK, render_host_edge_compose
from deploycli.host_prep import EDGE_COMPOSE_PATH, STACK_ROOT, render_prepare_host_script
from support.host_stand import DEPLOY_USER, STACK, RunResult, listener, machine, run_script

ACME_EMAIL = "ops@example.com"
STACK_DIR = f"{STACK_ROOT}/{STACK}"
READ_ONLY_UNIT_ACTIONS = frozenset({"is-active", "is-enabled", "status", "show"})


def _script(*, with_edge: bool = False) -> str:
    return render_prepare_host_script(
        stack=STACK,
        deploy_user=DEPLOY_USER,
        edge_acme_email=ACME_EMAIL if with_edge else None,
    )


def _run(tmp_path: Path, *, with_edge: bool = False, **facts: Any) -> RunResult:
    return run_script(_script(with_edge=with_edge), tmp_path, machine(**facts))


def _clean_machine(tmp_path: Path, *, with_edge: bool = False) -> RunResult:
    return _run(
        tmp_path,
        with_edge=with_edge,
        docker=False,
        network_driver=None,
        deploy_user_exists=False,
        deploy_user_groups=(),
        stack_dir_owner=None,
        ufw=None,
        fail2ban=None,
        unattended_upgrades=None,
    )


def _edge_raised(result: RunResult) -> bool:
    """Поднимал ли прогон край хоста: `docker compose version` не в счёт."""
    return result.called("docker", "compose", "-f", EDGE_COMPOSE_PATH, "up", "-d")


def _package_arguments(result: RunResult) -> set[str]:
    return {
        argument
        for call in result.calls_of("apt-get")
        if call.args[:1] == ("install",)
        for argument in call.args
    }


# --- чистая машина -----------------------------------------------------------


def test_clean_machine_gets_docker_with_the_compose_plugin(tmp_path: Path) -> None:
    result = _clean_machine(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.called("apt-get", "update")
    assert {"docker-ce", "docker-compose-plugin"} <= _package_arguments(result)
    assert result.called("systemctl", "enable", "--now", "docker")


def test_clean_machine_takes_docker_packages_from_the_official_repository(tmp_path: Path) -> None:
    # Пакет docker-compose-v2 из репозитория Ubuntu не обещает порога 2.24.4
    # (ADR-011, ADR-012), поэтому источник — репозиторий самого docker.
    result = _clean_machine(tmp_path)
    keyring = [call for call in result.calls_of("curl") if "download.docker.com" in call.line]
    sources = [call for call in result.calls_of("tee") if "docker.sources" in call.line]

    assert keyring, "ключ репозитория docker не скачан"
    assert sources, "источник пакетов docker не записан"


def test_clean_machine_gets_the_external_network_of_the_contract(tmp_path: Path) -> None:
    result = _clean_machine(tmp_path)

    assert result.called("docker", "network", "create", EDGE_NETWORK)


def test_clean_machine_gets_the_deploy_user_without_sudo_and_in_the_docker_group(
    tmp_path: Path,
) -> None:
    result = _clean_machine(tmp_path)

    assert result.called("useradd")
    assert result.called("usermod", "-aG", "docker", DEPLOY_USER)
    assert not any("sudo" in call.line for call in result.calls_of("usermod"))
    assert not any("sudo" in call.line for call in result.calls_of("useradd"))


def test_clean_machine_gets_the_stack_directory_owned_by_the_deploy_user(tmp_path: Path) -> None:
    result = _clean_machine(tmp_path)
    created = [call for call in result.calls_of("install") if call.args[-1] == STACK_DIR]

    assert created, "каталог стека не создан"
    assert "-d" in created[0].args
    assert DEPLOY_USER in created[0].args
    assert STACK_DIR in result.stdout


def test_clean_machine_reports_every_change_it_made(tmp_path: Path) -> None:
    result = _clean_machine(tmp_path)

    assert "docker" in result.stdout
    assert EDGE_NETWORK in result.stdout
    assert DEPLOY_USER in result.stdout
    assert "Изменений нет" not in result.stdout


# --- повторный прогон --------------------------------------------------------


def test_prepared_machine_changes_nothing_and_stays_silent_about_what_is_right(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.mutations == ()
    assert "Изменений нет" in result.stdout


def test_suitable_existing_network_is_accepted_as_is(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.called("docker", "network", "inspect")
    assert not result.called("docker", "network", "create")
    assert not result.called("docker", "network", "rm")


def test_only_the_missing_part_is_reported_on_a_partly_prepared_machine(tmp_path: Path) -> None:
    result = _run(tmp_path, stack_dir_owner="root")

    assert result.returncode == 0, result.stderr
    assert result.called("chown")
    assert STACK_DIR in result.stdout
    assert "docker" not in result.stdout.split("Изменено:")[-1]


# --- отказы до первого изменения ---------------------------------------------


def test_run_without_root_refuses(tmp_path: Path) -> None:
    result = _run(tmp_path, root=False)

    assert result.returncode != 0
    assert result.mutations == ()
    assert "root" in result.stderr


def test_foreign_process_on_an_edge_port_refuses_and_names_what_to_move_out(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, listeners=[listener(80, "nginx")])

    assert result.returncode != 0
    assert result.mutations == ()
    assert "80" in result.stderr
    assert "nginx" in result.stderr


def test_foreign_container_on_an_edge_port_refuses_and_names_the_container(tmp_path: Path) -> None:
    result = _run(tmp_path, port_owners={443: ("legacy-proxy", "foreign")})

    assert result.returncode != 0
    assert result.mutations == ()
    assert "443" in result.stderr
    assert "legacy-proxy" in result.stderr


def test_container_holding_the_port_is_found_even_when_ss_sees_no_listener(
    tmp_path: Path,
) -> None:
    # При выключенном userland-прокси docker публикует порт правилом в
    # netfilter, и слушающего процесса в выводе ss нет вовсе; владельца порта
    # в этом случае знает только сам docker.
    result = _run(tmp_path, port_owners={80: ("legacy-proxy", "foreign")}, userland_proxy=False)

    assert result.returncode != 0
    assert result.mutations == ()
    assert "legacy-proxy" in result.stderr


def test_foreign_edge_on_the_contract_network_is_accepted_without_the_flag(tmp_path: Path) -> None:
    # ADR-002: край хоста может быть собран владельцем машины сам; подготовка
    # без флага его не трогает, а соответствие контракту проверяет предполёт.
    result = _run(
        tmp_path,
        port_owners={
            80: ("owners-traefik", "foreign-edge"),
            443: ("owners-traefik", "foreign-edge"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert not _edge_raised(result)


def test_foreign_edge_refuses_when_the_flag_asks_for_our_own_edge(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        with_edge=True,
        port_owners={
            80: ("owners-traefik", "foreign-edge"),
            443: ("owners-traefik", "foreign-edge"),
        },
    )

    assert result.returncode != 0
    assert result.mutations == ()
    assert "owners-traefik" in result.stderr


def test_network_with_an_unsuitable_driver_refuses_instead_of_recreating_it(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, network_driver="macvlan")

    assert result.returncode != 0
    assert result.mutations == ()
    assert "macvlan" in result.stderr
    assert EDGE_NETWORK in result.stderr


def test_deploy_user_with_sudo_refuses_and_does_not_strip_privileges(tmp_path: Path) -> None:
    result = _run(tmp_path, deploy_user_sudo=True)

    assert result.returncode != 0
    assert result.mutations == ()
    assert DEPLOY_USER in result.stderr
    assert not result.called("usermod")
    assert not result.called("chmod")


def test_missing_ss_refuses_instead_of_skipping_the_port_check(tmp_path: Path) -> None:
    result = _run(tmp_path, ss_available=False)

    assert result.returncode != 0
    assert result.mutations == ()
    assert "ss" in result.stderr


def test_stopped_docker_daemon_refuses_instead_of_starting_it(tmp_path: Path) -> None:
    result = _run(tmp_path, docker_daemon=False)

    assert result.returncode != 0
    assert result.mutations == ()
    assert not result.called("systemctl", "start", "docker")
    assert not result.called("systemctl", "enable", "--now", "docker")


def test_machine_without_apt_refuses_before_touching_anything(tmp_path: Path) -> None:
    result = _run(tmp_path, docker=False, apt=False)

    assert result.returncode != 0
    assert result.mutations == ()
    assert "apt-get" in result.stderr


def test_missing_docker_group_stops_the_run_instead_of_a_user_outside_it(tmp_path: Path) -> None:
    # Группу docker заводит сам пакет docker; её отсутствие — состояние, в
    # котором деплой-пользователь до демона не дотянется, а тихо оставить
    # его снаружи группы подготовка не может.
    result = _run(tmp_path, docker_group=False, deploy_user_groups=("deploy",))

    assert result.returncode != 0
    assert "docker" in result.stderr
    assert not result.called("usermod")


def test_unreadable_os_release_stops_the_install_before_apt(tmp_path: Path) -> None:
    result = _run(tmp_path, docker=False, os_codename=None)

    assert result.returncode != 0
    assert "os-release" in result.stderr
    assert result.calls_of("apt-get") == ()


def test_all_refusals_are_reported_in_one_run(tmp_path: Path) -> None:
    result = _run(tmp_path, root=False, network_driver="macvlan", deploy_user_sudo=True)

    assert result.returncode != 0
    assert "root" in result.stderr
    assert "macvlan" in result.stderr
    assert DEPLOY_USER in result.stderr


# --- край хоста по флагу -----------------------------------------------------


def test_edge_flag_raises_the_edge_from_the_shared_template(tmp_path: Path) -> None:
    result = _run(tmp_path, with_edge=True)
    written = [path.read_text() for path in result.written]

    assert result.returncode == 0, result.stderr
    assert render_host_edge_compose(ACME_EMAIL) in written
    assert _edge_raised(result)
    # Запуск края обязан попасть в список изменений прогона: иначе проверки
    # «отказ ничего не изменил» в этом файле были бы слепы к нему.
    assert any(call.command == "docker" for call in result.mutations)


def test_edge_compose_is_installed_next_to_nothing_else(tmp_path: Path) -> None:
    result = _run(tmp_path, with_edge=True)
    installs = [call for call in result.calls_of("install") if call.args[-1] == EDGE_COMPOSE_PATH]

    assert installs, "compose края не установлен"
    assert not result.called("docker", "network", "rm")


def test_without_the_flag_there_is_no_edge_but_the_network_is_created(tmp_path: Path) -> None:
    result = _run(tmp_path, network_driver=None)

    assert result.returncode == 0, result.stderr
    assert result.called("docker", "network", "create", EDGE_NETWORK)
    assert not _edge_raised(result)
    assert "traefik" not in result.stdout


def test_unchanged_running_edge_is_left_alone_on_a_repeat_run(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        with_edge=True,
        edge_compose="current",
        edge_running=True,
        port_owners={80: ("host-edge-edge-1", "ours"), 443: ("host-edge-edge-1", "ours")},
    )

    assert result.returncode == 0, result.stderr
    assert result.mutations == ()
    assert "Изменений нет" in result.stdout


def test_stale_edge_compose_is_rewritten_and_the_edge_restarted(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        with_edge=True,
        edge_compose="stale",
        edge_running=True,
        port_owners={80: ("host-edge-edge-1", "ours"), 443: ("host-edge-edge-1", "ours")},
    )

    assert result.returncode == 0, result.stderr
    assert _edge_raised(result)
    assert "Изменений нет" not in result.stdout


def test_running_edge_with_current_compose_is_not_restarted(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        with_edge=True,
        edge_compose="current",
        edge_running=True,
        port_owners={80: ("host-edge-edge-1", "ours"), 443: ("host-edge-edge-1", "ours")},
    )

    assert not _edge_raised(result)


def test_stopped_edge_with_current_compose_is_started_again(tmp_path: Path) -> None:
    result = _run(tmp_path, with_edge=True, edge_compose="current", edge_running=False)

    assert _edge_raised(result)


def test_script_survives_delivery_through_stdin(tmp_path: Path) -> None:
    # Настоящий канал доставки — 'ssh <хост> sudo bash -s': текст скрипта
    # приходит по stdin, и потомок, читающий stdin, съел бы его остаток.
    # Тело целиком лежит в main, поэтому bash разбирает его до первой команды.
    result = run_script(
        _script(with_edge=True),
        tmp_path,
        machine(edge_compose="current", edge_running=True),
        through_stdin=True,
    )
    written = [path.read_text() for path in result.written]

    assert result.returncode == 0, result.stderr
    assert "Изменений нет" in result.stdout
    assert render_host_edge_compose(ACME_EMAIL) in written


# --- отчёт о гигиене ---------------------------------------------------------


def _hygiene_section(stdout: str) -> str:
    section = stdout.split("Гигиена машины")[1]
    return section.split("Изменен")[0]


@pytest.mark.parametrize("area", ["Брандмауэр", "fail2ban", "SSH", "втообновлени"])
def test_hygiene_report_names_every_area_left_outside(tmp_path: Path, area: str) -> None:
    result = _run(tmp_path)

    assert area in _hygiene_section(result.stdout)


def test_hygiene_report_tells_what_is_missing_on_a_bare_machine(tmp_path: Path) -> None:
    result = _clean_machine(tmp_path)
    section = _hygiene_section(result.stdout)

    assert "не установлен" in section
    assert section.count("не установлен") >= 2


def test_hygiene_report_offers_no_commands(tmp_path: Path) -> None:
    # ADR-006: печатать команды для чужой области — тот же груз в облегчённом
    # виде. Отчёт называет состояние и молчит о том, как его менять.
    section = _hygiene_section(_run(tmp_path).stdout)
    forbidden = re.compile(
        r"apt(-get)?\s+install|systemctl\s+(enable|start)|ufw\s+(enable|allow)|sudo\s|"
        r"выполнит|запустит|командой|`",
        re.IGNORECASE,
    )

    assert not forbidden.search(section), section


def test_hygiene_check_changes_no_rule(tmp_path: Path) -> None:
    result = _run(tmp_path, ufw="inactive", fail2ban=None, unattended_upgrades=None)

    assert result.returncode == 0, result.stderr
    assert not result.calls_of("ufw") or all(
        call.args[:1] == ("status",) for call in result.calls_of("ufw")
    )
    assert all(
        call.args[0] in READ_ONLY_UNIT_ACTIONS or call.args[-1] == "docker"
        for call in result.calls_of("systemctl")
    )
    assert all(call.args[:1] == ("-T",) for call in result.calls_of("sshd"))


def test_hygiene_report_is_printed_even_when_nothing_changed(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert "Гигиена машины" in result.stdout
    assert "Изменений нет" in result.stdout
