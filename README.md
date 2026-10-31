# BBCerver Stats

Passwortgeschützte Statistik-Seite für unseren Minecraft-Server.

Alle 15 Minuten holt GitHub Actions die Spieler-Statistiken per FTP vom Server,
verschlüsselt sie mit dem Seiten-Passwort und veröffentlicht sie über GitHub Pages.
Ohne Passwort ist auf der Seite nichts zu sehen – auch nicht im Code.

## Einstellungen

Unter **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Inhalt |
|---|---|
| `SITE_PASSWORD` | Passwort für die Seite (mind. 6 Zeichen, besser länger) |
| `FTP_HOST` | FTP-Adresse aus dem G-Portal-Panel |
| `FTP_PORT` | FTP-Port (leer = 21) |
| `FTP_USER` | FTP-Benutzername |
| `FTP_PASSWORD` | FTP-Passwort |
| `MC_HOST` / `MC_PORT` | Adresse und Port des Minecraft-Servers (für die Online-Anzeige) |

Optional: `FTP_TLS` (`true`), `FTP_BASE_PATH`, `WORLD_NAME`.
Unter **Variables**: `SERVER_NAME`, `HIDDEN_PLAYERS` (kommagetrennt).

## Passwort ändern

Secret `SITE_PASSWORD` ändern, dann unter **Actions → Stats aktualisieren → Run workflow** einmal starten.

## Sofort aktualisieren

**Actions → Stats aktualisieren → Run workflow**.
