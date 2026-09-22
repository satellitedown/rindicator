"""Render GPU utilization on Corsair Vengeance RGB DDR5 RAM through OpenRGB.

The CLI has three commands:

* ``list``       - read-only GPU/OpenRGB inventory and capability report.
* ``run``        - sample GPU utilization and drive the configured RAM sticks.
* ``set-effect`` - persist the startup effect without touching hardware.

Exit codes: 0 on a requested shutdown, 2 for invalid configuration, unsupported
hardware capability, or an already-running writer, 1 for unavailable telemetry
or OpenRGB SDK/runtime I/O errors.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from openrgb import OpenRGBClient
from openrgb.utils import (
    DeviceType,
    ModeColors,
    OpenRGBDisconnected,
    RGBColor,
    ZoneType,
)

SDK_ADDRESS = "127.0.0.1"
SDK_PORT = 6742
SDK_CLIENT_NAME = "rindicator"
SDK_PROTOCOL_VERSION = 3
DIRECT_MODE = "Direct"

SAMPLE_INTERVAL_S = 0.25
SMOOTHING_TAU_S = 0.75
RESEND_INTERVAL_S = 5.0
DISCOVERY_TIMEOUT_S = 30.0
DISCOVERY_INTERVAL_S = 0.5
LOCK_FILENAME = "rindicator.lock"

EFFECT_GRADIENT = "gradient"
EFFECT_BAR = "bar"
EFFECTS = (EFFECT_GRADIENT, EFFECT_BAR)

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

_CONFIG_KEYS = frozenset({"gpu_uuid", "effect", "devices"})
_TARGET_KEYS = frozenset({"name", "location", "reverse"})
_UUID_RE = re.compile(r"GPU-[0-9A-Fa-f-]{16,}\Z")
_VENDOR = "corsair"
_NAME_PREFIX = "corsair vengeance"


class RindicatorError(Exception):
    """Expected failure; ``exit_code`` selects the process status."""

    exit_code = EXIT_RUNTIME_ERROR


class ConfigError(RindicatorError):
    """Invalid configuration, unsupported device capability, or a live writer."""

    exit_code = EXIT_CONFIG_ERROR


class BackendError(RindicatorError):
    """The OpenRGB SDK server is unreachable or has not exposed the targets."""


class TelemetryError(RindicatorError):
    """NVML GPU telemetry is unavailable."""


@dataclass(frozen=True)
class Target:
    """One RAM stick identified by its OpenRGB name and SDK location."""

    name: str
    location: str
    reverse: bool

    @property
    def label(self) -> str:
        return f"{self.name} @ {self.location}"


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    gpu_uuid: str
    effect: str
    devices: tuple[Target, ...]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "rindicator" / "config.json"


def _parse_target(entry: Any, index: int, source: str, seen: set[tuple[str, str]]) -> Target:
    where = f"{source}: devices[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a JSON object")
    missing = _TARGET_KEYS - set(entry)
    if missing:
        raise ConfigError(f"{where} is missing key(s) {sorted(missing)}")
    unknown = set(entry) - _TARGET_KEYS
    if unknown:
        raise ConfigError(f"{where} has unknown key(s) {sorted(unknown)}")
    name, location, reverse = entry["name"], entry["location"], entry["reverse"]
    if not isinstance(name, str) or not name:
        raise ConfigError(f'{where}: "name" must be a nonempty string')
    if not isinstance(location, str) or not location:
        raise ConfigError(f'{where}: "location" must be a nonempty string')
    if not isinstance(reverse, bool):
        raise ConfigError(f'{where}: "reverse" must be a boolean')
    identity = (name, location)
    if identity in seen:
        raise ConfigError(f"{where} duplicates device {name!r} at {location!r}")
    seen.add(identity)
    return Target(name=name, location=location, reverse=reverse)


def parse_config(data: Any, source: str) -> Config:
    """Validate already-decoded JSON into a :class:`Config`."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a JSON object")
    missing = _CONFIG_KEYS - set(data)
    if missing:
        raise ConfigError(f"{source}: missing key(s) {sorted(missing)}")
    unknown = set(data) - _CONFIG_KEYS
    if unknown:
        raise ConfigError(f"{source}: unknown key(s) {sorted(unknown)}")

    gpu_uuid = data["gpu_uuid"]
    if not isinstance(gpu_uuid, str) or not gpu_uuid:
        raise ConfigError(f'{source}: "gpu_uuid" must be a nonempty string')
    if not _UUID_RE.fullmatch(gpu_uuid):
        raise ConfigError(f'{source}: "gpu_uuid" {gpu_uuid!r} is not an NVML GPU UUID')

    effect = data["effect"]
    if effect not in EFFECTS:
        raise ConfigError(f'{source}: "effect" must be one of {list(EFFECTS)}, got {effect!r}')

    raw_devices = data["devices"]
    if not isinstance(raw_devices, list) or not raw_devices:
        raise ConfigError(f'{source}: "devices" must be a nonempty array')
    seen: set[tuple[str, str]] = set()
    devices = tuple(
        _parse_target(entry, index, source, seen) for index, entry in enumerate(raw_devices)
    )
    return Config(gpu_uuid=gpu_uuid, effect=effect, devices=devices)


