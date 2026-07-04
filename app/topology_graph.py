"""Topology graph edge construction — pure helpers + async enrichment."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

INFRA_NEIGHBOR_TYPES = frozenset({"access point", "gateway", "switch"})


def edges_from_switch_ports(
    switches: list[dict],
    port_results: list[Any],
    node_ids: set[str],
) -> tuple[list[dict], int, set[tuple[str, str]]]:
    """Build graph edges from switch port neighbourSerial fields."""
    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    port_fail_count = 0

    for sw, ports in zip(switches, port_results):
        if isinstance(ports, BaseException):
            port_fail_count += 1
            logger.warning(
                "Failed to fetch ports for switch %s (%s): %r",
                sw.get("name") or sw.get("serial"), sw.get("serial"), ports,
            )
            continue
        if not ports:
            continue
        sw_serial = sw.get("serial") or ""
        for port in ports:
            if not isinstance(port, dict):
                continue
            nbr_serial = (port.get("neighbourSerial") or "").strip()
            nbr_type = (port.get("neighbourType") or "").lower()
            if not nbr_serial or nbr_serial == sw_serial:
                continue
            if nbr_type not in INFRA_NEIGHBOR_TYPES:
                continue
            if nbr_serial not in node_ids:
                continue
            edge_key = tuple(sorted((sw_serial, nbr_serial)))
            if edge_key[0] == edge_key[1] or edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({
                "id": f"{sw_serial}-{nbr_serial}",
                "source": sw_serial,
                "target": nbr_serial,
                "port": port.get("name") or port.get("id") or "",
                "speed": port.get("speed") or 0,
                "via": "ports",
            })

    return edges, port_fail_count, seen_edges


def ap_serials_missing_edges(devices: list[dict], edges: list[dict]) -> list[dict]:
    """Online APs with no edge yet — candidates for uplink resolution."""
    linked = set()
    for edge in edges:
        linked.add(edge.get("source") or "")
        linked.add(edge.get("target") or "")
    return [
        d for d in devices
        if d.get("type") == "access_point"
        and (d.get("status") or "").lower() == "online"
        and d.get("serial")
        and d["serial"] not in linked
    ]


def edges_from_ap_uplinks(
    missing_aps: list[dict],
    uplink_results: list[Any],
    node_ids: set[str],
    seen_edges: set[tuple[str, str]],
) -> list[dict]:
    """Add switch→AP edges discovered via find_device_uplink."""
    edges: list[dict] = []
    for ap, uplink in zip(missing_aps, uplink_results):
        if isinstance(uplink, BaseException):
            logger.warning(
                "Uplink lookup failed for AP %s: %r", ap.get("serial"), uplink,
            )
            continue
        if not isinstance(uplink, dict):
            continue
        sw_serial = (uplink.get("switch_serial") or "").strip()
        ap_serial = ap.get("serial") or ""
        if not sw_serial or not ap_serial or sw_serial not in node_ids:
            continue
        edge_key = tuple(sorted((sw_serial, ap_serial)))
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        edges.append({
            "id": f"{sw_serial}-{ap_serial}",
            "source": sw_serial,
            "target": ap_serial,
            "port": uplink.get("port") or "",
            "speed": 0,
            "via": "uplink",
        })
    return edges


async def build_topology_edges(
    devices: list[dict],
    node_ids: set[str],
    get_switch_ports: Callable[[str], Awaitable[list[dict]]],
    find_device_uplink: Callable[[str], Awaitable[dict | None]] | None = None,
) -> tuple[list[dict], int]:
    """Assemble topology edges from switch ports plus optional AP uplink scans."""
    switches = [
        d for d in devices
        if d.get("type") == "switch" and (d.get("status") or "").lower() == "online"
    ]
    port_results = await asyncio.gather(
        *[get_switch_ports(sw["serial"]) for sw in switches],
        return_exceptions=True,
    )
    edges, port_fail_count, seen_edges = edges_from_switch_ports(
        switches, port_results, node_ids,
    )

    if find_device_uplink is not None:
        missing_aps = ap_serials_missing_edges(devices, edges)
        if missing_aps:
            uplink_results = await asyncio.gather(
                *[find_device_uplink(ap["serial"]) for ap in missing_aps],
                return_exceptions=True,
            )
            edges.extend(edges_from_ap_uplinks(
                missing_aps, uplink_results, node_ids, seen_edges,
            ))

    return edges, port_fail_count
