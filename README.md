# proxmox-discord-relay

Kleine relay tussen Proxmox VE notifications en Discord, als vangnet voor
problemen die Proxmox's eigen webhook-templating (Handlebars) niet kan
oplossen.

## Waarom

Proxmox's Discord-embed toont bij een job-notificatie de ruwe `message` van
die job. Bij backups met meerdere guests bevat die `message` honderden
`INFO: NN% (...)`-voortgangsregels, waardoor de Discord embed `description`
(max. 4096 tekens) al snel te lang wordt en Discord de webhook-call weigert.
Proxmox's webhook-Handlebars kent alleen `escape`, `json`, `url-encode`, `eq`
— **geen truncate/regex-helper** — dus filteren/inkorten kan niet binnen de
Proxmox-template zelf. Deze relay zit tussen Proxmox en de echte
Discord-webhook-URL in en doet dat filteren wél.

## Gedrag

- **Backup-jobs (vzdump)** worden herkend aan hun `Details`-tabel met
  `VMID`/`Status`/`Size`-kolommen (uniek voor backup-rapporten — replicatie
  heeft bv. `Guest`/`Target`/`Duration`, geen `Size`). Die tabel wordt
  omgebouwd tot een echte Discord-embed met één veld per VM
  (status-emoji ✅/⚠️/❌, naam, VMID, tijd, grootte) + een "Totaal"-veld. De
  volledige `Logs`-sectie (de voortgangsregels) wordt genegeerd.
- **Alle andere notificatietypes** (replicatie, prune/verify,
  package-updates, certificate-expiry, test, ...) gaan **ongewijzigd** door.
  Er zit alleen een onzichtbare veiligheids-afkap achter de hand voor het
  geval zo'n bericht toch de 4096-tekenlimiet zou raken.
- Alles wat niet herkend wordt (onverwachte payload-vorm, parsing mislukt)
  valt terug op diezelfde veilige doorsturing in plaats van te crashen of
  data te verliezen.

## Installatie

1. Zet `relay.py` ergens neer, bv. `/opt/proxmox-discord-relay/relay.py`.
2. Kopieer `relay.env.example` naar `/etc/proxmox-discord-relay/relay.env`
   (chmod 600) en vul je echte `DISCORD_WEBHOOK_URL` in.
3. Kopieer `proxmox-discord-relay.service` naar
   `/etc/systemd/system/proxmox-discord-relay.service`.
4. Maak een eigen systeemgebruiker aan (geen root nodig) en start de service:
   ```bash
   sudo useradd --system --no-create-home proxmox-relay
   sudo systemctl daemon-reload
   sudo systemctl enable --now proxmox-discord-relay
   ```

### Proxmox-kant

In Proxmox (Datacenter → Notifications) een webhook-target aanmaken die naar
`http://<lan-ip-van-de-relay-host>:8686/` post, met als body-template:

```json
{
  "embeds": [
    {
      "title": "{{ escape title }}",
      "url": "https://proxmox.url/",
      "description": "{{#if (eq severity 'error')}}⚠️ {{else if (eq severity 'warning')}}⚠️ {{/if}}```\n{{ escape message }}\n```\n\n🕒 <t:{{ timestamp }}:R>",
      "color": {{#if (eq severity "error")}}16711680{{else if (eq severity "warning")}}16753920{{else}}5763719{{/if}},
      "footer": {
        "text": "⚡ Severity: {{ severity }}"
      }
    }
  ]
}
```

Vergeet niet in Datacenter → Notifications → Matchers ook de andere
notificatietypes (replicatie, package-updates, ...) naar deze webhook-target
te laten routeren — anders komen die berichten nooit bij de relay aan.

## Debuggen

`print()` buffert onder systemd als het niet naar een terminal schrijft.
`Environment=PYTHONUNBUFFERED=1` (al in de unit-file) is cruciaal, anders
lijkt `journalctl -u proxmox-discord-relay -f` leeg terwijl de service prima
werkt.
