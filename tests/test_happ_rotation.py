import base64
import json
import tempfile
import unittest
from pathlib import Path

from happ_rotation import build_config, load_proxies, parse_proxy_line


class ParserTests(unittest.TestCase):
    def test_socks(self):
        outbound = parse_proxy_line("socks://alice:secret@127.0.0.1:1080")
        self.assertEqual(outbound["protocol"], "socks")
        self.assertEqual(outbound["settings"]["user"], "alice")
        self.assertEqual(outbound["settings"]["pass"], "secret")

    def test_vless_reality(self):
        outbound = parse_proxy_line(
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=reality&type=tcp&sni=www.microsoft.com"
            "&fp=chrome&pbk=abc&sid=1234&flow=xtls-rprx-vision"
        )
        self.assertEqual(outbound["protocol"], "vless")
        self.assertEqual(outbound["streamSettings"]["method"], "raw")
        self.assertEqual(outbound["streamSettings"]["security"], "reality")
        self.assertEqual(
            outbound["streamSettings"]["realitySettings"]["password"], "abc"
        )

    def test_vmess(self):
        payload = {
            "v": "2",
            "ps": "test",
            "add": "vm.example.com",
            "port": "443",
            "id": "11111111-1111-1111-1111-111111111111",
            "scy": "auto",
            "net": "ws",
            "tls": "tls",
            "sni": "vm.example.com",
            "host": "vm.example.com",
            "path": "/ws",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        outbound = parse_proxy_line("vmess://" + encoded)
        self.assertEqual(outbound["protocol"], "vmess")
        self.assertEqual(outbound["streamSettings"]["method"], "websocket")

    def test_shadowsocks(self):
        userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:secret").decode().rstrip("=")
        outbound = parse_proxy_line(f"ss://{userinfo}@ss.example.com:8388")
        self.assertEqual(outbound["protocol"], "shadowsocks")
        self.assertEqual(outbound["settings"]["method"], "aes-256-gcm")

    def test_raw_outbound(self):
        outbound = parse_proxy_line(
            '{"protocol":"socks","settings":{"address":"127.0.0.1","port":1080}}'
        )
        self.assertEqual(outbound["protocol"], "socks")


class ConfigTests(unittest.TestCase):
    def test_build_round_robin_fail_closed_and_ru_direct(self):
        proxies = [
            {"protocol": "socks", "settings": {"address": "127.0.0.1", "port": 1080}, "tag": "proxy-001"},
            {"protocol": "socks", "settings": {"address": "127.0.0.1", "port": 1081}, "tag": "proxy-002"},
        ]
        config = build_config(proxies)
        balancer = config["routing"]["balancers"][0]
        self.assertEqual(balancer["strategy"]["type"], "roundRobin")
        self.assertEqual(balancer["fallbackTag"], "block")
        self.assertTrue(any("geoip:ru" in r.get("ip", []) for r in config["routing"]["rules"]))
        self.assertTrue(any("geosite:category-ru" in r.get("domain", []) for r in config["routing"]["rules"]))

    def test_load_proxy_file_assigns_unique_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text(
                "socks://127.0.0.1:1080\n"
                "socks://127.0.0.1:1081\n",
                encoding="utf-8",
            )
            proxies = load_proxies(path)
        self.assertEqual([p["tag"] for p in proxies], ["proxy-001", "proxy-002"])


if __name__ == "__main__":
    unittest.main()
