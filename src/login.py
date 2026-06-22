"""
First-time Telegram login via QR code + 2FA.
Creates <TG_SESSION>.session in the working directory.

Run once before starting the service:
    python -m src.login
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

import qrcode
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded
from pyrogram.qrlogin import QRLogin

from src.config import load_config


async def _do_login() -> None:
    config = load_config()
    session_file = f"{config.env.tg_session}.session"

    if os.path.exists(session_file):
        print(f"Session already exists: {session_file}")
        print("Delete it and re-run if you need to re-authenticate.")
        return

    app = Client(
        name=config.env.tg_session,
        api_id=config.env.tg_api_id,
        api_hash=config.env.tg_api_hash,
    )

    await app.connect()

    qr = QRLogin(app, except_ids=[])
    await qr.recreate()

    print("Open Telegram → Settings → Devices → Link Desktop Device")
    print("Then scan the QR code below:\n")

    while True:
        qrc = qrcode.QRCode()
        qrc.add_data(qr.url)
        qrc.print_ascii(invert=True)
        print("\nWaiting up to 30 s for scan...")
        try:
            await qr.wait(timeout=30)
            break
        except asyncio.TimeoutError:
            print("Expired — regenerating...\n")
            await qr.recreate()
        except SessionPasswordNeeded:
            password = getpass.getpass("2FA cloud password: ")
            await app.check_password(password)
            break

    me = await app.get_me()
    print(f"\nLogin successful.")
    print(f"Account : {me.first_name} (id={me.id})")
    print(f"Session : {session_file}")
    print(f"\nRun: chmod 600 {session_file}")

    await app.disconnect()


def main() -> None:
    try:
        asyncio.run(_do_login())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
