
from pathlib import Path
from bs4 import BeautifulSoup
import json

for file in Path("debug").glob("*.html"):
    print("\n" + "=" * 65)
    print("FICHIER :", file.name)

    html = file.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    print("Taille HTML :", len(html))
    print("Titre :", soup.title.get_text(strip=True) if soup.title else "Absent")

    print("\nTEXTE DE LA PAGE :")
    print(soup.get_text(" ", strip=True)[:2500])

    print("\nLIENS :")
    for a in soup.select("a[href]")[:40]:
        print("-", a.get_text(" ", strip=True)[:100], "=>", a.get("href"))

    print("\nFICHIERS JAVASCRIPT :")
    for script in soup.select("script[src]")[:20]:
        print("-", script.get("src"))

    print("\nDONNÉES JSON INTÉGRÉES :")
    for script in soup.select('script[type="application/json"]')[:5]:
        print(script.get_text()[:500])
