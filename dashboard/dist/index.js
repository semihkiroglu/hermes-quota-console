// Projection helper lives at module scope (outside the dashboard IIFE) so
// Node test fixtures can exercise it without instantiating a fake React
// SDK. The browser bundle still calls the same function; only the location
// of the `function` declaration changes.
function projectProfiles(profiles) {
  const list = Array.isArray(profiles) ? profiles : [];
  const defaultProfile = list.find(function (item) {
    return item && (item.id === "default" || item.profile === "default");
  }) || null;
  const otherProfiles = list
    .filter(function (item) { return item && item !== defaultProfile; })
    .slice()
    .sort(function (a, b) {
      const nameA = String((a && (a.profile || a.id)) || "");
      const nameB = String((b && (b.profile || b.id)) || "");
      return nameA.localeCompare(nameB, undefined, { sensitivity: "base" });
    });
  return { defaultProfile: defaultProfile, otherProfiles: otherProfiles };
}

function canResetProfileStatus(status) {
  const normalized = String(status || "").trim().toLowerCase().replace(/-/g, "_");
  // Reset lifts a Hermes-imposed usage block (rate limit / degraded state).
  // auth_failed is a credential problem, not a reset concern: the profile
  // row still shows the status, but no reset action is offered for it.
  return normalized === "rate_limited" || normalized === "degraded";
}

// Split an overview of provider buckets into a visible group and a
// hidden group, preserving the input order inside each. Unconfigured
// buckets (no quota, no profiles, not configured in Hermes) never enter
// either group and are dropped before partitioning. The render loop then
// appends `[...visible, ...(customizeMode ? hidden : [])]` so hidden
// cards render at the bottom while customize mode is on.
function isAutoHiddenBucket(bucket) {
  if (!bucket) return false;
  const hasQuota = Boolean(bucket.has_quota);
  const configured = Boolean(bucket.configured);
  const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];
  // Configured in Hermes but with no quota source and no assigned profile:
  // the bucket carries no useful quota data yet, so it starts hidden.
  return !hasQuota && !bucketProfiles.length && configured;
}

function partitionBuckets(buckets, isHiddenFn) {
  const list = Array.isArray(buckets) ? buckets : [];
  const classify = typeof isHiddenFn === "function" ? isHiddenFn : isAutoHiddenBucket;
  const visible = [];
  const hidden = [];
  for (let index = 0; index < list.length; index += 1) {
    const bucket = list[index];
    if (!bucket) continue;
    const hasQuota = Boolean(bucket.has_quota);
    const configured = Boolean(bucket.configured);
    const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];
    // Unconfigured buckets (no quota, no profiles, not configured in
    // Hermes) never appear in either group.
    if (!hasQuota && !bucketProfiles.length && !configured) continue;
    if (classify(bucket)) {
      hidden.push(bucket);
    } else {
      visible.push(bucket);
    }
  }
  return { visible: visible, hidden: hidden };
}

// Pure helper for the dashboard bundle's balance-row className decision.
// Returns the CSS className suffix a balance row should render with given
// the item's source role and its computed alert level. A fallback item
// never raises an alert (AGENTS rule 5) so the role trumps the level:
// fallback rows render the neutral style regardless of the level the
// alarm layer computed. Exposed at module scope so the test suite can
// exercise the same decision the browser renders with.
function balanceRowClass(role, level) {
  const normalized = String(level || "").trim().toLowerCase();
  const levelSuffix =
    normalized === "low" || normalized === "exhausted" || normalized === "unknown"
      ? "usages-level--" + normalized
      : "";
  if (role === "fallback") return "";
  return levelSuffix;
}

// Apply a stored provider-card order to a bucket list. Buckets whose id
// appears in ``orderIds`` sort by their stored position; buckets missing
// from the order (newly configured providers) keep their backend order
// and trail the stored ones, so a fresh provider never vanishes.
function applyStoredOrder(buckets, orderIds) {
  const list = Array.isArray(buckets) ? buckets.slice() : [];
  const order = Array.isArray(orderIds) ? orderIds : [];
  if (!order.length) return list;
  const position = {};
  order.forEach(function (id, index) {
    if (id !== null && id !== undefined) position[String(id)] = index;
  });
  return list
    .map(function (bucket, backendIndex) {
      const id = bucket && bucket.id !== undefined ? String(bucket.id) : "";
      const stored = Object.prototype.hasOwnProperty.call(position, id) ? position[id] : order.length + backendIndex;
      return { bucket: bucket, stored: stored };
    })
    .sort(function (a, b) { return a.stored - b.stored; })
    .map(function (entry) { return entry.bucket; });
}

// Move ``fromId`` to just before or just after ``toId`` in a list of
// provider ids; everything between shifts by one. ``edge`` is "before"
// (default) or "after". Pure, so the drag-drop reorder path is directly
// testable under Node.
function moveProviderId(ids, fromId, toId, edge) {
  const list = Array.isArray(ids) ? ids.slice() : [];
  const from = list.indexOf(fromId);
  if (from === -1) return list;
  const moved = list.splice(from, 1)[0];
  const to = list.indexOf(toId);
  if (to === -1) {
    list.splice(from, 0, moved);
    return list;
  }
  list.splice(to + (edge === "after" ? 1 : 0), 0, moved);
  return list;
}

// Summarise a /summary payload into the shell banner state (module scope
// so Node fixtures can exercise the aggregation without a React SDK).
// Returns null when nothing needs attention (the banner stays hidden),
// otherwise { level: "critical" | "low", count, names[], extra } where
// ``names`` holds the first provider labels and ``extra`` the remaining
// count beyond three.
//
// Two attention families feed the banner:
//   - quota alerts (summary.alerts): exhausted balance/window or a
//     window/balance under its configured threshold -> the existing
//     top-alert inputs, already aggregated by the API layer;
//   - provider status (provider_overview): profiles blocked by Hermes
//     (rate_limited/degraded — reset action exists) or failing auth
//     (auth_failed — credential problem, no reset offered).
// Unavailable snapshots (a transient fetch failure, no operator action)
// never reach the banner: the card already shows the state and the next
// poll usually clears it. Critical (red) wins over low (yellow): one
// strip, red first.
function bannerAlertFromSummary(summary) {
  if (!summary || typeof summary !== "object") return null;
  const alerts = (summary.alerts && typeof summary.alerts === "object") ? summary.alerts : {};
  const exhausted = Array.isArray(alerts.exhausted) ? alerts.exhausted : [];
  const low = Array.isArray(alerts.low) ? alerts.low : [];
  const overview = Array.isArray(summary.provider_overview) ? summary.provider_overview : [];

  const critical = [];
  exhausted.forEach(function (entry) {
    if (entry && typeof entry.provider === "string" && entry.provider) critical.push(entry.provider);
  });
  overview.forEach(function (bucket) {
    if (!bucket || typeof bucket !== "object") return;
    const availability = bucket.provider_availability;
    const status = availability && typeof availability === "object" ? availability.status : null;
    if (status === "rate_limited" || status === "degraded" || status === "auth_failed") {
      const label = String(bucket.label || bucket.id || "");
      if (label) critical.push(label);
    }
  });
  // De-duplicate provider labels while preserving first-seen order.
  const seen = {};
  const uniqueCritical = critical.filter(function (label) {
    if (seen[label]) return false;
    seen[label] = true;
    return true;
  });

  function describe(items, level) {
    if (!items.length) return null;
    const names = items.slice(0, 3).map(function (entry) {
      return typeof entry === "string"
        ? entry
        : (entry && typeof entry.provider === "string" ? entry.provider : "unknown");
    });
    return {
      level: level,
      count: items.length,
      names: names,
      extra: items.length > 3 ? items.length - 3 : 0,
    };
  }
  return describe(uniqueCritical, "critical") || describe(low, "low") || null;
}

