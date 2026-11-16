(function () {
  "use strict";

  var POLL_MS = 60000;
  var DATA_URL = location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "status.json"
    : "https://raw.githubusercontent.com/concrete-app/status/status-data/status.json";

  var BANNERS = {
    operational: "Alle Systeme funktionieren",
    partial: "Teilweise Störung",
    major: "Grössere Störung"
  };
  var LABELS = {
    up: "Betriebsbereit",
    degraded: "Beeinträchtigt",
    down: "Störung"
  };

  var root = document.getElementById("root");
  var tip = document.getElementById("tip");
  var currentVersion = null;
  var lastData = null;

  function safeUrl(value) {
    try {
      var url = new URL(value);
      if (url.protocol === "https:" || url.protocol === "http:") return url.href;
    } catch (error) {
      return null;
    }
    return null;
  }

  function formatPercent(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
    var number = Number(value);
    var digits = number >= 99.995 ? 0 : 2;
    return new Intl.NumberFormat("de-CH", {
      minimumFractionDigits: digits,
      maximumFractionDigits: 2
    }).format(number) + " %";
  }

  function formatLatency(value) {
    if (value === null || value === undefined) return "";
    return new Intl.NumberFormat("de-CH").format(value) + " ms";
  }

  function formatDay(isoDate) {
    var parts = isoDate.split("-").map(Number);
    var date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
    return new Intl.DateTimeFormat("de-CH", {
      day: "numeric",
      month: "long",
      year: "numeric",
      timeZone: "UTC"
    }).format(date);
  }

  function formatWhen(iso) {
    var date = new Date(iso);
    return new Intl.DateTimeFormat("de-CH", {
      day: "numeric",
      month: "long",
      hour: "2-digit",
      minute: "2-digit"
    }).format(date);
  }

  function ago(iso) {
    var seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 45) return "vor weniger als einer Minute";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes === 1 ? "vor 1 Minute" : "vor " + minutes + " Minuten";
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours === 1 ? "vor 1 Stunde" : "vor " + hours + " Stunden";
    var days = Math.round(hours / 24);
    return days === 1 ? "vor 1 Tag" : "vor " + days + " Tagen";
  }

  function formatDuration(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return "unter einer Minute";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes === 1 ? "1 Minute" : minutes + " Minuten";
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    var hourText = hours === 1 ? "1 Stunde" : hours + " Stunden";
    if (!rest) return hourText;
    return hourText + " " + (rest === 1 ? "1 Minute" : rest + " Minuten");
  }

  function barClass(day) {
    if (!day.checks) return "empty";
    if (day.down && day.uptime < 99) return "down";
    if (day.down || day.degraded) return "degraded";
    return "up";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function showTip(event, text) {
    tip.textContent = text;
    tip.hidden = false;
    moveTip(event);
  }

  function moveTip(event) {
    tip.style.left = "0px";
    tip.style.top = "0px";
    var rect = tip.getBoundingClientRect();
    var x = event.clientX - rect.width / 2;
    var y = event.clientY - rect.height - 12;
    x = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
    if (y < 8) y = event.clientY + 16;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  function hideTip() {
    tip.hidden = true;
  }

  function recentIncidents(incidents) {
    var cutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
    return (incidents || []).filter(function (incident) {
      if (incident.status === "open") return true;
      var stamp = Date.parse(incident.endedAt || incident.startedAt);
      return stamp >= cutoff;
    }).sort(function (a, b) {
      if (a.status !== b.status) return a.status === "open" ? -1 : 1;
      return Date.parse(b.startedAt) - Date.parse(a.startedAt);
    });
  }

  function renderBanner(data, fetchError) {
    var state = (data.overall && data.overall.state) || "operational";
    if (!BANNERS[state]) state = "operational";
    var banner = el("section", "banner " + state);
    banner.appendChild(el("h1", null, BANNERS[state]));
    var checked = (data.overall && data.overall.checkedAt) || data.generatedAt;
    var uptime = data.overall ? data.overall.uptime90d : null;
    var line = el("p");
    line.appendChild(document.createTextNode("Verfügbarkeit (90 Tage) " + formatPercent(uptime)));
    if (checked) {
      line.appendChild(document.createTextNode(" · "));
      var time = el("time", null, "Zuletzt geprüft " + ago(checked));
      time.dateTime = checked;
      time.setAttribute("data-checked", checked);
      line.appendChild(time);
    }
    banner.appendChild(line);
    if (fetchError) {
      banner.appendChild(el("p", "stale", "Letzte Aktualisierung fehlgeschlagen. Angezeigt wird die letzte erfolgreiche Messung."));
    }
    return banner;
  }

  function renderComponent(component) {
    var block = el("article", "component");
    var top = el("div", "component-top");
    var heading = el("h3");
    var href = safeUrl(component.url);
    if (href) {
      var link = el("a", null, component.name);
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      heading.appendChild(link);
    } else {
      heading.textContent = component.name;
    }
    top.appendChild(heading);
    var status = el("span", "status " + (component.status || "up"));
    status.appendChild(el("span", "dot"));
    status.appendChild(document.createTextNode(LABELS[component.status] || component.status || ""));
    top.appendChild(status);
    block.appendChild(top);

    if (component.description) block.appendChild(el("p", "description", component.description));
    var extra = [];
    if (component.kind !== "upstream" && component.latencyMs !== null && component.latencyMs !== undefined) {
      extra.push(formatLatency(component.latencyMs));
    }
    if (component.detail) extra.push(component.detail);
    if (extra.length) block.appendChild(el("p", "detail", extra.join(" · ")));

    var uptimeRow = el("div", "uptime-row");
    uptimeRow.appendChild(el("span", null, "Verfügbarkeit (90 Tage)"));
    uptimeRow.appendChild(el("span", null, formatPercent(component.uptime90d)));
    block.appendChild(uptimeRow);

    var bars = el("div", "bars");
    var label = "Verfügbarkeit (90 Tage), " + formatPercent(component.uptime90d);
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label", label);
    (component.daily || []).forEach(function (day) {
      var bar = el("div", "bar " + barClass(day));
      var text = formatDay(day.date) + " — " + (day.checks ? formatPercent(day.uptime) : "keine Daten");
      bar.title = text;
      bar.setAttribute("aria-label", text);
      bar.addEventListener("mouseenter", function (event) { showTip(event, text); });
      bar.addEventListener("mousemove", moveTip);
      bar.addEventListener("mouseleave", hideTip);
      bar.addEventListener("blur", hideTip);
      bars.appendChild(bar);
    });
    block.appendChild(bars);
    return block;
  }

  function renderIncidents(incidents) {
    var section = el("section", "section");
    section.appendChild(el("h2", null, "Vorfälle"));
    var items = recentIncidents(incidents);
    if (!items.length) {
      section.appendChild(el("p", "empty", "Keine Vorfälle in den letzten 30 Tagen."));
      return section;
    }
    var list = el("ul", "incidents");
    items.forEach(function (incident) {
      var item = el("li", "incident");
      var when = el("time", null, formatWhen(incident.startedAt));
      when.dateTime = incident.startedAt;
      item.appendChild(when);
      var body = el("div");
      var title = el("p");
      title.style.margin = "0";
      var badge = el("span", "badge " + (incident.status === "open" ? "open" : "resolved"), incident.status === "open" ? "Offen" : "Behoben");
      title.appendChild(badge);
      title.appendChild(document.createTextNode(incident.title || incident.componentName || "Vorfall"));
      body.appendChild(title);
      var seconds;
      if (incident.status === "open") {
        seconds = (Date.now() - new Date(incident.startedAt).getTime()) / 1000;
        body.appendChild(el("p", "meta", (incident.section ? incident.section + " · " : "") + "seit " + formatDuration(seconds)));
      } else {
        seconds = incident.durationSec;
        if (seconds === null || seconds === undefined) {
          seconds = (new Date(incident.endedAt).getTime() - new Date(incident.startedAt).getTime()) / 1000;
        }
        var ended = incident.endedAt ? "bis " + formatWhen(incident.endedAt) + " · " : "";
        body.appendChild(el("p", "meta", (incident.section ? incident.section + " · " : "") + ended + "Dauer " + formatDuration(seconds)));
      }
      item.appendChild(body);
      list.appendChild(item);
    });
    section.appendChild(list);
    return section;
  }

  function render(data, fetchError) {
    hideTip();
    root.replaceChildren();
    if (!data) {
      root.appendChild(el("p", "error-line", "Statusdaten konnten nicht geladen werden."));
      return;
    }
    root.appendChild(renderBanner(data, fetchError));
    (data.sections || []).forEach(function (section) {
      var block = el("section", "section");
      block.appendChild(el("h2", null, section.name || ""));
      if (section.id === "dependencies") {
        block.appendChild(el("p", "note", "Diese Dienste betreibt nicht Concrete. Sie fliessen nicht in die Gesamtanzeige ein."));
      }
      (section.components || []).forEach(function (component) {
        block.appendChild(renderComponent(component));
      });
      root.appendChild(block);
    });
    root.appendChild(renderIncidents(data.incidents));
  }

  function refreshTimes() {
    document.querySelectorAll("[data-checked]").forEach(function (node) {
      node.textContent = "Zuletzt geprüft " + ago(node.getAttribute("data-checked"));
    });
  }

  function loadStatus() {
    return fetch(DATA_URL + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        lastData = data;
        render(data, null);
      })
      .catch(function () {
        render(lastData, lastData ? new Error("refresh") : new Error("missing"));
      });
  }

  function checkVersion() {
    return fetch("version.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) return null;
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.version) return;
        if (!currentVersion) {
          currentVersion = data.version;
          return;
        }
        if (data.version !== currentVersion) location.reload();
      })
      .catch(function () {});
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      checkVersion();
      loadStatus();
    }
  });

  loadStatus();
  checkVersion();
  window.setInterval(function () {
    loadStatus();
    checkVersion();
  }, POLL_MS);
  window.setInterval(refreshTimes, 30000);
})();
