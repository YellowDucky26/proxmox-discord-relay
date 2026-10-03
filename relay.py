#!/usr/bin/env python3
"""Relay tussen Proxmox notification webhook en Discord.

Gedrag:
- Backup-job meldingen (vzdump) worden herkend aan hun "Details"-tabel met
  VMID/Status/Size-kolommen. Die worden omgebouwd tot een nette Discord-embed
  met één veld per VM (status-emoji, tijd, grootte) + een totalen-veld. De
  "Logs"-sectie (de honderden "INFO: NN% (...)"-voortgangsregels) wordt
  volledig genegeerd, niet alleen ingekort.
- Alle andere notificatietypen (replicatie, prune/verify, package-updates,
  certificate-expiry, test, ...) worden ONGEWIJZIGD doorgestuurd. Er wordt
  alleen een onzichtbare veiligheids-afkap toegepast als een bericht toch
  Discord's 4096-tekenlimiet zou overschrijden, zodat de webhook nooit faalt.
- Alles wat niet herkend wordt (onverwachte payload-vorm, parsing mislukt)
  valt terug op die zelfde veilige, niet-destructieve afhandeling i.p.v. te
  crashen of data te verliezen.
"""
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/CHANGE/ME")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "8686"))
MAX_DESCRIPTION_LENGTH = int(os.environ.get("MAX_DESCRIPTION_LENGTH", "3900"))

# Proxmox's webhook-template (zie README) plakt de ruwe "message" van de job
# in een code-fence: "<prefix>```\n<message>\n```<suffix>" waarbij <prefix> een
# eventuele ⚠️ severity-emoji is en <suffix> de "\n\n🕒 <t:...:R>" tijdstempel.
FENCE_BLOCK_RE = re.compile(r"^(?P<prefix>.*?)```\n(?P<message>.*?)\n```(?P<suffix>.*)$", re.DOTALL)

DETAILS_HEADER_RE = re.compile(r"(?mi)^Details\s*\n=+\s*\n")
LOGS_HEADER_RE = re.compile(r"(?mi)\n+Logs\s*\n=+\s*\n")

# vzdump-specifieke kolomkop: alleen een backup-job-rapport heeft VMID + Status
# + Size samen in de header-regel van de Details-tabel (replicatie heeft bv.
# "Guest"/"Target"/"Duration", prune/verify geen "Size"-kolom per guest).
BACKUP_TABLE_HEADER_RE = re.compile(
    r"(?=.*\bVMID\b)(?=.*\bStatus\b)(?=.*\bSize\b)", re.I
)

TOTAL_TIME_RE = re.compile(r"^\s*Total running time:\s*(.+)$", re.I)
TOTAL_SIZE_RE = re.compile(r"^\s*Total size:\s*(.+)$", re.I)

STATUS_EMOJI = {
    "OK": "✅",
    "WARNINGS": "⚠️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "FAILED": "❌",
}

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BLANK_LINES_RE = re.compile(r"\n{3,}")
CODE_FENCE = "```"

MAX_PER_VM_FIELDS = 24  # Discord staat max. 25 embed-fields toe; 1 plek vrijhouden voor "Totaal"


def _status_emoji(status: str) -> str:
    return STATUS_EMOJI.get(status.strip().upper(), "❔")


def _safe_truncate(text: str, limit: int) -> str:
    """Kap `text` af tot `limit` tekens, maar sluit een eventueel open
    code-fence netjes af i.p.v. 'm half open te laten staan."""
    if len(text) <= limit:
        return text
    suffix = "\n... (ingekort)"
    cut = text[: limit - len(suffix)]
    if cut.count(CODE_FENCE) % 2 == 1:
        suffix = "\n" + CODE_FENCE + suffix
    return cut + suffix


def parse_backup_details(message: str):
    """Geeft (rows, totals) terug voor een vzdump Details-tabel in `message`,
    of None als dit geen (herkenbaar) backup-job-rapport is."""
    details_m = DETAILS_HEADER_RE.search(message)
    if not details_m:
        return None
    body = message[details_m.end():]

    logs_m = LOGS_HEADER_RE.search(body)
    if logs_m:
        body = body[: logs_m.start()]

    lines = body.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if BACKUP_TABLE_HEADER_RE.search(line):
            header_idx = i
            break
    if header_idx is None:
        return None

    rows = []
    totals = {}
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        tm = TOTAL_TIME_RE.match(stripped)
        if tm:
            totals["time"] = tm.group(1).strip()
            continue
        tm = TOTAL_SIZE_RE.match(stripped)
        if tm:
            totals["size"] = tm.group(1).strip()
            continue
        cols = re.split(r"\s{2,}", stripped)
        if len(cols) < 4 or not re.match(r"^\d+$", cols[0]):
            continue
        vmid, name, status = cols[0], cols[1], cols[2]
        time_ = cols[3] if len(cols) > 3 else "?"
        size = cols[4] if len(cols) > 4 else "?"
        rows.append({"vmid": vmid, "name": name, "status": status, "time": time_, "size": size})

    if not rows:
        return None
    return rows, totals


