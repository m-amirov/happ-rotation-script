#!/usr/bin/env python3
"""Rotate servers inside an existing Happ Desktop subscription on Windows.

This script does NOT generate or import a new Xray configuration. It drives the
existing Happ UI through Windows UI Automation and clicks a server row inside a
subscription that is already present in Happ.

Designed for Happ Desktop 4.3.x on Windows.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG = "config.json"
DEFAULT_STATE = ".happ-rotation-state.json"


class RotationError(RuntimeError):
    pass


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\ufe0f", "")
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


@dataclass(frozen=True)
class RotationConfig:
    subscription: str
    servers: tuple[str, ...]
    mode: str = "round-robin"
    interval_seconds: int = 600
    settle_seconds: float = 4.0
    happ_window_regex: str = r"^Happ(?:\s|$).*"
    reconnect_mode: str = "happ"
    require_subscription_visible: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RotationConfig":
        subscription = str(raw.get("subscription", "")).strip()
        if not subscription:
            raise RotationError("config: 'subscription' must not be empty")

        servers_raw = raw.get("servers")
        if not isinstance(servers_raw, list) or not servers_raw:
            raise RotationError("config: 'servers' must be a non-empty array")

        servers = tuple(str(x).strip() for x in servers_raw if str(x).strip())
        if len(servers) < 2:
            raise RotationError("config: at least two servers are required")

        mode = str(raw.get("mode", "round-robin")).strip().lower()
        if mode not in {"round-robin", "random"}:
            raise RotationError("config: mode must be 'round-robin' or 'random'")

        interval = int(raw.get("interval_seconds", 600))
        if interval < 10:
            raise RotationError("config: interval_seconds must be at least 10")

        settle = float(raw.get("settle_seconds", 4.0))
        if settle < 0:
            raise RotationError("config: settle_seconds must be >= 0")

        reconnect_mode = str(raw.get("reconnect_mode", "happ")).strip().lower()
        if reconnect_mode not in {"happ", "none"}:
            raise RotationError("config: reconnect_mode must be 'happ' or 'none'")

        return cls(
            subscription=subscription,
            servers=servers,
            mode=mode,
            interval_seconds=interval,
            settle_seconds=settle,
            happ_window_regex=str(raw.get("happ_window_regex", r"^Happ(?:\s|$).*")),
            reconnect_mode=reconnect_mode,
            require_subscription_visible=bool(raw.get("require_subscription_visible", True)),
        )


def load_config(path: Path) -> RotationConfig:
    if not path.exists():
        raise RotationError(
            f"Config file not found: {path}. Copy config.example.json to config.json first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RotationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RotationError("config root must be a JSON object")
    return RotationConfig.from_dict(raw)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(path: Path, server: str) -> None:
    data = {
        "last_server": server,
        "updated_at_unix": int(time.time()),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def choose_next_server(
    servers: Iterable[str],
    last_server: str | None,
    mode: str,
    rng: random.Random | None = None,
) -> str:
    items = list(servers)
    if not items:
        raise RotationError("No servers configured")

    if mode == "round-robin":
        if last_server in items:
            return items[(items.index(last_server) + 1) % len(items)]
        return items[0]

    if mode == "random":
        rng = rng or random.Random()
        if len(items) == 1:
            return items[0]
        candidates = [x for x in items if x != last_server]
        return rng.choice(candidates)

    raise RotationError(f"Unsupported rotation mode: {mode}")


class HappUI:
    def __init__(self, window_regex: str):
        self.window_regex = re.compile(window_regex, re.IGNORECASE)
        self.window = None

    @staticmethod
    def _import_pywinauto():
        if sys.platform != "win32":
            raise RotationError("Happ UI automation is supported only on Windows")
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RotationError(
                "pywinauto is not installed. Run: py -m pip install -r requirements.txt"
            ) from exc
        return Desktop

    @staticmethod
    def _name(control: Any) -> str:
        try:
            name = control.window_text()
            if name:
                return str(name)
        except Exception:
            pass
        try:
            return str(control.element_info.name or "")
        except Exception:
            return ""

    @staticmethod
    def _control_type(control: Any) -> str:
        try:
            return str(control.element_info.control_type or "")
        except Exception:
            return ""

    @staticmethod
    def _rectangle(control: Any):
        try:
            return control.rectangle()
        except Exception:
            return None

    def connect(self) -> Any:
        Desktop = self._import_pywinauto()
        desktop = Desktop(backend="uia")

        candidates = []
        for win in desktop.windows():
            try:
                title = win.window_text()
                if self.window_regex.search(title or "") and win.is_visible():
                    candidates.append(win)
            except Exception:
                continue

        if not candidates:
            raise RotationError(
                "Happ window not found. Start Happ and open the Servers page."
            )

        # Prefer the largest visible matching window, not tray/tool windows.
        candidates.sort(
            key=lambda w: (
                (w.rectangle().width() * w.rectangle().height())
                if self._rectangle(w)
                else 0
            ),
            reverse=True,
        )
        self.window = candidates[0]

        try:
            if self.window.is_minimized():
                self.window.restore()
        except Exception:
            pass

        return self.window

    def controls(self) -> list[Any]:
        if self.window is None:
            self.connect()
        try:
            return list(self.window.descendants())
        except Exception as exc:
            raise RotationError(f"Unable to enumerate Happ UI controls: {exc}") from exc

    def inspect_lines(self) -> list[str]:
        lines = []
        for control in self.controls():
            name = self._name(control).strip()
            ctype = self._control_type(control)
            rect = self._rectangle(control)
            if not name:
                continue
            if rect:
                pos = f"({rect.left},{rect.top})-({rect.right},{rect.bottom})"
            else:
                pos = "(no-rect)"
            lines.append(f"[{ctype}] {name!r} {pos}")
        return lines

    def _matching_controls(self, needle: str) -> list[Any]:
        wanted = normalize_text(needle)
        exact = []
        partial = []
        for control in self.controls():
            name = self._name(control)
            normalized = normalize_text(name)
            if not normalized:
                continue
            if normalized == wanted:
                exact.append(control)
            elif wanted in normalized:
                partial.append(control)

        matches = exact or partial

        # A server name may also be displayed in the right-side details area.
        # Prefer the left-most matching UI element, which corresponds to the
        # subscription/server list in Happ Desktop.
        def sort_key(control: Any):
            rect = self._rectangle(control)
            left = rect.left if rect else 10**9
            top = rect.top if rect else 10**9
            area = rect.width() * rect.height() if rect else 10**9
            return (left, top, area)

        return sorted(matches, key=sort_key)

    def has_text(self, text: str) -> bool:
        return bool(self._matching_controls(text))

    def _invoke_or_click(self, control: Any) -> None:
        # Prefer UIA InvokePattern, because it does not depend on coordinates.
        for candidate in [control, *self._parents(control, limit=4)]:
            try:
                iface = getattr(candidate, "iface_invoke", None)
                if iface is not None:
                    iface.Invoke()
                    return
            except Exception:
                pass

        # Fallback to a real mouse click on the matching text/row.
        for candidate in [control, *self._parents(control, limit=4)]:
            try:
                candidate.click_input()
                return
            except Exception:
                continue

        raise RotationError(
            f"Found UI element {self._name(control)!r}, but could not click it"
        )

    @staticmethod
    def _parents(control: Any, limit: int) -> list[Any]:
        out = []
        cur = control
        for _ in range(limit):
            try:
                cur = cur.parent()
            except Exception:
                break
            if cur is None:
                break
            out.append(cur)
        return out

    def ensure_subscription_and_server_visible(
        self, subscription: str, server: str, require_subscription_visible: bool
    ) -> None:
        if self.has_text(server):
            return

        subs = self._matching_controls(subscription)
        if not subs:
            if require_subscription_visible:
                raise RotationError(
                    f"Subscription {subscription!r} was not found in the visible Happ UI"
                )
            return

        # Server is not visible; the subscription is probably collapsed.
        self._invoke_or_click(subs[0])
        time.sleep(1.0)

        if not self.has_text(server):
            raise RotationError(
                f"Server {server!r} is not visible after expanding subscription "
                f"{subscription!r}. Check config.json or run --inspect."
            )

    def select_server(
        self, subscription: str, server: str, require_subscription_visible: bool = True
    ) -> None:
        self.ensure_subscription_and_server_visible(
            subscription, server, require_subscription_visible
        )
        matches = self._matching_controls(server)
        if not matches:
            raise RotationError(
                f"Server {server!r} was not found. Run --inspect to see names exposed by Happ."
            )
        self._invoke_or_click(matches[0])


def rotate_once(
    cfg: RotationConfig,
    state_path: Path,
    explicit_server: str | None = None,
    dry_run: bool = False,
) -> str:
    state = load_state(state_path)
    last = state.get("last_server")
    target = explicit_server or choose_next_server(cfg.servers, last, cfg.mode)

    if explicit_server and explicit_server not in cfg.servers:
        raise RotationError(
            f"Server {explicit_server!r} is not present in config.json servers"
        )

    print(f"Target server: {target}")

    if dry_run:
        print("DRY RUN: Happ was not changed")
        return target

    ui = HappUI(cfg.happ_window_regex)
    ui.connect()
    ui.select_server(
        subscription=cfg.subscription,
        server=target,
        require_subscription_visible=cfg.require_subscription_visible,
    )

    # Happ Desktop normally reconnects when another server is selected while
    # connected. We deliberately do not click the power button here: that
    # avoids a second, unnecessary disconnect/reconnect cycle. If a future
    # Happ build stops auto-switching, reconnect_mode can be extended without
    # changing the rotation/state logic.
    if cfg.settle_seconds:
        time.sleep(cfg.settle_seconds)

    save_state(state_path, target)
    print(f"OK: selected {target}")
    return target


def run_watch(cfg: RotationConfig, state_path: Path, dry_run: bool) -> int:
    print(
        f"Rotation started: subscription={cfg.subscription!r}, "
        f"mode={cfg.mode}, interval={cfg.interval_seconds}s"
    )
    print("Press Ctrl+C to stop.")

    while True:
        try:
            rotate_once(cfg, state_path, dry_run=dry_run)
        except RotationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)

        try:
            time.sleep(cfg.interval_seconds)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rotate server rows inside an existing Happ Desktop subscription."
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, help="path to config.json"
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE, help="rotation state file"
    )
    parser.add_argument(
        "--once", action="store_true", help="perform one server switch and exit"
    )
    parser.add_argument(
        "--server", help="select this configured server once and exit"
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="print text controls exposed by the current Happ window and exit",
    )
    parser.add_argument(
        "--inspect-output",
        help="also save --inspect output to this UTF-8 text file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="choose targets but do not click Happ",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        if args.inspect:
            ui = HappUI(r"^Happ(?:\s|$).*")
            ui.connect()
            lines = ui.inspect_lines()
            payload = "\n".join(lines) + ("\n" if lines else "")
            print(payload, end="")
            if args.inspect_output:
                Path(args.inspect_output).write_text(payload, encoding="utf-8")
                print(f"Saved: {args.inspect_output}", file=sys.stderr)
            return 0

        cfg = load_config(Path(args.config))
        state_path = Path(args.state)

        if args.server:
            rotate_once(
                cfg, state_path, explicit_server=args.server, dry_run=args.dry_run
            )
            return 0

        if args.once:
            rotate_once(cfg, state_path, dry_run=args.dry_run)
            return 0

        return run_watch(cfg, state_path, args.dry_run)

    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
