#!/usr/bin/env python3
"""Rotate servers inside an existing Happ Desktop subscription on Windows.

Happ 4.3.x renders the server list as a custom UI surface that may expose no
child controls through Windows UI Automation. This implementation therefore
finds the real Happ HWND and clicks server rows by coordinates relative to the
Happ client area.

No VPN configuration, subscription URL, or Happ internal database is modified.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG = "config.json"
DEFAULT_STATE = ".happ-rotation-state.json"


class RotationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RotationConfig:
    subscription: str
    servers: tuple[str, ...]
    mode: str = "round-robin"
    interval_seconds: int = 600
    settle_seconds: float = 4.0
    happ_window_regex: str = r"^Happ(?:\s|$).*"
    process_names: tuple[str, ...] = ("happ.exe",)
    click_x_ratio: float = 0.365
    first_server_y_ratio: float = 0.322
    row_step_ratio: float = 0.0713
    server_rows: dict[str, int] | None = None

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

        process_names_raw = raw.get("process_names", ["happ.exe"])
        if not isinstance(process_names_raw, list) or not process_names_raw:
            raise RotationError("config: process_names must be a non-empty array")
        process_names = tuple(str(x).strip().casefold() for x in process_names_raw if str(x).strip())

        click_x_ratio = float(raw.get("click_x_ratio", 0.365))
        first_server_y_ratio = float(raw.get("first_server_y_ratio", 0.322))
        row_step_ratio = float(raw.get("row_step_ratio", 0.0713))

        for key, value in {
            "click_x_ratio": click_x_ratio,
            "first_server_y_ratio": first_server_y_ratio,
            "row_step_ratio": row_step_ratio,
        }.items():
            if not 0.0 < value < 1.0:
                raise RotationError(f"config: {key} must be between 0 and 1")

        server_rows_raw = raw.get("server_rows")
        server_rows: dict[str, int] | None = None
        if server_rows_raw is not None:
            if not isinstance(server_rows_raw, dict):
                raise RotationError("config: server_rows must be an object")
            server_rows = {}
            for name, row in server_rows_raw.items():
                row_i = int(row)
                if row_i < 0:
                    raise RotationError("config: server row indexes must be >= 0")
                server_rows[str(name)] = row_i

        return cls(
            subscription=subscription,
            servers=servers,
            mode=mode,
            interval_seconds=interval,
            settle_seconds=settle,
            happ_window_regex=str(raw.get("happ_window_regex", r"^Happ(?:\s|$).*")),
            process_names=process_names,
            click_x_ratio=click_x_ratio,
            first_server_y_ratio=first_server_y_ratio,
            row_step_ratio=row_step_ratio,
            server_rows=server_rows,
        )

    def row_for_server(self, server: str) -> int:
        if self.server_rows and server in self.server_rows:
            return self.server_rows[server]
        try:
            return self.servers.index(server)
        except ValueError as exc:
            raise RotationError(f"Server {server!r} is not configured") from exc


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


def load_config_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RotationError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RotationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RotationError("config root must be a JSON object")
    return raw


def save_config_raw(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(path: Path, server: str) -> None:
    data = {"last_server": server, "updated_at_unix": int(time.time())}
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
        candidates = [x for x in items if x != last_server] or items
        return rng.choice(candidates)

    raise RotationError(f"Unsupported rotation mode: {mode}")


class Win32UI:
    def __init__(self, cfg: RotationConfig):
        self.cfg = cfg

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise RotationError("Happ window control is supported only on Windows")

    @staticmethod
    def _apis():
        Win32UI._require_windows()
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return ctypes, wintypes, user32, kernel32

    @staticmethod
    def windows() -> list[dict[str, Any]]:
        ctypes, wintypes, user32, kernel32 = Win32UI._apis()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        results: list[dict[str, Any]] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def process_image(pid: int) -> str:
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return ""
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    return buf.value
                return ""
            finally:
                kernel32.CloseHandle(handle)

        @EnumWindowsProc
        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            title_buf = ctypes.create_unicode_buffer(max(length + 1, 2))
            user32.GetWindowTextW(hwnd, title_buf, len(title_buf))

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                bounds = (rect.left, rect.top, rect.right, rect.bottom)
            else:
                bounds = (0, 0, 0, 0)

            results.append(
                {
                    "handle": int(hwnd),
                    "title": title_buf.value,
                    "pid": int(pid.value),
                    "image": process_image(pid.value),
                    "rect": bounds,
                }
            )
            return True

        user32.EnumWindows(callback, 0)
        return results

    @staticmethod
    def visible_window_lines() -> list[str]:
        lines = []
        for item in Win32UI.windows():
            image = Path(item["image"]).name if item["image"] else "?"
            title = item["title"] or "<no title>"
            l, t, r, b = item["rect"]
            lines.append(
                f"HWND=0x{item['handle']:X} PID={item['pid']} EXE={image!r} "
                f"TITLE={title!r} RECT=({l},{t})-({r},{b})"
            )
        return lines

    def find_happ(self) -> dict[str, Any]:
        regex = re.compile(self.cfg.happ_window_regex, re.IGNORECASE)
        candidates = []
        for item in self.windows():
            exe = Path(item["image"]).name.casefold() if item["image"] else ""
            title = item["title"] or ""
            if exe in self.cfg.process_names or regex.search(title):
                l, t, r, b = item["rect"]
                area = max(0, r - l) * max(0, b - t)
                if area > 0:
                    candidates.append((area, item))

        if not candidates:
            raise RotationError(
                "Happ window not found. Start Happ and keep its main window open. "
                "Use --list-windows for diagnostics."
            )

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
        ctypes, wintypes, user32, _kernel32 = Win32UI._apis()
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise RotationError("GetClientRect failed for Happ window")

        top_left = wintypes.POINT(rect.left, rect.top)
        bottom_right = wintypes.POINT(rect.right, rect.bottom)
        if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            raise RotationError("ClientToScreen failed for Happ window")
        if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            raise RotationError("ClientToScreen failed for Happ window")

        return (top_left.x, top_left.y, bottom_right.x, bottom_right.y)

    @staticmethod
    def foreground(hwnd: int) -> None:
        _ctypes, _wintypes, user32, _kernel32 = Win32UI._apis()
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)

    @staticmethod
    def cursor_position() -> tuple[int, int]:
        ctypes, wintypes, user32, _kernel32 = Win32UI._apis()
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            raise RotationError("GetCursorPos failed")
        return pt.x, pt.y

    @staticmethod
    def click_screen(x: int, y: int, restore_cursor: bool = True) -> None:
        _ctypes, _wintypes, user32, _kernel32 = Win32UI._apis()
        old_x, old_y = Win32UI.cursor_position()

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004

        if not user32.SetCursorPos(int(x), int(y)):
            raise RotationError("SetCursorPos failed")
        time.sleep(0.08)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        if restore_cursor:
            time.sleep(0.08)
            user32.SetCursorPos(old_x, old_y)

    def coordinate_for_row(self, row: int) -> tuple[int, int, tuple[int, int, int, int]]:
        happ = self.find_happ()
        rect = self.client_rect_screen(happ["handle"])
        left, top, right, bottom = rect
        width = right - left
        height = bottom - top

        x = left + round(width * self.cfg.click_x_ratio)
        y_ratio = self.cfg.first_server_y_ratio + row * self.cfg.row_step_ratio
        y = top + round(height * y_ratio)

        if not (left <= x < right and top <= y < bottom):
            raise RotationError(
                f"Calculated click point ({x},{y}) is outside Happ client rect {rect}. "
                "Run --calibrate."
            )
        return x, y, rect

    def select_server(self, server: str) -> tuple[int, int]:
        row = self.cfg.row_for_server(server)
        happ = self.find_happ()
        self.foreground(happ["handle"])
        x, y, _rect = self.coordinate_for_row(row)
        self.click_screen(x, y)
        return x, y


def rotate_once(
    cfg: RotationConfig,
    state_path: Path,
    explicit_server: str | None = None,
    dry_run: bool = False,
) -> str:
    state = load_state(state_path)
    target = explicit_server or choose_next_server(
        cfg.servers, state.get("last_server"), cfg.mode
    )

    if target not in cfg.servers:
        raise RotationError(f"Server {target!r} is not present in config.json")

    ui = Win32UI(cfg)
    row = cfg.row_for_server(target)
    x, y, rect = ui.coordinate_for_row(row)
    print(f"Target server: {target}")
    print(f"Happ client: {rect}; row={row}; click=({x},{y})")

    if dry_run:
        print("DRY RUN: no click was made")
        return target

    ui.select_server(target)
    if cfg.settle_seconds:
        time.sleep(cfg.settle_seconds)

    save_state(state_path, target)
    print(f"OK: selected row for {target}")
    return target


def run_watch(cfg: RotationConfig, state_path: Path, dry_run: bool) -> int:
    print(
        f"Rotation started: subscription={cfg.subscription!r}, "
        f"mode={cfg.mode}, interval={cfg.interval_seconds}s"
    )
    print("Keep the subscription expanded in Happ. Press Ctrl+C to stop.")
    while True:
        try:
            rotate_once(cfg, state_path, dry_run=dry_run)
        except RotationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
        time.sleep(cfg.interval_seconds)


def calibrate(config_path: Path) -> int:
    cfg = load_config(config_path)
    if len(cfg.servers) < 2:
        raise RotationError("Calibration needs at least two configured servers")

    ui = Win32UI(cfg)
    happ = ui.find_happ()
    ui.foreground(happ["handle"])
    left, top, right, bottom = ui.client_rect_screen(happ["handle"])
    width = right - left
    height = bottom - top

    first = cfg.servers[0]
    second = cfg.servers[1]

    print("Calibration uses your mouse position; it does NOT click anything.")
    print(f"Move the pointer to the CENTER of the row {first!r} in Happ, then press Enter here.")
    input()
    x1, y1 = ui.cursor_position()

    print(f"Now move the pointer to the CENTER of the row {second!r}, then press Enter here.")
    input()
    x2, y2 = ui.cursor_position()

    if not (left <= x1 < right and top <= y1 < bottom):
        raise RotationError("First calibration point is outside the Happ client area")
    if not (left <= x2 < right and top <= y2 < bottom):
        raise RotationError("Second calibration point is outside the Happ client area")
    if y2 <= y1:
        raise RotationError("Second row must be below the first row")

    click_x_ratio = ((x1 + x2) / 2 - left) / width
    first_y_ratio = (y1 - top) / height
    row_step_ratio = (y2 - y1) / height

    raw = load_config_raw(config_path)
    raw["click_x_ratio"] = round(click_x_ratio, 6)
    raw["first_server_y_ratio"] = round(first_y_ratio, 6)
    raw["row_step_ratio"] = round(row_step_ratio, 6)
    save_config_raw(config_path, raw)

    print("Saved calibration:")
    print(f"  click_x_ratio={raw['click_x_ratio']}")
    print(f"  first_server_y_ratio={raw['first_server_y_ratio']}")
    print(f"  row_step_ratio={raw['row_step_ratio']}")
    return 0


def preview(cfg: RotationConfig) -> int:
    ui = Win32UI(cfg)
    print(f"Subscription: {cfg.subscription}")
    for server in cfg.servers:
        row = cfg.row_for_server(server)
        x, y, rect = ui.coordinate_for_row(row)
        print(f"{row:02d}  {server}: ({x},{y})")
    print(f"Happ client rect: {rect}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rotate server rows inside an existing Happ Desktop subscription."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--server")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--preview-clicks", action="store_true")
    parser.add_argument("--list-windows", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.list_windows:
            print("\n".join(Win32UI.visible_window_lines()))
            return 0

        config_path = Path(args.config)

        if args.calibrate:
            return calibrate(config_path)

        cfg = load_config(config_path)

        if args.preview_clicks:
            return preview(cfg)

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
