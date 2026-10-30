/* COD Ping King UI. No framework, no build step. */

const ICONS = {
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"></rect></svg>',
  map: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 4.5-5 2v13l5-2 6 2 5-2v-13l-5 2-6-2Z"></path><path d="M9 4.5v13M15 6.5v13"></path></svg>',
  server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3.5" y="4" width="17" height="6" rx="1.6"></rect><rect x="3.5" y="14" width="17" height="6" rx="1.6"></rect><path d="M7 7h.01M7 17h.01"></path></svg>',
  rules: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 4.5h14v15H5z"></path><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"></path></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"></circle><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.4 1.4M16.6 16.6 18 18M18 6l-1.4 1.4M7.4 16.6 6 18"></path></svg>',
};

const SCREEN_META = {
  dashboard: ["Dashboard", "Live view of the server you are connected to."],
  regions: ["Regions", "Choose which parts of the world may host your matches."],
  servers: ["Servers", "Every endpoint the catalog knows about, and its verdict."],
  rules: ["Rules", "The policy file that decides what is allowed."],
  settings: ["Settings", "Startup, search guard and local data."],
};

// pywebview injects window.pywebview after the page scripts start, so the
// bridge can't be detected here. DEMO is resolved in apiReady().
const DEMO_PARAM = new URLSearchParams(location.search).has("demo");
// Docs screenshots pass "clean" so the preview banner stays out of the frame.
const CLEAN_PARAM = new URLSearchParams(location.search).has("clean");
let DEMO = DEMO_PARAM;

const state = {
  boot: null,
  screen: "dashboard",
  regionMode: "all",
  regionQuery: "",
  serverQuery: "",
  serverVerdict: "all",
  serverSort: { key: "bytes", asc: false },
  showSeed: false,
  policyDirty: false,
  lastPolicyText: "",
  currentRegionKey: null,
  wizardPreview: new URLSearchParams(location.search).has("wizard"),
  busy: false,
};

/* ------------------------------------------------------------------ */
/* API adapter                                                        */
/* ------------------------------------------------------------------ */

let bridgeReady;
function apiReady() {
  if (!bridgeReady) {
    bridgeReady = new Promise((resolve) => {
      if (DEMO_PARAM) return resolve();
      if (window.pywebview && window.pywebview.api) {
        DEMO = false;
        return resolve();
      }
      let settled = false;
      const settle = (live) => {
        if (settled) return;
        settled = true;
        DEMO = !live;
        resolve();
      };
      window.addEventListener("pywebviewready", () => settle(true), { once: true });
      setTimeout(() => settle(false), 4000);
    });
  }
  return bridgeReady;
}

async function call(method, ...args) {
  await apiReady();
  if (DEMO) return demoApi[method](...args);
  if (!window.pywebview || !window.pywebview.api) {
    throw new Error("The desktop bridge is not available. Run the app with python -m cod_region_filter.");
  }
  return window.pywebview.api[method](...args);
}

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return "--";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

function fmtPing(ms) {
  return ms === null || ms === undefined ? "--" : `${Math.round(ms)} ms`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 160ms";
    setTimeout(() => el.remove(), 180);
  }, kind === "bad" ? 6000 : 3200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard permissions vary on file:// pages, so fall back to a
    // selection the user can copy manually.
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

function confirmDialog(title, text, confirmLabel = "Confirm") {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    $("#modal-confirm").textContent = confirmLabel;
    $("#modal").hidden = false;
    const done = (value) => {
      $("#modal").hidden = true;
      $("#modal-confirm").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(value);
    };
    $("#modal-confirm").onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
  });
}

/* ------------------------------------------------------------------ */
/* Demo data                                                          */
/* ------------------------------------------------------------------ */

