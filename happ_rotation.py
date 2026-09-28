#!/usr/bin/env python3
"""Generate an Xray JSON profile for Happ with proxy rotation.

Input: one proxy per line in proxies.txt. Supported URI formats:
- vless://
- vmess:// (base64 JSON)
- trojan://
- socks:// / socks5://
- ss:// (SIP002)

A line may also be a raw Xray outbound JSON object. This is the escape hatch for
protocols/transports that are not parsed by this script.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED_STRATEGIES = ("roundRobin", "random", "leastPing", "leastLoad")


class ConfigError(ValueError):
    pass


def _b64decode_loose(value: str) -> bytes:
    value = value.strip()
    value += "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:  # pragma: no cover
        raise ConfigError("invalid base64 data") from exc


def _first(query: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for key in keys:
        values = query.get(key)
        if values:
            return values[0]
    return default


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _port(value: Any, context: str) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{context}: invalid port {value!r}") from exc
    if not 1 <= p <= 65535:
        raise ConfigError(f"{context}: port must be 1..65535")
    return p


def _transport_settings(query: dict[str, list[str]]) -> dict[str, Any]:
    transport = _first(query, "type", "network", default="tcp").lower()
    method_map = {
        "tcp": "raw",
        "raw": "raw",
        "ws": "websocket",
        "websocket": "websocket",
        "grpc": "grpc",
        "xhttp": "xhttp",
        "httpupgrade": "httpupgrade",
        "http-upgrade": "httpupgrade",
        "kcp": "mkcp",
        "mkcp": "mkcp",
    }
    if transport not in method_map:
        raise ConfigError(
            f"unsupported transport type={transport!r}; use a raw Xray outbound JSON line instead"
        )

    method = method_map[transport]
    stream: dict[str, Any] = {"method": method}

    security = _first(query, "security", default="none").lower()
    if security not in {"none", "tls", "reality"}:
        raise ConfigError(f"unsupported transport security={security!r}")
    stream["security"] = security

    sni = _first(query, "sni", "serverName")
    fp = _first(query, "fp", "fingerprint")
    alpn = _first(query, "alpn")

    if security == "reality":
        reality: dict[str, Any] = {}
        if sni:
            reality["serverName"] = sni
        if fp:
            reality["fingerprint"] = fp
        pbk = _first(query, "pbk", "password", "publicKey")
        if pbk:
            reality["password"] = pbk
        sid = _first(query, "sid", "shortId")
        if sid:
            reality["shortId"] = sid
        spx = _first(query, "spx", "spiderX")
        if spx:
            reality["spiderX"] = spx
        stream["realitySettings"] = reality
    elif security == "tls":
        tls: dict[str, Any] = {}
        if sni:
            tls["serverName"] = sni
        if fp:
            tls["fingerprint"] = fp
        if alpn:
            tls["alpn"] = [part.strip() for part in alpn.split(",") if part.strip()]
        insecure = _first(query, "insecure", "allowInsecure")
        if insecure:
            tls["allowInsecure"] = _truthy(insecure)
        stream["tlsSettings"] = tls

    path = unquote(_first(query, "path"))
    host = _first(query, "host")
    if method == "websocket":
        ws: dict[str, Any] = {}
        if path:
            ws["path"] = path
        if host:
            ws["host"] = host
        stream["wsSettings"] = ws
    elif method == "grpc":
        grpc: dict[str, Any] = {}
        service_name = unquote(_first(query, "serviceName", "service_name", "path"))
        authority = _first(query, "authority", "host")
        if service_name:
            grpc["serviceName"] = service_name.lstrip("/")
        if authority:
            grpc["authority"] = authority
        mode = _first(query, "mode")
        if mode:
            grpc["multiMode"] = mode.lower() in {"multi", "gun"}
        stream["grpcSettings"] = grpc
    elif method == "xhttp":
        xhttp: dict[str, Any] = {}
        if path:
            xhttp["path"] = path
        if host:
            xhttp["host"] = host
        mode = _first(query, "mode")
        if mode:
            xhttp["mode"] = mode
        stream["xhttpSettings"] = xhttp
    elif method == "httpupgrade":
        hup: dict[str, Any] = {}
        if path:
            hup["path"] = path
        if host:
            hup["host"] = host
        stream["httpupgradeSettings"] = hup

    return stream


def parse_vless(uri: str) -> dict[str, Any]:
    parsed = urlsplit(uri)
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ConfigError("vless: expected vless://id@host:port?... format")
    query = parse_qs(parsed.query, keep_blank_values=True)
    settings: dict[str, Any] = {
        "address": parsed.hostname,
        "port": _port(parsed.port, "vless"),
        "id": unquote(parsed.username),
        "encryption": _first(query, "encryption", default="none"),
    }
    flow = _first(query, "flow")
    if flow:
        settings["flow"] = flow
    return {
        "protocol": "vless",
        "settings": settings,
        "streamSettings": _transport_settings(query),
    }


def parse_trojan(uri: str) -> dict[str, Any]:
    parsed = urlsplit(uri)
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ConfigError("trojan: expected trojan://password@host:port?... format")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "security" not in query:
        query["security"] = ["tls"]
    return {
        "protocol": "trojan",
        "settings": {
            "address": parsed.hostname,
            "port": _port(parsed.port, "trojan"),
            "password": unquote(parsed.username),
        },
        "streamSettings": _transport_settings(query),
        "mux": {"enabled": True},
    }


def parse_socks(uri: str) -> dict[str, Any]:
    normalized = re.sub(r"^socks5://", "socks://", uri, count=1, flags=re.I)
    parsed = urlsplit(normalized)
    if not parsed.hostname or not parsed.port:
        raise ConfigError("socks: expected socks://[user:pass@]host:port format")
    settings: dict[str, Any] = {
        "address": parsed.hostname,
        "port": _port(parsed.port, "socks"),
    }
    if parsed.username is not None:
        settings["user"] = unquote(parsed.username)
        settings["pass"] = unquote(parsed.password or "")
    return {"protocol": "socks", "settings": settings}


def _decode_vmess_payload(uri: str) -> dict[str, Any]:
    payload = uri[len("vmess://") :].split("#", 1)[0].strip()
    try:
        data = json.loads(_b64decode_loose(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("vmess: payload is not valid base64 JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("vmess: decoded payload must be an object")
    return data


def parse_vmess(uri: str) -> dict[str, Any]:
    data = _decode_vmess_payload(uri)
    address = data.get("add") or data.get("address")
    port = data.get("port")
    user_id = data.get("id")
    if not address or not port or not user_id:
        raise ConfigError("vmess: decoded JSON must contain add/address, port and id")

    security = str(data.get("tls") or data.get("securityLayer") or "none").lower()
    if security in {"", "none"}:
        transport_security = "none"
    elif security in {"tls", "reality"}:
        transport_security = security
    else:
        transport_security = "tls" if str(data.get("tls", "")).lower() == "tls" else "none"

    q: dict[str, list[str]] = {
        "type": [str(data.get("net") or data.get("type") or "tcp")],
        "security": [transport_security],
    }
    mapping = {
        "sni": "sni",
        "fp": "fp",
        "host": "host",
        "path": "path",
        "alpn": "alpn",
        "pbk": "pbk",
        "sid": "sid",
        "serviceName": "serviceName",
    }
    for source, target in mapping.items():
        value = data.get(source)
        if value not in (None, ""):
            q[target] = [str(value)]

    return {
        "protocol": "vmess",
        "settings": {
            "address": str(address),
            "port": _port(port, "vmess"),
            "id": str(user_id),
            "security": str(data.get("scy") or data.get("security") or "auto"),
        },
        "streamSettings": _transport_settings(q),
    }


def parse_shadowsocks(uri: str) -> dict[str, Any]:
    body = uri[len("ss://") :]
    body = body.split("#", 1)[0]
    body = body.split("?", 1)[0]

    method: str
    password: str
    host: str
    port: int

    if "@" in body:
        userinfo, server = body.rsplit("@", 1)
        try:
            decoded_userinfo = _b64decode_loose(userinfo).decode("utf-8")
        except (ConfigError, UnicodeDecodeError):
            decoded_userinfo = unquote(userinfo)
        if ":" not in decoded_userinfo:
            raise ConfigError("ss: userinfo must decode to method:password")
        method, password = decoded_userinfo.split(":", 1)
        parsed_server = urlsplit("ss://" + server)
        if not parsed_server.hostname or not parsed_server.port:
            raise ConfigError("ss: missing host or port")
        host = parsed_server.hostname
        port = _port(parsed_server.port, "ss")
    else:
        try:
            decoded = _b64decode_loose(body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError("ss: full payload is not valid UTF-8") from exc
        if "@" not in decoded or ":" not in decoded.split("@", 1)[0]:
            raise ConfigError("ss: full payload must decode to method:password@host:port")
        userinfo, server = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        parsed_server = urlsplit("ss://" + server)
        if not parsed_server.hostname or not parsed_server.port:
            raise ConfigError("ss: missing host or port")
        host = parsed_server.hostname
        port = _port(parsed_server.port, "ss")

    return {
        "protocol": "shadowsocks",
        "settings": {
            "address": host,
            "port": port,
            "method": method,
            "password": password,
        },
    }


def parse_proxy_line(line: str) -> dict[str, Any]:
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"raw outbound JSON: {exc.msg}") from exc
        if not isinstance(obj, dict):
            raise ConfigError("raw outbound JSON must be an object")
        if not obj.get("protocol"):
            raise ConfigError("raw outbound JSON must contain protocol")
        return obj

    scheme = stripped.split(":", 1)[0].lower()
    if scheme == "vless":
        return parse_vless(stripped)
    if scheme == "vmess":
        return parse_vmess(stripped)
    if scheme == "trojan":
        return parse_trojan(stripped)
    if scheme in {"socks", "socks5"}:
        return parse_socks(stripped)
    if scheme == "ss":
        return parse_shadowsocks(stripped)
    raise ConfigError(
        f"unsupported scheme {scheme!r}; supported: vless, vmess, trojan, socks/socks5, ss, or raw Xray JSON"
    )


def load_proxies(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ConfigError(f"input file not found: {path}")
    outbounds: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            outbound = parse_proxy_line(line)
        except ConfigError as exc:
            raise ConfigError(f"{path}:{line_no}: {exc}") from exc
        outbound = dict(outbound)
        outbound["tag"] = f"proxy-{len(outbounds) + 1:03d}"
        outbounds.append(outbound)
    if not outbounds:
        raise ConfigError(f"no proxies found in {path}")
    return outbounds


def build_config(
    proxy_outbounds: list[dict[str, Any]],
    strategy: str = "roundRobin",
    direct_ru: bool = True,
    fallback: str = "block",
    socks_port: int = 10808,
    http_port: int = 10809,
    probe_url: str = "https://www.gstatic.com/generate_204",
    probe_interval: str = "30s",
) -> dict[str, Any]:
    if strategy not in SUPPORTED_STRATEGIES:
        raise ConfigError(f"unsupported strategy: {strategy}")
    if fallback not in {"block", "direct"}:
        raise ConfigError("fallback must be block or direct")

    rules: list[dict[str, Any]] = [
        {
            "type": "field",
            "ip": [
                "geoip:private",
                "127.0.0.0/8",
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "169.254.0.0/16",
            ],
            "outboundTag": "direct",
        }
    ]
    if direct_ru:
        rules.extend(
            [
                {
                    "type": "field",
                    "domain": ["geosite:category-ru"],
                    "outboundTag": "direct",
                },
                {
                    "type": "field",
                    "ip": ["geoip:ru"],
                    "outboundTag": "direct",
                },
            ]
        )
    rules.append(
        {
            "type": "field",
            "network": "tcp,udp",
            "balancerTag": "proxy-pool",
        }
    )

    fallback_tag = "direct" if fallback == "direct" else "block"
    balancer: dict[str, Any] = {
        "tag": "proxy-pool",
        "selector": ["proxy-"],
        "fallbackTag": fallback_tag,
        "strategy": {"type": strategy},
    }

    config: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "dns": {
            "servers": [
                "https://1.1.1.1/dns-query",
                "1.1.1.1",
            ]
        },
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"udp": True},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
            },
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": http_port,
                "protocol": "http",
                "settings": {},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                    "routeOnly": True,
                },
            },
        ],
        "outbounds": [
            *proxy_outbounds,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
            "balancers": [balancer],
        },
        "observatory": {
            "subjectSelector": ["proxy-"],
            "probeUrl": probe_url,
            "probeInterval": probe_interval,
            "enableConcurrency": True,
        },
        "remarks": "Happ Rotation",
        "meta": {
            "serverDescription": f"{strategy}: {len(proxy_outbounds)} proxy node(s)"
        },
    }
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Happ/Xray JSON profile with rotating proxy outbounds."
    )
    parser.add_argument("--input", "-i", default="proxies.txt", help="input proxy list")
    parser.add_argument(
        "--output", "-o", default="happ-rotation.json", help="output Xray JSON profile"
    )
    parser.add_argument(
        "--strategy",
        choices=SUPPORTED_STRATEGIES,
        default="roundRobin",
        help="Xray balancer strategy (default: roundRobin)",
    )
    parser.add_argument(
        "--no-direct-ru",
        action="store_true",
        help="send RU traffic through the proxy pool too",
    )
    parser.add_argument(
        "--fallback",
        choices=("block", "direct"),
        default="block",
        help="what to do when every proxy is unavailable (default: block)",
    )
    parser.add_argument("--socks-port", type=int, default=10808)
    parser.add_argument("--http-port", type=int, default=10809)
    parser.add_argument("--probe-url", default="https://www.gstatic.com/generate_204")
    parser.add_argument("--probe-interval", default="30s")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        proxies = load_proxies(Path(args.input))
        config = build_config(
            proxies,
            strategy=args.strategy,
            direct_ru=not args.no_direct_ru,
            fallback=args.fallback,
            socks_port=_port(args.socks_port, "SOCKS inbound"),
            http_port=_port(args.http_port, "HTTP inbound"),
            probe_url=args.probe_url,
            probe_interval=args.probe_interval,
        )
        output = Path(args.output)
        output.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"OK: generated {output} with {len(proxies)} proxy node(s)")
    print(f"Strategy: {args.strategy}")
    print("RU/LAN direct:", "yes" if not args.no_direct_ru else "no")
    print("All-proxies-down fallback:", args.fallback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
