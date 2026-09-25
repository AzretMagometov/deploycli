"""Стенд подготовки хоста: настоящий bash, заглушённая поверхность машины.

Живого VPS в тестах нет (зафиксировано при чартинге карты), а скрипт
подготовки ставит пакеты, заводит пользователя и поднимает контейнеры —
запускать его на машине разработчика нельзя. Поэтому здесь настоящий
``bash`` исполняет настоящий текст скрипта, а всё, чем скрипт трогает
машину и узнаёт её состояние, подменено заглушками: ``docker``,
``apt-get``, ``useradd``, ``ss``, ``stat``, ``systemctl`` и прочие.

Граница проведена по смыслу, а не по удобству: заглушены команды, которые
СООБЩАЮТ состояние машины или МЕНЯЮТ его. Разбор текста (``sed``,
``grep``, ``awk``, ``cut``, ``tr``, ``head``) остаётся настоящим — иначе
проверялась бы не логика скрипта, а сценарий заглушек. ``PATH`` во время
прогона состоит из одного каталога заглушек, поэтому команда, которой в
сценарии нет, для скрипта не существует: так выражается «на машине нет
docker».

Каждый вызов заглушки пишется в журнал, и тест смотрит и на журнал (что
скрипт сделал), и на код возврата с выводом (что он сказал).
"""

import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

BASH = shutil.which("bash") or "/bin/bash"

DEPLOY_USER = "deploy"
STACK = "shop"

_REAL_TOOLS = ("sed", "grep", "awk", "cut", "tr", "head", "sort")
"""Настоящие инструменты разбора текста: в каталог заглушек идут ссылками."""

_STUBBED = frozenset(
    {
        "apt-get",
        "cat",
        "chmod",
        "chown",
        "curl",
        "docker",
        "dpkg",
        "fail2ban-client",
        "getent",
        "id",
        "install",
        "mktemp",
        "rm",
        "sha256sum",
        "ss",
        "sshd",
        "stat",
        "sudo",
        "systemctl",
        "tee",
        "ufw",
        "useradd",
        "usermod",
    }
)
"""Поверхность машины: команды, которые сообщают её состояние или меняют его."""

MUTATING = frozenset({"apt-get", "chmod", "chown", "curl", "install", "tee", "useradd", "usermod"})
"""Команды, меняющие машину; у ``docker`` меняющие подкоманды названы ниже."""

MUTATING_DOCKER = ("network create", "network rm", "compose up", "compose down", "run", "start")

_WANT_HASH = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
_STALE_HASH = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

_OS_RELEASE = 'PRETTY_NAME="Ubuntu 24.04.3 LTS"\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\n'

_DEFAULT_SSHD = {"permitrootlogin": "prohibit-password", "passwordauthentication": "no"}


@dataclass(frozen=True, slots=True)
class Reply:
    """Ответ заглушки на вызов, аргументы которого начинаются с ``args``.

    Правила проверяются по порядку, отвечает первое совпавшее. Вызов, не
    совпавший ни с одним правилом, получает код 0 и пустой вывод.
    """

    args: tuple[str, ...] = ()
    returncode: int = 0
    stdout: str = ""


@dataclass(frozen=True, slots=True)
class Machine:
    """Сценарий машины: команды с ответами и те, что появятся после установки.

    ``installed_by_apt`` описывает единственный переход состояния, который
    скрипт делает сам: ``apt-get install`` пакетов docker превращает
    отсутствующую команду в существующую. До установки заглушки такой
    команды в ``PATH`` нет вовсе, а ``apt-get`` кладёт её туда сам.
    """

    commands: Mapping[str, tuple[Reply, ...]]
    installed_by_apt: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Call:
    """Один вызов заглушки: имя команды и её аргументы."""

    command: str
    args: tuple[str, ...]

    @property
    def line(self) -> str:
        return " ".join((self.command, *self.args))