const demo = (() => {
  const regions = [
    ["US-OH", "Ohio", "Columbus", 39.96, -83.0, 22, 4],
    ["US-VA", "Virginia", "Ashburn", 39.04, -77.49, 28, 9],
    ["US-NY", "New York", "New York", 40.71, -74.0, 31, 6],
    ["CA-ON", "Ontario", "Toronto", 43.65, -79.38, 34, 3],
    ["US-IL", "Illinois", "Chicago", 41.88, -87.63, 38, 5],
    ["US-TX", "Texas", "Dallas", 32.78, -96.8, 53, 7],
    ["US-GA", "Georgia", "Atlanta", 33.75, -84.39, 47, 4],
    ["US-CA", "California", "Los Angeles", 34.05, -118.24, 68, 8],
    ["GB-ENG", "England", "London", 51.51, -0.13, 91, 6],
    ["DE-HE", "Hesse", "Frankfurt", 50.11, 8.68, 96, 5],
    ["NL-NH", "North Holland", "Amsterdam", 52.37, 4.9, 99, 4],
    ["AU-NSW", "New South Wales", "Sydney", -33.87, 151.21, 214, 3],
  ];
  const built = regions.map(([key, region, city, lat, lon, rtt, count], i) => ({
    key,
    region,
    region_code: key.split("-")[1],
    country: key.startsWith("US") ? "United States" : key.startsWith("CA") ? "Canada" : key.startsWith("GB") ? "United Kingdom" : key.startsWith("DE") ? "Germany" : key.startsWith("NL") ? "Netherlands" : "Australia",
    country_code: key.split("-")[0],
    cluster_count: 1,
    server_count: count * 7,
    bytes: (12 - i) * 48_000_000,
    avg_rtt_ms: rtt,
    lat,
    lon,
    observed: i < 8,
    allowed: ["US-OH", "US-VA", "US-NY", "CA-ON"].includes(key),
    label: region,
  }));

  const servers = [];
  built.forEach((r, i) => {
    for (let n = 0; n < 4; n += 1) {
      servers.push({
        cidr: `203.0.${113 + (i % 3)}.${(i * 11 + n * 7) % 250}`,
        city: r.region === "Ohio" ? "Columbus" : r.region,
        region: r.region,
        region_code: r.region_code,
        country: r.country,
        country_code: r.country_code,
        lat: r.lat,
        lon: r.lon,
        source: n === 0 ? "observed" : "seed",
        confidence: n === 0 ? 0.9 : 0.4,
        is_service: 0,
        packets: n === 0 ? 18420 - i * 700 : 0,
        bytes: n === 0 ? r.bytes : 0,
        rtt_ms: n === 0 ? r.avg_rtt_ms : null,
        last_seen: "2026-10-30T18:20:00+00:00",
        region_key: r.key,
        allowed: r.allowed,
        verdict: r.allowed ? "Allowed by region" : "Not in allowlist",
      });
    }
  });

  const matches = [
    { city: "Ashburn", region: "Virginia", country: "United States", rtt_ms: 29, duration_s: 612, bytes: 18_400_000, allowed: 1, ended_at: "2026-10-30T18:02:00+00:00", remote_ip: "203.0.113.24", cidr: "203.0.113.0/24", blocked: false },
    { city: "Columbus", region: "Ohio", country: "United States", rtt_ms: 22, duration_s: 498, bytes: 14_200_000, allowed: 1, ended_at: "2026-10-30T17:44:00+00:00", remote_ip: "203.0.113.7", cidr: "203.0.113.0/24", blocked: false },
    { city: "New York", region: "New York", country: "United States", rtt_ms: 33, duration_s: 731, bytes: 21_800_000, allowed: 1, ended_at: "2026-10-30T17:22:00+00:00", remote_ip: "203.0.113.51", cidr: "203.0.113.0/24", blocked: false },
    { city: "Dallas", region: "Texas", country: "United States", rtt_ms: 54, duration_s: 0, bytes: 240_000, allowed: 0, ended_at: "2026-10-30T17:05:00+00:00", remote_ip: "198.51.100.19", cidr: "198.51.100.0/24", blocked: false },
    { city: "Ashburn", region: "Virginia", country: "United States", rtt_ms: 28, duration_s: 655, bytes: 17_900_000, allowed: 1, ended_at: "2026-10-30T16:48:00+00:00", remote_ip: "203.0.113.28", cidr: "203.0.113.0/24", blocked: false },
    { city: "Los Angeles", region: "California", country: "United States", rtt_ms: 71, duration_s: 0, bytes: 180_000, allowed: 0, ended_at: "2026-10-30T16:30:00+00:00", remote_ip: "198.51.100.72", cidr: "198.51.100.0/24", blocked: false },
  ];

  const policy = {
    mode: "allowlist",
    unknown: "allow",
    allow: { regions: built.filter((r) => r.allowed).map((r) => r.key), clusters: [], countries: [], pins: [], radius_km: 0 },
    deny: { regions: [], clusters: [], countries: [], cidrs: [] },
    max_rtt_ms: 0,
    soft_mode: { enabled: true, relax_after_s: 45 },
  };

  const config = {
    filtering: true,
    start_with_windows: false,
    auto_geolocate: true,
    show_seed_clusters: false,
    wizard_done: !new URLSearchParams(location.search).has("wizard"),
    minimize_to_tray: true,
    streamer_mode: false,
  };

  const demoLocation = { lat: 39.7684, lon: -86.1581, label: "Indianapolis, Indiana (39.77, -86.16)" };

  const current = {
    remote_ip: "203.0.113.28",
    port: 3074,
    cidr: "203.0.113.0/24",
    city: "Ashburn",
    region: "Virginia",
    region_key: "US-VA",
    country: "United States",
    duration_s: 254,
    bytes: 9_240_000,
    rtt_ms: 28,
    allowed: true,
    reason: "Allowed by region",
  };

  const stateObj = {
    filtering: true,
    soft_active: false,
    soft_reason: "",
    searching_s: null,
    allowed_packets: 148_233,
    denied_packets: 1_284,
    match_count: 6,
    current,
    recent: [
      { ip: "198.51.100.19", port: 3074, reason: "Not in allowlist", at: "2026-10-30T17:05:12+00:00" },
      { ip: "203.0.113.28", port: 3074, reason: "Allowed by region", at: "2026-10-30T16:58:03+00:00" },
    ],
  };

  function policyText() {
    const list = (v) => (v.length ? `[${v.map((x) => `"${x}"`).join(", ")}]` : "[]");
    return `# COD Ping King policy
# Preview only. The desktop app writes this file to your profile.

mode: ${policy.mode}
unknown: ${policy.unknown}
home:
  lat: 39.7684
  lon: -86.1581
allow:
  regions: ${list(policy.allow.regions)}
  clusters: []
  countries: []
  radius_km: ${policy.allow.radius_km}
  pins: []
deny:
  regions: []
  clusters: []
  countries: []
  cidrs: []
max_rtt_ms: 0
soft_mode:
  enabled: ${policy.soft_mode.enabled}
  relax_after_s: ${policy.soft_mode.relax_after_s}
`;
  }

  function summary() {
    const allowedClusters = built.filter((r) => r.allowed).length;
    return {
      mode: policy.mode,
      unknown: policy.unknown,
      clusters: built.length,
      allowed_clusters: allowedClusters,
      radius_keys: 0,
      max_rtt_ms: policy.max_rtt_ms,
      empty_allowlist: policy.allow.regions.length === 0,
      allow: policy.allow,
      deny: policy.deny,
      soft: policy.soft_mode,
    };
  }

  function regionsPayload() {
    return built.map((r) => ({ ...r, allowed: policy.allow.regions.includes(r.key) }));
  }

  function matchesPayload() {
    return config.streamer_mode
      ? matches.map((m) => ({ ...m, remote_ip: "Hidden", cidr: "Hidden range" }))
      : matches;
  }

  function statePayload() {
    if (!config.streamer_mode) return stateObj;
    return {
      ...stateObj,
      current: stateObj.current
        ? { ...stateObj.current, remote_ip: "Hidden", cidr: "Hidden range" }
        : null,
    };
  }

  const demoApi = {
    bootstrap() {
      return {
        version: "1.0.0",
        elevated: true,
        driver: true,
        game: { pid: 12844, name: "cod.exe", exe: "C:\\Games\\Call of Duty\\cod.exe" },
        config,
        policy_text: config.streamer_mode ? "" : policyText(),
        streamer: Boolean(config.streamer_mode),
        policy_error: "",
        policy_home: config.streamer_mode
          ? { lat: null, lon: null, label: "Hidden by streamer mode", hidden: true }
          : { ...demoLocation },
        capture: { available: false, running: false, status: "preview", error: "", pid: 12844, ports: [3074] },
        summary: summary(),
        catalog_update: { running: false, message: "", report: [], finished_at: 0 },
        state: statePayload(),
        matches: matchesPayload(),
        stats: { ranges: 6442, observed_ranges: 214, bytes: 2_940_000_000 },
        regions: regionsPayload(),
        clusters: built.map((r) => ({
          key: r.key,
          label: r.region,
          city: r.region,
          region: r.region,
          region_code: r.region_code,
          region_key: r.key,
          country: r.country,
          country_code: r.country_code,
          lat: r.lat,
          lon: r.lon,
          cidrs: [`203.0.113.${(r.server_count % 250) + 1}`],
          server_count: r.server_count,
          packets: r.observed ? r.server_count * 120 : 0,
          bytes: r.bytes,
          avg_rtt_ms: r.avg_rtt_ms,
          observed: r.observed,
          confidence: r.observed ? 0.9 : 0.4,
          last_seen: "2026-10-30T18:20:00+00:00",
          allowed: r.allowed,
        })),
        servers: config.streamer_mode
          ? servers.map((s) => ({ ...s, cidr: "Hidden range" }))
          : servers,
        paths: config.streamer_mode
          ? { policy: "%APPDATA%\\cod-ping-king\\policy.yaml", catalog: "%LOCALAPPDATA%\\cod-ping-king\\catalog.db", log: "" }
          : { policy: "%APPDATA%\\cod-ping-king\\policy.yaml", catalog: "%LOCALAPPDATA%\\cod-ping-king\\catalog.db", log: "" },
      };
    },
    poll() {
      return {
        state: statePayload(),
        game: { pid: 12844, name: "cod.exe", exe: "" },
        capture: { available: true, running: true, status: "capturing", error: "", pid: 12844, ports: [3074] },
        elevated: true,
        summary: summary(),
        catalog_update: { running: false, message: "", report: [], finished_at: 0 },
        matches: matchesPayload(),
        regions: regionsPayload(),
      };
    },
    get_regions: () => regionsPayload(),
    get_clusters: () => demoApi.bootstrap().clusters,
    get_servers(query = "") {
      const q = query.toLowerCase();
      // The wizard can change the allowlist after the demo rows were built.
      const current = new Map(built.map((r) => [r.key, r.allowed]));
      const list = servers.map((s) => {
        const on = current.has(s.region_key) ? current.get(s.region_key) : s.allowed;
        const row = { ...s, allowed: on, verdict: on ? "Allowed by region" : "Not in allowlist" };
        if (config.streamer_mode) row.cidr = "Hidden range";
        return row;
      });
      return list.filter((s) => !q || `${s.cidr} ${s.city} ${s.region} ${s.country}`.toLowerCase().includes(q));
    },
    get_policy: () => ({ text: policyText(), summary: summary(), error: "" }),
    validate_policy: () => ({ ok: true, errors: [], warnings: [] }),
    save_policy: () => ({ ok: true, errors: [], warnings: [] }),
    set_regions(keys, enabled) {
      const set = new Set(policy.allow.regions);
      keys.forEach((k) => (enabled ? set.add(k) : set.delete(k)));
      policy.allow.regions = Array.from(set);
      return { ok: true, errors: [], warnings: [] };
    },
    set_radius(km) {
      policy.allow.radius_km = Number(km) || 0;
      return { ok: true, errors: [], warnings: [] };
    },
    set_home() {
      demoLocation.lat = 39.7684;
      demoLocation.lon = -86.1581;
      demoLocation.label = "Indianapolis, Indiana (39.77, -86.16)";
      return { ok: true, home: { lat: 39.7684, lon: -86.1581, city: "Indianapolis", region: "Indiana", country: "United States" } };
    },
    detect_home() { return demoApi.set_home(); },
    clear_home() {
      demoLocation.lat = null;
      demoLocation.lon = null;
      demoLocation.label = "Not set";
      policy.allow.radius_km = 0;
      return { ok: true };
    },
    apply_recommended() {
      policy.allow.regions = built.filter((r) => r.avg_rtt_ms <= 40).map((r) => r.key);
      for (const r of built) r.allowed = policy.allow.regions.includes(r.key);
      config.wizard_done = true;
      return { ok: true, errors: [], warnings: [] };
    },
    set_setting(key, value) { config[key] = value; return { ok: true, config }; },
    set_soft_mode(enabled, seconds) { policy.soft_mode.enabled = enabled; policy.soft_mode.relax_after_s = seconds; return { ok: true }; },
    set_unknown(mode) { policy.unknown = mode; return { ok: true }; },
    set_filtering(on) { stateObj.filtering = on; config.filtering = on; return { ok: true, config }; },
    set_region_blocked(key, blocked) {
      const deny = new Set(policy.deny.regions);
      if (blocked) { deny.add(key); policy.allow.regions = policy.allow.regions.filter((k) => k !== key); }
      else deny.delete(key);
      policy.deny.regions = Array.from(deny);
      return { ok: true };
    },
    set_regions_blocked(keys, blocked) {
      const deny = new Set(policy.deny.regions);
      for (const key of keys) {
        if (blocked) {
          deny.add(key);
          policy.allow.regions = policy.allow.regions.filter((k) => k !== key);
        } else {
          deny.delete(key);
        }
      }
      policy.deny.regions = Array.from(deny);
      return { ok: true };
    },
    clear_blocked_regions() { policy.deny.regions = []; return { ok: true }; },
    block_server(ip, blocked = true) {
      const cidr = ip.split(".").slice(0, 3).join(".") + ".0/24";
      const set = new Set(policy.deny.cidrs);
      blocked ? set.add(cidr) : set.delete(cidr);
      policy.deny.cidrs = Array.from(set);
      matches.forEach((m) => { if (m.remote_ip === ip) m.blocked = Boolean(blocked); });
      return { ok: true, cidr };
    },
    export_csv() { return { ok: true, path: "%APPDATA%\\cod-ping-king\\server-catalog.csv" }; },
    export_policy() { return { ok: true, path: "%USERPROFILE%\\Documents\\cod-ping-king-policy.yaml" }; },
    reset_catalog() { return { ok: true }; },
    update_catalog() { return { ok: true }; },
    open_folder() { return { ok: true }; },
    relaunch_admin() { return { ok: false, error: "Preview mode" }; },
    quit() { return { ok: true }; },
  };

  return { demoApi };
})();

