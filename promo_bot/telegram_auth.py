from __future__ import annotations

import getpass
from collections.abc import Callable
from typing import Any

from telethon.errors import SessionPasswordNeededError


def render_terminal_qr(login_url: str) -> None:
    """Render a transient Telegram login token without printing or persisting it."""

    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(login_url)
    qr.make(fit=True)
    print("Scan this QR code in Telegram: Settings > Devices > Link Desktop Device.")
    qr.print_ascii(invert=True)


async def authorize_with_qr(
    client: Any,
    *,
    render_qr: Callable[[str], None] = render_terminal_qr,
    password_reader: Callable[[str], str] = getpass.getpass,
) -> None:
    """Connect an existing session or authorize a new one without a phone number."""

    await client.connect()
    if await client.is_user_authorized():
        return

    qr_login = await client.qr_login()
    render_qr(qr_login.url)
    try:
        await qr_login.wait()
    except SessionPasswordNeededError:
        password = password_reader("Telegram two-factor password: ")
        await client.sign_in(password=password)