@dataclass(frozen=True, slots=True)
class RunResult:
    """Результат прогона скрипта на стенде."""

    returncode: int
    stdout: str
    stderr: str
    calls: tuple[Call, ...]
    workdir: Path

    def called(self, command: str, *args: str) -> bool:
        """Был ли вызов ``command``, аргументы которого начинаются с ``args``."""
        return any(call.args[: len(args)] == args for call in self.calls_of(command))

    def calls_of(self, command: str) -> tuple[Call, ...]:
        return tuple(call for call in self.calls if call.command == command)

    @property
    def mutations(self) -> tuple[Call, ...]:
        """Вызовы, изменившие машину: установка, заведение, запуск, запись."""
        return tuple(
            call
            for call in self.calls
            if call.command in MUTATING
            or (
                call.command == "docker"
                and any(" ".join(call.args).startswith(sub) for sub in MUTATING_DOCKER)
            )
        )

    @property
    def written(self) -> tuple[Path, ...]:
        """Файлы, собранные скриптом через ``mktemp``: их читает тест."""
        return tuple(sorted((self.workdir / "mktemp").iterdir()))


def listener(port: int, process: str = "docker-proxy") -> str:
    """Строка вывода ``ss -H -ltnp`` о процессе, слушающем порт."""
    return f'LISTEN 0 4096 0.0.0.0:{port} 0.0.0.0:* users:(("{process}",pid=742,fd=6))\n'


def machine(
    *,
    root: bool = True,
    docker: bool = True,
    compose_plugin: bool = True,
    docker_daemon: bool = True,
    apt: bool = True,
    os_codename: str | None = "noble",
    ss_available: bool = True,
    network_driver: str | None = "bridge",
    listeners: Sequence[str] = (),
    port_owners: Mapping[int, tuple[str, str]] | None = None,
    userland_proxy: bool = True,
    edge_running: bool = False,
    edge_compose: str | None = None,
    deploy_user: str = DEPLOY_USER,
    deploy_user_exists: bool = True,
    deploy_user_groups: Sequence[str] = (DEPLOY_USER, "docker"),
    deploy_user_sudo: bool = False,
    docker_group: bool = True,
    stack_dir_owner: str | None = DEPLOY_USER,
    ufw: str | None = "active",
    fail2ban: str | None = "active",
    fail2ban_ssh_jail: bool = True,
    sshd: Mapping[str, str] | None = None,
    unattended_upgrades: str | None = "enabled",
) -> Machine:
    """Собирает сценарий машины: какие команды на ней есть и что они отвечают.

    Значения по умолчанию описывают уже подготовленную машину. Тест
    меняет ровно тот факт, который проверяет.

    ``port_owners`` отвечает на вопрос, кто занял порт: ключ — порт,
    значение — имя контейнера и его род (``ours`` — край, поднятый
    подготовкой, ``foreign-edge`` — чужой прокси в сети края,
    ``foreign`` — посторонний контейнер). Порт из ``port_owners``
    появляется и в выводе ``ss`` сам, отдельной строки не требует.
    ``edge_compose``: ``None`` — файла края на машине нет, ``current`` —
    совпадает с тем, что собирает подготовка, ``stale`` — отличается.
    ``userland_proxy`` выключают, чтобы описать машину, где опубликованный
    контейнером порт не виден в ``ss`` слушающим процессом.
    """
    owners = dict(port_owners or {})
    ss_lines = list(listeners) + [
        listener(port)
        for port in sorted(owners)
        if userland_proxy and not any(f":{port} " in line for line in listeners)
    ]
    scenario: dict[str, list[Reply]] = {
        "id": [
            Reply(("-u", deploy_user), 0 if deploy_user_exists else 1, "1001\n"),
            Reply(("-nG", deploy_user), 0, " ".join(deploy_user_groups) + "\n"),
            Reply(("-gn", deploy_user), 0, (deploy_user_groups or ("nogroup",))[0] + "\n"),
            Reply(("-u",), 0, ("0" if root else "1000") + "\n"),
        ],
        "cat": [Reply(("/etc/os-release",), 0 if os_codename else 1, _os_release(os_codename))],
        "getent": [Reply(("group", "docker"), 0 if docker_group else 2)],
        "sudo": [Reply(("-n", "-l", "-U", deploy_user), 0 if deploy_user_sudo else 1)],
        "stat": [
            Reply(("-c", "%U"), 0, f"{stack_dir_owner}\n")
            if stack_dir_owner
            else Reply(("-c", "%U"), 1)
        ],
        "sha256sum": _sha256sum_replies(edge_compose),
        "sshd": _sshd_replies(_DEFAULT_SSHD if sshd is None else sshd),
        "systemctl": _systemctl_replies(fail2ban, unattended_upgrades),
        "dpkg": [Reply(("--print-architecture",), 0, "amd64\n")],
        "chmod": [],
        "chown": [],
        "curl": [],
        "install": [],
        "rm": [],
        "tee": [],
        "useradd": [],
        "usermod": [],
    }
    if ss_available:
        scenario["ss"] = [Reply(("-H", "-ltnp"), 0, "".join(ss_lines))]
    if apt:
        scenario["apt-get"] = []
    if ufw is not None:
        scenario["ufw"] = [Reply(("status",), 0, f"Status: {ufw}\n")]
    if fail2ban is not None:
        scenario["fail2ban-client"] = [Reply((), 0 if fail2ban_ssh_jail else 1)]
    scenario["docker"] = _docker_replies(
        compose_plugin=compose_plugin,
        docker_daemon=docker_daemon,
        network_driver=network_driver,
        owners=owners,
        edge_running=edge_running,
    )
    return Machine(
        commands={key: tuple(value) for key, value in scenario.items()},
        installed_by_apt=() if docker else ("docker",),
    )