const demoApi = demo.demoApi;

/* ------------------------------------------------------------------ */
/* Rendering                                                          */
/* ------------------------------------------------------------------ */

function nav(screen) {
  state.screen = screen;
  $$(".nav-item").forEach((el) => el.classList.toggle("is-active", el.dataset.screen === screen));
  $$(".screen").forEach((el) => el.classList.toggle("is-active", el.id === `screen-${screen}`));
  const [title, sub] = SCREEN_META[screen];
  $("#screen-title").textContent = title;
  $("#screen-sub").textContent = sub;
  if (screen === "regions") renderMap();
  if (screen === "servers") renderServers();
  if (screen === "rules") loadPolicyEditor();
}

function setStatus(dotClass, title, sub) {
  $("#rail-dot").className = `dot ${dotClass}`;
  $("#rail-status-text").textContent = title;
  $("#rail-status-sub").textContent = sub;
}

function renderChrome(boot) {
  const s = boot.state;
  const capture = boot.capture || {};
  $("#rail-version").textContent = `v${boot.version}`;
  $("#filter-toggle").textContent = s.filtering ? "Pause filtering" : "Resume filtering";
  $("#rail-pause").textContent = s.filtering ? "Pause" : "Resume";

  const game = boot.game;
  const pill = $("#game-pill");
  pill.classList.toggle("ok", Boolean(game));
  $("#game-pill-text").textContent = game ? `${game.name} running` : "Game not detected";

  if (!boot.elevated) {
    setStatus("warn", "Administrator required", "Catalog only, no blocking");
  } else if (!boot.driver) {
    setStatus("warn", "Driver not installed", "Install pydivert to filter");
  } else if (!s.filtering) {
    setStatus("warn", "Filtering paused", "All servers reachable");
  } else if (game) {
    setStatus("ok", "Filtering on", "Game detected");
  } else {
    setStatus("ok", "Filtering on", "Waiting for game");
  }

  $("#admin-banner").hidden = Boolean(boot.elevated && boot.driver);
  $("#setup-banner").hidden = !(boot.config && !boot.config.wizard_done) || CLEAN_PARAM;
  $("#policy-banner").hidden = !boot.policy_error;
  $("#policy-banner-text").textContent = boot.policy_error || "";
  $("#rules-badge").hidden = !boot.policy_error;

  $("#soft-banner").hidden = !s.soft_active;
  if (s.soft_active) $("#soft-banner-text").textContent = s.soft_reason;
}

function renderDashboard(boot) {
  const s = boot.state;
  const current = s.current;

  $("#stat-filtering").textContent = s.filtering ? "On" : "Off";
  $("#stat-filtering-sub").textContent = boot.elevated
    ? (s.soft_active ? "Search guard lifted" : "Dropping blocked servers")
    : "Administrator access required";

  $("#stat-server").textContent = current ? (current.city || "Unclassified") : "Searching";
  $("#stat-server-sub").textContent = current
    ? `${current.region || current.country || "Unknown location"}${current.allowed ? "" : " (blocked)"}`
    : (s.searching_s ? `Searching for ${fmtDuration(s.searching_s)}` : "No match in progress");

  $("#stat-rtt").textContent = fmtPing(current && current.rtt_ms);
  $("#stat-denied").textContent = s.denied_packets.toLocaleString();
  $("#stat-denied-sub").textContent = `${s.allowed_packets.toLocaleString()} allowed`;

  $("#session-tag").textContent = current ? (current.allowed ? "Connected" : "Blocked") : "Idle";
  $("#session-tag").className = `tag ${current ? (current.allowed ? "ok" : "bad") : ""}`;
  $("#session-city").textContent = current ? (current.city || "Unclassified server") : "No active match";
  $("#session-region").textContent = current
    ? `${current.region || current.country || "Unknown location"} - ${current.cidr}`
    : "Start Call of Duty and enter a lobby or match.";
  $("#session-duration").textContent = current ? fmtDuration(current.duration_s) : "--";
  $("#session-rtt").textContent = current ? fmtPing(current.rtt_ms) : "--";
  $("#session-ip").textContent = current ? `${current.remote_ip}:${current.port}` : "--";
  $("#session-reason").textContent = current ? current.reason : "";
  $("#session-note").textContent = s.soft_active
    ? "Search guard is allowing unclassified endpoints until a match is found."
    : current
      ? "Traffic is matched live against your rules."
      : "Waiting for the game to join a match.";

  state.currentRegionKey = current ? current.region_key : null;
  $("#allow-region-btn").disabled = !current || !current.region_key || current.allowed;
  $("#deny-region-btn").disabled = !current || !current.region_key || !current.allowed;

  const blocklist = (boot.summary || {}).mode === "blocklist";
  $("#allowlist-title").textContent = blocklist ? "Blocked regions" : "Your allowlist";
  $("#allowlist-sub").textContent = blocklist
    ? "Every region is allowed unless you block it."
    : "Regions that may host your matches.";

  const allowed = (boot.regions || []).filter((r) => r.allowed);
  const box = $("#allowlist-summary");
  if (blocklist) {
    const blocked = (boot.regions || []).filter((r) => r.blocked);
    box.innerHTML = blocked.length
      ? blocked.slice(0, 5).map((r) => `<div class="allow-row"><span class="name">${esc(r.label || r.region)}</span><span class="meta">blocked</span></div>`).join("")
        + (blocked.length > 5 ? `<div class="allow-row"><span class="name">+ ${blocked.length - 5} more regions</span></div>` : "")
      : '<p class="allow-empty">Nothing is blocked. Add regions on the Regions screen or switch to an allowlist.</p>';
  } else if (!allowed.length) {
    box.innerHTML = '<p class="allow-empty">Nothing is allowed yet. Pick regions or use the recommended set.</p>';
  } else {
    box.innerHTML = allowed
      .sort((a, b) => (a.avg_rtt_ms ?? 999) - (b.avg_rtt_ms ?? 999))
      .slice(0, 5)
      .map((r) => `<div class="allow-row"><span class="name">${esc(r.label || r.region)}</span><span class="meta">${r.avg_rtt_ms ? `${r.avg_rtt_ms} ms` : "no ping yet"}</span></div>`)
      .join("") + (allowed.length > 5 ? `<div class="allow-row"><span class="name">+ ${allowed.length - 5} more regions</span></div>` : "");
  }

  const tbody = $("#matches-table tbody");
  const matches = boot.matches || [];
  const canBlock = !(boot.config || {}).streamer_mode;
  tbody.innerHTML = matches.map((m) => `
    <tr>
      <td><div class="mono">${esc(m.remote_ip)}</div><div class="hint">${esc(m.cidr)}</div></td>
      <td>${esc(m.city || "Unclassified")}<div class="hint">${esc(m.region || m.country || "")}</div></td>
      <td class="num">${fmtPing(m.rtt_ms)}</td>
      <td class="num">${fmtDuration(m.duration_s)}</td>
      <td class="num">${fmtBytes(m.bytes)}</td>
      <td><span class="verdict ${m.allowed ? "" : "blocked"}"><i></i>${m.allowed ? "Allowed" : "Blocked"}</span><div class="hint">${esc(m.reason || "")}</div></td>
      <td class="num">${canBlock ? `<button class="btn btn-small match-block" data-ip="${esc(m.remote_ip)}" data-blocked="${m.blocked ? "1" : "0"}">${m.blocked ? "Unblock" : "Block"}</button>` : ""}</td>
    </tr>`).join("");
  $("#matches-empty").hidden = matches.length > 0;
  $("#matches-table").hidden = matches.length === 0;
}

