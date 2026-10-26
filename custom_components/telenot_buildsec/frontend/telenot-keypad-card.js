/*
 * Telenot keypad card - shipped with the telenot_buildsec integration.
 *
 * Mirrors the BuildSec virtual keypad: LCD, four free LEDs with the labels from the
 * integration options, alarm/fault/walk-test indicators and all keys. Everything it
 * shows comes from the `keypad` attribute of the alarm control panel entity; keys are
 * sent with the integration's `send_keys` service.
 *
 *   type: custom:telenot-keypad-card
 *   entity: alarm_control_panel.alarmanlage
 *   confirm_arming: true   # arm keys need a second tap within 3 s
 *   show_logo: true
 */

const DOMAIN = "telenot_buildsec";
const CONFIRM_MS = 3000;

const TEXT = {
  de: {
    led: "LED",
    paused: "Fernzugriff pausiert",
    pausedHint: "Die Verbindung ist für die BuildSec-App freigegeben.",
    offline: "Keine Verbindung zur Zentrale",
    offlineHint: "Home Assistant verbindet sich automatisch neu.",
    resume: "Verbinden",
    release: "Für App freigeben",
    releaseConfirm: "Verbindung trennen und für die BuildSec-App freigeben?",
    confirm: "Zum Senden erneut tippen",
    failed: "Taste konnte nicht gesendet werden",
    missing: "Entität nicht gefunden",
    alarm: "Alarm",
    fault: "Störung",
    walk: "Gehtest",
    open: (n) => (n === 1 ? "1 Meldung" : `${n} Meldungen`),
    keys: {
      disarm: "Unscharf",
      arm_home: "Intern scharf",
      arm_away: "Extern scharf",
      function: "Funktionstaste",
      up: "Hoch",
      down: "Runter",
      info: "Details",
    },
  },
  en: {
    led: "LED",
    paused: "Remote access paused",
    pausedHint: "The connection is released for the BuildSec app.",
    offline: "Not connected to the panel",
    offlineHint: "Home Assistant reconnects automatically.",
    resume: "Connect",
    release: "Release for app",
    releaseConfirm: "Disconnect and release the panel for the BuildSec app?",
    confirm: "Tap again to send",
    failed: "Could not send the key",
    missing: "Entity not found",
    alarm: "Alarm",
    fault: "Fault",
    walk: "Walk test",
    open: (n) => (n === 1 ? "1 message" : `${n} messages`),
    keys: {
      disarm: "Disarm",
      arm_home: "Arm home",
      arm_away: "Arm away",
      function: "Function key",
      up: "Up",
      down: "Down",
      info: "Details",
    },
  },
};

// [key sent to send_keys, main label or icon, small label above]
const NUMPAD = [
  ["1", "1", "Menu"],
  ["2", "2", "Reset"],
  ["3", "3", "‹ ›"],
  ["4", "4", ""],
  ["5", "5", ""],
  ["6", "6", ""],
  ["7", "7", ""],
  ["8", "8", ""],
  ["9", "9", ""],
  ["esc", "Esc", "∨"],
  ["0", "0", ""],
  ["ok", "mdi:check-bold", ""],
];
const SIDE = [
  ["disarm", "mdi:lock-open-variant"],
  ["arm_home", "mdi:home-account"],
  ["arm_away", "mdi:home-outline"],
  ["function", "mdi:circle-outline"],
];
const ARM_KEYS = new Set(["arm_home", "arm_away"]);