def _os_release(codename: str | None) -> str:
    return (
        _OS_RELEASE
        if codename == "noble"
        else (f"VERSION_CODENAME={codename}\n" if codename else "")
    )


def _sha256sum_replies(edge_compose: str | None) -> list[Reply]:
    current = {"current": _WANT_HASH, "stale": _STALE_HASH}.get(edge_compose or "")
    file_reply = (
        Reply(("/opt/host-edge/docker-compose.yml",), 0, f"{current}  /opt/host-edge\n")
        if current
        else Reply(("/opt/host-edge/docker-compose.yml",), 1)
    )
    return [file_reply, Reply((), 0, f"{_WANT_HASH}  -\n")]


def _sshd_replies(sshd: Mapping[str, str]) -> list[Reply]:
    if not sshd:
        return [Reply(("-T",), 1)]
    return [Reply(("-T",), 0, "".join(f"{key} {value}\n" for key, value in sshd.items()))]


def _systemctl_replies(fail2ban: str | None, unattended_upgrades: str | None) -> list[Reply]:
    replies = [
        Reply(("is-active", "fail2ban"), 0 if fail2ban == "active" else 3, f"{fail2ban}\n")
        if fail2ban
        else Reply(("is-active", "fail2ban"), 4, "inactive\n"),
        Reply(
            ("is-enabled", "fail2ban"),
            0 if fail2ban else 4,
            "enabled\n" if fail2ban else "not-found\n",
        ),
    ]
    unit = "unattended-upgrades"
    if unattended_upgrades is None:
        replies.append(Reply(("is-enabled", unit), 4, "not-found\n"))
    else:
        replies.append(
            Reply(
                ("is-enabled", unit),
                0 if unattended_upgrades == "enabled" else 1,
                f"{unattended_upgrades}\n",
            )
        )
    return replies


def _docker_replies(
    *,
    compose_plugin: bool,
    docker_daemon: bool,
    network_driver: str | None,
    owners: Mapping[int, tuple[str, str]],
    edge_running: bool,
) -> list[Reply]:
    replies = [
        Reply(("info",), 0 if docker_daemon else 1),
        Reply(("compose", "version"), 0 if compose_plugin else 1, "Docker Compose version v2.40.2"),
        Reply(
            ("network", "inspect"),
            0 if network_driver else 1,
            f"{network_driver}\n" if network_driver else "",
        ),
    ]
    for port, (name, kind) in sorted(owners.items()):
        publish = ("ps", "--filter", f"publish={port}")
        replies.append(Reply((*publish, "--filter", "label="), 0, _name(name, kind == "ours")))
        replies.append(
            Reply(
                (*publish, "--filter", "network="),
                0,
                _name(name, kind in {"ours", "foreign-edge"}),
            )
        )
        replies.append(Reply(publish, 0, _name(name, True)))
    replies.append(Reply(("ps", "--filter", "label="), 0, _name("host-edge-edge-1", edge_running)))
    return replies


def _name(name: str, shown: bool) -> str:
    return f"{name}\n" if shown else ""