const MAP_W = 1000;
const MAP_H = 500;
const VIEW_W = 760;
const VIEW_H = 420;

function mercatorY(lat) {
  const clamped = Math.max(-85, Math.min(85, lat));
  const rad = (clamped * Math.PI) / 180;
  return (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * MAP_H;
}

function inverseMercatorY(y) {
  const n = Math.PI - (2 * Math.PI * y) / MAP_H;
  return (Math.atan(Math.sinh(n)) * 180) / Math.PI;
}

function mapView() {
  if (!state.mapView) {
    const home = state.boot && (state.boot.policy_home || demoHome(state.boot));
    state.mapView = home && typeof home.lat === "number"
      ? { span: 130, lat: home.lat, lon: home.lon }
      : { span: 360, lat: 20, lon: 0 };
  }
  return state.mapView;
}

function renderMap() {
  const boot = state.boot;
  if (!boot) return;
  const svg = $("#map");
  const view = mapView();
  const k = (VIEW_W / MAP_W) * (360 / view.span);
  const tx = VIEW_W / 2 - ((view.lon + 180) / 360) * MAP_W * k;
  const ty = VIEW_H / 2 - mercatorY(view.lat) * k;
  const toScreen = (lon, lat) => [
    ((lon + 180) / 360) * MAP_W * k + tx,
    mercatorY(lat) * k + ty,
  ];

  const parts = [];
  parts.push(`<defs><clipPath id="map-clip"><rect x="0" y="0" width="${VIEW_W}" height="${VIEW_H}"></rect></clipPath></defs>`);
  parts.push(`<g clip-path="url(#map-clip)">`);
  parts.push(
    `<g transform="translate(${tx.toFixed(2)} ${ty.toFixed(2)}) scale(${k.toFixed(4)})">` +
    `<path class="land" d="${WORLD_LAND_PATH}"></path>` +
    `<path class="land-edge" d="${WORLD_LAND_PATH}"></path></g>`
  );

  const lonStep = view.span > 200 ? 60 : view.span > 90 ? 30 : 15;
  for (let lon = -180; lon <= 180; lon += lonStep) {
    const [x] = toScreen(lon, 0);
    if (x < -1 || x > VIEW_W + 1) continue;
    parts.push(`<line class="graticule" x1="${x.toFixed(1)}" y1="0" x2="${x.toFixed(1)}" y2="${VIEW_H}"></line>`);
  }
  const latTop = inverseMercatorY((0 - ty) / k);
  const latBottom = inverseMercatorY((VIEW_H - ty) / k);
  for (let lat = Math.floor(latTop / 30) * 30; lat <= Math.ceil(latBottom / 30) * 30; lat += 30) {
    if (lat < -80 || lat > 80) continue;
    const [, y] = toScreen(0, lat);
    parts.push(`<line class="graticule" x1="0" y1="${y.toFixed(1)}" x2="${VIEW_W}" y2="${y.toFixed(1)}"></line>`);
  }

  const policy = boot.summary || {};
  const home = boot.policy_home || demoHome(boot);
  if (home && typeof home.lat === "number" && policy.allow && policy.allow.radius_km > 0) {
    const ring = circlePoints(home.lat, home.lon, policy.allow.radius_km, toScreen);
    parts.push(`<polygon class="home-ring" points="${ring}"></polygon>`);
  }

  const clusters = (boot.clusters || []).filter((c) => state.showSeed || c.observed || c.allowed);
  const top = clusters.slice().sort((a, b) => b.bytes - a.bytes).slice(0, 6);
  const labelBoxes = [];
  const overlaps = (box) => labelBoxes.some(
    (b) => box.x < b.x + b.w && box.x + box.w > b.x && box.y < b.y + b.h && box.y + box.h > b.y
  );
  const placeLabel = (city, x, y) => {
    const text = String(city || "");
    if (!text) return "";
    const w = text.length * 5.4 + 4;
    const box = { x, y: y - 8, w, h: 12 };
    if (overlaps(box)) return "";
    labelBoxes.push(box);
    return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}">${esc(text)}</text>`;
  };
  for (const c of clusters) {
    if (c.lat === null || c.lon === null) continue;
    const [x, y] = toScreen(c.lon, c.lat);
    if (x < -12 || x > VIEW_W + 12 || y < -12 || y > VIEW_H + 12) continue;
    const r = Math.max(3.2, Math.min(8, 2.6 + Math.sqrt(c.server_count || 1) * 0.9));
    const label = top.includes(c) ? placeLabel(c.city || c.region, x + r + 3, y + 3) : "";
    parts.push(
      `<g class="cluster ${c.allowed ? "" : "blocked"}" data-region="${esc(c.region_key || "")}">` +
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r.toFixed(1)}"><title>${esc(c.city || "Unknown")} - ${esc(c.region || c.country || "")}${c.avg_rtt_ms ? ` - ${c.avg_rtt_ms} ms` : ""}</title></circle>${label}</g>`
    );
  }

  if (home && typeof home.lat === "number") {
    const [x, y] = toScreen(home.lon, home.lat);
    parts.push(`<circle class="home-marker" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4.5"></circle>`);
    if (placeLabel("You", x + 8, y + 4)) {
      parts.push(`<text class="home-label" x="${(x + 8).toFixed(1)}" y="${(y + 4).toFixed(1)}">You</text>`);
    }
  }
  parts.push(`</g>`);

  svg.innerHTML = parts.join("");
  svg.onclick = async (event) => {
    const pt = svg.createSVGPoint();
    pt.x = event.clientX;
    pt.y = event.clientY;
    const local = pt.matrixTransform(svg.getScreenCTM().inverse());
    const wx = (local.x - tx) / k;
    const wy = (local.y - ty) / k;
    const lon = (wx / MAP_W) * 360 - 180;
    const lat = inverseMercatorY(wy);
    if (lat < -60 || lat > 75 || lon < -180 || lon > 180) return;
    await call("set_home", lat, lon);
    state.mapView = { span: Math.min(view.span, 130), lat, lon };
    await refresh();
    const updated = state.boot && state.boot.policy_home;
    toast(updated && updated.label ? `Home set to ${updated.label}` : "Home location updated");
  };
}

