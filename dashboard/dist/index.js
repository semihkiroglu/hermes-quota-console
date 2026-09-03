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
    // Alert layer: tint the row when the balance is low or exhausted.
    // Fallback balances (the wallet behind a still-healthy plan) can also be
    // low/exhausted without changing the bucket alert — the row colour just
    // surfaces that the value itself is below threshold; the bucket level
    // and top alerts remain driven by primary sources only.
    const levelModifier = levelClass(props.balance.level);
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

  function SettingsDialog(props) {
    // Operator-editable settings dialog. Renders the global defaults layer
    // first and then one expandable card per provider so operators can set
    // per-provider overrides without losing the global baseline. The dialog
    // keeps a local draft of the layers and only PUTs on Save.
    const initial = props.initial || { defaults: {}, providers: {} };
    const schema = props.schema || { note_max_length: 120 };
    const providers = Array.isArray(props.providers) ? props.providers : [];
    const [draftDefaults, setDraftDefaults] = useState(function () {
      return Object.assign({}, initial.defaults || {});
    });
    const [draftProviders, setDraftProviders] = useState(function () {
      return Object.assign({}, initial.providers || {});
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
      setError(null);
    }

    function save() {
      setSaving(true);
      setError(null);
      SDK.fetchJSON(SETTINGS_API, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ defaults: draftDefaults, providers: draftProviders }),
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

  function UsagePage() {
    const [state, setState] = useState({ loading: true, data: null, error: false });
    const [resetting, setResetting] = useState(null);
    const [actionMessage, setActionMessage] = useState(null);
    const [showSettings, setShowSettings] = useState(false);
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
          "a",
          { className: "usages-footnote-brand", href: "https://github.com/semihkiroglu/hermes-quota-console", target: "_blank", rel: "noopener noreferrer" },
          "Hermes Quota Console",
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
  };
}