def load_config(path: Path) -> Config:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse_config(data, str(path))


def serialize_config(config: Config) -> str:
    return (
        json.dumps(
            {
                "gpu_uuid": config.gpu_uuid,
                "effect": config.effect,
                "devices": [
                    {"name": target.name, "location": target.location, "reverse": target.reverse}
                    for target in config.devices
                ],
            },
            indent=2,
        )
        + "\n"
    )


def write_config_atomic(path: Path, config: Config) -> None:
    """Replace ``path`` with ``config`` through a temp file in the same directory."""
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialize_config(config))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    except OSError as exc:
        raise ConfigError(f"cannot write config {path}: {exc}") from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #


def render_colors(
    percent: float, led_count: int, effect: str, reverse: bool = False
) -> list[RGBColor]:
    """Pure blue-red gradient or rising red level bar for ``percent`` GPU load.

    ``percent`` is clamped to ``[0, 100]``; LED 0 is the configured bottom LED
    unless ``reverse`` is set, which flips the finished frame.
    """
    if not isinstance(led_count, int) or isinstance(led_count, bool) or led_count <= 0:
        raise ValueError(f"led_count must be a positive integer, got {led_count!r}")
    if effect not in EFFECTS:
        raise ValueError(f"unknown effect {effect!r}")
    if not math.isfinite(percent):
        raise ValueError(f"percent must be finite, got {percent!r}")

    fraction = min(max(percent, 0.0), 100.0) / 100.0
    if effect == EFFECT_GRADIENT:
        red = math.floor(255 * fraction + 0.5)
        colors = [RGBColor(red, 0, 255 - red)] * led_count
    else:
        fill = led_count * fraction
        colors = [
            RGBColor(math.floor(255 * min(max(fill - index, 0.0), 1.0) + 0.5), 0, 0)
            for index in range(led_count)
        ]
    if reverse:
        colors.reverse()
    return colors


def encode_frame(colors: list[RGBColor]) -> tuple[tuple[int, int, int], ...]:
    return tuple((color.red, color.green, color.blue) for color in colors)


def format_frame(colors: list[RGBColor]) -> str:
    return " ".join(f"{c.red:02x}{c.green:02x}{c.blue:02x}" for c in colors)


def _error_text(exc: BaseException) -> str:
    """Message for exceptions that carry none (e.g. OpenRGBDisconnected)."""
    return str(exc) or type(exc).__name__


def _timestamp() -> str:
    """Wall-clock ``HH:MM:SS.mmm`` from a single clock reading."""
    wall = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(wall))}.{int(wall % 1 * 1000):03d}"


def smooth(previous: float, raw: float, elapsed: float) -> float:
    """Exponential smoothing with a 0.75 s time constant, independent of jitter."""
    alpha = 1.0 - math.exp(-max(elapsed, 0.0) / SMOOTHING_TAU_S)
    return previous + alpha * (raw - previous)


# --------------------------------------------------------------------------- #
# Device capability
# --------------------------------------------------------------------------- #


def capability_problem(device: Any) -> str | None:
    """Return why ``device`` cannot be driven, or ``None`` when it can."""
    if device.type != DeviceType.DRAM:
        return f"device type is {getattr(device.type, 'name', device.type)}, not DRAM"
    if not device.name.lower().startswith(_NAME_PREFIX):
        return f"name {device.name!r} is not a Corsair Vengeance device"
    metadata = device.metadata
    if metadata is None or (metadata.vendor or "").strip().lower() != _VENDOR:
        vendor = None if metadata is None else metadata.vendor
        return f"vendor is {vendor!r}, not {_VENDOR.title()!r}"
    zones = list(device.zones)
    if len(zones) != 1:
        return f"expected exactly one zone, found {len(zones)}"
    zone = zones[0]
    if zone.type != ZoneType.LINEAR:
        return f"zone {zone.name!r} is {getattr(zone.type, 'name', zone.type)}, not LINEAR"
    if len(zone.leds) != len(device.leds):
        return f"linear zone covers {len(zone.leds)} of {len(device.leds)} LEDs"
    if len(device.leds) <= 1:
        return f"only {len(device.leds)} LED(s)"
    direct = next((mode for mode in device.modes if mode.name.lower() == DIRECT_MODE.lower()), None)
    if direct is None:
        return f"no {DIRECT_MODE!r} mode"
    if direct.color_mode != ModeColors.PER_LED:
        return f"{DIRECT_MODE!r} color mode is {direct.color_mode.name}, not PER_LED"
    return None