function zoomMap(factor, anchor) {
  const view = mapView();
  const before = { ...view };
  const next = Math.max(40, Math.min(360, before.span * factor));
  if (next === before.span) return;
  view.span = next;
  if (anchor) {
    const k = (VIEW_W / MAP_W) * (360 / before.span);
    const tx = VIEW_W / 2 - ((before.lon + 180) / 360) * MAP_W * k;
    const ty = VIEW_H / 2 - mercatorY(before.lat) * k;
    const wx = (anchor.x - tx) / k;
    const wy = (anchor.y - ty) / k;
    const lon = (wx / MAP_W) * 360 - 180;
    const lat = inverseMercatorY(wy);
    view.lon = lon;
    view.lat = Math.max(-60, Math.min(75, lat));
  }
  renderMap();
}

function focusHome() {
  const boot = state.boot;
  const home = boot && (boot.policy_home || demoHome(boot));
  if (!home || typeof home.lat !== "number") {
    toast("Set a home location first", "warn");
    return;
  }
  state.mapView = { span: 130, lat: home.lat, lon: home.lon };
  renderMap();
}

function demoHome(boot) {
  const text = boot.policy_text || "";
  const lat = /lat:\s*(-?\d+(?:\.\d+)?)/.exec(text);
  const lon = /lon:\s*(-?\d+(?:\.\d+)?)/.exec(text);
  if (lat && lon) return { lat: Number(lat[1]), lon: Number(lon[1]) };
  return null;
}

