"""
Aruba Central client — delegates to central_bridge (centralmcp).

Falls back to mock data if centralmcp is not available (local dev without
the mounted volume).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Tracks whether the last successful read came from centralmcp ("live") or mock fallback.
_data_source: str = "unknown"


def get_data_source() -> str:
    """Return ``live`` or ``mock`` based on the most recent aruba client fetch."""
    return _data_source


def _norm_device(d: dict) -> dict:
    """Normalise centralmcp device fields to the names templates expect."""
    device_type_raw = (d.get("deviceType") or d.get("device_type") or "").upper()
    if device_type_raw in ("AP", "IAP"):
        dtype = "access_point"
    elif device_type_raw in ("SWITCH", "AOS_S", "AOS-S", "CX", "AOS_CX"):
        dtype = "switch"
    elif device_type_raw in ("GATEWAY", "GW", "VPNC"):
        dtype = "gateway"
    else:
        dtype = device_type_raw.lower() or "unknown"

    status_raw = (d.get("status") or d.get("deviceStatus") or "").lower()
    status = "online" if status_raw in ("up", "online", "connected") else "offline"

    return {
        "serial": d.get("serialNumber") or d.get("serial") or d.get("serial_number") or "",
        "name": d.get("deviceName") or d.get("name") or d.get("hostname") or "",
        "type": dtype,
        "model": d.get("model") or d.get("modelNumber") or "",
        "mac": d.get("macAddress") or d.get("mac") or d.get("mac_address") or "",
        "status": status,
        "ip": d.get("ipv4") or d.get("ipAddress") or d.get("ip") or d.get("ip_address") or "",
        "site": d.get("siteName") or d.get("site_name") or d.get("site") or "",
        "persona": d.get("persona") or "",
        "deployment": d.get("deployment") or "",
        "device_function": d.get("deviceFunction") or "",
        "part_number": d.get("partNumber") or "",
        "group_name": d.get("deviceGroupName") or "",
        "group_id": d.get("deviceGroupId") or "",
        "site_id": d.get("siteId") or "",
        "_raw": d,
    }


def _norm_client(c: dict) -> dict:
    """Normalise centralmcp client fields to the names templates expect."""
    conn_type = (c.get("clientConnectionType") or c.get("connection_type") or c.get("type") or "").lower()
    ctype = "wired" if conn_type == "wired" else "wireless"

    return {
        # Core identity
        "mac": c.get("macAddress") or c.get("mac") or "",
        "ip": c.get("ipv4") or c.get("ipAddress") or c.get("ip") or "",
        "ipv6": c.get("ipv6") or "",
        # hostName comes from DHCP option 12; clientName falls back to MAC — exclude that fallback
        "hostname": c.get("hostName") or c.get("hostname") or "",
        "username": c.get("userName") or "",
        "status": c.get("status") or "Unknown",
        # Connection
        "type": ctype,
        "connected_to": c.get("connectedTo") or c.get("connected_to") or c.get("apName") or c.get("switchName") or "",
        "connected_device_serial": c.get("connectedDeviceSerial") or "",
        "connected_device_type": c.get("connectedDeviceType") or "",
        "connected_at": c.get("connectedAt") or "",
        "port": c.get("port") or "",
        # Network
        "vlan": c.get("vlanId") or c.get("vlan") or "",
        "vlan_name": c.get("vlanName") or "",
        "role": c.get("role") or "",
        "site": c.get("siteName") or c.get("site") or "",
        "ssid": c.get("wlanName") or c.get("network") or c.get("ssid") or "",
        # Wireless detail
        "band": c.get("wirelessBand") or "",
        "channel": c.get("wirelessChannel") or "",
        "security": c.get("wirelessSecurity") or "",
        "key_mgmt": c.get("keyManagement") or "",
        "phy_type": c.get("phyType") or "",
        "capabilities": c.get("clientCapabilities") or "",
        "snr": c.get("snr") or 0,
        "bssid": c.get("bssid") or "",
        # Device info
        "vendor": c.get("clientVendor") or c.get("clientManufacturer") or "",
        "manufacturer": c.get("clientManufacturer") or "",
        "os": c.get("clientOperatingSystem") or c.get("osType") or "",
        "category": c.get("clientCategory") or "",
        "function": c.get("clientFunction") or "",
        # Auth
        "auth_type": c.get("authenticationType") or "",
        "tunnel_type": c.get("tunnelType") or "",
        "_raw": c,
    }


class ArubaCentralClient:
    async def get_devices(
        self,
        site_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        global _data_source
        try:
            if site_id or limit is not None:
                from vendors.central_bridge import get_devices as _get_devices
                raw = await _get_devices(
                    site_id=str(site_id) if site_id else None,
                    limit=limit if limit is not None else 200,
                )
            else:
                from vendors.central_bridge import get_all_devices
                raw = await get_all_devices()
            _data_source = "live"
            return [_norm_device(d) for d in raw if isinstance(d, dict)]
        except Exception as exc:
            logger.warning("central_bridge unavailable, using mock data: %s", exc)
            _data_source = "mock"
            return _mock_devices()

    async def get_device(self, serial: str) -> dict | None:
        global _data_source
        try:
            from vendors.central_bridge import get_device
            raw = await get_device(serial)
            _data_source = "live"
            return _norm_device(raw) if isinstance(raw, dict) and raw else None
        except Exception as exc:
            logger.warning("central_bridge unavailable, using mock data: %s", exc)
            _data_source = "mock"
            devices = _mock_devices()
            return next((d for d in devices if d["serial"] == serial), None)

    async def get_clients(
        self,
        site_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        global _data_source
        try:
            if site_id or limit is not None:
                from vendors.central_bridge import get_clients as _get_clients
                raw = await _get_clients(
                    site_id=str(site_id) if site_id else None,
                    limit=limit if limit is not None else 200,
                )
            else:
                from vendors.central_bridge import get_all_clients
                raw = await get_all_clients()
            _data_source = "live"
            return [_norm_client(c) for c in raw if isinstance(c, dict)]
        except Exception as exc:
            logger.warning("central_bridge unavailable, using mock data: %s", exc)
            _data_source = "mock"
            return _mock_clients()


# Singleton used by routes
aruba = ArubaCentralClient()


# -- Mock data fallback --

def _mock_devices() -> list[dict]:
    return [
        {"serial": "VNVQMPJ028", "name": "BY-AP763", "type": "access_point",
         "model": "AP-763", "mac": "f4:e1:fc:c9:4f:a0", "status": "online",
         "ip": "10.11.154.56", "site": "Memphis HQ"},
        {"serial": "SG30LMR164", "name": "CX6300-CORE", "type": "switch",
         "model": "CX-6300M", "mac": "4c:d5:87:32:c0:80", "status": "online",
         "ip": "10.11.154.1", "site": "Memphis HQ"},
        {"serial": "PHSXM52029", "name": "LR-AP735", "type": "access_point",
         "model": "AP-735", "mac": "48:00:20:c9:ab:0a", "status": "online",
         "ip": "10.11.154.55", "site": "Memphis HQ"},
    ]


def _mock_clients() -> list[dict]:
    return [
        {"mac": "00:0c:29:54:69:96", "ip": "10.11.154.19", "type": "wired",
         "vlan": 5, "connected_to": "CX6300-CORE", "port": "1/1/23", "role": "-"},
        {"mac": "3c:a9:ab:7c:a9:51", "ip": "192.168.1.89", "type": "wireless",
         "vlan": 200, "connected_to": "LR-AP735", "port": "-", "role": "aruba-home"},
    ]
