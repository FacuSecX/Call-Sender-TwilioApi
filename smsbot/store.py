# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import math
import re

from .domain import name, normalized, phone
from .simulation import fictional_number, valid_recipient
from .voice import DEFAULT_VOICE, preset


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL UNIQUE,phone TEXT NOT NULL UNIQUE,favorite INTEGER NOT NULL DEFAULT 0,preferred_api TEXT,consent INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS members(group_id INTEGER NOT NULL REFERENCES groups_(id) ON DELETE CASCADE,contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,PRIMARY KEY(group_id,contact_id));
        CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',created TEXT NOT NULL,confirmed TEXT,stop INTEGER NOT NULL DEFAULT 0,mode TEXT NOT NULL DEFAULT 'live');
        CREATE TABLE IF NOT EXISTS recipients(id INTEGER PRIMARY KEY,campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,contact_id INTEGER,contact_name TEXT NOT NULL,phone TEXT NOT NULL,preferred_api TEXT,status TEXT NOT NULL DEFAULT 'pending',api_id TEXT,message_sid TEXT,error TEXT NOT NULL DEFAULT '',updated TEXT NOT NULL,UNIQUE(campaign_id,phone));
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,recipient_id INTEGER NOT NULL REFERENCES recipients(id),api_id TEXT NOT NULL,status TEXT NOT NULL,code TEXT,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_messages(chat_id INTEGER,message_id INTEGER,created TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
        CREATE INDEX IF NOT EXISTS recipients_status ON recipients(status);
        """)
        if "simulation_only" not in {row["name"] for row in self.db.execute("PRAGMA table_info(contacts)")}:
            self.execute("ALTER TABLE contacts ADD COLUMN simulation_only INTEGER NOT NULL DEFAULT 0")
        if "channel" not in {row["name"] for row in self.db.execute("PRAGMA table_info(campaigns)")}:
            self.execute("ALTER TABLE campaigns ADD COLUMN channel TEXT NOT NULL DEFAULT 'sms'")
        if "voice_consent" not in {row["name"] for row in self.db.execute("PRAGMA table_info(contacts)")}:
            self.execute("ALTER TABLE contacts ADD COLUMN voice_consent INTEGER NOT NULL DEFAULT 0")
        if "tts_voice" not in {row["name"] for row in self.db.execute("PRAGMA table_info(campaigns)")}:
            self.execute("ALTER TABLE campaigns ADD COLUMN tts_voice TEXT NOT NULL DEFAULT 'female'")
        for column,definition in (('audio_token',"TEXT NOT NULL DEFAULT ''"),('audio_seconds','REAL NOT NULL DEFAULT 0')):
            if column not in {row['name'] for row in self.db.execute('PRAGMA table_info(campaigns)')}:
                self.execute('ALTER TABLE campaigns ADD COLUMN '+column+' '+definition)
        path.chmod(0o600)

    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params)]

    def one(self, sql: str, params=()) -> dict | None:
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params=()):
        with self.db:
            return self.db.execute(sql, params)

    def contacts(self, favorites=False) -> list[dict]:
        return self.rows("SELECT * FROM contacts" + (" WHERE favorite=1" if favorites else "") + " ORDER BY name_key")

    def contact(self, contact_id: int) -> dict | None:
        return self.one("SELECT * FROM contacts WHERE id=?", (contact_id,))

    def add_contact(self, label: str, number: str, consent=False, *, simulation_only=False) -> int:
        label = name(label)
        if simulation_only:
            if not fictional_number(number):
                raise ValueError("La simulación solo admite identificadores internos.")
        else:
            number = phone(number)
        try:
            return self.execute("INSERT INTO contacts(name,name_key,phone,consent,created,simulation_only,voice_consent) VALUES(?,?,?,?,?,?,?)", (label, normalized(label), number, int(consent), now(), int(simulation_only), int(simulation_only))).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ese nombre o número ya está agregado.") from exc

    def rename_contact(self, contact_id: int, label: str):
        label = name(label)
        try:
            self.execute("UPDATE contacts SET name=?,name_key=? WHERE id=?", (label, normalized(label), contact_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ese nombre ya está en uso.") from exc

    def delete_contact(self, contact_id: int):
        self.execute("DELETE FROM contacts WHERE id=?", (contact_id,))

    def groups(self) -> list[dict]:
        return self.rows("SELECT g.*,COUNT(m.contact_id) count FROM groups_ g LEFT JOIN members m ON m.group_id=g.id GROUP BY g.id ORDER BY g.name_key")

    def group(self, group_id: int) -> dict | None:
        return self.one("SELECT * FROM groups_ WHERE id=?", (group_id,))

    def add_group(self, label: str) -> int:
        label = name(label)
        try:
            return self.execute("INSERT INTO groups_(name,name_key) VALUES(?,?)", (label, normalized(label))).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ese grupo ya existe.") from exc

    def rename_group(self, group_id: int, label: str):
        label = name(label)
        try:
            self.execute("UPDATE groups_ SET name=?,name_key=? WHERE id=?", (label, normalized(label), group_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ese nombre de grupo ya existe.") from exc

    def members(self, group_id: int) -> list[dict]:
        return self.rows("SELECT c.* FROM contacts c JOIN members m ON m.contact_id=c.id WHERE m.group_id=? ORDER BY name_key", (group_id,))

    def toggle_member(self, group_id: int, contact_id: int):
        if self.one("SELECT 1 FROM members WHERE group_id=? AND contact_id=?", (group_id, contact_id)):
            self.execute("DELETE FROM members WHERE group_id=? AND contact_id=?", (group_id, contact_id))
        else:
            self.execute("INSERT INTO members VALUES(?,?)", (group_id, contact_id))

    def state(self, key: str, default=None):
        row = self.one("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value):
        self.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def draft(self, title: str, text: str, contacts: list[dict], mode="live", channel="sms", *, tts_voice=DEFAULT_VOICE,audio_token='',audio_seconds=0) -> str:
        if not contacts:
            raise ValueError("No hay destinatarios.")
        if channel not in {"sms", "voice_text", "voice_ring", "voice_audio"}:
            raise ValueError("Tipo de envío inválido.")
        if channel=='voice_audio' and (not isinstance(audio_token,str) or not re.fullmatch(r'[0-9a-f]{64}',audio_token) or not isinstance(audio_seconds,(int,float)) or not math.isfinite(audio_seconds) or not 0<audio_seconds<=2400):
            raise ValueError('El audio de la llamada no es válido.')
        if channel!='voice_audio' and (audio_token or audio_seconds):raise ValueError('Audio inesperado en este tipo de envío.')
        preset(tts_voice)
        if any(not c["consent" if channel=="sms" else "voice_consent"] for c in contacts):
            action="enviar SMS" if channel=="sms" else "llamar"
            raise ValueError(f"Falta confirmar el permiso para {action} a uno o más contactos.")
        if mode not in {"live", "test", "simulation"} or any(not valid_recipient(c, mode) for c in contacts):
            raise ValueError("Los contactos simulados no se pueden mezclar con contactos reales ni usar para envíos a Twilio.")
        campaign_id = secrets.token_hex(8)
        with self.db:
            self.db.execute("INSERT INTO campaigns(id,title,body,created,mode,channel,tts_voice,audio_token,audio_seconds) VALUES(?,?,?,?,?,?,?,?,?)", (campaign_id, title, text, now(), mode,channel,tts_voice,audio_token,audio_seconds))
            self.db.executemany("INSERT INTO recipients(campaign_id,contact_id,contact_name,phone,preferred_api,updated) VALUES(?,?,?,?,?,?)", [(campaign_id,c["id"],c["name"],c["phone"],c["preferred_api"],now()) for c in contacts])
        return campaign_id

    def campaign(self, campaign_id: str) -> dict | None:
        return self.one("SELECT * FROM campaigns WHERE id=?", (campaign_id,))

    def recipients(self, campaign_id: str) -> list[dict]:
        return self.rows("SELECT * FROM recipients WHERE campaign_id=? ORDER BY id", (campaign_id,))

    def confirm(self, campaign_id: str) -> bool:
        return self.execute("UPDATE campaigns SET status='queued',confirmed=? WHERE id=? AND status='draft' AND stop=0", (now(), campaign_id)).rowcount == 1

    def cancel(self, campaign_id: str):
        with self.db:
            self.db.execute("UPDATE campaigns SET stop=1,status=CASE WHEN status IN ('draft','queued') THEN 'cancelled' ELSE status END WHERE id=?", (campaign_id,))
            self.db.execute("UPDATE recipients SET status='cancelled',updated=? WHERE campaign_id=? AND status='pending'", (now(),campaign_id))

    def update_recipient(self, recipient_id: int, status: str, *, api_id=None, sid=None, error=""):
        self.execute("UPDATE recipients SET status=?,api_id=COALESCE(?,api_id),message_sid=COALESCE(?,message_sid),error=?,updated=? WHERE id=?", (status,api_id,sid,error,now(),recipient_id))

    def attempt(self, recipient_id: int, api_id: str, status: str, code=""):
        self.execute("INSERT INTO attempts(recipient_id,api_id,status,code,created) VALUES(?,?,?,?,?)", (recipient_id,api_id,status,str(code),now()))

    def recover(self):
        with self.db:
            self.db.execute("UPDATE recipients SET status='unknown',error='El programa se interrumpió durante el envío. No se reenvía automáticamente.' WHERE status='submitting'")
            self.db.execute("UPDATE recipients SET status='cancelled',error='Campaña interrumpida al reiniciar.' WHERE status='pending' AND campaign_id IN (SELECT id FROM campaigns WHERE status IN ('running','queued','interrupted'))")
            self.db.execute("UPDATE campaigns SET status='interrupted',stop=1 WHERE status IN ('running','queued')")

    def track(self, chat: int, message_id: int):
        self.execute("INSERT OR IGNORE INTO chat_messages VALUES(?,?,?)", (chat,message_id,now()))
        self.execute("DELETE FROM chat_messages WHERE chat_id=? AND message_id NOT IN (SELECT message_id FROM chat_messages WHERE chat_id=? ORDER BY message_id DESC LIMIT 300)", (chat,chat))

    def close(self):
        self.db.close()