function circlePoints(lat, lon, km, project) {
  const points = [];
  const d = km / 6371;
  for (let i = 0; i <= 64; i += 1) {
    const brg = (i / 64) * 2 * Math.PI;
    const lat1 = (lat * Math.PI) / 180;
    const lon1 = (lon * Math.PI) / 180;
    const lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(brg));
    const lon2 = lon1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
    const [x, y] = project((lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI);
    points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  return points.join(" ");
}

function visibleRegions() {
  const boot = state.boot;
  let regions = (boot.regions || []).filter((r) => {
    if (state.regionMode === "observed" && !r.observed) return false;
    if (state.regionMode === "allowed" && !r.allowed) return false;
    if (state.regionQuery) {
      const q = state.regionQuery.toLowerCase();
      if (!`${r.label} ${r.country} ${r.region_code}`.toLowerCase().includes(q)) return false;
    }
    return true;
  });
  return regions.sort((a, b) => {
    const ar = a.avg_rtt_ms ?? 9999;
    const br = b.avg_rtt_ms ?? 9999;
    return ar - br;
  });
}

function renderRegions() {
  const boot = state.boot;
  const blocklist = (boot.summary || {}).mode === "blocklist";
  const regions = visibleRegions();
  const list = $("#region-list");
  const byCountry = new Map();
  for (const r of regions) {
    if (!byCountry.has(r.country_code)) byCountry.set(r.country_code, []);
    byCountry.get(r.country_code).push(r);
  }

  const isOn = (r) => (blocklist ? r.permitted : r.allowed);

  const html = [];
  for (const [cc, group] of byCountry) {
    const country = group[0].country || cc;
    const allOn = group.every(isOn);
    html.push(`<div class="region-group">
      <div class="region-group-head">
        <input type="checkbox" data-group="${esc(cc)}" ${allOn ? "checked" : ""} aria-label="Toggle ${esc(country)}">
        <span>${esc(country)}</span>
        <span class="count">${group.length}</span>
      </div>`);
    for (const r of group) {
      const on = isOn(r);
      html.push(`<label class="region-row ${on ? "" : "is-off"}">
        <input type="checkbox" data-region="${esc(r.key)}" ${on ? "checked" : ""}>
        <span class="name">${esc(r.label || r.region)}</span>
        <span class="meta ${r.avg_rtt_ms && r.avg_rtt_ms <= 40 ? "hot" : ""}">${r.avg_rtt_ms ? `${r.avg_rtt_ms} ms` : "no ping"}</span>
        <span class="meta">${r.server_count} srv</span>
      </label>`);
    }
    html.push("</div>");
  }
  list.innerHTML = html.join("") || '<div class="empty">No regions match this filter yet. Play a match so the catalog can locate servers.</div>';
  $("#region-count").textContent = `${regions.length} of ${(boot.regions || []).length} locations`;
  $("#select-visible").textContent = blocklist ? "Unblock visible" : "Allow visible";
  $("#clear-regions").textContent = blocklist ? "Clear blocklist" : "Clear allowlist";

  const radius = (boot.summary && boot.summary.allow && boot.summary.allow.radius_km) || 0;
  $("#radius").value = String(Math.min(3000, radius));
  $("#radius-value").textContent = radius ? `${Math.round(radius)} km` : "Off";
  const home = boot.policy_home || demoHome(boot);
  $("#home-label").textContent = home && home.hidden
    ? home.label
    : home && typeof home.lat === "number"
      ? (home.label || `${home.lat.toFixed(2)}, ${home.lon.toFixed(2)}`)
      : "Not set";
}

function renderServers() {
  const boot = state.boot;
  if (!boot) return;
  const q = state.serverQuery.toLowerCase();
  let rows = (boot.servers || []).filter((s) => {
    if (state.serverVerdict === "allowed" && !s.allowed) return false;
    if (state.serverVerdict === "blocked" && s.allowed) return false;
    if (state.serverVerdict === "observed" && !s.packets) return false;
    if (q && !`${s.cidr} ${s.city} ${s.region} ${s.country}`.toLowerCase().includes(q)) return false;
    return true;
  });

  const { key, asc } = state.serverSort;
  rows = rows.sort((a, b) => {
    const av = a[key] ?? (typeof b[key] === "number" ? -1 : "");
    const bv = b[key] ?? (typeof a[key] === "number" ? -1 : "");
    if (typeof av === "number" && typeof bv === "number") return asc ? av - bv : bv - av;
    return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });

  $("#servers-table tbody").innerHTML = rows.map((s) => `
    <tr>
      <td class="mono">${esc(s.cidr)}</td>
      <td>${esc(s.city || "Unclassified")}</td>
      <td>${esc(s.region || s.country || "")}${s.region_key ? `<div class="hint">${esc(s.region_key)}</div>` : ""}</td>
      <td class="num">${fmtPing(s.rtt_ms)}</td>
      <td class="num">${s.packets ? s.packets.toLocaleString() : "--"}</td>
      <td class="num">${fmtBytes(s.bytes)}</td>
      <td><span class="verdict ${s.allowed ? "" : "blocked"}"><i></i>${s.allowed ? "Allowed" : "Blocked"}</span><div class="hint">${esc(s.verdict)}</div></td>
    </tr>`).join("");
  $("#servers-empty").hidden = rows.length > 0;
  $("#servers-table").hidden = rows.length === 0;

  $$("#servers-table th").forEach((th) => {
    const k = th.dataset.sort;
    th.classList.toggle("sorted", k === key);
    th.classList.toggle("asc", k === key && asc);
  });
}

function renderPolicyFacts() {
  const boot = state.boot;
  const summary = boot.summary || {};
  const allow = summary.allow || {};
  const facts = [
    ["Mode", summary.mode === "allowlist" ? "Allowlist" : "Blocklist"],
    ["Regions allowed", allow.regions ? allow.regions.length : 0],
    ["Countries allowed", allow.countries ? allow.countries.length : 0],
    ["Radius", allow.radius_km ? `${Math.round(allow.radius_km)} km` : "Off"],
    ["Ping limit", summary.max_rtt_ms ? `${summary.max_rtt_ms} ms` : "Off"],
    ["Unknown endpoints", summary.unknown === "deny" ? "Blocked" : "Allowed"],
  ];
  $("#policy-facts").innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  $("#policy-scope").textContent = `${summary.allowed_clusters || 0} of ${summary.clusters || 0} located clusters allowed`;

  const notices = [];
  if (summary.empty_allowlist) notices.push("No allow rules. Every located server will be blocked.");
  if (summary.unknown === "deny") notices.push("Unclassified endpoints are blocked. New servers will fail until they are located.");
  const notice = $("#policy-notice");
  notice.hidden = notices.length === 0;
  notice.className = `notice ${summary.empty_allowlist ? "bad" : ""}`;
  notice.innerHTML = notices.map((n) => `<div>${esc(n)}</div>`).join("");
}

function renderSettings() {
  const boot = state.boot;
  const cfg = boot.config || {};
  const summary = boot.summary || {};
  $("#set-startup").checked = Boolean(cfg.start_with_windows);
  $("#set-autofilter").checked = Boolean(cfg.filtering);
  $("#set-tray").checked = cfg.minimize_to_tray !== false;
  $("#set-streamer").checked = Boolean(cfg.streamer_mode);
  $("#set-soft").checked = Boolean(summary.soft && summary.soft.enabled);
  $("#set-soft-seconds").value = (summary.soft && summary.soft.relax_after_s) || 45;
  $("#set-unknown").value = summary.unknown || "allow";
  $("#set-geo").checked = Boolean(cfg.auto_geolocate);
  $("#show-seed").checked = Boolean(state.showSeed);

  const stats = boot.stats || {};
  $("#catalog-facts").innerHTML = [
    ["Known ranges", (stats.ranges || 0).toLocaleString()],
    ["Observed ranges", (stats.observed_ranges || 0).toLocaleString()],
    ["Traffic recorded", fmtBytes(stats.bytes || 0)],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

  const update = boot.catalog_update || {};
  $("#catalog-update-status").textContent = update.message
    || "Adds the Geomate and cod-server-list ranges to the local catalog.";
  $("#update-catalog").disabled = Boolean(update.running);

  const capture = boot.capture || {};
  $("#app-facts").innerHTML = [
    ["Version", boot.version],
    ["Administrator", boot.elevated ? "Yes" : "No"],
    ["Packet driver", boot.driver ? (capture.running ? "Running" : "Ready") : "Not installed"],
    ["Game", boot.game ? `${boot.game.name} (${boot.game.pid})` : "Not detected"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

  const st = boot.state || {};
  $("#diag-facts").innerHTML = [
    ["Watched UDP ports", (capture.ports || []).join(", ") || "None"],
    ["Service endpoints pinned", summary.service_pins || 0],
    ["Policy", `${summary.mode || "?"}, unknown ${summary.unknown || "?"}`],
    ["Allowed / denied packets", `${st.allowed_packets || 0} / ${st.denied_packets || 0}`],
    ["Matches recorded", st.match_count || 0],
    ["Driver status", capture.status || "unknown"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("");

  const streamer = Boolean(cfg.streamer_mode);
  for (const id of ["#export-policy", "#import-policy"]) {
    const el = $(id);
    if (el) el.disabled = streamer;
  }
}

function loadPolicyEditor() {
  const boot = state.boot;
  if (!boot) return;
  const streamer = Boolean((boot.config || {}).streamer_mode);
  const editor = $("#policy-editor");
  editor.disabled = streamer;
  for (const id of ["#policy-save", "#policy-revert", "#policy-reset"]) {
    const el = $(id);
    if (el) el.disabled = streamer;
  }
  if (streamer) {
    // The policy file carries the home coordinates, so it stays off screen
    // until streamer mode is turned off.
    if (!state.policyDirty) {
      editor.value = "Streamer mode is on. Turn it off in Settings to view or edit the policy.";
    }
    $("#policy-path").textContent = "Hidden";
    $("#policy-state").textContent = "Hidden";
    $("#policy-state").className = "tag";
    renderPolicyFacts();
    return;
  }
  if (!state.policyDirty) {
    $("#policy-editor").value = boot.policy_text;
    state.lastPolicyText = boot.policy_text;
  }
  $("#policy-path").textContent = boot.paths ? boot.paths.policy : "";
  $("#policy-state").textContent = boot.policy_error ? "Rejected" : "Valid";
  $("#policy-state").className = `tag ${boot.policy_error ? "bad" : "ok"}`;
  renderPolicyFacts();
}

/* ------------------------------------------------------------------ */
/* Data flow                                                          */
/* ------------------------------------------------------------------ */

async function refresh() {
  try {
    const boot = await call("bootstrap");
    state.boot = boot;
    state.showSeed = Boolean(boot.config.show_seed_clusters);
    renderChrome(boot);
    renderDashboard(boot);
    renderRegions();
    renderMap();
    renderServers();
    renderSettings();
    if (state.screen === "rules" && !state.policyDirty) loadPolicyEditor();
    if (!boot.config.wizard_done && (!DEMO || state.wizardPreview) && !state.wizardShown) {
      state.wizardShown = true;
      openWizard(1);
    }
  } catch (err) {
    toast(err.message || String(err), "bad");
  }
}

async function poll() {
  if (!state.boot || state.busy) return;
  try {
    const data = await call("poll");
    state.boot.state = data.state;
    state.boot.game = data.game;
    state.boot.elevated = data.elevated;
    state.boot.summary = data.summary;
    state.boot.matches = data.matches;
    state.boot.regions = data.regions;
    state.boot.capture = data.capture;
    if (data.catalog_update) {
      const previous = state.boot.catalog_update || {};
      state.boot.catalog_update = data.catalog_update;
      if (previous.running && !data.catalog_update.running) {
        const ok = (data.catalog_update.report || []).some((entry) => entry.ok);
        toast(data.catalog_update.message, ok ? "ok" : "bad");
        refresh();
      }
    }
    renderChrome(state.boot);
    if (state.screen === "dashboard") renderDashboard(state.boot);
    if (state.screen === "regions") {
      renderRegions();
      if (data.state.current) renderMap();
    }
    if (state.screen === "rules") renderPolicyFacts();
  } catch (err) {
    /* polling errors are transient during shutdown */
  }
}

async function mutate(promise, successMessage) {
  state.busy = true;
  try {
    const result = await promise;
    if (result && result.ok === false) {
      toast((result.errors && result.errors.join("; ")) || result.error || "Change rejected", "bad");
      return false;
    }
    if (result && result.warnings && result.warnings.length) {
      toast(result.warnings.join(" "), "bad");
    }
    if (successMessage) toast(successMessage, "ok");
    await refresh();
    return result || {};
  } catch (err) {
    toast(err.message || String(err), "bad");
    return false;
  } finally {
    state.busy = false;
  }
}

/* ------------------------------------------------------------------ */
/* First run wizard                                                   */
/* ------------------------------------------------------------------ */

function openWizard(step) {
  const modal = $("#modal");
  const box = $(".modal");
  const boot = state.boot;
  const steps = {
    1: {
      title: "Set up filtering",
      body: `<p>COD Ping King drops game traffic to servers outside the regions you allow.</p>
        <ul class="wizard-list">
          <li><strong>Game</strong><span>${boot.game ? `${esc(boot.game.name)} detected` : "Not running yet"}</span></li>
          <li><strong>Administrator</strong><span>${boot.elevated ? "Yes, filtering available" : "No, restart as administrator to filter"}</span></li>
          <li><strong>Catalog</strong><span>${(boot.stats.ranges || 0).toLocaleString()} known ranges</span></li>
        </ul>
        <p class="hint">Nothing is blocked until you choose regions.</p>`,
      next: "Continue",
    },
    2: {
      title: "Where are you playing from?",
      body: `<p>Your location is used for the radius option and for sorting regions by ping. It is stored only in the local policy file.</p>
        <div class="wizard-home" id="wizard-home">${$("#home-label").textContent}</div>
        <p class="hint">You can also click the map later to move it.</p>`,
      next: "Detect location",
      action: "detect",
    },
    3: {
      title: "Choose your regions",
      body: `<p>Recommended picks every region measured under 40 ms. You can fine-tune this at any time on the Regions page.</p>
        <ul class="wizard-list" id="wizard-regions">
          ${(boot.regions || []).filter((r) => r.avg_rtt_ms && r.avg_rtt_ms <= 40).slice(0, 6).map((r) => `<li><strong>${esc(r.label)}</strong><span>${r.avg_rtt_ms} ms</span></li>`).join("") || "<li><strong>No ping data yet</strong><span>Play one match first</span></li>"}
        </ul>`,
      next: "Use recommended",
      action: "recommend",
    },
  };
  const content = steps[step];
  box.innerHTML = `
    <div class="wizard-progress" role="img" aria-label="Step ${step} of 3">
      ${[1, 2, 3].map((n) => `<span class="${n <= step ? "on" : ""}"></span>`).join("")}
    </div>
    <h2 id="modal-title">${content.title}</h2>
    <div class="modal-text">${content.body}</div>
    <div class="modal-actions">
      ${step > 1 ? '<button class="btn" data-wiz="back">Back</button>' : '<button class="btn" data-wiz="skip">Skip</button>'}
      <button class="btn btn-primary" data-wiz="next">${content.next}</button>
    </div>`;
  modal.hidden = false;

  const skipBtn = box.querySelector('[data-wiz="skip"]');
  if (skipBtn) skipBtn.onclick = () => finishWizard();
  const backBtn = box.querySelector('[data-wiz="back"]');
  if (backBtn) backBtn.onclick = () => openWizard(step - 1);
  box.querySelector('[data-wiz="next"]').onclick = async () => {
    if (content.action === "detect") {
      const result = await call("detect_home");
      if (!result.ok) {
        toast(result.error || "Location lookup failed. Pick a point on the map instead.", "bad");
        return;
      }
      toast("Location set", "ok");
      await refresh();
      openWizard(3);
      return;
    }
    if (content.action === "recommend") {
      await call("apply_recommended", 40);
      await refresh();
      finishWizard();
      return;
    }
    openWizard(step + 1);
  };
}

async function finishWizard() {
  $("#modal").hidden = true;
  await call("set_setting", "wizard_done", true);
  await refresh();
  toast("Setup complete. Filtering is ready.", "ok");
}

function rebuildModal() {
  $(".modal").innerHTML = `
    <h2 id="modal-title">Confirm</h2>
    <p id="modal-text"></p>
    <div class="modal-actions">
      <button class="btn" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-confirm">Confirm</button>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* Events                                                             */
/* ------------------------------------------------------------------ */

function bindEvents() {
  $$(".nav-item").forEach((el) => el.addEventListener("click", () => nav(el.dataset.screen)));
  $$("[data-go]").forEach((el) => el.addEventListener("click", () => nav(el.dataset.go)));

  $("#filter-toggle").addEventListener("click", () => {
    const next = !state.boot.state.filtering;
    mutate(call("set_filtering", next), next ? "Filtering resumed" : "Filtering paused");
  });
  $("#rail-pause").addEventListener("click", () => $("#filter-toggle").click());

  $("#elevate-btn").addEventListener("click", async () => {
    const result = await call("relaunch_admin");
    if (!result.ok) toast(result.error || "Could not request elevation", "bad");
  });

  $("#soft-stop-btn").addEventListener("click", async () => {
    await call("set_soft_mode", false, 45);
    toast("Search guard disabled");
    await refresh();
  });

  $("#allow-region-btn").addEventListener("click", () => {
    if (!state.currentRegionKey) return;
    mutate(call("set_region_blocked", state.currentRegionKey, false), "Region allowed");
  });
  $("#deny-region-btn").addEventListener("click", () => {
    if (!state.currentRegionKey) return;
    mutate(call("set_region_blocked", state.currentRegionKey, true), "Region blocked");
  });

  $("#export-dash").addEventListener("click", async () => {
    const result = await call("export_csv");
    toast(result.ok ? `Exported to ${result.path}` : result.error || "Export failed", result.ok ? "ok" : "bad");
  });

  $("#matches-table").addEventListener("click", async (e) => {
    const btn = e.target.closest(".match-block");
    if (!btn) return;
    const ip = btn.dataset.ip;
    const blocked = btn.dataset.blocked === "1";
    const result = await mutate(call("block_server", ip, !blocked), "");
    if (result) {
      toast(blocked ? `Unblocked ${ip}` : `Blocked ${result.cidr || ip} for future matches`, "ok");
    }
  });

  // regions
  $("#region-search").addEventListener("input", (e) => {
    state.regionQuery = e.target.value;
    renderRegions();
  });
  $$("#region-filter button").forEach((btn) => btn.addEventListener("click", () => {
    $$("#region-filter button").forEach((b) => b.classList.toggle("is-active", b === btn));
    state.regionMode = btn.dataset.mode;
    renderRegions();
  }));
  $("#region-list").addEventListener("change", async (event) => {
    const target = event.target;
    const blocklist = (state.boot.summary || {}).mode === "blocklist";
    if (target.dataset.region) {
      if (blocklist) await call("set_region_blocked", target.dataset.region, !target.checked);
      else await call("set_regions", [target.dataset.region], target.checked);
      await refresh();
    } else if (target.dataset.group) {
      const keys = visibleRegions().filter((r) => r.country_code === target.dataset.group).map((r) => r.key);
      if (blocklist) {
        await call("set_regions_blocked", keys, !target.checked);
      } else {
        await call("set_regions", keys, target.checked);
      }
      await refresh();
    }
  });
  $("#region-list").addEventListener("click", (event) => {
    const cluster = event.target.closest("[data-region]");
    if (cluster && state.screen === "regions") {
      const key = cluster.dataset.region;
      const row = $(`.region-row input[data-region="${CSS.escape(key)}"]`);
      if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  });
  $("#select-visible").addEventListener("click", () => {
    const keys = visibleRegions().map((r) => r.key);
    if ((state.boot.summary || {}).mode === "blocklist") {
      mutate(call("set_regions_blocked", keys, false), "Visible regions unblocked");
    } else {
      mutate(call("set_regions", keys, true), "Visible regions allowed");
    }
  });
  $("#clear-regions").addEventListener("click", async () => {
    const blocklist = (state.boot.summary || {}).mode === "blocklist";
    const ok = await confirmDialog(
      blocklist ? "Clear the blocklist?" : "Clear the allowlist?",
      blocklist ? "All regions become allowed until you block one again." : "Every located server will be blocked until you pick regions again.",
      "Clear",
    );
    if (!ok) return;
    if (blocklist) {
      mutate(call("clear_blocked_regions"), "Blocklist cleared");
    } else {
      const keys = (state.boot.regions || []).map((r) => r.key);
      mutate(call("set_regions", keys, false), "Allowlist cleared");
    }
  });
  $("#recommend-regions").addEventListener("click", () => mutate(call("apply_recommended", 40), "Recommended regions applied"));
  $("#detect-home").addEventListener("click", async () => {
    const result = await call("detect_home");
    if (!result.ok) return toast(result.error || "Location lookup failed", "bad");
    toast("Home location updated", "ok");
    state.mapView = null;
    await refresh();
  });
  $("#clear-home").addEventListener("click", async () => {
    const ok = await confirmDialog(
      "Reset home location?",
      "The radius allowlist will be turned off. You can detect your location again at any time.",
      "Reset"
    );
    if (!ok) return;
    await call("clear_home");
    state.mapView = null;
    await refresh();
    toast("Home location cleared", "ok");
  });
  $("#radius").addEventListener("change", async (event) => {
    const value = Number(event.target.value);
    const home = (state.boot && state.boot.policy_home) || {};
    if (value > 0 && typeof home.lat !== "number") {
      toast("Finding your location for the radius", "ok");
      const result = await call("detect_home");
      if (!result.ok) {
        event.target.value = "0";
        $("#radius-value").textContent = "Off";
        toast(result.error || "Set your home location before using a radius", "bad");
        return;
      }
      await refresh();
      if (result.home && typeof result.home.lat === "number") {
        state.mapView = { span: 130, lat: result.home.lat, lon: result.home.lon };
        renderMap();
      }
    }
    mutate(call("set_radius", value), value ? "Radius allowlist applied" : "Radius allowlist off");
  });
  $("#radius").addEventListener("input", (event) => {
    const value = Number(event.target.value);
    $("#radius-value").textContent = value ? `${value} km` : "Off";
  });
  $("#show-seed").addEventListener("change", async (event) => {
    state.showSeed = event.target.checked;
    await call("set_setting", "show_seed_clusters", state.showSeed);
    renderMap();
  });
  $("#map-zoom-in").addEventListener("click", () => zoomMap(0.7));
  $("#map-zoom-out").addEventListener("click", () => zoomMap(1 / 0.7));
  $("#map-home").addEventListener("click", focusHome);
  $("#map-world").addEventListener("click", () => {
    state.mapView = { span: 360, lat: 20, lon: 0 };
    renderMap();
  });
  $("#map").addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = $("#map").getBoundingClientRect();
    const anchor = {
      x: ((event.clientX - rect.left) / rect.width) * VIEW_W,
      y: ((event.clientY - rect.top) / rect.height) * VIEW_H,
    };
    zoomMap(event.deltaY > 0 ? 1.25 : 0.8, anchor);
  }, { passive: false });

  // servers
  $("#server-search").addEventListener("input", (e) => {
    state.serverQuery = e.target.value;
    renderServers();
  });
  $$("#server-chips .chip").forEach((btn) => btn.addEventListener("click", () => {
    $$("#server-chips .chip").forEach((b) => b.classList.toggle("is-active", b === btn));
    state.serverVerdict = btn.dataset.verdict;
    renderServers();
  }));
  $$("#servers-table th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    state.serverSort = { key, asc: state.serverSort.key === key ? !state.serverSort.asc : false };
    renderServers();
  }));

  // rules
  let validateTimer;
  $("#policy-editor").addEventListener("input", () => {
    state.policyDirty = true;
    $("#policy-state").textContent = "Unsaved";
    $("#policy-state").className = "tag warn";
    $("#policy-hint").textContent = "Checking...";
    clearTimeout(validateTimer);
    validateTimer = setTimeout(async () => {
      const text = $("#policy-editor").value;
      const result = await call("validate_policy", text);
      const errors = (result.errors || []).concat(result.warnings || []);
      $("#policy-state").textContent = result.ok ? (errors.length ? "Warning" : "Valid") : "Error";
      $("#policy-state").className = `tag ${result.ok ? (errors.length ? "warn" : "ok") : "bad"}`;
      $("#policy-editor").classList.toggle("invalid", !result.ok);
      $("#policy-hint").textContent = errors[0] || "Changes apply when you save.";
    }, 350);
  });
  $("#policy-save").addEventListener("click", async () => {
    const result = await call("save_policy", $("#policy-editor").value);
    if (!result.ok) {
      $("#policy-state").textContent = "Rejected";
      $("#policy-state").className = "tag bad";
      $("#policy-editor").classList.add("invalid");
      $("#policy-hint").textContent = (result.errors || []).join("; ");
      return;
    }
    state.policyDirty = false;
    $("#policy-editor").classList.remove("invalid");
    toast("Policy saved", "ok");
    await refresh();
    loadPolicyEditor();
  });
  $("#policy-revert").addEventListener("click", async () => {
    state.policyDirty = false;
    await refresh();
    loadPolicyEditor();
    toast("Reverted to saved policy");
  });

  // settings
  $("#set-startup").addEventListener("change", (e) => mutate(call("set_setting", "start_with_windows", e.target.checked), "Startup preference saved"));
  $("#set-autofilter").addEventListener("change", (e) => mutate(call("set_setting", "filtering", e.target.checked), "Startup filtering saved"));
  $("#set-geo").addEventListener("change", (e) => mutate(call("set_setting", "auto_geolocate", e.target.checked), "Catalog lookups updated"));
  $("#set-tray").addEventListener("change", (e) => mutate(call("set_setting", "minimize_to_tray", e.target.checked), e.target.checked ? "Close will minimize to tray" : "Close will quit the app"));
  $("#set-streamer").addEventListener("change", async (e) => {
    const on = e.target.checked;
    state.mapView = null;
    await mutate(call("set_setting", "streamer_mode", on), on ? "Streamer mode on. Details are hidden." : "Streamer mode off. Details are visible.");
  });
  $("#set-soft").addEventListener("change", (e) => mutate(call("set_soft_mode", e.target.checked, Number($("#set-soft-seconds").value) || 45), "Search guard saved"));
  $("#set-soft-seconds").addEventListener("change", (e) => mutate(call("set_soft_mode", $("#set-soft").checked, Number(e.target.value) || 45), "Search guard delay saved"));
  $("#set-unknown").addEventListener("change", (e) => mutate(call("set_unknown", e.target.value), "Unknown endpoint rule saved"));
  $("#update-catalog").addEventListener("click", async () => {
    const result = await call("update_catalog");
    if (!result.ok) {
      toast(result.error || "Update already running", "bad");
      return;
    }
    state.boot.catalog_update = { running: true, message: "Downloading public server lists...", report: [] };
    renderSettings();
  });
  $("#export-catalog").addEventListener("click", async () => {
    const result = await call("export_csv");
    toast(result.ok ? `Exported to ${result.path}` : result.error || "Export failed", result.ok ? "ok" : "bad");
  });
  $("#open-folder").addEventListener("click", () => call("open_folder", "policy"));
  $("#reset-catalog").addEventListener("click", async () => {
    const ok = await confirmDialog("Reset the catalog?", "All observed servers, locations and match history will be deleted. The policy file is kept.", "Reset");
    if (!ok) return;
    mutate(call("reset_catalog"), "Catalog reset");
  });

  $("#setup-btn").addEventListener("click", () => openWizard());
  $("#export-policy").addEventListener("click", async () => {
    const result = await call("export_policy");
    toast(result.ok ? `Policy exported to ${result.path}` : result.error || "Export failed", result.ok ? "ok" : "bad");
  });
  $("#import-policy").addEventListener("click", () => $("#import-policy-file").click());
  $("#import-policy-file").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const text = await file.text();
    const ok = await confirmDialog(
      "Replace your policy?",
      `Import rules from ${file.name}? Your current regions, radius and rules will be replaced.`,
      "Import"
    );
    if (!ok) return;
    mutate(call("save_policy", text), "Policy imported");
  });
  $("#copy-diag").addEventListener("click", async () => {
    const boot = state.boot || {};
    const payload = {
      version: boot.version,
      elevated: boot.elevated,
      driver: boot.driver,
      game: boot.game,
      capture: boot.capture,
      summary: boot.summary,
      state: boot.state,
    };
    const ok = await copyText(JSON.stringify(payload, null, 2));
    toast(ok ? "Diagnostics copied" : "Could not access the clipboard", ok ? "ok" : "bad");
  });
  $("#open-log").addEventListener("click", () => call("open_folder", "log"));

  $("#quit-app").addEventListener("click", async () => {
    const yes = await confirmDialog(
      "Quit COD Ping King?",
      "Filtering stops and the window closes. The tray icon is removed as well.",
      "Quit"
    );
    if (yes) call("quit");
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#modal").hidden) {
      $("#modal").hidden = true;
      rebuildModal();
    }
  });
}

/* ------------------------------------------------------------------ */
/* Boot                                                               */
/* ------------------------------------------------------------------ */

(async function init() {
  $$(".nav-icon").forEach((el) => { el.innerHTML = ICONS[el.dataset.icon] || ""; });
  rebuildModal();
  bindEvents();
  await apiReady();
  if (DEMO && !CLEAN_PARAM) {
    const banner = document.createElement("div");
    banner.className = "banner banner-info";
    banner.innerHTML = "<div><strong>Preview data.</strong><span>This window is showing sample servers. Install the desktop app to read live game traffic.</span></div>";
    document.querySelector(".main").insertBefore(banner, $("#admin-banner"));
  }
  await refresh();
  setInterval(poll, 2000);
})();