// Semantic version comparison: returns true when ``left`` is strictly
// newer than ``right`` (treats "1.10.0" > "1.9.0", which a string
// comparison would get wrong). Non-numeric input makes the check
// fail-closed: the caller shows nothing rather than a bogus update.
function versionNewerThan(left, right) {
  const parse = function (value) {
    const match = String(value).trim().replace(/^v/, "").match(/^(\d+)\.(\d+)\.(\d+)/);
    return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
  };
  const a = parse(left);
  const b = parse(right);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

// Returns the latest published release tag when it is strictly newer
// than the running version, or null when the running version is up to
// date, newer than the latest release (e.g. a dev checkout ahead of the
// latest tag), or the check is unavailable. Pure, so Node fixtures can
// exercise the comparison without a React SDK.
function releaseUpdate(currentVersion, latestRelease) {
  if (!currentVersion || !latestRelease) return null;
  return versionNewerThan(latestRelease, currentVersion) ? String(latestRelease) : null;
}

// Dismissable in-page update alert state. Returns the release tag to
// show (a newer tag the operator has not dismissed yet) or null.
// ``dismissedTag`` is whatever the operator closed before — the same
// tag stays hidden, a newer one re-appears. Pure, Node-testable.
function updateAlertVisible(currentVersion, latestRelease, dismissedTag) {
  const update = releaseUpdate(currentVersion, latestRelease);
  if (!update) return null;
  return update === dismissedTag ? null : update;
}

// Canonical notification level ordering and the built-in opt-in defaults.
// Mirrors dashboard/settings.py on the server side. The frontend never
// invents new levels; the backend rejects anything outside this list.
const NOTIFICATION_LEVELS = ["critical", "low"];
const NOTIFICATION_DEFAULTS = Object.freeze({
  enabled: false,
  levels: ["critical"],
  cooldown_minutes: 60,
});

// Stable identity for one (level, provider) alert. The frontend uses it
// as the dedupe key: a notification fires when the alert set changes,
// i.e. when the identity list grows OR a known identity returns with a
// strictly higher level ("low" → "critical"). Repeated polls that yield
// the same set do not re-fire — cooldown applies on top to suppress the
// case where the operator keeps the page open across long outages.
function notificationAlertIdentity(summary) {
  if (!summary || typeof summary !== "object") return [];
  const alerts = (summary.alerts && typeof summary.alerts === "object") ? summary.alerts : {};
  const overview = Array.isArray(summary.provider_overview) ? summary.provider_overview : [];
  const exhausted = Array.isArray(alerts.exhausted) ? alerts.exhausted : [];
  const low = Array.isArray(alerts.low) ? alerts.low : [];
  const seen = {};
  const out = [];
  function pushFromAlert(level, entry) {
    if (!entry || typeof entry.provider !== "string" || !entry.provider) return;
    const seenKey = level + "|" + entry.provider;
    if (seen[seenKey]) return;
    seen[seenKey] = true;
    out.push({ level: level, provider: entry.provider });
  }
  exhausted.forEach(function (entry) { pushFromAlert("critical", entry); });
  low.forEach(function (entry) { pushFromAlert("low", entry); });
  // Provider availability also drives the red top alert: rate_limited,
  // degraded, and auth_failed profiles surface as critical even when the
  // quota layer is green. Forward those as identities so the operator
  // sees the same "needs attention" cue the top alert renders.
  overview.forEach(function (bucket) {
    if (!bucket || typeof bucket !== "object") return;
    const availability = bucket.provider_availability;
    const status = availability && typeof availability === "object" ? availability.status : null;
    if (status === "rate_limited" || status === "degraded" || status === "auth_failed") {
      const label = String(bucket.label || bucket.id || "");
      if (label) pushFromAlert("critical", { provider: label });
    }
  });
  // Canonical sort so two equivalent alerts hash the same way regardless
  // of summary payload order.
  out.sort(function (a, b) {
    if (a.level !== b.level) return a.level === "critical" ? -1 : 1;
    return a.provider.localeCompare(b.provider, undefined, { sensitivity: "base" });
  });
  return out;
}

// Decide whether the dashboard should fire a notification for the
// current summary, given the previous identity list and the operator's
// opt-in settings. Pure: same inputs always produce the same output, so
// the Node test fixtures can drive every branch.
//
//   * ``notifications.enabled`` is the master switch — false → [].
//   * ``notifications.levels`` filters which severity counts. Levels
//     not on the allowlist are silently dropped.
//   * Identity comparison uses the sorted (level, provider) tuple. A
//     new identity, or a strictly higher level for an existing one,
//     triggers a fire. Equal sets or downgrades (critical → low) never
//     fire again on the same identity, so the same outage does not
//     spam the operator.
//   * ``now`` (epoch ms) and ``cooldownMap`` (identityKey -> lastFiredAt)
//     implement the per-alert cooldown: when the same identity fires
//     twice within ``cooldown_minutes`` minutes, the second one is
//     suppressed. The cooldown is updated for every fired identity so a
//     refreshing alert keeps respecting the window until it clears.
function notificationDecisions(
  summary,
  previousIdentity,
  notifications,
  previousState
) {
  const defaults = NOTIFICATION_DEFAULTS;
  const settings = (notifications && typeof notifications === "object") ? notifications : defaults;
  const enabled = settings.enabled === true;
  const allowed = Array.isArray(settings.levels) && settings.levels.length > 0
    ? settings.levels.filter(function (level) {
        return NOTIFICATION_LEVELS.indexOf(level) !== -1;
      })
    : defaults.levels.slice();
  const cooldownMinutes = (typeof settings.cooldown_minutes === "number" && settings.cooldown_minutes >= 0)
    ? settings.cooldown_minutes
    : defaults.cooldown_minutes;
  const cooldownMs = cooldownMinutes * 60 * 1000;
  const identity = notificationAlertIdentity(summary);
  const prev = Array.isArray(previousIdentity) ? previousIdentity : [];
  const state = (previousState && typeof previousState === "object") ? previousState : {};
  const now = (typeof state.now === "number") ? state.now : Date.now();
  const cooldownMap = (state.cooldownMap && typeof state.cooldownMap === "object") ? state.cooldownMap : {};

  function identityKey(item) { return item.level + "|" + item.provider; }
  // Dedup uses (level, provider) pairs; downgrade detection uses the
  // provider alone because the level slot itself flips. Building a
  // provider map lets us answer "what level did this provider fire at
  // last time?" without scanning the identity twice.
  const prevByProvider = {};
  prev.forEach(function (item) {
    if (!item || typeof item.provider !== "string") return;
    prevByProvider[item.provider] = item;
  });

  const toFire = [];
  const nextCooldown = Object.assign({}, cooldownMap);
  if (!enabled) {
    // Master switch off — wipe the cooldown map so enabling again
    // immediately surfaces the current state.
    return {
      identity: identity,
      fires: [],
      cooldownMap: {},
      allowed: allowed,
    };
  }
  identity.forEach(function (item) {
    if (allowed.indexOf(item.level) === -1) return;
    const key = identityKey(item);
    const previous = prevByProvider[item.provider];
    const isNew = !previous;
    // Escalation: critical replaces an existing low at the same provider.
    // A downgrade (critical → low) is intentionally silent — the
    // operator already saw the red alert when the outage started; the
    // calmer "low" tone would be noise. Also silence a "low → critical"
    // repeat at the same provider when the same critical alert was
    // observed on the previous poll: the operator already saw it.
    const escalated = previous && previous.level !== "critical" && item.level === "critical";
    if (!isNew && !escalated) {
      // A downgrade at the same provider is never re-fired: the operator
      // already saw the more severe alert and a calmer tone would only
      // add noise.
      if (previous && previous.level === "critical" && item.level === "low") {
        // Keep the cooldown map untouched so the original critical fire
        // still gates any future re-fire at this provider.
        return;
      }
      // Same level, same provider — only re-fire if the cooldown
      // already elapsed (i.e. the previous fire was long enough ago).
      const lastFired = nextCooldown[key];
      if (typeof lastFired !== "number" || cooldownMs <= 0 || (now - lastFired) >= cooldownMs) {
        toFire.push(item);
        nextCooldown[key] = now;
      }
      return;
    }
    // Fresh identity or an escalation: respect cooldown the same way so
    // a brand-new critical alert that lands inside a cooldown window
    // (e.g. the operator just opened the page during an outage) still
    // does not re-fire if the same identity fired moments ago.
    const lastFired = nextCooldown[key];
    if (typeof lastFired === "number" && cooldownMs > 0 && (now - lastFired) < cooldownMs) {
      return;
    }
    toFire.push(item);
    nextCooldown[key] = now;
  });
  // Stale cooldown entries: identities that are no longer present in the
  // current snapshot can fire again the next time they appear. Drop
  // those keys so a returning alert after a clear is not silently
  // swallowed by a leftover cooldown entry. The reference set is the
  // CURRENT identity, not the previous one — otherwise the first poll
  // would always wipe the map before any cooldown can take effect.
  const currentKeys = {};
  identity.forEach(function (item) { currentKeys[identityKey(item)] = true; });
  Object.keys(nextCooldown).forEach(function (key) {
    if (!currentKeys[key]) delete nextCooldown[key];
  });
  return {
    identity: identity,
    fires: toFire,
    cooldownMap: nextCooldown,
    allowed: allowed,
  };
}

// Build the title/body pair the Notification API consumes. Pure, so
// Node tests assert the copy without a fake DOM.
function notificationBody(item) {
  if (!item || typeof item.provider !== "string" || !item.provider) return null;
  if (item.level === "critical") {
    return {
      title: "Provider out of quota",
      body: item.provider + " needs attention. Open Quota Console for details.",
    };
  }
  if (item.level === "low") {
    return {
      title: "Provider running low",
      body: item.provider + " is running low. Open Quota Console for details.",
    };
  }
  return null;
}

(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const Button = SDK.components.Button;
  const { useCallback, useEffect, useRef, useState } = SDK.hooks;
  const API = "/api/plugins/quota-console/summary";
  const RESET_API = "/api/plugins/quota-console/reset";
  const SETTINGS_API = "/api/plugins/quota-console/settings";

  const PROFILE_STATUS_LABELS = {
    ready: "Ready",
    rate_limited: "Rate limited",
    auth_failed: "Auth failed",
    degraded: "Degraded",
    unconfigured: "Not configured",
    untracked: "Not tracked",
  };

  // Canonical field order for the operator settings dialog. Mirrors the
  // server-side contract (dashboard/settings.py). ``note`` is deliberately
  // per-provider only and excluded from the global defaults section.
  const SETTINGS_FIELDS = [
    "window_low_percent",
    "balance_low_amount",
    "balance_exhausted_at_zero",
    "note",
  ];

  // Reset timestamps cross the API boundary as ISO values; render them
  // through the browser's own locale and timezone so every operator sees
  // dates in their preferred form without losing the unambiguous ISO
  // source of truth.
  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat(undefined, {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  // Reset notes on the yellow top alert only need the time portion. Keep
  // the same locale rules as formatDate: no explicit locale override so
  // the browser picks whatever the operator set for the dashboard. The
  // format is "HH:MM in the user-preferred format"
  // — not a hardcoded 24h clock.
  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  // Translate the alert-layer level names into the CSS class suffixes
  // used by the window/balance rows. Unknown level keeps the default
  // neutral styling; "ok" never renders.
  function levelClass(level) {
    const normalized = String(level || "").trim().toLowerCase();
    if (normalized === "low" || normalized === "exhausted" || normalized === "unknown") {
      return "usages-level--" + normalized;
    }
    return "";
  }

  // Compute the className suffix a balance row should render with given
  // its source role and computed alert level. Pure helper so the test
  // suite can assert the fallback-vs-primary decision without spinning up
  // a fake React SDK. The browser's BalanceRow uses the same rule:
  // fallback rows drop the level modifier entirely (they stay neutral),
  // primary rows keep it.
  function balanceRowClass(role, level) {
    if (role === "fallback") return levelClass(""); // neutral — role trumps level
    return levelClass(level);
  }
  function formatCount(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "";
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
  }

  function Status(props) {
    const live = props.status === "ok";
    return h(
      "span",
      {
        className: "usages-status usages-status--" + (live ? "ok" : "unavailable"),
        "data-status": live ? "ok" : "unavailable",
      },
      h("span", { className: "usages-status-dot", "aria-hidden": "true" }),
      live ? "Live" : "Unavailable",
    );
  }

  function ModelStatus(props) {
    const status = props.status || "unconfigured";
    const label = props.label || PROFILE_STATUS_LABELS[status] || "Unknown";
    return h(
      "span",
      {
        className: "usages-model-status usages-model-status--" + status.replace(/_/g, "-"),
        "data-status": status.replace(/_/g, "-"),
      },
      h("span", { className: "usages-status-dot", "aria-hidden": "true" }),
      label,
    );
  }

  function Progress(props) {
    const remaining = Math.max(0, Math.min(100, Number(props.remaining) || 0));
    return h(
      "div",
      {
        className: "usages-progress",
        role: "progressbar",
        "aria-valuemin": 0,
        "aria-valuemax": 100,
        "aria-valuenow": remaining,
        "aria-label": remaining + "% remaining",
      },
      h("span", { className: "usages-progress-fill", style: { width: remaining + "%" } }),
    );
  }

  function WindowRow(props) {
    const item = props.item;
    if (item.state === "not_included") {
      return h(
        "div",
        { className: "usages-window usages-window--muted" },
        h("span", { className: "usages-window-label" }, item.label),
        h("span", { className: "usages-window-value" }, "Not included"),
      );
    }
    if (item.unlimited) {
      return h(
        "div",
        { className: "usages-window" },
        h("span", { className: "usages-window-label" }, item.label),
        h("span", { className: "usages-window-value" }, "Unlimited"),
      );
    }

    const remaining = typeof item.remaining_percent === "number" ? item.remaining_percent : null;
    const unit = typeof item.unit === "string" && item.unit.trim() ? item.unit.trim() : "";
    const count = item.remaining != null && item.entitlement != null
      ? formatCount(item.remaining) + " / " + formatCount(item.entitlement) + (unit ? " " + unit : "")
      : null;
    const value = remaining == null
      ? "Unavailable"
      : remaining + "% remaining" + (count ? " · " + count : "");
    const reset = formatDate(item.reset_at);
    // Alert layer: tint the row when the window is low or exhausted.
    // The role/level annotation arrived from the backend (see providers/base.py
    // annotate_items); the row class only carries the visual signal — the
    // alert itself is computed server-side and rendered by the top alerts.
    const levelModifier = levelClass(item.level);
    const className = "usages-window" + (levelModifier ? " " + levelModifier : "");
    const attributes = { className: className };
    if (item.role) attributes["data-role"] = item.role;

    return h(
      "div",
      attributes,
      h(
        "div",
        { className: "usages-window-top" },
        h("span", { className: "usages-window-label" }, item.label),
        h("span", { className: "usages-window-value" }, value),
      ),
      remaining == null ? null : h(Progress, { remaining: remaining }),
      reset ? h("span", { className: "usages-reset" }, "Resets " + reset) : null,
    );
  }

  function BalanceRow(props) {
    // Label always "Balance" - no USD/Credits/Credits balance variation
    const label = "Balance";
    const amount = props.balance.unlimited
      ? "Unlimited"
      : typeof props.balance.amount === "number"
        ? props.balance.unitless
          ? formatCount(props.balance.amount)
          : props.balance.currency
            ? new Intl.NumberFormat(undefined, {
              style: "currency",
              currency: props.balance.currency,
              maximumFractionDigits: 2,
            }).format(props.balance.amount)
            : formatCount(props.balance.amount)
        : "Unavailable";
    // Alert layer: tint the row when the balance is primary and its level
    // is low/exhausted/unknown. A fallback balance (the wallet behind a
    // still-healthy plan) never raises an alert per AGENTS rule 5, so its
    // computed ``level`` is informational only and must not surface as a
    // red destructive tint while the primary source is healthy. The bucket
    // level and the top alerts remain driven by primary sources only.
    const levelModifier = balanceRowClass(props.balance.role, props.balance.level);
    const className = "usages-balance" + (levelModifier ? " " + levelModifier : "");
    const attributes = { className: className };
    if (props.balance.role) attributes["data-role"] = props.balance.role;
    return h(
      "div",
      attributes,
      h("span", { className: "usages-window-label" }, label),
      h("span", { className: "usages-balance-value" }, amount),
    );
  }

  function ProviderSummary(props) {
    const item = props.item;
    if (!item) {
      return null;
    }

    const windows = Array.isArray(item.windows) ? item.windows : [];
    const balances = Array.isArray(item.balances) ? item.balances : [];
    const body = [];

    if (windows.length) {
      body.push(h("div", { className: "usages-section", key: "windows" }, windows.map(function (windowItem, index) {
        return h(WindowRow, { item: windowItem, key: item.id + "-window-" + index });
      })));
    }
    if (balances.length) {
      body.push(h("div", { className: "usages-section usages-section--balances", key: "balances" }, balances.map(function (balance, index) {
        return h(BalanceRow, { balance: balance, key: item.id + "-balance-" + index });
      })));
    }
    if (item.notice) {
      body.push(h("p", { className: "usages-notice", key: "notice" }, item.notice));
    }
    if (!body.length) {
      body.push(h("p", { className: "usages-notice", key: "empty" }, "No current quota data."));
    }

    return h(
      "div",
      { className: "usages-provider-bucket-usage" },
      h(
        "div",
        { className: "usages-provider-bucket-meta" },
        item.plan ? h("span", { className: "usages-plan" }, item.plan) : null,
      ),
      body,
    );
  }

  // Row-level reset controls only render while the profile actually has
  // cached rate-limit state (see canResetProfileStatus), so this title
  // always describes an actionable reset.
  function resetProfileTitle(item) {
    const profile = item && item.profile ? item.profile : "profile";
    return "Reset cached rate-limit state for " + profile + ".";
  }

  // Render the threshold/note value with the right hint copy. A null value
  // is the contract signal "do not raise an alert" — the dialog must
  // surface it as an explicit off switch, never as a hidden 0. When the
  // row belongs to a per-provider override layer and the field is unset
  // there, the global default (if any) actually applies — the copy says so
  // instead of pretending the rule is off. The note field is not an alert:
  // when it is empty the row reads as unset without implying any alert
  // behaviour.
  function describeSettingValue(field, value, globalValue) {
    const isSet = value !== null && value !== undefined && value !== "";
    const hasGlobal = globalValue !== null && globalValue !== undefined && globalValue !== "";
    if (isSet) {
      if (field === "window_low_percent") return "Low at " + value + "% remaining";
      if (field === "balance_low_amount") return "Low below " + formatCount(value);
      if (field === "balance_exhausted_at_zero") {
        return value ? "Treat zero balance as exhausted" : "Zero balance raises no alert";
      }
      if (field === "note") return "\u201C" + value + "\u201D";
      return String(value);
    }
    if (field === "note") return "Not set";
    if (hasGlobal) {
      return "Uses global default (" + describeSettingValue(field, globalValue) + ")";
    }
    return "Off (no alerts)";
  }

  function SettingsFieldRow(props) {
    // One row in the settings dialog. Booleans render a single switch;
    // numeric/text fields render a direct input where an empty value means
    // "unset" (off). There is deliberately no separate On/Off toggle next
    // to a value control: an empty input IS the off state.
    const field = props.field;
    const value = props.value;
    const label = props.label;
    const hint = props.hint;
    const isSet = value !== null && value !== undefined && value !== "";
    const inputId = "usages-settings-" + (props.scope || "defaults") + "-" + field;
    const isBoolean = field === "balance_exhausted_at_zero";
    const isProviderScope = Boolean(props.scope) && props.scope.indexOf("provider-") === 0;
    return h(
      "div",
      { className: "usages-settings-field" },
      h(
        "div",
        { className: "usages-settings-field-label" },
        h("label", { htmlFor: inputId }, label),
        hint ? h("p", { className: "usages-settings-field-hint" }, hint) : null,
      ),
      h(
        "div",
        { className: "usages-settings-field-input" },
        isBoolean
          ? h(
              "label",
              { htmlFor: inputId, className: "usages-settings-toggle" },
              // The checkbox shows the override state. Without an override
              // the rule follows the global default, so the label reads
              // "Default" here instead of pretending the rule is off; the
              // field-current line below names the value that applies.
              h("input", {
                id: inputId,
                type: "checkbox",
                checked: isSet ? Boolean(value) : false,
                disabled: props.disabled,
                onChange: function (event) {
                  props.onChange(event.target.checked ? true : null); // off = unset
                },
              }),
              isSet ? (Boolean(value) ? "On" : "Off") : isProviderScope ? "Default" : "Off",
            )
          : field === "note"
          ? h("input", {
              id: inputId,
              type: "text",
              maxLength: props.noteMaxLength || 120,
              value: isSet ? value : "",
              disabled: props.disabled,
              placeholder: "Optional note shown under this provider",
              onChange: function (event) {
                const next = String(event.target.value || "");
                if (next.length > (props.noteMaxLength || 120)) return;
                props.onChange(next === "" ? null : next);
              },
            })
          : h("input", {
              id: inputId,
              type: "number",
              min: field === "window_low_percent" ? 1 : 0,
              max: field === "window_low_percent" ? 100 : undefined,
              step: field === "window_low_percent" ? 1 : "any",
              value: isSet ? value : "",
              disabled: props.disabled,
              placeholder: field === "window_low_percent" ? "e.g. 20" : "e.g. 5",
              onChange: function (event) {
                const raw = event.target.value;
                if (raw === "") return props.onChange(null);
                const parsed = field === "window_low_percent" ? parseInt(raw, 10) : parseFloat(raw);
                if (!Number.isFinite(parsed)) return;
                props.onChange(field === "window_low_percent" ? Math.max(1, Math.min(100, parsed)) : Math.max(0, parsed));
              },
            }),
      ),
      h(
        "div",
        { className: "usages-settings-field-current" },
        describeSettingValue(field, value, props.globalValue),
      ),
    );
  }

  function NotificationsSection(props) {
    // Operator opt-in for browser notifications. Pure-decision: every
    // control drives the same ``notificationDraft`` state the Settings
    // dialog PUTs as a single ``notifications`` block. Permission is
    // requested on the explicit "Enable" click so the browser never
    // asks for it on page load.
    const initial = props.initial || {};
    const schema = props.schema || {};
    const levels = Array.isArray(schema.notification_levels) && schema.notification_levels.length > 0
      ? schema.notification_levels
      : NOTIFICATION_LEVELS;
    const cooldownMin = typeof schema.notification_cooldown_min === "number"
      ? schema.notification_cooldown_min : 0;
    const cooldownMax = typeof schema.notification_cooldown_max === "number"
      ? schema.notification_cooldown_max : 24 * 60;
    const [draft, setDraft] = useState(function () {
      return {
        enabled: typeof initial.enabled === "boolean" ? initial.enabled : NOTIFICATION_DEFAULTS.enabled,
        levels: Array.isArray(initial.levels) && initial.levels.length > 0
          ? initial.levels.slice()
          : NOTIFICATION_DEFAULTS.levels.slice(),
        cooldown_minutes: typeof initial.cooldown_minutes === "number" && initial.cooldown_minutes >= 0
          ? initial.cooldown_minutes : NOTIFICATION_DEFAULTS.cooldown_minutes,
      };
    });
    const [permission, setPermission] = useState(function () {
      try {
        return typeof window.Notification === "function" ? window.Notification.permission : "unsupported";
      } catch (error) { return "unsupported"; }
    });
    const [permissionError, setPermissionError] = useState(null);

    function updateEnabled(next) {
      setDraft(function (prev) { return Object.assign({}, prev, { enabled: next }); });
    }
    function toggleLevel(level) {
      setDraft(function (prev) {
        const current = Array.isArray(prev.levels) ? prev.levels : [];
        const has = current.indexOf(level) !== -1;
        let nextLevels;
        if (has) {
          nextLevels = current.filter(function (item) { return item !== level; });
          // Keep at least one level so the operator cannot accidentally
          // silence notifications without flipping the master switch.
          if (!nextLevels.length) return prev;
        } else {
          nextLevels = current.concat([level]);
        }
        // Preserve canonical order so the PUT body is stable.
        nextLevels.sort(function (a, b) {
          return NOTIFICATION_LEVELS.indexOf(a) - NOTIFICATION_LEVELS.indexOf(b);
        });
        return Object.assign({}, prev, { levels: nextLevels });
      });
    }
    function updateCooldown(value) {
      setDraft(function (prev) {
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) return prev;
        const clamped = Math.max(cooldownMin, Math.min(cooldownMax, Math.round(numeric)));
        return Object.assign({}, prev, { cooldown_minutes: clamped });
      });
    }
    function refreshPermission() {
      try {
        if (typeof window.Notification === "function") {
          setPermission(window.Notification.permission);
        }
      } catch (error) { /* sandboxed: leave the previous value */ }
    }
    function requestPermission() {
      // The browser requires a user gesture to surface the permission
      // prompt; this is the only place we ever call requestPermission.
      if (typeof window.Notification !== "function") {
        setPermissionError("Browser notifications are not supported in this browser.");
        return;
      }
      try {
        const outcome = window.Notification.requestPermission(function (result) {
          setPermission(result);
        });
        if (outcome && typeof outcome.then === "function") {
          outcome.then(setPermission).catch(function () {
            setPermissionError("Could not request browser notification permission.");
          });
        }
      } catch (error) {
        setPermissionError("Could not request browser notification permission.");
      }
    }
    function sendTestNotification() {
      // Operator-driven sanity check. Reuses the notificationBody helper
      // so the copy matches what real alerts render with. The notification
      // is fired regardless of cooldown so the operator can verify the
      // permission grant on demand.
      if (typeof window.Notification !== "function") {
        setPermissionError("Browser notifications are not supported in this browser.");
        return;
      }
      if (window.Notification.permission !== "granted") {
        setPermissionError("Enable browser notifications first to send a test.");
        return;
      }
      try {
        const test = new window.Notification("Quota Console notifications enabled", {
          body: "You will see alerts here when the levels you selected fire.",
          tag: "quota-console-test",
        });
        if (test && typeof test.addEventListener === "function") {
          test.addEventListener("click", function () {
            try {
              if (typeof window.focus === "function") window.focus();
              window.location.assign("/quota-console");
            } catch (error) { /* navigation is best-effort */ }
          });
        }
      } catch (error) {
        setPermissionError("Could not send a test notification.");
      }
    }

    // Push draft up so the dialog body picks it up in the next PUT.
    // The save() handler below serialises ``props.draft`` — we copy ours
    // back into this.draft through props.onChange.
    useEffect(function () {
      if (typeof props.onChange === "function") {
        props.onChange(draft);
      }
    }, [draft.enabled, draft.levels.join(","), draft.cooldown_minutes]);

    const permissionLabel = permission === "granted"
      ? "Permission granted"
      : permission === "denied"
        ? "Permission denied"
        : permission === "unsupported"
          ? "Not supported in this browser"
          : "Permission not requested";
    const permissionClass = "usages-notifications-permission usages-notifications-permission--"
      + permission.replace(/[^a-z0-9_-]/g, "unknown");

    return h(
      "section",
      { className: "usages-settings-section usages-settings-notifications" },
      h(
        "header",
        { className: "usages-settings-notifications-header" },
        h("h3", { className: "usages-settings-section-title" }, "Notifications"),
        h("span", { className: permissionClass }, permissionLabel),
      ),
      h(
        "p",
        { className: "usages-settings-field-hint" },
        "Browser notifications are off until you turn them on. ",
        "They only fire while this dashboard tab is open or in the background — ",
        "closed tabs are out of scope for this version.",
      ),
      h(
        "label",
        { className: "usages-settings-notifications-toggle" },
        h("input", {
          type: "checkbox",
          checked: Boolean(draft.enabled),
          onChange: function (event) { updateEnabled(Boolean(event.target.checked)); },
          "aria-describedby": "usages-notifications-description",
        }),
        h("span", null, "Enable browser notifications"),
      ),
      h(
        "fieldset",
        {
          className: "usages-settings-notifications-levels",
          disabled: !draft.enabled,
          "aria-label": "Alert levels that fire a notification",
        },
        h("legend", { className: "usages-settings-field-hint" }, "Fire a notification for:"),
        levels.map(function (level) {
          const checked = draft.levels.indexOf(level) !== -1;
          return h(
            "label",
            { key: level, className: "usages-settings-notifications-level" },
            h("input", {
              type: "checkbox",
              checked: checked,
              onChange: function () { toggleLevel(level); },
            }),
            h(
              "span",
              null,
              level === "critical" ? "Critical (out of quota, rate-limited, auth failed)" : "Low (running low)",
            ),
          );
        }),
      ),
      h(
        "div",
        { className: "usages-settings-notifications-cooldown" },
        h(
          "label",
          { htmlFor: "usages-notifications-cooldown-input" },
          "Cooldown between repeats (minutes)",
        ),
        h("input", {
          id: "usages-notifications-cooldown-input",
          type: "number",
          min: cooldownMin,
          max: cooldownMax,
          step: 1,
          value: draft.cooldown_minutes,
          onChange: function (event) { updateCooldown(event.target.value); },
          disabled: !draft.enabled,
          "aria-describedby": "usages-notifications-description",
        }),
      ),
      h(
        "div",
        { className: "usages-settings-notifications-actions" },
        permission === "granted"
          ? h(
              Button,
              {
                type: "button",
                size: "sm",
                onClick: sendTestNotification,
              },
              "Send test notification",
            )
          : permission === "denied" || permission === "unsupported"
            ? h(
                Button,
                { type: "button", size: "sm", disabled: true },
                "Browser blocks notifications",
              )
            : h(
                Button,
                {
                  type: "button",
                  size: "sm",
                  onClick: requestPermission,
                },
                "Enable browser notifications",
              ),
        h(
          Button,
          { type: "button", size: "sm", variant: "ghost", onClick: refreshPermission },
          "Refresh status",
        ),
      ),
      permissionError
        ? h("p", { className: "usages-settings-error", role: "alert" }, permissionError)
        : null,
      h(
        "p",
        { id: "usages-notifications-description", className: "usages-settings-notifications-note" },
        "Notifications follow the existing alert set: a new alert fires once, repeats are deduped per provider and level, and the cooldown suppresses back-to-back duplicates while the page stays open.",
      ),
    );
  }

  function SettingsDialog(props) {
    // Operator-editable settings dialog. Renders the global defaults layer
    // first and then one expandable card per provider so operators can set
    // per-provider overrides without losing the global baseline. The
    // dialog keeps a local draft of the layers and only PUTs on Save.
    const initial = props.initial || { defaults: {}, providers: {} };
    const schema = props.schema || { note_max_length: 120 };
    const providers = Array.isArray(props.providers) ? props.providers : [];
    const initialNotifications = props.notifications || NOTIFICATION_DEFAULTS;
    const [draftDefaults, setDraftDefaults] = useState(function () {
      return Object.assign({}, initial.defaults || {});
    });
    const [draftProviders, setDraftProviders] = useState(function () {
      return Object.assign({}, initial.providers || {});
    });
    const [draftNotifications, setDraftNotifications] = useState(function () {
      return {
        enabled: typeof initialNotifications.enabled === "boolean"
          ? initialNotifications.enabled : NOTIFICATION_DEFAULTS.enabled,
        levels: Array.isArray(initialNotifications.levels) && initialNotifications.levels.length > 0
          ? initialNotifications.levels.slice() : NOTIFICATION_DEFAULTS.levels.slice(),
        cooldown_minutes: typeof initialNotifications.cooldown_minutes === "number"
          ? initialNotifications.cooldown_minutes : NOTIFICATION_DEFAULTS.cooldown_minutes,
      };
    });
    // A provider section opens on mount when it already carries overrides
    // in the loaded settings. The value is captured once and never driven
    // by the draft afterwards: React must not rewrite the <details> open
    // attribute on later renders, or clearing the last override field
    // would collapse the section the operator is working in. From the
    // first render on, the native disclosure toggle owns the state.
    const [initiallyExpanded] = useState(function () {
      const map = {};
      providers.forEach(function (p) {
        const layer = (initial.providers || {})[p.id];
        map[p.id] = Boolean(layer && Object.keys(layer).length > 0);
      });
      return map;
    });
    const [error, setError] = useState(null);
    const [saving, setSaving] = useState(false);

    function updateDefault(field, value) {
      setDraftDefaults(function (prev) {
        const next = Object.assign({}, prev);
        if (value === null) {
          delete next[field];
        } else {
          next[field] = value;
        }
        return next;
      });
    }

    function updateProvider(providerId, field, value) {
      setDraftProviders(function (prev) {
        const next = Object.assign({}, prev);
        const layer = Object.assign({}, next[providerId] || {});
        if (value === null) {
          delete layer[field];
        } else {
          layer[field] = value;
        }
        if (Object.keys(layer).length === 0) {
          delete next[providerId];
        } else {
          next[providerId] = layer;
        }
        return next;
      });
    }

    function resetDraft() {
      setDraftDefaults(Object.assign({}, initial.defaults || {}));
      setDraftProviders(Object.assign({}, initial.providers || {}));
      setDraftNotifications({
        enabled: typeof initialNotifications.enabled === "boolean"
          ? initialNotifications.enabled : NOTIFICATION_DEFAULTS.enabled,
        levels: Array.isArray(initialNotifications.levels) && initialNotifications.levels.length > 0
          ? initialNotifications.levels.slice() : NOTIFICATION_DEFAULTS.levels.slice(),
        cooldown_minutes: typeof initialNotifications.cooldown_minutes === "number"
          ? initialNotifications.cooldown_minutes : NOTIFICATION_DEFAULTS.cooldown_minutes,
      });
      setError(null);
    }

    function save() {
      setSaving(true);
      setError(null);
      SDK.fetchJSON(SETTINGS_API, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          defaults: draftDefaults,
          providers: draftProviders,
          notifications: draftNotifications,
        }),
      })
        .then(function (result) {
          setSaving(false);
          if (props.onSaved) props.onSaved(result);
        })
        .catch(function (err) {
          setSaving(false);
          const detail = err && err.body && err.body.detail ? err.body.detail : "Could not save settings.";
          setError(String(detail));
        });
    }

    return h(
      "div",
      {
        className: "usages-settings-overlay",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": "Provider settings",
        onClick: function (event) {
          if (event.target === event.currentTarget && !saving) props.onClose();
        },
      },
      h(
        "div",
        { className: "usages-settings-dialog" },
        h(
          "div",
          { className: "usages-settings-header" },
          h("h2", { className: "usages-settings-title" }, "Provider settings"),
          h(
            "p",
            { className: "usages-settings-description" },
            "Global defaults apply to every provider. Override a field per provider to diverge from them. Thresholds are off unless explicitly set \u2014 no alerts are raised until you turn one on.",
          ),
        ),
        h(
          "section",
          { className: "usages-settings-section" },
          h("h3", { className: "usages-settings-section-title" }, "Global defaults"),
          SETTINGS_FIELDS.filter(function (field) { return field !== "note"; }).map(function (field) {
            return h(SettingsFieldRow, {
              key: "default-" + field,
              scope: "defaults",
              field: field,
              value: Object.prototype.hasOwnProperty.call(draftDefaults, field) ? draftDefaults[field] : null,
              label: field === "window_low_percent"
                ? "Low remaining-percent threshold"
                : field === "balance_low_amount"
                ? "Low balance amount"
                : field === "balance_exhausted_at_zero"
                ? "Treat zero balance as exhausted"
                : "Provider note",
              hint: field === "window_low_percent"
                ? "Alert when a usage window falls below this percentage."
                : field === "balance_low_amount"
                ? "Alert when a balance drops below this number (in the API-reported unit)."
                : field === "balance_exhausted_at_zero"
                ? "Off by default. Turn on to raise an exhausted alert when a balance is exactly 0."
                : "Optional note shown under this provider (shown in the provider row).",
              disabled: saving,
              noteMaxLength: schema.note_max_length,
              onChange: function (value) { updateDefault(field, value); },
            });
          }),
        ),
        h(
          "section",
          { className: "usages-settings-section" },
          h("h3", { className: "usages-settings-section-title" }, "Per-provider overrides"),
          providers.length === 0
            ? h("p", { className: "usages-settings-empty" }, "No providers are loaded yet.")
            : providers.map(function (provider) {
                const layer = draftProviders[provider.id] || {};
                return h(
                  "details",
                  {
                    className: "usages-settings-provider" + (provider.autoHidden ? " usages-settings-provider--dimmed" : ""),
                    key: provider.id,
                    open: Boolean(initiallyExpanded[provider.id]),
                  },
                  h(
                    "summary",
                    { className: "usages-settings-provider-summary" },
                    h("span", { className: "usages-settings-provider-name" }, provider.label || provider.id),
                    provider.autoHidden && Object.keys(layer).length === 0
                      ? h("span", { className: "usages-settings-provider-tag usages-settings-provider-tag--empty" }, "No quota data")
                      : Object.keys(layer).length > 0
                        ? h("span", { className: "usages-settings-provider-tag" }, Object.keys(layer).length + " override" + (Object.keys(layer).length === 1 ? "" : "s"))
                        : h("span", { className: "usages-settings-provider-tag usages-settings-provider-tag--empty" }, "No overrides"),
                  ),
                  SETTINGS_FIELDS.map(function (field) {
                    return h(SettingsFieldRow, {
                      key: provider.id + "-" + field,
                      scope: "provider-" + provider.id,
                      field: field,
                      value: Object.prototype.hasOwnProperty.call(layer, field) ? layer[field] : null,
                      globalValue: Object.prototype.hasOwnProperty.call(draftDefaults, field) ? draftDefaults[field] : null,
                      label: field === "window_low_percent"
                        ? "Low remaining-percent threshold"
                        : field === "balance_low_amount"
                        ? "Low balance amount"
                        : field === "balance_exhausted_at_zero"
                        ? "Treat zero balance as exhausted"
                        : "Note (shown under provider row)",
                      hint: field === "note"
                        ? "Single line, " + schema.note_max_length + " characters max. Shown under the provider row."
                        : field === "window_low_percent"
                        ? "Empty value falls back to the global default."
                        : field === "balance_low_amount"
                        ? "Empty value falls back to the global default."
                        : "Off falls back to the global default.",
                      disabled: saving,
                      noteMaxLength: schema.note_max_length,
                      onChange: function (value) { updateProvider(provider.id, field, value); },
                    });
                  }),
                );
              }),
        ),
        error ? h("p", { className: "usages-settings-error", role: "alert" }, error) : null,
        h(
          NotificationsSection,
          {
            initial: draftNotifications,
            schema: schema,
            onChange: setDraftNotifications,
          },
        ),
        h(
          "div",
          { className: "usages-settings-footer" },
          h(
            Button,
            { type: "button", size: "sm", onClick: function () { resetDraft(); }, disabled: saving },
            "Reset",
          ),
          h(
            Button,
            { type: "button", size: "sm", onClick: function () { props.onClose(); }, disabled: saving },
            "Close",
          ),
          h(
            Button,
            {
              type: "button",
              size: "sm",
              onClick: save,
              disabled: saving,
              "aria-label": saving ? "Saving settings" : "Save settings",
            },
            saving ? "Saving\u2026" : "Save",
          ),
        ),
      ),
    );
  }

  function maybeFireNotifications(prev, summary, notifications) {
    // Browser-side firing logic. Lives at module scope (well, here inside
    // the SDK-aware IIFE) so we can reach ``window.Notification`` while
    // keeping the decision functions themselves pure. The pure pieces
    // (``notificationAlertIdentity``, ``notificationDecisions``,
    // ``notificationBody``) are exported for Node tests; this wrapper
    // binds them to the browser API and to a useRef-tracked cooldown map.
    if (!summary || typeof summary !== "object") return { identity: [], cooldownMap: prev.cooldownMap || {} };
    if (!notifications || typeof notifications !== "object") return { identity: [], cooldownMap: prev.cooldownMap || {} };
    const decision = notificationDecisions(
      summary,
      prev.identity || [],
      notifications,
      { cooldownMap: prev.cooldownMap || {}, now: Date.now() },
    );
    if (Array.isArray(decision.fires) && decision.fires.length > 0
        && typeof window.Notification === "function"
        && window.Notification.permission === "granted") {
      decision.fires.forEach(function (item) {
        const built = notificationBody(item);
        if (!built) return;
        try {
          const note = new window.Notification(built.title, {
            body: built.body,
            tag: "quota-console-" + item.level + "-" + item.provider,
          });
          if (note && typeof note.addEventListener === "function") {
            note.addEventListener("click", function () {
              try {
                if (typeof window.focus === "function") window.focus();
                window.location.assign("/quota-console");
              } catch (error) { /* navigation is best-effort */ }
            });
          }
        } catch (error) {
          // Some browsers throw on duplicate tags inside the cooldown
          // window; swallow the failure and keep the cooldown map intact
          // so the operator does not see duplicate toasts back-to-back.
        }
      });
    }
    return { identity: decision.identity, cooldownMap: decision.cooldownMap };
  }

  function UsagePage() {
    const [state, setState] = useState({ loading: true, data: null, error: false });
    const [resetting, setResetting] = useState(null);
    const [actionMessage, setActionMessage] = useState(null);
    const [showSettings, setShowSettings] = useState(false);
    // Browser-notification opt-in state. Mirrored from the server-side
    // settings block on first summary, then kept in lockstep so the
    // firing logic uses the operator's most recent choice even before
    // the next PUT round-trip lands.
    const [notifications, setNotifications] = useState(function () {
      return {
        identity: [],
        cooldownMap: {},
        settings: Object.assign({}, NOTIFICATION_DEFAULTS),
      };
    });
    // Ref mirror so the 60s poll always reads the latest opt-in without
    // rebuilding the timer every time the operator flips a checkbox in
    // the settings dialog. Stale-state capture inside useCallback is the
    // classic React foot-gun; the ref keeps the load() closure fresh.
    const notificationsRef = useRef(notifications);
    notificationsRef.current = notifications;
    const [hiddenProviders, setHiddenProviders] = useState(function () {
      try {
        const raw = window.localStorage.getItem("quota-console-hidden-providers");
        if (raw) return JSON.parse(raw);
      } catch (error) { /* private mode or unavailable storage: start empty */ }
      return {};
    });
    const [providerOrder, setProviderOrder] = useState(function () {
      try {
        const raw = window.localStorage.getItem("quota-console-provider-order");
        if (raw) {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) return parsed;
        }
      } catch (error) { /* private mode or unavailable storage: start empty */ }
      return [];
    });
    // Customize mode is the operator's "edit layout" state: while it is on,
    // every provider card shows its hide/show button and drag handle so the
    // operator can decide visibility and order; turning it off returns to
    // the clean read-only view. The toolbar button toggles this mode.
    const [customizeMode, setCustomizeMode] = useState(false);
    const [dragId, setDragId] = useState(null);
    // Drop indicator: the card id currently hovered while dragging plus
    // the edge ("before" | "after") where the dragged card would land.
    const [dropTarget, setDropTarget] = useState(null);
    // Release-tag the operator dismissed in this browser. The in-page
    // update alert stays hidden for that tag and re-appears when a
    // newer release exists.
    const [dismissedUpdate, setDismissedUpdate] = useState(function () {
      try {
        return window.localStorage.getItem("quota-console-dismissed-update") || null;
      } catch (error) { /* private mode or unavailable storage: alert shows */ }
      return null;
    });
    const mounted = useRef(true);

    useEffect(function () {
      return function () { mounted.current = false; };
    }, []);

    useEffect(function () {
      try {
        window.localStorage.setItem("quota-console-hidden-providers", JSON.stringify(hiddenProviders));
      } catch (error) { /* storage unavailable: keep state only for this session */ }
    }, [hiddenProviders]);

    useEffect(function () {
      try {
        window.localStorage.setItem("quota-console-provider-order", JSON.stringify(providerOrder));
      } catch (error) { /* storage unavailable: keep state only for this session */ }
    }, [providerOrder]);

    function setProviderVisible(providerId, visible) {
      // Explicit user choice wins over the automatic hidden state:
      // visible=false stores true (hide), visible=true stores false
      // (forced show override for auto-hidden cards).
      setHiddenProviders(function (previous) {
        const next = Object.assign({}, previous);
        next[providerId] = visible ? false : true;
        return next;
      });
    }

    const load = useCallback(function () {
      setState(function (previous) {
        return { loading: true, data: previous.data, error: false };
      });
      return SDK.fetchJSON(API)
        .then(function (data) {
          if (!mounted.current) return data;
          setState({ loading: false, data: data, error: false });
          // Fire-and-forget the notification check. Pure-decision
          // functions drive the call; the wrapper inside the IIFE
          // reaches ``window.Notification`` to render the toast. The
          // resulting {identity, cooldownMap} lands back in state so the
          // next poll's diff only includes new alerts.
          if (data) {
            const current = notificationsRef.current;
            const next = maybeFireNotifications(
              current,
              data,
              data.settings && data.settings.notifications,
            );
            const incomingSettings = (data.settings && data.settings.notifications) || current.settings;
            const settingsChanged = JSON.stringify(incomingSettings) !== JSON.stringify(current.settings);
            const stateChanged = next && (next.identity !== current.identity
                || next.cooldownMap !== current.cooldownMap);
            if (stateChanged || settingsChanged) {
              setNotifications(function () {
                return {
                  identity: next ? next.identity : current.identity,
                  cooldownMap: next ? next.cooldownMap : current.cooldownMap,
                  settings: incomingSettings,
                };
              });
            }
          }
          return data;
        })
        .catch(function (error) {
          if (!mounted.current) return null;
          setState(function (previous) {
            return { loading: false, data: previous.data, error: true };
          });
          throw error;
        });
    }, []);

    const reset = useCallback(function (scope, profile, providerId) {
      const isProvider = scope === "provider";
      const target = isProvider ? (providerId || "provider") : profile;
      if (typeof window.confirm === "function" && !window.confirm("Reset cached rate-limit state for " + target + "?")) {
        return;
      }
      const key = isProvider ? "provider:" + (providerId || "") : profile;
      setResetting(key);
      setActionMessage(null);
      SDK.fetchJSON(RESET_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isProvider ? { scope: "provider", provider: providerId } : { scope: "profile", profile: profile }),
      })
        .then(function (result) {
          if (!result || result.ok !== true) throw new Error("reset failed");
          setActionMessage({ kind: "success", text: "Rate-limit state reset for " + target + "." });
          return load();
        })
        .catch(function () {
          if (!mounted.current) return;
          setActionMessage({ kind: "error", text: "Could not reset rate-limit state." });
        })
        .then(function () {
          if (mounted.current) setResetting(null);
        });
    }, [load]);

    useEffect(function () {
      load().catch(function () { /* state already records the load failure */ });
      const timer = window.setInterval(function () {
        load().catch(function () { /* state already records the load failure */ });
      }, 60 * 1000);
      return function () { window.clearInterval(timer); };
    }, [load]);

    // First paint: while the initial summary request is in flight there is
    // nothing to render yet, so show a centered loading state instead of an
    // empty page skeleton. Once data arrives (or the request fails) this
    // early return no longer matches and the normal page renders. Manual
    // refreshes keep the old data on screen (loading only disables the
    // Refresh button), so this branch is exclusive to the first load.
    if (state.loading && !state.data) {
      return h(
        "div",
        { className: "usages-page usages-page--loading" },
        h(
          "div",
          { className: "usages-loading", role: "status" },
          h("span", { className: "usages-loading-spinner", "aria-hidden": "true" }),
          h("span", { className: "usages-loading-label" }, "Loading Quota Console\u2026"),
        ),
      );
    }

    const data = state.data || {};
    const profiles = Array.isArray(data.profiles) ? data.profiles : [];
    const overview = Array.isArray(data.provider_overview) ? data.provider_overview : [];
    const updated = formatDate(data.updated_at);

    // Effective hidden state: automatic for providers with no quota source,
    // no assigned profile, but configured in Hermes (they carry no useful
    // info until revealed); explicit user choice overrides the default.
    function isProviderHidden(bucket) {
      const autoHidden = isAutoHiddenBucket(bucket);
      const choice = hiddenProviders[bucket.id];
      return choice === undefined ? autoHidden : Boolean(choice);
    }
    const hiddenCount = overview.filter(function (bucket) {
      const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];
      if (!Boolean(bucket.has_quota) && !bucketProfiles.length && !Boolean(bucket.configured)) return false;
      return isProviderHidden(bucket);
    }).length;

    // Action required: providers that need attention (any assigned profile
    // is rate_limited or degraded). auth_failed profiles surface their
    // status on the row itself but are not reset concerns, so they do not
    // appear in this action list.
    const actionRequiredProviders = overview.filter(function (bucket) {
      return (Array.isArray(bucket.profiles) ? bucket.profiles : []).some(function (item) {
        return canResetProfileStatus(item && item.status);
      });
    });
    const actionRequiredCount = actionRequiredProviders.length;

    // Alert component for action required providers
    function ActionAlert(props) {
      if (!props.count) return null;
      const items = props.providers.slice(0, 3).map(function (b) {
        return b.label;
      }).join(", ");
      const more = props.providers.length > 3 ? " +" + (props.providers.length - 3) + " more" : "";
      return h(
        "div",
        { className: "usages-alert usages-alert--action", role: "alert" },
        h("span", { className: "usages-alert-icon" }, "⚠"),
        h("span", { className: "usages-alert-text" },
          "Action required: " + props.count + " provider(s) need attention (" + items + more + ")"
        ),
      );
    }

    // Yellow alert for primary sources at "low" level. The copy
    // is "N provider(s) running low (X %12, Y, Z)" — every provider label
    // surfaces once. When the lowest item carries a reset timestamp the
    // operator sees "resets HH:MM" in their browser locale/timezone so
    // they know when self-heal kicks in.
    function QuotaLowAlert(props) {
      if (!props.items || !props.items.length) return null;
      const labels = props.items.slice(0, 3).map(function (entry) {
        return entry.provider;
      }).join(", ");
      const more = props.items.length > 3 ? " +" + (props.items.length - 3) + " more" : "";
      // Find the earliest reset across the listed items so the alert copy
      // answers "when does this self-heal?" without forcing the operator
      // to scroll through every card.
      const resets = props.items
        .map(function (entry) { return formatTime(entry.reset_at); })
        .filter(function (text) { return text; });
      const resetNote = resets.length ? " resets " + resets[0] : "";
      return h(
        "div",
        { className: "usages-alert usages-alert--low", role: "status" },
        h("span", { className: "usages-alert-icon" }, "⚠"),
        h("span", { className: "usages-alert-text" },
          props.items.length + " provider(s) running low (" + labels + more + ")" + resetNote
        ),
      );
    }

    // Red alert for primary sources at "exhausted" level. The
    // profile-driven red "Action required" alert stays separate and
    // keeps its existing copy; this one only fires when the alert layer
    // declares a primary source exhausted.
    function QuotaExhaustedAlert(props) {
      if (!props.items || !props.items.length) return null;
      const labels = props.items.slice(0, 3).map(function (entry) {
        return entry.provider;
      }).join(", ");
      const more = props.items.length > 3 ? " +" + (props.items.length - 3) + " more" : "";
      return h(
        "div",
        { className: "usages-alert usages-alert--exhausted", role: "alert" },
        h("span", { className: "usages-alert-icon" }, "⚠"),
        h("span", { className: "usages-alert-text" },
          props.items.length + " provider(s) out of quota (" + labels + more + ")"
        ),
      );
    }

    // Top alert inputs: aggregate bucket alerts from the summary.
    // Fallback sources never feed these (they only matter when primary is
    // exhausted), and the per-profile "Action required" red alert keeps
    // working untouched.
    const alerts = data.alerts || {};
    const exhaustedItems = Array.isArray(alerts.exhausted) ? alerts.exhausted : [];
    const lowItems = Array.isArray(alerts.low) ? alerts.low : [];

    // Dismissable in-page update notice: newer release exists and the
    // operator has not closed this exact tag in this browser. Dismissing
    // stores the tag; a later release re-opens the notice.
    const updateTag = updateAlertVisible(data.version, data.latest_release, dismissedUpdate);
    function dismissUpdate() {
      if (!updateTag) return;
      try {
        window.localStorage.setItem("quota-console-dismissed-update", updateTag);
      } catch (error) { /* storage unavailable: keep it visible next time */ }
      setDismissedUpdate(updateTag);
    }

    return h(
      "div",
      { className: "usages-page" },
      h(
        "div",
        { className: "usages-toolbar" },
        h(
          "div",
          null,
          h("h1", { className: "usages-title" }, "Quota Console"),
          h("p", { className: "usages-subtitle" }, updated ? "Updated " + updated : "Provider quotas at a glance"),
        ),
        h(
          "div",
          { className: "usages-toolbar-actions" },
          h(
            Button,
            {
              type: "button",
              size: "sm",
              className: "usages-open-settings",
              onClick: function () { setShowSettings(true); },
              "aria-haspopup": "dialog",
            },
            "Settings",
          ),
          h(
            Button,
            {
              type: "button",
              size: "sm",
              className: "usages-show-hidden",
              onClick: function () { setCustomizeMode(function (value) { return !value; }); },
              disabled: !overview.length,
              title: customizeMode
                ? "Done: apply the visibility and order changes."
                : "Show or hide provider cards and change their order.",
              "aria-pressed": customizeMode,
            },
            customizeMode ? "Done" : "Customize",
          ),
          h(
            Button,
            { type: "button", size: "sm", onClick: function () { load().catch(function () {}); }, disabled: state.loading },
            state.loading ? "Refreshing…" : "Refresh",
          ),
        ),
      ),
      state.error
        ? h(
            "div",
            { className: "usages-error", role: "status" },
            h("span", { className: "usages-action-message-text" }, "Could not load usage data."),
            h(
              "button",
              {
                type: "button",
                className: "usages-action-message-close",
                onClick: function () { setState(function (previous) { return Object.assign({}, previous, { error: false }); }); },
                "aria-label": "Dismiss message",
              },
              "\u00D7",
            ),
          )
        : null,
      actionMessage
        ? h(
            "div",
            { className: "usages-action-message usages-action-message--" + actionMessage.kind, role: "status" },
            h("span", { className: "usages-action-message-text" }, actionMessage.text),
            h(
              "button",
              {
                type: "button",
                className: "usages-action-message-close",
                onClick: function () { setActionMessage(null); },
                "aria-label": "Dismiss message",
              },
              "\u00D7",
            ),
          )
        : null,
      updateTag
        ? h(
            "div",
            { className: "usages-alert usages-alert--update", role: "status" },
            h("span", { className: "usages-alert-icon" }, "\u2726"),
            h(
              "span",
              { className: "usages-alert-text" },
              "New version " + updateTag.replace(/^v/, "") + " available.",
            ),
            h(
              "a",
              { className: "usages-alert-link", href: "https://github.com/semihkiroglu/hermes-quota-console/releases", target: "_blank", rel: "noopener noreferrer" },
              "View releases",
            ),
            h(
              "button",
              {
                type: "button",
                className: "usages-alert-dismiss",
                onClick: dismissUpdate,
                "aria-label": "Dismiss update notice",
              },
              "\u00D7",
            ),
          )
        : null,
      h(ActionAlert, { count: actionRequiredCount, providers: actionRequiredProviders }),
      // Alert-layer top alerts. Render in severity order: exhausted
      // first (red, role=alert), then low (yellow, role=status so screen
      // readers can ignore when nothing is on fire). The profile-driven
      // ActionAlert above stays as-is — it answers a different question
      // (auth/rate-limit state on the live profile), not quota levels.
      h(QuotaExhaustedAlert, { items: exhaustedItems }),
      h(QuotaLowAlert, { items: lowItems }),
      showSettings && data.settings
        ? h(SettingsDialog, {
            initial: {
              defaults: (data.settings && data.settings.defaults) || {},
              providers: (data.settings && data.settings.providers) || {},
            },
            notifications: (data.settings && data.settings.notifications) || null,
            schema: (data.settings && data.settings.schema) || { note_max_length: 120 },
            providers: (function () {
              // Settings list mirrors the main-screen visibility rules:
              // unconfigured buckets (no quota, no profiles, no Hermes
              // credentials) never appear; auto-hidden buckets (configured
              // but no quota data) render dimmed at the bottom so the
              // operator can still set thresholds/notes on them.
              const partitioned = partitionBuckets(overview, isAutoHiddenBucket);
              const rows = partitioned.visible.map(function (bucket) {
                return { id: bucket.id, label: bucket.label };
              });
              partitioned.hidden.forEach(function (bucket) {
                rows.push({ id: bucket.id, label: bucket.label, autoHidden: true });
              });
              return rows;
            }()),
            onClose: function () { setShowSettings(false); },
            onSaved: function (result) {
              setShowSettings(false);
              setActionMessage({ kind: "success", text: "Settings saved." });
              // The summary cache was invalidated server-side, but reload to
              // pick up the new effective view in the next render.
              load().catch(function () {});
              if (result && data && data.settings) {
                data.settings = Object.assign({}, data.settings, result);
              }
            },
          })
        : null,
      h(
        "section",
        { className: "usages-block" },
        h(
          "div",
          { className: "usages-block-heading" },
          h("h2", { className: "usages-block-title" }, "Providers by profile"),
          hiddenCount > 0 && !customizeMode
            ? h("span", { className: "usages-hidden-badge", title: "Hidden providers are shown while customize mode is on." }, hiddenCount + " hidden")
            : null,
        ),
        overview.length
          ? (function () {
              // Visible buckets render first in their stored order (the
              // backend order until the operator drags them into a new
              // one). The hidden group is appended only while customize
              // mode is on, so cards can be shown again or re-hidden.
              // Unconfigured buckets never enter either group —
              // partitionBuckets drops them up front.
              const partitioned = partitionBuckets(overview, isProviderHidden);
              const orderedVisible = applyStoredOrder(partitioned.visible, providerOrder);
              const ordered = orderedVisible.concat(customizeMode ? partitioned.hidden : []);
              const visibleIds = orderedVisible.map(function (bucket) { return bucket.id; });

              // Drop the dragged card onto ``targetId``: move it to just
              // before or after that card (pointer in the upper half of the
              // target means "before", lower half "after") and persist the
              // new visible order. ``edge`` mirrors the drop indicator.
              function handleDropOn(targetId, edge) {
                if (!dragId || dragId === targetId) {
                  setDragId(null);
                  setDropTarget(null);
                  return;
                }
                setProviderOrder(moveProviderId(visibleIds, dragId, targetId, edge || "before"));
                setDragId(null);
                setDropTarget(null);
              }

              // While dragging over a card, decide whether the dragged card
              // would land before or after it (upper/lower half of the
              // target) and record it so the row can show a drop indicator.
              function handleDragOver(bucket, event) {
                if (!dragId || dragId === bucket.id) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                const rect = event.currentTarget.getBoundingClientRect();
                const edge = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
                setDropTarget({ id: bucket.id, edge: edge });
              }

              function startDrag(bucket, event) {
                event.dataTransfer.setData("text/plain", bucket.id);
                event.dataTransfer.effectAllowed = "move";
                setDragId(bucket.id);
                setDropTarget(null);
              }

              return h(
                "div",
                {
                  className: "usages-provider-overview",
                  onDragOver: customizeMode
                    ? function (event) {
                        // Dragging over the gaps between cards (not over a
                        // card itself) clears any stale drop indicator.
                        if (!(event.target instanceof Element) || !event.target.closest(".usages-provider-bucket")) {
                          if (dropTarget) setDropTarget(null);
                        }
                      }
                    : null,
                  onDragLeave: customizeMode
                    ? function (event) {
                        // Leaving the whole list clears the indicator; the
                        // per-card dragover handlers re-arm it on entry.
                        if (!(event.target instanceof Element) || !event.target.closest(".usages-provider-bucket")) {
                          if (dropTarget) setDropTarget(null);
                        }
                      }
                    : null,
                },
                ordered.map(function (bucket) {
              const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];
              const provider = bucket.provider || null;
              const hasQuota = Boolean(bucket.has_quota);
              const configured = Boolean(bucket.configured);
              // No quota source, no assigned profile and no Hermes
              // credentials: nothing useful to render, skip entirely.
              // (partitionBuckets already dropped these from the ordered
              // list — this guard is defensive in case a caller bypasses
              // the helper.)
              if (!hasQuota && !bucketProfiles.length && !configured) return null;
              const isHidden = isProviderHidden(bucket);
              if (isHidden && !customizeMode) return null;
              // Hidden cards render as a compact single-row strip, not as a
              // full dimmed card: the operator only needs the name and a
              // way to bring the card back. Full content stays hidden.
              if (isHidden) {
                return h(
                  "div",
                  {
                    className: "usages-provider-bucket usages-provider-bucket--hidden-row",
                    key: bucket.id,
                    onDragOver: customizeMode
                      ? function (event) {
                          // Hidden rows are not drop targets; hovering one
                          // while dragging clears any stale indicator.
                          if (dropTarget) setDropTarget(null);
                        }
                      : null,
                  },
                  h(
                    "div",
                    { className: "usages-provider-bucket-title" },
                    h("div", { className: "usages-provider-bucket-heading" }, bucket.label),
                  ),
                  h(
                    "div",
                    { className: "usages-provider-bucket-actions" },
                    h(
                      Button,
                      {
                        type: "button",
                        size: "sm",
                        className: "usages-provider-bucket-toggle",
                        onClick: function () { setProviderVisible(bucket.id, true); },
                        "aria-pressed": false,
                        title: "Show this provider card again.",
                      },
                      "Show",
                    ),
                  ),
                );
              }
              const dragging = dragId === bucket.id;
              const dropHere = dropTarget && dropTarget.id === bucket.id ? dropTarget.edge : null;
              const bucketClass =
                "usages-provider-bucket" +
                (dragging ? " usages-provider-bucket--dragging" : "") +
                (dropHere === "before" ? " usages-provider-bucket--drop-before" : "") +
                (dropHere === "after" ? " usages-provider-bucket--drop-after" : "");
              return h(
                "div",
                {
                  className: bucketClass,
                  key: bucket.id,
                  onDragOver: customizeMode
                    ? function (event) { handleDragOver(bucket, event); }
                    : null,
                  onDrop: customizeMode
                    ? function (event) {
                        event.preventDefault();
                        const rect = event.currentTarget.getBoundingClientRect();
                        const edge = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
                        handleDropOn(bucket.id, edge);
                      }
                    : null,
                },
                h(
                  "div",
                  { className: "usages-provider-bucket-header" },
                  h(
                    "div",
                    { className: "usages-provider-bucket-title" },
                    customizeMode && !isHidden
                      ? h(
                          "span",
                          {
                            className: "usages-provider-bucket-drag",
                            draggable: true,
                            onDragStart: function (event) { startDrag(bucket, event); },
                            onDragEnd: function () { setDragId(null); setDropTarget(null); },
                            title: "Drag to reorder",
                            "aria-label": "Drag to reorder " + bucket.label,
                          },
                          "\u22EE\u22EE",
                        )
                      : null,
                    h("div", { className: "usages-provider-bucket-heading" }, bucket.label),
                  ),
                  h(
                    "div",
                    { className: "usages-provider-bucket-actions" },
                    provider ? h(Status, { status: provider.status }) : null,
                    customizeMode
                      ? h(
                          Button,
                          {
                            type: "button",
                            size: "sm",
                            className: "usages-provider-bucket-toggle",
                            onClick: function () { setProviderVisible(bucket.id, isHidden); },
                            "aria-pressed": isHidden,
                            title: isHidden ? "Show this provider card again." : "Hide this provider card.",
                          },
                          isHidden ? "Show" : "Hide",
                        )
                      : null,
                  ),
                ),
                bucket.settings && bucket.settings.note
                  ? h("p", { className: "usages-provider-note" }, bucket.settings.note)
                  : null,
                h(ProviderSummary, { item: provider ? provider : null }),
                h(
                  "ul",
                  { className: "usages-provider-profile-list" },
                  h(
                    "li",
                    { className: "usages-provider-profile usages-provider-profile--provider", key: "provider-availability" },
                    h("span", { className: "usages-provider-profile-name" }, bucket.label),
                    h(ModelStatus, { status: bucket.provider_availability && bucket.provider_availability.status, label: bucket.provider_availability && bucket.provider_availability.status_label }),
                    h("span", { className: "usages-provider-profile-model" }, "-"),
                    bucket.reset_at
                      ? h("span", { className: "usages-reset" }, "Resets " + formatDate(bucket.reset_at))
                      : h("span", { className: "usages-reset" }, ""),
                    canResetProfileStatus(bucket.provider_availability && bucket.provider_availability.status)
                      ? h(
                          "span",
                          {
                            className: "usages-provider-profile-action-wrap",
                            title: "Reset cached rate-limit state for every profile on " + bucket.label + ".",
                          },
                          h(
                            Button,
                            {
                              type: "button",
                              size: "sm",
                              className: "usages-provider-profile-action",
                              disabled: Boolean(resetting),
                              onClick: function () {
                                if (canResetProfileStatus(bucket.provider_availability && bucket.provider_availability.status)) reset("provider", null, bucket.id);
                              },
                            },
                            resetting === "provider:" + bucket.id ? "Resetting…" : "Reset usage",
                          ),
                        )
                      : null,
                  ),
                  bucketProfiles.map(function (item) {
                        const busy = resetting === item.profile;
                        const canReset = canResetProfileStatus(item.status);
                        const resetTitle = resetProfileTitle(item);
                        return h(
                          "li",
                          { className: "usages-provider-profile", key: (item.id || item.profile) + "-provider" },
                          h("span", { className: "usages-provider-profile-name" }, item.profile),
                          h(ModelStatus, { status: item.status, label: item.status_label }),
                          h("span", { className: "usages-provider-profile-model" }, item.model || "No default model"),
                          item.reset_at
                            ? h("span", { className: "usages-reset" }, "Resets " + formatDate(item.reset_at))
                            : h("span", { className: "usages-reset" }, ""),
                          canReset
                            ? h(
                                "span",
                                { className: "usages-provider-profile-action-wrap", title: resetTitle },
                                h(
                                  Button,
                                  {
                                    type: "button",
                                    size: "sm",
                                    className: "usages-provider-profile-action",
                                    disabled: Boolean(resetting),
                                    "aria-label": resetTitle,
                                    onClick: function () {
                                      if (canReset) reset("profile", item.profile);
                                    },
                                  },
                                  busy ? "Resetting…" : "Reset usage",
                                ),
                              )
                            : null,
                        );
                      }),
                  ),
                  bucketProfiles.length ? null : h("p", { className: "usages-empty" }, "No profiles use this provider yet."),
              );
            }))
            ;
            })()
          : state.loading
            ? h("div", { className: "usages-empty" }, "Loading…")
            : h("div", { className: "usages-empty" }, "No provider profile mapping available."),
      ),
      h(
        "div",
        { className: "usages-footnote" },
        h(
          "span",
          { className: "usages-footnote-brand-line" },
          h(
            "a",
            { className: "usages-footnote-brand", href: "https://github.com/semihkiroglu/hermes-quota-console", target: "_blank", rel: "noopener noreferrer" },
            "Hermes Quota Console",
          ),
          data.version
            ? h("span", { className: "usages-footnote-version" }, "v" + data.version)
            : null,
          releaseUpdate(data.version, data.latest_release)
            ? h(
                "a",
                {
                  className: "usages-footnote-update",
                  href: "https://github.com/semihkiroglu/hermes-quota-console/releases",
                  target: "_blank",
                  rel: "noopener noreferrer",
                  title: "New release " + releaseUpdate(data.version, data.latest_release) + " available",
                  "aria-label": "New release " + releaseUpdate(data.version, data.latest_release) + " available",
                },
                h("span", { className: "usages-footnote-update-dot" }),
              )
            : null,
        ),
        h(
          "div",
          { className: "usages-footnote-author" },
          "by ",
          h(
            "a",
            { className: "usages-footnote-link", href: "https://github.com/semihkiroglu", target: "_blank", rel: "noopener noreferrer" },
            "Semih K\u0131ro\u011flu",
          ),
          " and ",
          h(
            "a",
            { className: "usages-footnote-link", href: "https://github.com/semihkiroglu/hermes-quota-console/graphs/contributors", target: "_blank", rel: "noopener noreferrer" },
            "contributors",
          ),
        ),
        h(
          "div",
          { className: "usages-footnote-links" },
          h(
            "a",
            { className: "usages-footnote-btn", href: "https://github.com/sponsors/semihkiroglu", target: "_blank", rel: "noopener noreferrer" },
            "\u2661 Sponsor",
          ),
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("quota-console", UsagePage);

  // -------------------------------------------------------------------
  // Global shell banner (header-banner slot)
  // -------------------------------------------------------------------
  //
  // The dashboard shell renders a full-width strip below the top nav on
  // every page (App.tsx renders <PluginSlot name="header-banner" /> next
  // to its own system banners). When the summary reports quota alerts we
  // register a slim, clickable strip in that slot so the operator sees
  // \"provider out of quota\" before opening the plugin tab; clicking it
  // navigates to the plugin page. The banner polls the same 30s-cached
  // summary endpoint on its own 60s interval (the tab page owns its own
  // interval and unmounts when hidden, so a shared timer is not possible).
  // bannerAlertFromSummary lives at module scope so the Node test suite
  // can exercise the same aggregation the browser renders with.
  function QuotaAlertBanner() {
    const [alert, setAlert] = useState(null);
    const mounted = useRef(true);

    useEffect(function () {
      return function () { mounted.current = false; };
    }, []);

    function refresh() {
      SDK.fetchJSON(API)
        .then(function (data) {
          if (!mounted.current) return;
          setAlert(bannerAlertFromSummary(data));
        })
        .catch(function () {
          // Keep the last known alert (or nothing) on transient failures;
          // the banner must never block the dashboard or log details.
        });
    }

    useEffect(function () {
      refresh();
      const timer = window.setInterval(refresh, 60 * 1000);
      return function () { window.clearInterval(timer); };
    }, []);

    if (!alert) return null;
    const names = alert.names.join(", ") + (alert.extra ? " +" + alert.extra + " more" : "");
    const copy = alert.level === "critical"
      ? alert.count + " provider(s) need attention (" + names + ")"
      : alert.count + " provider(s) running low (" + names + ")";
    return h(
      "a",
      {
        className: "usages-banner usages-banner--" + alert.level,
        href: "/quota-console",
        role: alert.level === "exhausted" ? "alert" : "status",
      },
      h("span", { className: "usages-banner-icon" }, "\u26A0"),
      h("span", { className: "usages-banner-text" }, copy),
      h("span", { className: "usages-banner-cta" }, "Open Quota Console \u2192"),
    );
  }

  if (window.__HERMES_PLUGINS__.registerSlot) {
    window.__HERMES_PLUGINS__.registerSlot("quota-console", "header-banner", QuotaAlertBanner);
  }
})();

// When loaded under Node (test fixtures only), expose projectProfiles so
// the test suite can exercise the same projection rules the browser uses.
// The browser never executes this branch because `module` is undefined
// there and the dashboard plugin SDK takes over before the bundle returns.
if (typeof module !== "undefined" && module && module.exports) {
  module.exports = {
    projectProfiles: projectProfiles,
    canResetProfileStatus: canResetProfileStatus,
    partitionBuckets: partitionBuckets,
    applyStoredOrder: applyStoredOrder,
    moveProviderId: moveProviderId,
    balanceRowClass: balanceRowClass,
    bannerAlertFromSummary: bannerAlertFromSummary,
    releaseUpdate: releaseUpdate,
    notificationAlertIdentity: notificationAlertIdentity,
    notificationDecisions: notificationDecisions,
    notificationBody: notificationBody,
  };
}