def run_script(script: str, workdir: Path, scenario: Machine | None = None) -> RunResult:
    """Исполняет текст скрипта настоящим bash на заглушённой машине."""
    target = scenario if scenario is not None else machine()
    rules = dict(target.commands)
    stub_dir = workdir / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    pending_dir = workdir / "pending"
    pending_dir.mkdir(exist_ok=True)
    mktemp_dir = workdir / "mktemp"
    mktemp_dir.mkdir(exist_ok=True)
    log = workdir / "calls.log"
    log.write_text("")

    for tool in _REAL_TOOLS:
        real = shutil.which(tool, path="/usr/bin:/bin:/usr/sbin:/sbin")
        link = stub_dir / tool
        if real is not None and not link.exists():
            link.symlink_to(real)
    for command, replies in rules.items():
        _write_stub(
            pending_dir if command in target.installed_by_apt else stub_dir,
            command,
            replies,
            installs=tuple(
                (pending_dir / name, stub_dir / name) for name in target.installed_by_apt
            )
            if command == "apt-get"
            else (),
        )
    _write_stub(stub_dir, "mktemp", (), mktemp_dir=mktemp_dir)
    if "cat" not in rules:
        _write_stub(stub_dir, "cat", ())

    script_path = workdir / "prepare-host.sh"
    script_path.write_text(script)
    completed = subprocess.run(
        [BASH, str(script_path)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=workdir,
        env={"PATH": str(stub_dir), "HOME": str(workdir), "DPC_STUB_LOG": str(log), "LC_ALL": "C"},
        check=False,
    )
    return RunResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        calls=_read_calls(log),
        workdir=workdir,
    )


def _write_stub(
    stub_dir: Path,
    command: str,
    replies: tuple[Reply, ...],
    *,
    mktemp_dir: Path | None = None,
    installs: tuple[tuple[Path, Path], ...] = (),
) -> None:
    if command not in _STUBBED:
        raise ValueError(f"команда '{command}' не входит в заглушаемую поверхность машины")
    body = [
        # PATH прогона состоит из одного каталога заглушек, поэтому bash в
        # заголовке заглушки назван полным путём: искать его негде.
        f"#!{BASH}",
        "# Заглушка стенда подготовки хоста: журналирует вызов и отвечает по сценарию.",
        f"printf '%s' {_quote(command)} >> \"$DPC_STUB_LOG\"",
        'for arg in "$@"; do printf \'\\t%s\' "$arg" >> "$DPC_STUB_LOG"; done',
        "printf '\\n' >> \"$DPC_STUB_LOG\"",
    ]
    if command == "tee":
        # Настоящий tee читает stdin; заглушка обязана его вычитать, иначе
        # пишущая сторона конвейера получает SIGPIPE, которого на машине нет.
        body.append("/bin/cat >/dev/null")
    for source, installed in installs:
        # Установка пакетов docker кладёт заглушку docker в PATH: до неё
        # команды на машине нет вовсе.
        body.append(
            f'case "$*" in *docker-ce*) /bin/cp {_quote(str(source))} {_quote(str(installed))}'
            " ;; esac"
        )
    if mktemp_dir is not None:
        body += [
            f"target={_quote(str(mktemp_dir))}/$$-$RANDOM",
            'printf "" > "$target"',
            "printf '%s\\n' \"$target\"",
            "exit 0",
        ]
    else:
        body.append('case "$*" in')
        for reply in replies:
            printer = f"printf '%s' {_quote(reply.stdout)}; " if reply.stdout else ""
            body.append(f"  {_pattern(reply.args)}) {printer}exit {reply.returncode} ;;")
        body.append("esac")
        body.append(_fallback(command))
    path = stub_dir / command
    path.unlink(missing_ok=True)
    path.write_text("\n".join(body) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fallback(command: str) -> str:
    # `cat` заглушается только ради /etc/os-release: остальные его вызовы —
    # сборка текста самим скриптом, и их обслуживает настоящий cat.
    real = shutil.which(command, path="/bin:/usr/bin") if command == "cat" else None
    return f'exec {real} "$@"' if real else "exit 0"


def _pattern(args: tuple[str, ...]) -> str:
    return _quote(" ".join(args)) + "*" if args else "*"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _read_calls(log: Path) -> tuple[Call, ...]:
    calls = []
    for line in log.read_text().splitlines():
        command, *args = line.split("\t")
        calls.append(Call(command=command, args=tuple(args)))
    return tuple(calls)
