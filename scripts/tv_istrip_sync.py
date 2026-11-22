#!/usr/bin/env python3
"""Mantiene sincronizado el estado de la TV LG con el del la tira LED iStrip.

Este es un script standalone, que habla con la TV usando ``aiowebostv``e invoca el comando
``istrip`` para cambiar el estado del LED. El programa ``istrip`` está en el repositorio
https://github.com/Rehzet/iStrip_plus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

_LOGGER = logging.getLogger("tv-istrip-sync")


def _config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "tv-istrip-sync" / "webos-keys.json"


def _load_client_key(tv_ip: str) -> str | None:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("No se pudo leer la clave guardada de la TV: %s", err)
        return None
    key = data.get(tv_ip) if isinstance(data, dict) else None
    return key if isinstance(key, str) and key else None


def _save_client_key(tv_ip: str, key: str) -> None:
    path = _config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get(tv_ip) == key:
        return

    data[tv_ip] = key
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError("debe ser un número de segundos") from err
    if seconds <= 0:
        raise argparse.ArgumentTypeError("debe ser mayor que cero")
    return seconds


async def _run_istrip(command: str, action: str, strip_mac: str) -> None:
    process = await asyncio.create_subprocess_exec(
        command,
        action,
        "--address",
        strip_mac,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if output:
        print(output.decode(errors="replace").rstrip(), flush=True)
    if process.returncode:
        raise RuntimeError(f"istrip {action} terminó con código {process.returncode}")


async def _watch(args: argparse.Namespace) -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError("aiowebostv necesita Python 3.12 o posterior")
    try:
        from aiowebostv import WebOsClient
    except ImportError as err:
        raise RuntimeError(
            "Falta aiowebostv. Instálalo en el entorno de este script con "
            "'python -m pip install aiowebostv'."
        ) from err

    command = shutil.which(args.istrip_command)
    if command is None:
        raise RuntimeError(
            f"No encuentro '{args.istrip_command}'. Indica la ruta completa con "
            "--istrip-command."
        )

    client = WebOsClient(args.tv_ip, _load_client_key(args.tv_ip))
    last_power: bool | None = None
    last_command_at: float | None = None

    async def set_strip_power(is_on: bool, reason: str) -> None:
        nonlocal last_power, last_command_at
        now = time.monotonic()
        if is_on == last_power:
            # If the strip turns itself back on while the TV is off, repeat
            # the off command periodically instead of assuming it stayed off.
            if is_on or last_command_at is None:
                return
            if now - last_command_at < args.off_retry:
                return

        action = "on" if is_on else "off"
        _LOGGER.info("%s; envío istrip %s", reason, action)
        try:
            await _run_istrip(command, action, args.strip_mac)
        except (OSError, RuntimeError) as err:
            _LOGGER.error("No se pudo cambiar la tira: %s", err)
            return

        last_power = is_on
        last_command_at = time.monotonic()

    async def state_changed(tv_state: object) -> None:
        power_state = getattr(tv_state, "power_state", {})
        # The TV can close the connection without sending a final power-state
        # event. The reconnect loop treats that connection failure as off.
        if not power_state or not power_state.get("state"):
            return

        is_on = bool(getattr(tv_state, "is_on", False))
        await set_strip_power(
            is_on,
            "La TV informa que está encendida" if is_on else "La TV informa que está en espera",
        )

    await client.register_state_update_callback(state_changed)
    _LOGGER.info(
        "Vigilando la TV %s. En el primer inicio, acepta el emparejamiento "
        "que aparezca en la pantalla de la TV.",
        args.tv_ip,
    )

    last_connection_notice = 0.0
    try:
        while True:
            try:
                await client.connect()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                now = time.monotonic()
                if now - last_connection_notice >= 60:
                    _LOGGER.info("TV no disponible todavía (%s); volveré a intentar.", err)
                    last_connection_notice = now
                await set_strip_power(False, "TV inaccesible; fuerzo el apagado de la tira")
            else:
                if client.client_key:
                    _save_client_key(args.tv_ip, client.client_key)
                last_connection_notice = 0.0
                if not client.is_connected():
                    await set_strip_power(False, "TV desconectada; fuerzo el apagado de la tira")
            await asyncio.sleep(args.interval)
    finally:
        await client.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enciende o apaga una tira iStrip+ según el estado de una TV LG webOS. "
            "Es un programa independiente que llama al comando istrip."
        )
    )
    parser.add_argument(
        "--tv-ip", required=True, help="IP de la TV LG en la red local"
    )
    parser.add_argument(
        "--strip-mac", required=True, help="MAC Bluetooth de la tira iStrip+"
    )
    parser.add_argument(
        "--istrip-command",
        default="istrip",
        help="comando o ruta completa de istrip (predeterminado: istrip)",
    )
    parser.add_argument(
        "--interval",
        type=_positive_seconds,
        default=5.0,
        help="segundos entre intentos de conexión (predeterminado: 5)",
    )
    parser.add_argument(
        "--off-retry",
        type=_positive_seconds,
        default=30.0,
        help=(
            "segundos entre reenvíos de istrip off mientras la TV siga apagada "
            "(predeterminado: 30)"
        ),
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
    )
    args = _parser().parse_args()
    try:
        asyncio.run(_watch(args))
    except KeyboardInterrupt:
        print("\nMonitor detenido.")
    except Exception as err:
        _LOGGER.error("%s", err)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
