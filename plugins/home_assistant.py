"""
Home Assistant integration plugin for JARVIS.
Controls lights, switches, climate, covers, media players, and more via REST API.
"""

import json
import requests
from typing import Optional

PLUGIN = {
    "name": "home_assistant",
    "description": (
        "Control Home Assistant entities: lights, switches, climate, covers, media players, "
        "fans, locks, valves, and more. Use for: 'turn on living room lights', 'set thermostat "
        "to 22', 'open garage door', 'pause TV', 'lock front door'. "
        "Do NOT use for generic web search or computer control — those are separate tools."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to perform: turn_on, turn_off, toggle, set_state, get_state, call_service",
            },
            "entity_id": {
                "type": "STRING",
                "description": "Home Assistant entity ID (e.g., 'light.living_room', 'climate.thermostat', 'cover.garage')",
            },
            "domain": {
                "type": "STRING",
                "description": "Domain for call_service when entity_id not enough (e.g., 'light', 'climate', 'cover', 'media_player')",
            },
            "service": {
                "type": "STRING",
                "description": "Service to call (e.g., 'turn_on', 'set_temperature', 'open_cover', 'media_play_pause')",
            },
            "service_data": {
                "type": "OBJECT",
                "description": "Additional service parameters (e.g., {'brightness': 200}, {'temperature': 22}, {'position': 50})",
            },
            "area": {
                "type": "STRING",
                "description": "Area name to target all entities in area (e.g., 'kitchen', 'bedroom') — used with action 'turn_on'/'turn_off'/'toggle'",
            },
        },
        "required": ["action"],
    },
}

# ─── Config ──────────────────────────────────────────────────────────────
CONFIG_FILE = "config/home_assistant.json"

def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_config(cfg: dict) -> None:
    import os
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def _get_session() -> requests.Session:
    cfg = _load_config()
    url = cfg.get("url", "").rstrip("/")
    token = cfg.get("token", "")
    if not url or not token:
        raise RuntimeError("Home Assistant not configured. Set URL and token in config/home_assistant.json")
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    sess.base_url = url
    return sess

def _resolve_area_entities(area: str, domain: Optional[str] = None) -> list[str]:
    """Get all entity IDs in an area, optionally filtered by domain."""
    sess = _get_session()
    r = sess.get(f"{sess.base_url}/api/states")
    r.raise_for_status()
    entities = []
    for ent in r.json():
        ent_id = ent["entity_id"]
        if "." not in ent_id:
            continue
        ent_domain = ent_id.split(".")[0]
        if domain and ent_domain != domain:
            continue
        # Check area via attributes or registry (simplified: match area in entity_id or friendly_name)
        attrs = ent.get("attributes", {})
        if area.lower() in ent_id.lower() or area.lower() in attrs.get("friendly_name", "").lower():
            entities.append(ent_id)
    return entities

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").lower().strip()
    entity_id = parameters.get("entity_id", "").strip()
    domain = parameters.get("domain", "").strip().lower()
    service = parameters.get("service", "").strip().lower()
    service_data = parameters.get("service_data") or {}
    area = parameters.get("area", "").strip().lower()

    try:
        sess = _get_session()
        base = sess.base_url

        # ─── Area-based bulk actions ──────────────────────────────────────
        if area and action in ("turn_on", "turn_off", "toggle"):
            if not domain:
                # Default to lights+switches for area commands
                domains = ["light", "switch"]
            else:
                domains = [domain]
            results = []
            for dom in domains:
                entities = _resolve_area_entities(area, dom)
                for eid in entities:
                    try:
                        r = sess.post(f"{base}/api/services/{dom}/{action}", json={"entity_id": eid})
                        r.raise_for_status()
                        results.append(f"{eid}: {action}")
                    except Exception as e:
                        results.append(f"{eid}: failed ({e})")
            return f"Home Assistant {area}: " + "; ".join(results) if results else f"No {', '.join(domains)} found in {area}."

        # ─── Single entity actions ────────────────────────────────────────
        if not entity_id:
            return "Please specify an entity_id (e.g., light.living_room) or an area."

        ent_domain = entity_id.split(".")[0] if "." in entity_id else domain
        if not ent_domain:
            return "Could not determine domain from entity_id. Provide domain parameter."

        if action == "get_state":
            r = sess.get(f"{base}/api/states/{entity_id}")
            r.raise_for_status()
            state = r.json()
            return f"{entity_id} is {state['state']} ({state.get('attributes', {}).get('friendly_name', '')})"

        if action in ("turn_on", "turn_off", "toggle"):
            r = sess.post(f"{base}/api/services/{ent_domain}/{action}", json={"entity_id": entity_id})
            r.raise_for_status()
            return f"{entity_id} {action}ed."

        if action == "set_state":
            # Generic state set via homeassistant/set_state (requires admin)
            r = sess.post(f"{base}/api/states/{entity_id}", json={"state": service_data.get("state"), "attributes": service_data.get("attributes", {})})
            r.raise_for_status()
            return f"{entity_id} state set."

        if action == "call_service":
            if not domain or not service:
                return "call_service requires 'domain' and 'service' parameters."
            payload = {"entity_id": entity_id}
            payload.update(service_data)
            r = sess.post(f"{base}/api/services/{domain}/{service}", json=payload)
            r.raise_for_status()
            return f"Called {domain}.{service} on {entity_id}."

        return f"Unknown action: {action}. Use: turn_on, turn_off, toggle, get_state, set_state, call_service."

    except requests.HTTPError as e:
        return f"Home Assistant API error: {e.response.status_code} — {e.response.text[:200]}"
    except Exception as e:
        return f"Home Assistant error: {e}"

# ─── Setup helper (call from UI or manually) ─────────────────────────────
def setup(url: str, token: str) -> str:
    """Save HA URL and long-lived access token."""
    _save_config({"url": url, "token": token})
    # Test connection
    try:
        sess = _get_session()
        r = sess.get(f"{sess.base_url}/api/")
        r.raise_for_status()
        return "Home Assistant connected successfully."
    except Exception as e:
        return f"Saved but connection test failed: {e}"

def test_connection(url: str, token: str) -> str:
    """Test connection without saving (for settings UI button)."""
    try:
        sess = requests.Session()
        sess.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        sess.base_url = url.rstrip("/")
        r = sess.get(f"{sess.base_url}/api/")
        r.raise_for_status()
        return "Connection successful! ✓"
    except Exception as e:
        return f"Connection failed: {e}"


PLUGIN_SETTINGS = {
    "namespace": "home_assistant",
    "title": "Home Assistant",
    "fields": [
        {"key": "url", "label": "Home Assistant URL", "type": "text", "placeholder": "http://homeassistant.local:8123"},
        {"key": "token", "label": "Long-Lived Access Token", "type": "password", "placeholder": "eyJ..."},
    ],
    "action": {"label": "Test Connection", "run": test_connection},
}


def discover_entities(domain: Optional[str] = None) -> list[dict]:
    """Return list of entities for UI picker."""
    try:
        sess = _get_session()
        r = sess.get(f"{sess.base_url}/api/states")
        r.raise_for_status()
        entities = []
        for ent in r.json():
            eid = ent["entity_id"]
            if "." not in eid:
                continue
            d = eid.split(".")[0]
            if domain and d != domain:
                continue
            entities.append({
                "entity_id": eid,
                "name": ent.get("attributes", {}).get("friendly_name", eid),
                "state": ent["state"],
                "domain": d,
            })
        return entities
    except Exception:
        return []