"""Pure status-board logic: classify checks, roll history, open and close incidents.

The monitor script in monitor.py does the network and git work. Everything that
decides up / degraded / down lives here so it can be tested without GitHub.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

USER_AGENT = "ConcreteStatusMonitor/1.0 (+https://status.concrete.ch)"
MAX_BODY = 2_000_000
SCHEMA = 1

RANK = {"up": 0, "degraded": 1, "down": 2}
REGION_HINTS = (
    "europe-west4",
    "europe-west6",
    "zurich",
    "zürich",
    "netherlands",
    "niederlande",
    "global",
)
STATUSPAGE_LEVEL = {
    "none": "up",
    "minor": "degraded",
    "major": "down",
    "critical": "down",
    "maintenance": "degraded",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def assert_public_https(url: str) -> None:
    """Reject anything the monitor should never call from a public workflow."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{url} must use https")
    if parsed.username or parsed.password:
        raise ValueError(f"{url} must not contain credentials")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"{url} has no host")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        raise ValueError(f"{url} is not a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError(f"{url} is not a public address")


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("components.yml must be a mapping")
    defaults = raw.get("defaults") or {}
    slow_ms = int(defaults.get("slow_ms", 3000))
    timeout = float(defaults.get("timeout_sec", 20))
    config = {
        "slow_ms": slow_ms,
        "timeout_sec": timeout,
        "history_days": int(defaults.get("history_days", 90)),
        "recent_hours": int(defaults.get("recent_hours", 48)),
        "incident_retention_days": int(defaults.get("incident_retention_days", 90)),
        "sections": [],
    }
    seen: set[str] = set()
    for section in raw.get("sections") or []:
        components = []
        for comp in section.get("components") or []:
            cid = str(comp["id"])
            if cid in seen:
                raise ValueError(f"duplicate component id {cid}")
            seen.add(cid)
            kind = comp.get("kind") or ("upstream" if comp.get("provider") else "http")
            if kind not in {"http", "upstream"}:
                raise ValueError(f"{cid} has unknown kind {kind}")
            item = {
                "id": cid,
                "name": str(comp["name"]),
                "description": str(comp.get("description") or ""),
                "kind": kind,
                "section_id": section["id"],
                "section_name": section["name"],
                "counts_overall": bool(comp.get("counts_overall", kind == "http")),
                "slow_ms": int(comp.get("slow_ms", slow_ms)),
                "timeout_sec": float(comp.get("timeout_sec", timeout)),
                "expect_body": comp.get("expect_body"),
                "up_statuses": [int(code) for code in comp.get("up_statuses") or []],
            }
            if kind == "http":
                assert_public_https(comp["url"])
                item["url"] = comp["url"]
                item["link"] = comp["url"]
            else:
                provider = comp.get("provider")
                if provider not in {"firebase", "gcp", "statuspage"}:
                    raise ValueError(f"{cid} has unknown provider {provider}")
                assert_public_https(comp["status_url"])
                link = comp.get("link") or comp["status_url"]
                assert_public_https(link)
                products = [str(name) for name in comp.get("products") or []]
                if provider in {"firebase", "gcp"} and not products:
                    raise ValueError(f"{cid} needs a products list")
                item.update(
                    {
                        "provider": provider,
                        "status_url": comp["status_url"],
                        "link": link,
                        "url": link,
                        "products": products,
                    }
                )
            components.append(item)
        config["sections"].append(
            {"id": section["id"], "name": section["name"], "components": components}
        )
    if not config["sections"]:
        raise ValueError("components.yml has no sections")
    return config


def classify(
    status: int | None,
    latency_ms: int | None,
    error: str | None,
    slow_ms: int,
    up_statuses: list[int] | None = None,
    body: str = "",
    expect_body: str | None = None,
) -> str:
    """Map one HTTP result to up, degraded, or down.

    2xx (and a followed-to completion) faster than slow_ms is up.
    Slower than slow_ms, or a 4xx, is degraded: the server did answer.
    A timeout, a connection error, or a 5xx is down.
    """
    if error or status is None:
        return "down"
    allowed = set(up_statuses or [])
    if allowed:
        if status not in allowed:
            return "down" if status >= 500 else "degraded"
        if expect_body and expect_body not in body:
            return "degraded"
        if latency_ms is not None and latency_ms > slow_ms:
            return "degraded"
        return "up"
    if status >= 500:
        return "down"
    if status >= 400:
        return "degraded"
    # 301/302/307/308 only surface here when the redirect could not be followed.
    if status in {301, 302, 303, 307, 308}:
        return "down"
    if expect_body and expect_body not in body:
        return "degraded"
    if latency_ms is not None and latency_ms > slow_ms:
        return "degraded"
    return "up"


def detail_for(level: str, fetched: dict, component: dict) -> str | None:
    if level == "down":
        if fetched.get("error"):
            return None
        if fetched.get("status") and fetched["status"] >= 500:
            return f"HTTP {fetched['status']}"
        return None
    if level != "degraded":
        return None
    expect = component.get("expect_body")
    body = fetched.get("body") or ""
    status = fetched.get("status")
    if expect and status and status < 400 and expect not in body:
        return "Antwort ohne erwarteten Inhalt"
    if status and status >= 400 and not component.get("up_statuses"):
        return f"HTTP {status}"
    latency = fetched.get("latency_ms")
    if latency is not None and latency > component["slow_ms"]:
        return f"Langsam ({latency} ms)"
    if expect and expect not in body:
        return "Antwort ohne erwarteten Inhalt"
    return None


def default_fetch(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BODY)
            elapsed = int((time.perf_counter() - started) * 1000)
            return {
                "status": getattr(response, "status", None) or response.getcode(),
                "latency_ms": elapsed,
                "body": body.decode("utf-8", "replace"),
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        raw = exc.read(MAX_BODY) if exc.fp else b""
        return {
            "status": exc.code,
            "latency_ms": elapsed,
            "body": raw.decode("utf-8", "replace"),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — network failures are data, not bugs
        elapsed = int((time.perf_counter() - started) * 1000)
        name = type(exc).__name__
        if isinstance(exc, (TimeoutError, socket.timeout)):
            name = "timeout"
        return {"status": None, "latency_ms": elapsed, "body": "", "error": name}


def _http_once(component: dict, fetch) -> dict:
    fetched = fetch(component["url"], component["timeout_sec"])
    level = classify(
        status=fetched.get("status"),
        latency_ms=fetched.get("latency_ms"),
        error=fetched.get("error"),
        slow_ms=component["slow_ms"],
        up_statuses=component.get("up_statuses"),
        body=fetched.get("body") or "",
        expect_body=component.get("expect_body"),
    )
    return {
        "status": level,
        "http_status": fetched.get("status"),
        "latency_ms": fetched.get("latency_ms"),
        "detail": detail_for(level, fetched, component),
    }


def check_http_component(component: dict, fetch=default_fetch) -> dict:
    """GET a component. A down result is checked a second time before it sticks."""
    first = _http_once(component, fetch)
    if first["status"] != "down":
        return first
    return _http_once(component, fetch)


def _incident_names(incident: dict) -> set[str]:
    names = []
    if incident.get("service_name"):
        names.append(incident["service_name"])
    for product in incident.get("affected_products") or []:
        if isinstance(product, dict):
            if product.get("title"):
                names.append(product["title"])
            if product.get("current_title"):
                names.append(product["current_title"])
    return {name.strip().casefold() for name in names if name and str(name).strip()}


def is_ongoing(incident: dict) -> bool:
    if incident.get("end"):
        return False
    update = incident.get("most_recent_update") or {}
    state = str(update.get("status") or "").upper()
    if state in {"AVAILABLE", "RESOLVED", "CLOSED"}:
        return False
    return True


def incident_level(incident: dict) -> str:
    impact = str(incident.get("status_impact") or "").upper()
    severity = str(incident.get("severity") or "").lower()
    if impact == "SERVICE_OUTAGE" or severity == "high":
        return "down"
    if impact == "SERVICE_INFORMATION" or severity == "low":
        return "up"
    return "degraded"


def region_relevant(incident: dict) -> bool:
    locations = incident.get("currently_affected_locations") or []
    if not locations:
        return True
    blob = " ".join(
        f"{loc.get('id', '')} {loc.get('title', '')}" for loc in locations if isinstance(loc, dict)
    ).casefold()
    return any(hint in blob for hint in REGION_HINTS)


def parse_google_incidents(payload, products: list[str], region_filter: bool) -> dict:
    if not isinstance(payload, list):
        raise ValueError("incident feed is not a list")
    wanted = {name.strip().casefold() for name in products}
    worst = "up"
    detail = None
    for incident in payload:
        if not isinstance(incident, dict) or not is_ongoing(incident):
            continue
        if not (_incident_names(incident) & wanted):
            continue
        if region_filter and not region_relevant(incident):
            continue
        level = incident_level(incident)
        if RANK[level] > RANK[worst]:
            worst = level
            text = str(incident.get("external_desc") or "").strip()
            detail = text or None
    if worst == "up":
        detail = None
    elif detail and len(detail) > 220:
        detail = detail[:217].rstrip() + "..."
    return {"status": worst, "detail": detail}


def parse_statuspage(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("statuspage payload is not an object")
    status = payload.get("status") or {}
    indicator = str(status.get("indicator") or "").lower()
    description = str(status.get("description") or "").strip() or None
    if indicator not in STATUSPAGE_LEVEL:
        return {"status": "degraded", "detail": description or "Unbekannte Statusantwort"}
    level = STATUSPAGE_LEVEL[indicator]
    if level == "up":
        return {"status": "up", "detail": None}
    return {"status": level, "detail": description}


def _read_upstream(component: dict, fetch) -> dict | None:
    fetched = fetch(component["status_url"], component["timeout_sec"])
    if fetched.get("error") or not fetched.get("status") or fetched["status"] >= 400:
        return None
    try:
        payload = json.loads(fetched.get("body") or "")
    except json.JSONDecodeError:
        return None
    if component["provider"] == "statuspage":
        parsed = parse_statuspage(payload)
    else:
        parsed = parse_google_incidents(
            payload,
            component.get("products") or [],
            region_filter=component["provider"] == "gcp",
        )
    parsed["http_status"] = fetched.get("status")
    parsed["latency_ms"] = fetched.get("latency_ms")
    return parsed


def check_upstream_component(component: dict, fetch=default_fetch) -> dict:
    """Read a provider status API. A reported outage is confirmed once.

    If the status page itself cannot be read, the dependency is degraded
    rather than down: we failed to observe it, which is not the same as an
    outage at the provider, and it must not open an incident.
    """
    unreachable = {
        "status": "degraded",
        "http_status": None,
        "latency_ms": None,
        "detail": "Statusseite nicht erreichbar",
    }
    first = _read_upstream(component, fetch)
    if first is None or first["status"] == "down":
        second = _read_upstream(component, fetch)
        if second is not None:
            return second
        if first is not None:
            return first
        return unreachable
    return first


def build_daily(days: dict, now: datetime, history_days: int) -> list[dict]:
    start = now.date() - timedelta(days=history_days - 1)
    series = []
    for offset in range(history_days):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        bucket = days.get(key)
        if not bucket:
            series.append(
                {"date": key, "uptime": None, "checks": 0, "up": 0, "degraded": 0, "down": 0}
            )
            continue
        up = int(bucket.get("up", 0))
        degraded = int(bucket.get("degraded", 0))
        down = int(bucket.get("down", 0))
        checks = up + degraded + down
        uptime = round(100.0 * (up + degraded) / checks, 2) if checks else None
        series.append(
            {
                "date": key,
                "uptime": uptime,
                "checks": checks,
                "up": up,
                "degraded": degraded,
                "down": down,
            }
        )
    return series


def uptime_from_daily(daily: list[dict]) -> float | None:
    checks = 0
    available = 0.0
    for day in daily:
        count = int(day.get("checks") or 0)
        if not count or day.get("uptime") is None:
            continue
        checks += count
        available += count * (float(day["uptime"]) / 100.0)
    if not checks:
        return None
    return round(100.0 * available / checks, 2)


def apply_result(
    previous: dict | None,
    component: dict,
    result: dict,
    now: datetime,
    history_days: int,
    recent_hours: int,
) -> dict:
    days = dict((previous or {}).get("days") or {})
    recent = list((previous or {}).get("recent") or [])
    key = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    bucket = dict(days.get(key) or {"up": 0, "degraded": 0, "down": 0})
    level = result["status"]
    bucket[level] = int(bucket.get(level, 0)) + 1
    days[key] = {"up": int(bucket.get("up", 0)), "degraded": int(bucket.get("degraded", 0)), "down": int(bucket.get("down", 0))}
    recent.append(
        {
            "t": iso(now),
            "s": level,
            "ms": result.get("latency_ms"),
            "c": result.get("http_status"),
        }
    )
    cutoff = now - timedelta(hours=recent_hours)
    recent = [item for item in recent if parse_iso(item["t"]) >= cutoff]
    recent = recent[-2000:]
    oldest = (now.date() - timedelta(days=history_days - 1)).isoformat()
    days = {day: counts for day, counts in days.items() if day >= oldest}
    daily = build_daily(days, now, history_days)
    return {
        "id": component["id"],
        "name": component["name"],
        "description": component.get("description") or "",
        "url": component.get("link") or component.get("url"),
        "kind": component["kind"],
        "countsOverall": bool(component.get("counts_overall", True)),
        "status": level,
        "httpStatus": result.get("http_status"),
        "latencyMs": result.get("latency_ms") if component["kind"] == "http" else None,
        "checkedAt": iso(now),
        "detail": result.get("detail"),
        "uptime90d": uptime_from_daily(daily),
        "daily": daily,
        "days": days,
        "recent": recent,
    }


def overall_state(components: list[dict]) -> str:
    """Banner state from Concrete's own components, not third parties.

    The website being down, or two or more components being down, is a major
    outage. A single other outage, or any degraded component, is partial.
    """
    own = [component for component in components if component.get("countsOverall", True)]
    down = [component for component in own if component["status"] == "down"]
    degraded = [component for component in own if component["status"] == "degraded"]
    if not down and not degraded:
        return "operational"
    website_down = any(component["id"] == "website" and component["status"] == "down" for component in own)
    if website_down or len(down) >= 2:
        return "major"
    return "partial"


def overall_uptime(components: list[dict]) -> float | None:
    daily = []
    for component in components:
        if not component.get("countsOverall", True):
            continue
        daily.extend(component.get("daily") or [])
    return uptime_from_daily(daily)


def update_incidents(
    previous: list[dict] | None,
    components: list[dict],
    meta_by_id: dict,
    now: datetime,
    retention_days: int,
) -> list[dict]:
    incidents = [dict(item) for item in (previous or [])]
    open_by_component = {
        item["componentId"]: item for item in incidents if item.get("status") == "open"
    }
    for component in components:
        current = open_by_component.get(component["id"])
        if component["status"] == "down":
            if current is None:
                meta = meta_by_id[component["id"]]
                if meta["kind"] == "upstream":
                    title = f"{component['name']} meldet eine Störung"
                else:
                    title = f"{component['name']} ist nicht erreichbar"
                incidents.append(
                    {
                        "id": f"{component['id']}-{now.strftime('%Y%m%dT%H%M%SZ')}",
                        "componentId": component["id"],
                        "componentName": component["name"],
                        "section": meta.get("section_name") or "",
                        "title": title,
                        "status": "open",
                        "startedAt": iso(now),
                        "endedAt": None,
                        "durationSec": None,
                        "countsOverall": bool(component.get("countsOverall", True)),
                    }
                )
            continue
        if current is not None:
            started = parse_iso(current["startedAt"])
            current["status"] = "resolved"
            current["endedAt"] = iso(now)
            current["durationSec"] = max(0, int((now - started).total_seconds()))
    cutoff = now - timedelta(days=retention_days)
    kept = []
    for incident in incidents:
        if incident.get("status") == "open":
            kept.append(incident)
            continue
        ended = incident.get("endedAt")
        if ended and parse_iso(ended) >= cutoff:
            kept.append(incident)
    return kept


def index_previous(document: dict | None) -> dict:
    found = {}
    if not document:
        return found
    for section in document.get("sections") or []:
        for component in section.get("components") or []:
            found[component["id"]] = component
    return found


def run_checks(config: dict, previous: dict | None, now: datetime, fetch=default_fetch) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    components = [component for section in config["sections"] for component in section["components"]]
    results: dict[str, dict] = {}

    def invoke(component: dict) -> dict:
        if component["kind"] == "http":
            return check_http_component(component, fetch)
        return check_upstream_component(component, fetch)

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(components)))) as pool:
        futures = {pool.submit(invoke, component): component["id"] for component in components}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                results[cid] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[cid] = {
                    "status": "down",
                    "http_status": None,
                    "latency_ms": None,
                    "detail": type(exc).__name__,
                }

    meta = {component["id"]: component for component in components}
    previous_components = index_previous(previous)
    sections_out = []
    flat = []
    for section in config["sections"]:
        rendered = []
        for component in section["components"]:
            state = apply_result(
                previous_components.get(component["id"]),
                component,
                results[component["id"]],
                now,
                config["history_days"],
                config["recent_hours"],
            )
            rendered.append(state)
            flat.append(state)
        sections_out.append({"id": section["id"], "name": section["name"], "components": rendered})

    incidents = update_incidents(
        (previous or {}).get("incidents"),
        flat,
        meta,
        now,
        config["incident_retention_days"],
    )
    return {
        "schema": SCHEMA,
        "generatedAt": iso(now),
        "overall": {
            "state": overall_state(flat),
            "uptime90d": overall_uptime(flat),
            "checkedAt": iso(now),
        },
        "sections": sections_out,
        "incidents": incidents,
    }


def dump_status(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