def _find_device(client: OpenRGBClient, target: Target) -> Any | None:
    matches = [
        device
        for device in client.devices
        if device.name == target.name
        and device.metadata is not None
        and device.metadata.location == target.location
    ]
    if len(matches) > 1:
        raise ConfigError(f"{target.label}: matched {len(matches)} OpenRGB devices")
    return matches[0] if matches else None


def _refresh(client: OpenRGBClient) -> None:
    try:
        client.update()
    except (OSError, OpenRGBDisconnected) as exc:
        raise BackendError(f"lost the OpenRGB SDK connection: {_error_text(exc)}") from exc


def resolve_targets(
    client: OpenRGBClient, targets: tuple[Target, ...], deadline: float
) -> list[Any]:
    """Wait for every configured identity and validate all of them."""
    while True:
        resolved: list[Any] = []
        missing: list[Target] = []
        for target in targets:
            device = _find_device(client, target)
            if device is None:
                missing.append(target)
                continue
            problem = capability_problem(device)
            if problem is not None:
                raise ConfigError(f"{target.label}: unsupported device: {problem}")
            resolved.append(device)
        if not missing:
            return resolved
        if time.monotonic() >= deadline:
            labels = ", ".join(target.label for target in missing)
            raise BackendError(
                f"OpenRGB did not expose {labels} within {DISCOVERY_TIMEOUT_S:.0f} s"
            )
        time.sleep(DISCOVERY_INTERVAL_S)
        _refresh(client)


def connect_client() -> OpenRGBClient:
    try:
        return OpenRGBClient(
            address=SDK_ADDRESS,
            port=SDK_PORT,
            name=SDK_CLIENT_NAME,
            protocol_version=SDK_PROTOCOL_VERSION,
        )
    except (OSError, OpenRGBDisconnected) as exc:
        raise BackendError(
            f"cannot reach the OpenRGB SDK server at {SDK_ADDRESS}:{SDK_PORT}: {_error_text(exc)}"
        ) from exc


def disconnect_client(client: OpenRGBClient) -> None:
    try:
        client.disconnect()
    except Exception as exc:  # noqa: BLE001 - teardown must not mask the real error
        print(f"rindicator: disconnect failed: {exc}", file=sys.stderr)


def write_frame(device: Any, target: Target, colors: list[RGBColor]) -> None:
    try:
        device.set_colors(colors, fast=True)
    except (OSError, struct.error, OpenRGBDisconnected) as exc:
        raise BackendError(
            f"failed to write colors to {target.label}: {_error_text(exc)}"
        ) from exc


# --------------------------------------------------------------------------- #
# NVML telemetry
# --------------------------------------------------------------------------- #


def _load_pynvml() -> Any:
    import pynvml  # imported lazily so no command touches NVML by accident

    return pynvml


