"""
Notifie sur Discord tous les candidats IT déjà en base.
Remet notified=0 pour forcer le renvoi.
"""
import asyncio
import aiohttp
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
if not WEBHOOK:
    raise SystemExit("❌ DISCORD_WEBHOOK_URL absent du .env")

DB_PATH = ROOT / "missions.db"


async def discord_notify(session, item):
    """Envoie un candidat sur Discord avec délai anti-429."""
    url = str(item.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        print(f"[SKIP] URL invalide pour '{item.get('title', '?')[:50]}'")
        return False

    payload = {
        "username": "I-LEAD Mission Intelligence",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "👤 Nouveau CANDIDAT IT en recherche d'opportunité",
            "description": str(item.get("title") or "Sans titre")[:4000],
            "url": url,
            "color": 3447003,
            "fields": [
                {"name": "Plateforme", "value": str(item.get("platform") or "LinkedIn Candidat")[:100], "inline": True}
            ],
            "footer": {"text": "I-LEAD Mission Scraper"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }

    for attempt in range(3):
        try:
            async with session.post(WEBHOOK, json=payload) as resp:
                if resp.status in (200, 204):
                    return True

                body = await resp.text()
                print(f"[DISCORD ERROR] HTTP {resp.status} : {body[:200]}")

                if resp.status == 429:
                    try:
                        data = await resp.json()
                        delay = float(data.get("retry_after", 2))
                    except Exception:
                        delay = 2
                    await asyncio.sleep(min(delay, 20))
                    continue

                if resp.status >= 500:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue

                return False

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[DISCORD EXCEPTION] tentative {attempt+1}/3 : {e}")
            if attempt == 2:
                return False
            await asyncio.sleep(2 * (attempt + 1))

    return False


async def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Récupérer les 48 candidats
    rows = conn.execute("""
        SELECT id, platform, title, url, notified
        FROM missions
        WHERE source = 'linkedin_candidate'
        ORDER BY first_seen DESC
    """).fetchall()

    print(f"\n=== {len(rows)} CANDIDATS À NOTIFIER ===\n")

    if not rows:
        print("Aucun candidat en base.")
        conn.close()
        return

    # Remettre notified=0 pour tous les candidats
    conn.execute("UPDATE missions SET notified=0 WHERE source='linkedin_candidate'")
    conn.commit()

    async with aiohttp.ClientSession() as session:
        sent = failed = 0
        for i, r in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {r['title'][:80]}")
            ok = await discord_notify(session, dict(r))
            if ok:
                conn.execute("UPDATE missions SET notified=1 WHERE id=?", (r["id"],))
                conn.commit()
                sent += 1
                print(f"    ✓ Envoyé")
            else:
                failed += 1
                print(f"    ✗ Échec")

            # ⚠️ Délai entre chaque notification (anti-429)
            await asyncio.sleep(0.6)

        print(f"\n=== RÉSULTAT ===")
        print(f"Envoyés : {sent}")
        print(f"Échecs  : {failed}")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())