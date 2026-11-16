"""Unit tests for check classification, history, and incidents."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import unittest

from monitor import push_args
from statuslib import (
    apply_result,
    assert_public_https,
    check_http_component,
    classify,
    load_config,
    overall_state,
    parse_google_incidents,
    parse_statuspage,
    run_checks,
    update_incidents,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def http_component(**overrides):
    component = {
        "id": "website",
        "name": "Website",
        "description": "",
        "kind": "http",
        "section_id": "platform",
        "section_name": "Plattform",
        "counts_overall": True,
        "slow_ms": 3000,
        "timeout_sec": 5,
        "expect_body": None,
        "up_statuses": [],
        "url": "https://concrete.ch/",
        "link": "https://concrete.ch/",
    }
    component.update(overrides)
    return component


class PushArgsTests(unittest.TestCase):
    def test_lease_stays_attached_to_the_flag(self):
        args = push_args("status-data", "abc123")
        self.assertEqual(
            args,
            [
                "push",
                "--force-with-lease=refs/heads/status-data:abc123",
                "origin",
                "HEAD:refs/heads/status-data",
            ],
        )
        self.assertNotIn("--force-with-lease", args)

    def test_first_push_has_no_lease(self):
        self.assertEqual(push_args("status-data", ""), ["push", "origin", "HEAD:refs/heads/status-data"])


class ClassifyTests(unittest.TestCase):
    def test_fast_success_is_up(self):
        self.assertEqual(classify(200, 80, None, 3000), "up")
        self.assertEqual(classify(204, 10, None, 3000), "up")
        self.assertEqual(classify(304, 10, None, 3000), "up")

    def test_slow_success_is_degraded(self):
        self.assertEqual(classify(200, 3001, None, 3000), "degraded")

    def test_client_error_is_degraded_and_server_error_is_down(self):
        self.assertEqual(classify(404, 20, None, 3000), "degraded")
        self.assertEqual(classify(401, 20, None, 3000), "degraded")
        self.assertEqual(classify(500, 20, None, 3000), "down")
        self.assertEqual(classify(503, 9000, None, 3000), "down")

    def test_network_error_and_unfollowed_redirect_are_down(self):
        self.assertEqual(classify(None, 20000, "timeout", 3000), "down")
        self.assertEqual(classify(302, 30, None, 3000), "down")

    def test_expected_status_and_body(self):
        self.assertEqual(
            classify(400, 100, None, 3000, up_statuses=[400], body='{"error":"EGRID"}', expect_body="EGRID"),
            "up",
        )
        self.assertEqual(
            classify(400, 5000, None, 3000, up_statuses=[400], body="EGRID", expect_body="EGRID"),
            "degraded",
        )
        self.assertEqual(
            classify(200, 40, None, 3000, body="other", expect_body="Concrete"),
            "degraded",
        )
        self.assertEqual(classify(500, 10, None, 3000, up_statuses=[400]), "down")

    def test_retry_once_before_down(self):
        calls = {"n": 0}

        def fetch(url, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"status": None, "latency_ms": 20, "body": "", "error": "timeout"}
            return {"status": 200, "latency_ms": 30, "body": "Concrete", "error": None}

        result = check_http_component(http_component(expect_body="Concrete"), fetch)
        self.assertEqual(result["status"], "up")
        self.assertEqual(calls["n"], 2)

    def test_degraded_is_not_retried(self):
        calls = {"n": 0}

        def fetch(url, timeout):
            calls["n"] += 1
            return {"status": 404, "latency_ms": 15, "body": "missing", "error": None}

        result = check_http_component(http_component(), fetch)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(calls["n"], 1)

    def test_two_failures_stay_down(self):
        def fetch(url, timeout):
            return {"status": 502, "latency_ms": 10, "body": "", "error": None}

        result = check_http_component(http_component(), fetch)
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["detail"], "HTTP 502")


class HistoryTests(unittest.TestCase):
    def test_rollup_uptime_and_prune(self):
        component = http_component()
        old = NOW - timedelta(days=100)
        previous = {
            "days": {
                old.strftime("%Y-%m-%d"): {"up": 1, "degraded": 0, "down": 0},
                "2026-11-16": {"up": 1, "degraded": 0, "down": 1},
            },
            "recent": [
                {"t": "2026-11-01T00:00:00Z", "s": "up", "ms": 1, "c": 200},
                {"t": "2026-11-16T11:00:00Z", "s": "down", "ms": 1, "c": 500},
            ],
        }
        state = apply_result(
            previous,
            component,
            {"status": "up", "http_status": 200, "latency_ms": 40, "detail": None},
            NOW,
            history_days=90,
            recent_hours=48,
        )
        self.assertNotIn(old.strftime("%Y-%m-%d"), state["days"])
        self.assertEqual(state["days"]["2026-11-16"], {"up": 2, "degraded": 0, "down": 1})
        self.assertEqual(len(state["daily"]), 90)
        self.assertEqual(state["daily"][-1]["date"], "2026-11-16")
        self.assertEqual(state["daily"][-1]["uptime"], 66.67)
        self.assertEqual(state["daily"][0]["uptime"], None)
        self.assertEqual(state["uptime90d"], 66.67)
        self.assertEqual([item["t"] for item in state["recent"]], ["2026-11-16T11:00:00Z", "2026-11-16T12:00:00Z"])
        self.assertEqual(state["latencyMs"], 40)

    def test_upstream_hides_probe_latency(self):
        component = http_component(kind="upstream", counts_overall=False, link="https://www.githubstatus.com")
        state = apply_result(
            None,
            component,
            {"status": "up", "http_status": 200, "latency_ms": 40, "detail": None},
            NOW,
            90,
            48,
        )
        self.assertIsNone(state["latencyMs"])
        self.assertFalse(state["countsOverall"])


class IncidentTests(unittest.TestCase):
    def meta(self):
        return {
            "website": http_component(),
            "projektraum": http_component(id="projektraum", name="Projektraum"),
            "github": http_component(
                id="github",
                name="GitHub",
                kind="upstream",
                counts_overall=False,
                section_name="Drittanbieter",
            ),
        }

    def test_open_on_down_ignore_degraded_then_close(self):
        meta = self.meta()
        components = [
            {"id": "website", "name": "Website", "status": "degraded", "countsOverall": True},
            {"id": "projektraum", "name": "Projektraum", "status": "down", "countsOverall": True},
        ]
        opened = update_incidents([], components, meta, NOW, 90)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["status"], "open")
        self.assertEqual(opened[0]["title"], "Projektraum ist nicht erreichbar")
        self.assertIsNone(opened[0]["endedAt"])

        later = NOW + timedelta(minutes=12)
        still = [
            {"id": "website", "name": "Website", "status": "up", "countsOverall": True},
            {"id": "projektraum", "name": "Projektraum", "status": "down", "countsOverall": True},
        ]
        held = update_incidents(opened, still, meta, later, 90)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["status"], "open")

        recovered = [
            {"id": "website", "name": "Website", "status": "up", "countsOverall": True},
            {"id": "projektraum", "name": "Projektraum", "status": "up", "countsOverall": True},
        ]
        closed = update_incidents(held, recovered, meta, later, 90)
        self.assertEqual(closed[0]["status"], "resolved")
        self.assertEqual(closed[0]["durationSec"], 12 * 60)
        self.assertEqual(closed[0]["endedAt"], "2026-11-16T12:12:00Z")

    def test_upstream_outage_uses_its_own_title_and_old_incidents_drop(self):
        meta = self.meta()
        opened = update_incidents(
            [],
            [{"id": "github", "name": "GitHub", "status": "down", "countsOverall": False}],
            meta,
            NOW,
            90,
        )
        self.assertEqual(opened[0]["title"], "GitHub meldet eine Störung")
        opened[0]["status"] = "resolved"
        opened[0]["endedAt"] = "2026-01-01T00:00:00Z"
        kept = update_incidents(
            opened,
            [{"id": "github", "name": "GitHub", "status": "up", "countsOverall": False}],
            meta,
            NOW,
            90,
        )
        self.assertEqual(kept, [])


class OverallTests(unittest.TestCase):
    def component(self, cid, status, counts=True):
        return {"id": cid, "status": status, "countsOverall": counts}

    def test_banner_rules(self):
        self.assertEqual(overall_state([self.component("website", "up")]), "operational")
        self.assertEqual(
            overall_state([self.component("website", "up"), self.component("projektraum", "degraded")]),
            "partial",
        )
        self.assertEqual(
            overall_state([self.component("website", "up"), self.component("projektraum", "down")]),
            "partial",
        )
        self.assertEqual(overall_state([self.component("website", "down")]), "major")
        self.assertEqual(
            overall_state(
                [self.component("website", "up"), self.component("a", "down"), self.component("b", "down")]
            ),
            "major",
        )
        self.assertEqual(
            overall_state(
                [self.component("website", "up"), self.component("github", "down", counts=False)]
            ),
            "operational",
        )


class UpstreamParseTests(unittest.TestCase):
    def test_statuspage_indicators(self):
        self.assertEqual(parse_statuspage({"status": {"indicator": "none", "description": "All Systems Operational"}})["status"], "up")
        minor = parse_statuspage({"status": {"indicator": "minor", "description": "Degraded performance"}})
        self.assertEqual(minor["status"], "degraded")
        self.assertEqual(minor["detail"], "Degraded performance")
        self.assertEqual(parse_statuspage({"status": {"indicator": "critical", "description": "Outage"}})["status"], "down")
        self.assertEqual(parse_statuspage({"status": {}})["status"], "degraded")

    def test_firebase_ongoing_product_and_gcp_region_filter(self):
        payload = [
            {
                "service_name": "Hosting",
                "external_desc": "Hosting errors",
                "status_impact": "SERVICE_DISRUPTION",
                "severity": "medium",
                "most_recent_update": {"status": "SERVICE_DISRUPTION"},
                "affected_products": [{"title": "Hosting"}],
            },
            {
                "service_name": "Crashlytics",
                "external_desc": "unrelated",
                "status_impact": "SERVICE_OUTAGE",
                "severity": "high",
                "affected_products": [{"title": "Crashlytics"}],
            },
            {
                "service_name": "Hosting",
                "end": "2026-11-01T00:00:00Z",
                "external_desc": "already over",
                "status_impact": "SERVICE_OUTAGE",
                "severity": "high",
                "affected_products": [{"title": "Hosting"}],
            },
        ]
        parsed = parse_google_incidents(payload, ["Hosting", "App Hosting"], region_filter=False)
        self.assertEqual(parsed["status"], "degraded")
        self.assertEqual(parsed["detail"], "Hosting errors")

        gcp = [
            {
                "service_name": "Cloud Run",
                "external_desc": "us only",
                "status_impact": "SERVICE_OUTAGE",
                "severity": "high",
                "affected_products": [{"title": "Cloud Run"}],
                "currently_affected_locations": [{"id": "us-east1", "title": "South Carolina"}],
            },
            {
                "service_name": "Multiple Products",
                "external_desc": "Zurich functions",
                "status_impact": "SERVICE_OUTAGE",
                "severity": "high",
                "affected_products": [{"title": "Cloud Functions"}, {"title": "BigQuery"}],
                "currently_affected_locations": [{"id": "europe-west6", "title": "Zurich"}],
            },
        ]
        ignored = parse_google_incidents(gcp[:1], ["Cloud Run", "Cloud Functions"], region_filter=True)
        self.assertEqual(ignored["status"], "up")
        hit = parse_google_incidents(gcp, ["Cloud Run", "Cloud Functions"], region_filter=True)
        self.assertEqual(hit["status"], "down")
        self.assertIn("Zurich", hit["detail"])

    def test_informational_notice_stays_up(self):
        payload = [
            {
                "service_name": "Cloud Run",
                "external_desc": "scheduled notice",
                "status_impact": "SERVICE_INFORMATION",
                "severity": "low",
                "affected_products": [{"title": "Cloud Run"}],
                "currently_affected_locations": [],
            }
        ]
        parsed = parse_google_incidents(payload, ["Cloud Run"], region_filter=True)
        self.assertEqual(parsed["status"], "up")


class ConfigTests(unittest.TestCase):
    def test_live_config_is_public_https_and_unique(self):
        config = load_config(ROOT / "components.yml")
        ids = []
        for section in config["sections"]:
            for component in section["components"]:
                ids.append(component["id"])
                assert_public_https(component["url"])
        self.assertEqual(
            ids,
            [
                "website",
                "www",
                "login",
                "tools",
                "oereb",
                "projektraum",
                "wissensdatenbank",
                "firebase",
                "google-cloud",
                "github",
                "stripe",
                "resend",
            ],
        )
        self.assertEqual(config["slow_ms"], 3000)
        oereb = next(
            component
            for section in config["sections"]
            for component in section["components"]
            if component["id"] == "oereb"
        )
        self.assertEqual(oereb["up_statuses"], [400])
        github = next(
            component
            for section in config["sections"]
            for component in section["components"]
            if component["id"] == "github"
        )
        self.assertFalse(github["counts_overall"])

    def test_rejects_private_targets(self):
        with self.assertRaises(ValueError):
            assert_public_https("http://concrete.ch/")
        with self.assertRaises(ValueError):
            assert_public_https("https://127.0.0.1/status")
        with self.assertRaises(ValueError):
            assert_public_https("https://169.254.169.254/latest")
        with self.assertRaises(ValueError):
            assert_public_https("https://user:pw@concrete.ch/")


class EndToEndTests(unittest.TestCase):
    def test_run_checks_with_fake_fetch(self):
        config = load_config(ROOT / "components.yml")
        # Shrink the fixture so the test stays about the rules, not the network.
        config["sections"] = config["sections"][:1]
        config["sections"][0]["components"] = [
            component
            for component in config["sections"][0]["components"]
            if component["id"] in {"website", "login"}
        ]

        def fetch(url, timeout):
            if url.endswith("/login/"):
                return {"status": 500, "latency_ms": 15, "body": "nope", "error": None}
            return {"status": 200, "latency_ms": 40, "body": "Concrete homepage", "error": None}

        document = run_checks(config, None, NOW, fetch)
        self.assertEqual(document["overall"]["state"], "partial")
        by_id = {component["id"]: component for section in document["sections"] for component in section["components"]}
        self.assertEqual(by_id["website"]["status"], "up")
        self.assertEqual(by_id["login"]["status"], "down")
        self.assertEqual(len(document["incidents"]), 1)
        self.assertEqual(document["incidents"][0]["componentId"], "login")
        again = run_checks(config, document, NOW + timedelta(minutes=5), fetch)
        self.assertEqual(len(again["incidents"]), 1)
        self.assertEqual(again["incidents"][0]["status"], "open")

    def test_real_redirect_and_status_codes(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                if self.path == "/ok":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Concrete ok")
                    return
                if self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.path == "/boom":
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"EGRID"}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            followed = check_http_component(
                http_component(url=base + "/redir", expect_body="Concrete"),
            )
            self.assertEqual(followed["status"], "up")
            self.assertEqual(followed["http_status"], 200)
            missing = check_http_component(http_component(url=base + "/missing", expect_body=None))
            self.assertEqual(missing["status"], "degraded")
            boom = check_http_component(http_component(url=base + "/boom", expect_body=None))
            self.assertEqual(boom["status"], "down")
            expected = check_http_component(
                http_component(url=base + "/egrid", up_statuses=[400], expect_body="EGRID")
            )
            self.assertEqual(expected["status"], "up")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
