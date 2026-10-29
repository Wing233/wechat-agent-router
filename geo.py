"""Offline IP geolocation using the vendored ip2region xdb files."""
from __future__ import annotations

import ipaddress
import sys
import threading


class Geo:
    def __init__(self, vendor_dir: str, v4_path: str, v6_path: str):
        sys.path.insert(0, vendor_dir)
        from ip2region import searcher, util  # type: ignore

        self._v4 = searcher.new_with_buffer(util.IPv4, util.load_content_from_file(v4_path))
        self._v6 = searcher.new_with_buffer(util.IPv6, util.load_content_from_file(v6_path)) if v6_path else None
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def lookup(self, ip: str) -> str:
        ip = (ip or "").strip()
        if not ip:
            return "未知"
        with self._lock:
            if ip in self._cache:
                return self._cache[ip]
        try:
            addr = ipaddress.ip_address(ip)
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                result = "内网"
            else:
                searcher = self._v4 if addr.version == 4 else self._v6
                if searcher is None:
                    result = "未知"
                else:
                    raw = searcher.search(str(addr))
                    result = self._format(raw)
        except Exception:
            result = "未知"
        with self._lock:
            self._cache[ip] = result
        return result

    @staticmethod
    def _format(raw: str) -> str:
        if not raw:
            return "未知"
        parts = raw.split("|")
        country = parts[0] if len(parts) > 0 else ""
        province = parts[1] if len(parts) > 1 else ""
        city = parts[2] if len(parts) > 2 else ""
        isp = parts[3] if len(parts) > 3 else ""
        if country == "中国":
            place = "".join(p for p in (province, city) if p and p != "0")
        else:
            place = " ".join(p for p in (country, province, city) if p and p != "0")
        place = place or "未知"
        return f"{place} · {isp}" if isp and isp != "0" else place