def build_backup_fields(rows, totals):
    fields = []
    if len(rows) <= MAX_PER_VM_FIELDS:
        for r in rows:
            fields.append(
                {
                    "name": f"{_status_emoji(r['status'])} {r['name']} (VMID {r['vmid']})",
                    "value": f"⏱️ {r['time']} · \U0001f4be {r['size']}",
                    "inline": True,
                }
            )
    else:
        lines = [
            f"{_status_emoji(r['status'])} {r['vmid']} {r['name']} — {r['time']}, {r['size']}"
            for r in rows
        ]
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1000] + "\n... (ingekort)"
        fields.append({"name": "Guests", "value": value, "inline": False})

    ok = sum(1 for r in rows if r["status"].strip().upper() == "OK")
    failed = sum(1 for r in rows if r["status"].strip().upper() in ("ERROR", "FAILED"))
    summary_lines = [f"\U0001f4ca {ok} geslaagd" + (f", {failed} mislukt" if failed else "")]
    if "time" in totals:
        summary_lines.append(f"⏱️ Totale tijd: {totals['time']}")
    if "size" in totals:
        summary_lines.append(f"\U0001f4be Totale grootte: {totals['size']}")
    fields.append({"name": "Totaal", "value": "\n".join(summary_lines), "inline": False})
    return fields


def transform_backup_embed(embed: dict) -> bool:
    """Probeert `embed` om te bouwen tot een nette backup-embed. Geeft True
    terug als dat gelukt is (embed is dan in-place aangepast)."""
    desc = embed.get("description")
    if not isinstance(desc, str):
        return False
    m = FENCE_BLOCK_RE.match(desc)
    if not m:
        return False
    parsed = parse_backup_details(m.group("message"))
    if not parsed:
        return False
    rows, totals = parsed

    embed["fields"] = build_backup_fields(rows, totals)

    prefix = m.group("prefix").strip()
    suffix = m.group("suffix").strip()
    short_desc = "\n".join(part for part in (prefix, suffix) if part)
    if short_desc:
        embed["description"] = short_desc
    else:
        embed.pop("description", None)
    return True


def clean_passthrough_description(text: str) -> str:
    """Niet-destructieve opschoning voor alle niet-backup meldingen: alleen
    ANSI-codes strippen en een veiligheids-afkap, verder blijft alles gelijk."""
    text = ANSI_ESCAPE_RE.sub("", text)
    text = BLANK_LINES_RE.sub("\n\n", text)
    return _safe_truncate(text, MAX_DESCRIPTION_LENGTH)


class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        print(f"[{self.client_address[0]}] POST {self.path} ontvangen, {length} bytes")
        raw_body = self.rfile.read(length)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            print(f"JSON-parse mislukt: {e}. Raw body: {raw_body[:500]!r}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json")
            return

        embeds = payload.get("embeds")
        if isinstance(embeds, list):
            for embed in embeds:
                if not isinstance(embed, dict):
                    continue
                if transform_backup_embed(embed):
                    print("backup-job herkend, omgebouwd naar embed-fields")
                    continue
                desc = embed.get("description")
                if isinstance(desc, str):
                    cleaned = clean_passthrough_description(desc)
                    if cleaned != desc:
                        print(f"niet-backup melding: veiligheids-opschoning toegepast ({len(desc)} -> {len(cleaned)} tekens)")
                        embed["description"] = cleaned
        else:
            print("Geen 'embeds'-array in payload gevonden, ongewijzigd doorgestuurd")

        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Discord/Cloudflare blokkeert de standaard urllib User-Agent met 403.
                "User-Agent": "proxmox-discord-relay/1.2 (+https://github.com/YellowDucky26/proxmox-discord-relay)",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                print(f"Discord accepteerde het bericht, status: {status}")
        except urllib.error.HTTPError as e:
            status = e.code
            print(f"Discord antwoordde met {status}: {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            print(f"Kon Discord niet bereiken: {e.reason}")
            status = 502

        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[{self.client_address[0]}] {format % args}")


if __name__ == "__main__":
    if "CHANGE/ME" in DISCORD_WEBHOOK_URL:
        print("Let op: zet eerst DISCORD_WEBHOOK_URL, zie voorbeeld hieronder.")
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), RelayHandler)
    print(f"Relay luistert op poort {LISTEN_PORT}, stuurt door naar {DISCORD_WEBHOOK_URL}")
    server.serve_forever()
