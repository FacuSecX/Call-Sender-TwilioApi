# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import re
import secrets


def fictional_number(number: str) -> bool:
    """Recognize internal simulation identifiers, which are never phone numbers."""
    return isinstance(number, str) and bool(re.fullmatch(r"sim_[0-9a-f]{24}", number))


def valid_recipient(contact, mode: str) -> bool:
    demo = bool(contact.get("simulation_only"))
    internal = fictional_number(contact.get("phone"))
    return (demo and internal) if mode == "simulation" else not demo and not internal


def prepare_demo(store) -> str:
    if store.state("simulation_request") or store.one("SELECT 1 FROM campaigns WHERE status IN ('running','queued')"):
        raise ValueError("Ya hay un envío o una simulación pendiente. Esperá a que termine.")
    suffix = secrets.token_hex(2)
    group_id = store.add_group("Simulación SMS " + suffix)
    for index in range(1, 9):
        identifier = "sim_" + secrets.token_hex(12)
        contact_id = store.add_contact(
            f"Simulación {index:02d} {suffix}", identifier,
            consent=True, simulation_only=True,
        )
        store.toggle_member(group_id, contact_id)
    campaign_id = store.draft(
        "Simulación local", "Simulación local sin conexión al proveedor.",
        store.members(group_id), "simulation",
    )
    store.set_state("simulation_request", campaign_id)
    return campaign_id