class NvmlSession:
    """Read-only NVML session; a session without a UUID never resolves a handle."""

    def __init__(self, gpu_uuid: str | None = None) -> None:
        self._uuid = gpu_uuid
        self._module: Any | None = None
        self._handle: Any | None = None

    def __enter__(self) -> NvmlSession:
        return self.open()

    def open(self) -> NvmlSession:
        """Initialize NVML and bind the configured UUID."""
        module = _load_pynvml()
        try:
            module.nvmlInit()
        except Exception as exc:
            raise TelemetryError(f"NVML initialization failed: {exc}") from exc
        self._module = module
        if self._uuid is not None:
            try:
                self._handle = module.nvmlDeviceGetHandleByUUID(self._uuid)
            except Exception as exc:
                self.close()
                raise ConfigError(f"GPU {self._uuid!r} is not present in NVML: {exc}") from exc
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        module, self._module = self._module, None
        self._handle = None
        if module is None:
            return
        try:
            module.nvmlShutdown()
        except Exception:  # noqa: BLE001 - shutdown failures must not mask the real error
            pass

    def _require_module(self) -> Any:
        if self._module is None:
            raise TelemetryError("NVML session is not initialized")
        return self._module

    def sample(self) -> float:
        """Current GPU utilization percentage for the configured UUID."""
        module = self._require_module()
        if self._handle is None:
            raise TelemetryError("NVML session has no GPU handle")
        try:
            usage = module.nvmlDeviceGetUtilizationRates(self._handle)
        except Exception as exc:
            raise TelemetryError(f"GPU utilization query failed: {exc}") from exc
        return float(usage.gpu)

    def gpus(self) -> list[tuple[str, str]]:
        """All present GPUs as ``(uuid, name)`` pairs."""
        module = self._require_module()
        try:
            gpus = []
            for index in range(module.nvmlDeviceGetCount()):
                handle = module.nvmlDeviceGetHandleByIndex(index)
                name = module.nvmlDeviceGetName(handle)
                gpus.append(
                    (
                        module.nvmlDeviceGetUUID(handle),
                        name if isinstance(name, str) else name.decode(),
                    )
                )
            return gpus
        except Exception as exc:
            raise TelemetryError(f"GPU enumeration failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# Writer lock
# --------------------------------------------------------------------------- #


def acquire_writer_lock() -> IO[str]:
    """Take the per-user writer lock; raises when another writer owns it."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir or not os.path.isdir(runtime_dir):
        raise ConfigError(f"XDG_RUNTIME_DIR is not an existing directory: {runtime_dir!r}")
    path = Path(runtime_dir) / LOCK_FILENAME
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ConfigError(f"another rindicator writer holds {path}: {exc}") from exc
    return handle


def release_writer_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


# --------------------------------------------------------------------------- #
# Loops
# --------------------------------------------------------------------------- #


def install_signal_handlers(stopping: threading.Event) -> None:
    def handler(_signum: int, _frame: Any) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handler)


def control_loop(
    targets: tuple[Target, ...],
    devices: list[Any],
    effect: str,
    telemetry: NvmlSession,
    stopping: threading.Event,
) -> None:
    """Drive the owned sticks until ``stopping`` is set or a failure escapes."""
    frames: dict[int, tuple[tuple[tuple[int, int, int], ...], float]] = {}
    previous_time: float | None = None
    smoothed: float | None = None
    while not stopping.is_set():
        now = time.monotonic()
        raw = telemetry.sample()
        if smoothed is None:
            smoothed = raw
        else:
            assert previous_time is not None
            smoothed = smooth(smoothed, raw, now - previous_time)
        previous_time = now
        for index, (target, device) in enumerate(zip(targets, devices)):
            colors = render_colors(smoothed, len(device.leds), effect, target.reverse)
            frame = encode_frame(colors)
            cached = frames.get(index)
            if cached is not None and cached[0] == frame and now - cached[1] < RESEND_INTERVAL_S:
                continue
            write_frame(device, target, colors)
            frames[index] = (frame, now)
        stopping.wait(SAMPLE_INTERVAL_S)


def dry_run_loop(
    targets: tuple[Target, ...],
    devices: list[Any],
    effect: str,
    telemetry: NvmlSession,
    stopping: threading.Event,
) -> None:
    """Print samples and intended colors without writing to any device."""
    print(f"dry run: effect={effect}, {len(targets)} target(s), no writes", flush=True)
    for index, (target, device) in enumerate(zip(targets, devices)):
        print(
            f"  [{index}] {target.location} ({len(device.leds)} LEDs, "
            f"reverse={'true' if target.reverse else 'false'})",
            flush=True,
        )
    previous_time: float | None = None
    smoothed: float | None = None
    while not stopping.is_set():
        now = time.monotonic()
        raw = telemetry.sample()
        if smoothed is None:
            smoothed = raw
        else:
            assert previous_time is not None
            smoothed = smooth(smoothed, raw, now - previous_time)
        previous_time = now
        frames = " | ".join(
            f"[{index}]={format_frame(render_colors(smoothed, len(device.leds), effect, target.reverse))}"
            for index, (target, device) in enumerate(zip(targets, devices))
        )
        stamp = _timestamp()
        print(f"[{stamp}] raw={raw:5.1f}% smooth={smoothed:6.2f}% {frames}", flush=True)
        stopping.wait(SAMPLE_INTERVAL_S)


def clear_owned(targets: tuple[Target, ...], devices: list[Any]) -> None:
    """Best-effort black frame on the owned sticks; never raises."""
    for target, device in zip(targets, devices):
        try:
            device.set_colors([RGBColor(0, 0, 0)] * len(device.leds), fast=True)
        except Exception as exc:  # noqa: BLE001 - reported, never propagated
            print(
                f"rindicator: could not clear {target.label}: {_error_text(exc)} "
                "(the RAM may keep its last color)",
                file=sys.stderr,
            )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def list_command(_args: argparse.Namespace) -> int:
    """Read-only inventory; never changes modes or colors."""
    with NvmlSession() as session:
        gpus = session.gpus()
    print("GPUs (NVML):")
    for uuid, name in gpus:
        print(f"  {uuid}  {name}")

    client = connect_client()
    try:
        devices = list(client.devices)
        print(f"\nSDK devices at {SDK_ADDRESS}:{SDK_PORT}: {len(devices)}")
        for index, device in enumerate(devices):
            metadata = device.metadata
            zones = ", ".join(
                f"{zone.name}:{getattr(zone.type, 'name', zone.type)}({len(zone.leds)})"
                for zone in device.zones
            )
            modes = ", ".join(
                f"{mode.name}[{getattr(mode.color_mode, 'name', mode.color_mode)}]"
                for mode in device.modes
            )
            problem = capability_problem(device)
            print(
                f"  [{index}] {device.name}  type={getattr(device.type, 'name', device.type)}"
                f"  vendor={None if metadata is None else metadata.vendor!r}"
            )
            print(f"      location: {'' if metadata is None else metadata.location}")
            print(f"      leds={len(device.leds)}  zones={zones or 'none'}")
            print(f"      modes: {modes or 'none'}")
            if problem is None:
                print(
                    f"      usable: yes ({len(device.leds)} LEDs, {DIRECT_MODE} per-LED, "
                    f"single linear zone)"
                )
            else:
                print(f"      usable: no ({problem})")
    finally:
        disconnect_client(client)
    return EXIT_OK


def run_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    effect = args.effect if args.effect is not None else config.effect
    stopping = threading.Event()
    install_signal_handlers(stopping)

    lock: IO[str] | None = None
    telemetry: NvmlSession | None = None
    client: OpenRGBClient | None = None
    devices: list[Any] | None = None
    lighting = False
    try:
        if not args.dry_run:
            lock = acquire_writer_lock()
        telemetry = NvmlSession(config.gpu_uuid).open()
        client = connect_client()
        devices = resolve_targets(client, config.devices, time.monotonic() + DISCOVERY_TIMEOUT_S)
        if args.dry_run:
            dry_run_loop(config.devices, devices, effect, telemetry, stopping)
        else:
            for target, device in zip(config.devices, devices):
                device.set_mode(DIRECT_MODE, save=False)
                print(
                    f"rindicator: driving {target.label} "
                    f"({len(device.leds)} LEDs, {effect}, "
                    f"reverse={'true' if target.reverse else 'false'})",
                    flush=True,
                )
            lighting = True
            control_loop(config.devices, devices, effect, telemetry, stopping)
    finally:
        if client is not None:
            if lighting and devices is not None:
                clear_owned(config.devices, devices)
            disconnect_client(client)
        if telemetry is not None:
            telemetry.close()
        if lock is not None:
            release_writer_lock(lock)
    return EXIT_OK


def set_effect_command(args: argparse.Namespace) -> int:
    """Persist the effect only; never touches NVML or the OpenRGB SDK."""
    config = load_config(args.config)
    updated = Config(gpu_uuid=config.gpu_uuid, effect=args.effect, devices=config.devices)
    if updated.effect == config.effect:
        print(f"effect is already {args.effect} in {args.config}")
    else:
        write_config_atomic(args.config, updated)
        print(f"effect set to {args.effect} in {args.config}")
    print("apply with: systemctl --user restart rindicator.service")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rindicator",
        description="Render GPU utilization on Corsair Vengeance RGB DDR5 RAM via OpenRGB.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="runtime configuration JSON (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="show GPUs and OpenRGB devices without changing any lighting"
    )
    list_parser.set_defaults(handler=list_command)

    run_parser = subparsers.add_parser("run", help="drive the RAM colors from GPU utilization")
    run_parser.add_argument(
        "--effect",
        choices=EFFECTS,
        default=None,
        help="override the saved effect for this run only",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print samples and intended colors without writing to any device",
    )
    run_parser.set_defaults(handler=run_command)

    effect_parser = subparsers.add_parser(
        "set-effect", help="persist the effect used by 'run' on the next start"
    )
    effect_parser.add_argument("effect", choices=EFFECTS)
    effect_parser.set_defaults(handler=set_effect_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except RindicatorError as exc:
        print(f"rindicator: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
