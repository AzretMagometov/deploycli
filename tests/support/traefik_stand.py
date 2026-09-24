"""Живой стенд Traefik для dpc-tm3.27: настоящие контейнеры, настоящий HTTP(S).

Отличие от ``tests/support/traefik_render.py`` (dpc-tm3.26): та оснастка
разбирает ОТРЕНДЕРЕННЫЙ ``docker compose config`` — это подтверждает
байты вывода генератора, но не поведение Traefik при слиянии одноимённых
определений (ADR-003, раздел Rationale: «golden-файл подтверждает байты,
а не смысл»). Здесь — настоящий Traefik и настоящие backend-контейнеры:
единственная проверка, которая смотрит на действительную семантику
Traefik, а не на наше представление о ней.

Метки сервисов берутся из :func:`deploycli.traefik_labels.service_labels`
(ADR-003) — имена и приоритет роутера здесь не пересчитываются заново.

Самоподписанный сертификат подаётся файловым провайдером СТЕНДА (не
сгенерированного вывода): запрет ADR-002 на файловый провайдер касается
только вывода генератора, ACME в тестах недоступен.
"""

import os
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

_TRAEFIK_IMAGE = "traefik:v3.6"
# v3.6.1+ обязателен: до этой версии докер-провайдер Traefik был жёстко
# пришит к Docker API 1.24 и падал на Docker Engine 29+ ("client version
# 1.24 is too old") — эмпирически проверено в этой сессии на traefik:v3.5.6
# + Docker Engine 29.3.1 перед тем, как остановиться на 3.6 (сам Traefik
# начиная с 3.6.1 согласовывает версию Docker API с движком автоматически).
_BACKEND_IMAGE = "hashicorp/http-echo"
BACKEND_PORT = 5678
"""Порт, на котором ``hashicorp/http-echo`` слушает внутри контейнера —
именно это значение идёт в ``PublicRoute.port`` при сборке меток backend-сервисов
стенда."""
_EDGE_NETWORK = "proxy"
# ADR-003: `traefik.docker.network=proxy` — литеральная константа контракта
# с хостом в шаблоне меток (src/deploycli/templates/service_labels.j2), а
# не параметр стенда. Чтобы докер-провайдер вообще нашёл backend-контейнеры,
# сеть с этим именем обязана существовать на хосте под этим же именем.


