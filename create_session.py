import asyncio
from linkedin_scraper import BrowserManager, wait_for_manual_login

async def create_session():
    async with BrowserManager(headless=False) as browser:
        await browser.page.goto("https://www.linkedin.com/login")
        print("=" * 60)
        print("CONNECTEZ-VOUS MANUELLEMENT À LINKEDIN")
        print("Vous avez 10 minutes pour vous connecter.")
        print("Si un CAPTCHA ou une 2FA apparaît, résolvez-le normalement.")
        print("=" * 60)
        
        # ⚠️ TIMEOUT AUGMENTÉ À 600 SECONDES (10 MINUTES)
        await wait_for_manual_login(browser.page, timeout=600)
        
        await browser.save_session("session.json")
        print("✓ Session sauvegardée dans session.json")

asyncio.run(create_session())