const STYLE = `
  :host { display: block; }
  ha-card {
    container-type: inline-size;
    overflow: hidden;
    --tk-body: #e8e8e8;
    --tk-panel: #a7a7a7;
    --tk-panel-text: #fff;
    --tk-key: linear-gradient(180deg, #f2f2f2, #dadada);
    --tk-key-text: #111;
    --tk-key-shadow: #2f2f2f;
    --tk-led-off: #d8d8d8;
    --tk-icon-off: #c9c9c9;
    background: var(--tk-body);
    color: var(--tk-key-text);
  }
  ha-card.dark {
    --tk-body: #232427;
    --tk-panel: #4a4c50;
    --tk-panel-text: #f1f1f1;
    --tk-key: linear-gradient(180deg, #3b3d42, #2c2e32);
    --tk-key-text: #f1f1f1;
    --tk-key-shadow: #0b0b0c;
    --tk-led-off: #6b6d72;
    --tk-icon-off: #55575c;
  }
  .pad { position: relative; padding: 5cqw 6cqw 4cqw; }
  .lcd {
    background: var(--tk-panel);
    border-radius: 3cqw;
    padding: 2.4cqw 3cqw;
    box-shadow: inset 0 0.6cqw 1.6cqw rgba(0, 0, 0, 0.25);
    font-family: "Courier New", ui-monospace, monospace;
    font-weight: 700;
    font-size: 6.4cqw;
    line-height: 1.25;
    letter-spacing: 0.06em;
    color: var(--tk-panel-text);
    text-align: center;
    white-space: pre;
    overflow: hidden;
    text-shadow: 0 0 0.6cqw rgba(255, 255, 255, 0.35);
  }
  .lcd .hint {
    display: block;
    font-family: var(--ha-font-family-body, sans-serif);
    font-size: 3cqw;
    letter-spacing: 0;
    opacity: 0.9;
  }
  .middle {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto 16cqw;
    gap: 2.6cqw;
    align-items: center;
    margin-top: 6cqw;
  }
  .leds {
    background: var(--tk-panel);
    border-radius: 3cqw;
    padding: 1.5cqw 3cqw 1.5cqw 8cqw;
    margin-left: 3cqw;
    display: grid;
    gap: 0.6cqw;
  }
  .led-row { position: relative; display: flex; align-items: center; min-height: 9.4cqw; }
  .led {
    position: absolute;
    left: -11cqw;
    width: 7.2cqw;
    height: 7.2cqw;
    border-radius: 50%;
    background: var(--tk-led-off);
    border: 1cqw solid var(--tk-panel);
    box-shadow: inset 0 0.4cqw 0.8cqw rgba(0, 0, 0, 0.25);
    transition: background 0.2s, box-shadow 0.2s;
  }
  .led.green { background: radial-gradient(circle at 35% 35%, #b8ffb0, #2fbf3a 60%); box-shadow: 0 0 2.4cqw #3ddc4a; }
  .led.red { background: radial-gradient(circle at 35% 35%, #ffb3ad, #e0281c 60%); box-shadow: 0 0 2.4cqw #ff3b2f; }
  .led.yellow { background: radial-gradient(circle at 35% 35%, #fff2a8, #f2b705 60%); box-shadow: 0 0 2.4cqw #ffc400; }
  .blink { animation: blink 1s steps(1, end) infinite; }
  @keyframes blink { 50% { opacity: 0.15; } }
  .led-label { color: var(--tk-panel-text); font-size: 4.3cqw; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .side-top { display: grid; justify-items: center; gap: 2.5cqw; }
  .logo { font-size: 3.8cqw; font-weight: 800; letter-spacing: 0.12em; white-space: nowrap; }
  .logo b { color: #e3001b; letter-spacing: 0; margin-right: 0.8cqw; }
  .status { display: grid; gap: 2.2cqw; }
  .status ha-icon { --mdc-icon-size: 8cqw; color: var(--tk-icon-off); transition: color 0.2s, filter 0.2s; }
  .status ha-icon.red { color: #e0281c; filter: drop-shadow(0 0 1.2cqw #ff3b2f); }
  .status ha-icon.yellow { color: #f2b705; filter: drop-shadow(0 0 1.2cqw #ffc400); }
  .status ha-icon.green { color: #2fbf3a; filter: drop-shadow(0 0 1.2cqw #3ddc4a); }
  .arrows { display: grid; gap: 2.6cqw; }
  .keys {
    display: grid;
    grid-template-columns: repeat(3, 1fr) 0.8fr 1fr;
    gap: 2.6cqw 2.6cqw;
    margin-top: 7cqw;
  }
  button.key {
    all: unset;
    box-sizing: border-box;
    cursor: pointer;
    position: relative;
    aspect-ratio: 1.12;
    border-radius: 2.6cqw;
    background: var(--tk-key);
    color: var(--tk-key-text);
    box-shadow: 0.7cqw 0.9cqw 0 var(--tk-key-shadow), inset 0 0.3cqw 0 rgba(255, 255, 255, 0.6);
    display: grid;
    place-items: center;
    font-size: 8.6cqw;
    font-weight: 700;
    line-height: 1;
    transition: transform 0.06s, box-shadow 0.06s;
    -webkit-tap-highlight-color: transparent;
    user-select: none;
  }
  ha-card.dark button.key { box-shadow: 0.7cqw 0.9cqw 0 var(--tk-key-shadow), inset 0 0.3cqw 0 rgba(255, 255, 255, 0.08); }
  button.key:focus-visible { outline: 0.6cqw solid var(--primary-color); outline-offset: 0.6cqw; }
  button.key.pressed, button.key:active { transform: translate(0.5cqw, 0.7cqw); box-shadow: 0.2cqw 0.2cqw 0 var(--tk-key-shadow); }
  button.key.pending { box-shadow: 0 0 0 0.8cqw var(--warning-color, #ffa600), 0.7cqw 0.9cqw 0 var(--tk-key-shadow); }
  button.key ha-icon { --mdc-icon-size: 10cqw; }
  button.key .sub { position: absolute; top: 1.2cqw; left: 0; right: 0; text-align: center; font-size: 3.6cqw; font-weight: 400; }
  button.key.has-sub .main { margin-top: 3.4cqw; }
  button.key.esc { font-size: 7.4cqw; }
  button.key.esc .sub { left: 1.8cqw; text-align: left; }
  button.key.arrow { aspect-ratio: 1.12; width: 100%; }
  .info { all: unset; cursor: pointer; display: grid; place-items: center; color: #1e88e5; }
  .info ha-icon { --mdc-icon-size: 9cqw; }
  .footer { display: flex; align-items: center; justify-content: space-between; gap: 2cqw; margin-top: 5cqw; font-size: 3.6cqw; }
  .footer .state { opacity: 0.75; }
  .footer .info-row { display: flex; align-items: center; gap: 2cqw; min-width: 0; }
  .footer .open { display: none; align-items: center; gap: 1cqw; padding: 0.6cqw 2.2cqw; border-radius: 99px; background: rgba(242, 183, 5, 0.18); color: var(--primary-text-color); white-space: nowrap; }
  .footer .open.show { display: inline-flex; }
  .footer .open ha-icon { --mdc-icon-size: 4.2cqw; color: #f2b705; }
  [data-more]:not([data-more=""]) { cursor: pointer; }
  .led-row[data-more]:not([data-more=""]):hover .led-label { text-decoration: underline; }
  .status ha-icon[data-more]:not([data-more=""]):hover { opacity: 0.7; }
  .footer button, .overlay button {
    all: unset; cursor: pointer; padding: 1.6cqw 3.2cqw; border-radius: 99px;
    background: var(--primary-color); color: var(--text-primary-color, #fff); font-size: 3.6cqw; font-weight: 500;
  }
  .footer button.ghost { background: transparent; color: var(--primary-color); border: 1px solid currentColor; }
  .overlay {
    position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center;
    gap: 2.4cqw; padding: 8cqw; text-align: center;
    background: color-mix(in srgb, var(--tk-body) 82%, transparent);
    backdrop-filter: blur(2px);
  }
  .overlay.show { display: flex; }
  .overlay ha-icon { --mdc-icon-size: 12cqw; opacity: 0.7; }
  .overlay .title { font-size: 5.2cqw; font-weight: 700; }
  .overlay .hint { font-size: 3.8cqw; opacity: 0.8; }
`;

class TelenotKeypadCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        {
          name: "entity",
          required: true,
          selector: { entity: { domain: "alarm_control_panel", integration: DOMAIN } },
        },
        { name: "confirm_arming", default: true, selector: { boolean: {} } },
        { name: "show_logo", default: true, selector: { boolean: {} } },
      ],
    };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass.entities || {}).find(
      (e) => e.platform === DOMAIN && e.entity_id.startsWith("alarm_control_panel.")
    );
    return { entity: entity ? entity.entity_id : "", confirm_arming: true, show_logo: true };
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("entity is required");
    this._config = { confirm_arming: true, show_logo: true, ...config };
    if (this._root) this._root.querySelector(".logo").style.display = this._config.show_logo ? "" : "none";
  }

  getCardSize() {
    return 12;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    this._update();
  }

  // ------------------------------------------------------------------ rendering

  get _t() {
    const lang = (this._hass && (this._hass.locale?.language || this._hass.language)) || "en";
    return TEXT[lang.startsWith("de") ? "de" : "en"];
  }

  _build() {
    const t = this._t;
    const root = this.attachShadow({ mode: "open" });
    const key = (id, main, sub = "", extra = "") => {
      const body = main.startsWith("mdi:") ? `<ha-icon icon="${main}"></ha-icon>` : `<span class="main">${main}</span>`;
      const title = t.keys[id] || main;
      return `<button class="key ${sub ? "has-sub" : ""} ${extra}" data-key="${id}" aria-label="${title}" title="${title}">
        ${sub ? `<span class="sub">${sub}</span>` : ""}${body}</button>`;
    };
    const rows = [0, 1, 2, 3]
      .map((r) => {
        const digits = NUMPAD.slice(r * 3, r * 3 + 3)
          .map(([id, main, sub]) => key(id, main, sub, id === "esc" ? "esc" : ""))
          .join("");
        const middle = r === 0 ? `<button class="info" data-action="info" title="${t.keys.info}"><ha-icon icon="mdi:information-outline"></ha-icon></button>` : "<span></span>";
        return digits + middle + key(SIDE[r][0], SIDE[r][1]);
      })
      .join("");
    root.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="pad">
          <div class="lcd"><span class="l1"> </span>\n<span class="l2"> </span><span class="hint"></span></div>
          <div class="middle">
            <div class="leds">
              ${[0, 1, 2, 3].map((i) => `<div class="led-row" data-row="${i}" data-more=""><span class="led" data-led="${i}"></span><span class="led-label" data-label="${i}"></span></div>`).join("")}
            </div>
            <div class="side-top">
              <div class="logo"><b>▚</b>TELENOT</div>
              <div class="status">
                <ha-icon data-ind="alarm" icon="mdi:alarm-light-outline" title="${t.alarm}"></ha-icon>
                <ha-icon data-ind="fault" icon="mdi:alert-outline" title="${t.fault}"></ha-icon>
                <ha-icon data-ind="walk_test" icon="mdi:walk" title="${t.walk}"></ha-icon>
              </div>
            </div>
            <div class="arrows">
              ${key("up", "mdi:menu-up", "", "arrow")}
              ${key("down", "mdi:menu-down", "", "arrow")}
            </div>
          </div>
          <div class="keys">${rows}</div>
          <div class="footer">
            <div class="info-row">
              <span class="state"></span>
              <span class="open" data-more=""><ha-icon icon="mdi:message-alert-outline"></ha-icon><span></span></span>
            </div>
            <button class="ghost" data-action="release">${t.release}</button>
          </div>
          <div class="overlay">
            <ha-icon icon="mdi:lan-disconnect"></ha-icon>
            <div class="title"></div>
            <div class="hint"></div>
            <button data-action="resume">${t.resume}</button>
          </div>
        </div>
      </ha-card>`;
    this._root = root;
    root.querySelector(".logo").style.display = this._config.show_logo ? "" : "none";
    root.addEventListener("click", (ev) => this._onClick(ev));
    root.querySelectorAll("button.key").forEach((b) => {
      b.addEventListener("pointerdown", () => b.classList.add("pressed"));
      ["pointerup", "pointerleave", "pointercancel"].forEach((e) => b.addEventListener(e, () => b.classList.remove("pressed")));
    });
  }

  _remoteEntity() {
    if (this._config.remote_access_entity) return this._config.remote_access_entity;
    const entities = this._hass.entities || {};
    const own = entities[this._config.entity];
    if (!own || !own.device_id) return undefined;
    const match = Object.values(entities).find(
      (e) => e.device_id === own.device_id && e.platform === DOMAIN && e.translation_key === "remote_access"
    );
    return match && match.entity_id;
  }

  _update() {
    const hass = this._hass;
    const root = this._root;
    const t = this._t;
    const stateObj = hass.states[this._config.entity];
    const card = root.querySelector("ha-card");
    card.classList.toggle("dark", Boolean(hass.themes && hass.themes.darkMode));

    const keypad = stateObj && stateObj.attributes.keypad;
    const remote = this._remoteEntity();
    const paused = remote && hass.states[remote] && hass.states[remote].state === "off";
    const online = Boolean(keypad) && stateObj.state !== "unavailable";

    const overlay = root.querySelector(".overlay");
    overlay.classList.toggle("show", !online);
    if (!online) {
      overlay.querySelector(".title").textContent = !stateObj ? t.missing : paused ? t.paused : t.offline;
      overlay.querySelector(".hint").textContent = !stateObj ? this._config.entity : paused ? t.pausedHint : t.offlineHint;
      overlay.querySelector("button").style.display = paused ? "" : "none";
    }
    root.querySelector('[data-action="release"]').style.display = online && remote ? "" : "none";
    root.querySelector(".footer .state").textContent = stateObj
      ? hass.formatEntityState
        ? hass.formatEntityState(stateObj)
        : stateObj.state
      : "";
    const open = root.querySelector(".footer .open");
    const openCount = keypad ? (keypad.open_detectors || []).length : 0;
    open.classList.toggle("show", online && openCount > 0);
    open.dataset.more = (keypad && keypad.open_detectors_entity_id) || "";
    open.title = keypad ? (keypad.open_detectors || []).join("\n") : "";
    open.querySelector("span").textContent = t.open(openCount);
    if (!keypad) return;

    const [l1, l2] = keypad.display || ["", ""];
    root.querySelector(".l1").textContent = l1 || " ";
    root.querySelector(".lcd").dataset.more = keypad.display_entity_id || "";
    root.querySelector(".l2").textContent = l2 || " ";
    root.querySelector(".lcd .hint").textContent = this._pending ? t.confirm : "";

    (keypad.leds || []).forEach((led, i) => {
      const dot = root.querySelector(`[data-led="${i}"]`);
      dot.className = `led ${led.color !== "off" ? led.color : ""} ${led.blinking ? "blink" : ""}`;
      root.querySelector(`[data-label="${i}"]`).textContent = led.label || `${t.led} ${i + 1}`;
      root.querySelector(`[data-row="${i}"]`).dataset.more = led.entity_id || "";
    });
    ["alarm", "fault", "walk_test"].forEach((name) => {
      const ind = keypad[name] || { color: "off" };
      const icon = root.querySelector(`[data-ind="${name}"]`);
      icon.setAttribute("class", `${ind.color !== "off" ? ind.color : ""} ${ind.blinking ? "blink" : ""}`);
      icon.dataset.more = ind.entity_id || "";
    });
  }

  // ------------------------------------------------------------------ actions

  _onClick(ev) {
    const target = ev
      .composedPath()
      .find((el) => el.dataset && (el.dataset.key || el.dataset.action || el.dataset.more));
    if (!target) return;
    if (target.dataset.more) {
      this._fire("hass-more-info", { entityId: target.dataset.more });
      return;
    }
    if (target.dataset.action) {
      this._action(target.dataset.action);
      return;
    }
    const id = target.dataset.key;
    if (this._config.confirm_arming && ARM_KEYS.has(id) && this._pending !== id) {
      this._setPending(id, target);
      return;
    }
    this._setPending(null);
    this._send(id);
  }

  _setPending(id, button) {
    clearTimeout(this._pendingTimer);
    this._root.querySelectorAll("button.key.pending").forEach((b) => b.classList.remove("pending"));
    this._pending = id;
    if (id) {
      button.classList.add("pending");
      this._pendingTimer = setTimeout(() => this._setPending(null), CONFIRM_MS);
    }
    this._update();
  }

  _send(key) {
    this._fire("haptic", "light");
    this._hass
      .callService(DOMAIN, "send_keys", { keys: key }, { entity_id: this._config.entity })
      .catch((err) => this._fire("hass-notification", { message: `${this._t.failed}: ${err.message || err}` }));
  }

  _action(action) {
    const remote = this._remoteEntity();
    if (action === "info") {
      this._fire("hass-more-info", { entityId: this._config.entity });
    } else if (action === "resume" && remote) {
      this._hass.callService("switch", "turn_on", {}, { entity_id: remote });
    } else if (action === "release" && remote && window.confirm(this._t.releaseConfirm)) {
      this._hass.callService("switch", "turn_off", {}, { entity_id: remote });
    }
  }

  _fire(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
  }
}

if (!customElements.get("telenot-keypad-card")) {
  customElements.define("telenot-keypad-card", TelenotKeypadCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "telenot-keypad-card",
    name: "Telenot keypad",
    description: "BuildSec-style keypad for the Telenot (BuildSec) integration",
    preview: false,
    documentationURL: "https://github.com/kegelmeier/ha-telenot",
  });
}