class StandStartupError(RuntimeError):
    """Стенд Traefik не поднялся или сеть ``proxy`` уже занята на хосте."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    """Ответ настоящего HTTP(S)-запроса к стенду."""

    status: int
    body: str


@dataclass(frozen=True, slots=True)
class LiveService:
    """Один сервис настоящего backend-стека: метки и опознаваемое тело ответа.

    ``labels`` — дословный вывод :func:`deploycli.traefik_labels.service_labels`,
    ``response_text`` — то, что вернёт ``hashicorp/http-echo`` этого контейнера,
    чтобы тест мог отличить стеки и сервисы друг от друга по телу ответа.
    """

    labels: tuple[str, ...]
    response_text: str


class TraefikStand:
    """Живой Traefik на сети ``proxy`` с самоподписанным TLS.

    Контекстный менеджер: ``__enter__`` создаёт сеть ``proxy`` и поднимает
    контейнер Traefik, ``__exit__`` гасит всё, что стенд создал (контейнеры,
    compose-стеки и сеть), не оставляя после себя ничего на хосте.
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._id = uuid.uuid4().hex[:8]
        self._traefik_container = f"dpc-e2e-traefik-{self._id}"
        self._stacks: list[tuple[Path, str]] = []
        self._network_created = False
        self._traefik_started = False
        self._https_port: int | None = None

    def __enter__(self) -> TraefikStand:
        try:
            self._ensure_network_free()
            self._create_network()
            self._start_traefik()
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cleanup()

    def up_stack(self, name: str, services: Mapping[str, LiveService]) -> None:
        """Поднимает настоящий backend-стек ``docker compose`` с ``COMPOSE_PROJECT_NAME=name``.

        Тот же механизм, которым в проде разводятся контуры (ADR-003):
        ``${COMPOSE_PROJECT_NAME}`` в метках подставляется самим compose из
        переменной окружения, а не зашивается здесь.
        """
        stack_dir = self._tmp_path / name
        stack_dir.mkdir(parents=True, exist_ok=True)
        _write_live_stack(stack_dir, services)
        self._stacks.append((stack_dir, name))
        _run(
            ["docker", "compose", "up", "-d"],
            cwd=stack_dir,
            env_overrides={"COMPOSE_PROJECT_NAME": name},
            error=StandStartupError,
        )

    def https_get(self, domain: str, path: str = "/", timeout: float = 5.0) -> HttpResult:
        """Один настоящий TLS-запрос к стенду: SNI и заголовок Host — ``domain``,
        TCP-соединение — на ``127.0.0.1:<порт стенда>`` (см. ``--resolve`` у curl).

        Проверка сертификата отключена: сертификат самоподписанный (стендовый
        файловый провайдер), проверять его нечем и незачем.
        """
        assert self._https_port is not None, "https_get вызван до __enter__"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["http/1.1"])

        raw = socket.create_connection(("127.0.0.1", self._https_port), timeout=timeout)
        try:
            with context.wrap_socket(raw, server_hostname=domain) as tls:
                request = (
                    f"GET {path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                tls.sendall(request)
                chunks = []
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        finally:
            raw.close()
        return _parse_http_response(b"".join(chunks))

    def wait_for_body(
        self, domain: str, path: str, expected: str, timeout: float = 20.0
    ) -> HttpResult:
        """Опрашивает стенд, пока тело ответа не совпадёт с ``expected``.

        Docker-провайдер Traefik подхватывает новые контейнеры асинхронно —
        сразу после ``docker compose up -d`` роутер ещё может быть не готов.
        """

        def _attempt() -> HttpResult | None:
            result = self.https_get(domain, path)
            return result if result.body.strip() == expected else None

        return _poll_until(_attempt, timeout=timeout)

    def _ensure_network_free(self) -> None:
        result = subprocess.run(
            ["docker", "network", "inspect", _EDGE_NETWORK],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            raise StandStartupError(
                f"Сеть '{_EDGE_NETWORK}' уже существует на хосте — стенд её не трогает "
                "(могла остаться от чужого проекта или от аварийно прерванного прогона; "
                f"удалить вручную: docker network rm {_EDGE_NETWORK})"
            )

    def _create_network(self) -> None:
        _run(["docker", "network", "create", _EDGE_NETWORK], error=StandStartupError)
        self._network_created = True

    def _start_traefik(self) -> None:
        dynamic_dir = self._tmp_path / "dynamic"
        dynamic_dir.mkdir(parents=True, exist_ok=True)
        _generate_self_signed_cert(dynamic_dir / "cert.pem", dynamic_dir / "key.pem")
        (dynamic_dir / "tls.yml").write_text(
            "tls:\n"
            "  stores:\n"
            "    default:\n"
            "      defaultCertificate:\n"
            "        certFile: /dynamic/cert.pem\n"
            "        keyFile: /dynamic/key.pem\n"
        )
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self._traefik_container,
                "--network",
                _EDGE_NETWORK,
                "-p",
                "127.0.0.1::80",
                "-p",
                "127.0.0.1::443",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "-v",
                f"{dynamic_dir}:/dynamic:ro",
                _TRAEFIK_IMAGE,
                "--providers.docker=true",
                "--providers.docker.exposedbydefault=false",
                f"--providers.docker.network={_EDGE_NETWORK}",
                "--providers.file.directory=/dynamic",
                "--entrypoints.web.address=:80",
                "--entrypoints.websecure.address=:443",
                "--log.level=INFO",
            ],
            error=StandStartupError,
        )
        self._traefik_started = True
        self._https_port = _poll_until(
            lambda: _published_port(self._traefik_container, 443) or None,
            timeout=15.0,
        )
        # Ждём, пока Traefik действительно начнёт принимать TLS-соединения
        # (не только пока порт опубликован докером).
        _poll_until(self._tls_handshake_ready, timeout=15.0)

    def _tls_handshake_ready(self) -> bool | None:
        assert self._https_port is not None
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with (
                socket.create_connection(("127.0.0.1", self._https_port), timeout=1.0) as raw,
                context.wrap_socket(raw, server_hostname="dpc-e2e-probe.test"),
            ):
                return True
        except OSError:
            return None

    def _cleanup(self) -> None:
        for stack_dir, name in reversed(self._stacks):
            subprocess.run(
                ["docker", "compose", "down", "--volumes", "--remove-orphans"],
                cwd=stack_dir,
                env=_env_with(name),
                capture_output=True,
                text=True,
                check=False,
            )
        self._stacks.clear()
        if self._traefik_started:
            subprocess.run(
                ["docker", "rm", "-f", self._traefik_container],
                capture_output=True,
                text=True,
                check=False,
            )
            self._traefik_started = False
        if self._network_created:
            subprocess.run(
                ["docker", "network", "rm", _EDGE_NETWORK],
                capture_output=True,
                text=True,
                check=False,
            )
            self._network_created = False


def _write_live_stack(stack_dir: Path, services: Mapping[str, LiveService]) -> None:
    """Пишет ``docker-compose.yml`` настоящего стенда: настоящий образ, команда,
    сеть ``proxy`` и метки в форме списка строк.

    Списочная форма меток обязательна (ADR-003): в форме отображения compose
    не подставляет переменную в КЛЮЧЕ метки, и ``${COMPOSE_PROJECT_NAME}``
    остался бы литералом в имени роутера.
    """
    compose = {
        "networks": {_EDGE_NETWORK: {"external": True}},
        "services": {
            name: {
                "image": _BACKEND_IMAGE,
                "command": [f"-text={service.response_text}"],
                "networks": [_EDGE_NETWORK],
                "labels": list(service.labels),
            }
            for name, service in services.items()
        },
    }
    (stack_dir / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))


def _generate_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    if shutil.which("openssl") is None:
        raise StandStartupError(
            "openssl не найден в PATH — самоподписанный сертификат нечем выпустить"
        )
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "2",
            "-nodes",
            "-subj",
            "/CN=dpc-e2e-stand",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise StandStartupError(result.stderr)


def _published_port(container: str, container_port: int) -> int | None:
    result = subprocess.run(
        ["docker", "port", container, str(container_port)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    first_line = result.stdout.strip().splitlines()[0]
    return int(first_line.rsplit(":", 1)[-1])


def _env_with(project_name: str) -> dict[str, str]:
    return {**os.environ, "COMPOSE_PROJECT_NAME": project_name}


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    error: type[Exception],
) -> None:
    env = {**os.environ, **env_overrides} if env_overrides else None
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise error(result.stderr or result.stdout)


def _poll_until[T](attempt: Callable[[], T | None], timeout: float, interval: float = 0.3) -> T:
    deadline = time.monotonic() + timeout
    while True:
        result = attempt()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise StandStartupError(f"стенд не ответил ожидаемым за {timeout}s")
        time.sleep(interval)


def _parse_http_response(raw: bytes) -> HttpResult:
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    # "HTTP/1.1 200 OK" -> 200
    status = int(status_line.split(b" ")[1])
    return HttpResult(status=status, body=body.decode("utf-8", errors="replace"))
