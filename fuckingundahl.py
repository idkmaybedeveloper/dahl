# https://nx.kroot.sh/lain/dahl_chair -> ???
# TODO: public release possibly??
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import uuid
from pathlib import Path
from dataclasses import dataclass
from enum import IntEnum

import httpx
from telethon import TelegramClient
from telethon.tl.functions.messages import StartBotRequest


class DahlStatus(IntEnum):
    NOT_FOUND =  0     # not using dahl
    OFFLINE   =  1     # no devices
    ONLINE    =  2     # user exist
    ERROR     = -1     # req err


@dataclass
class LookupResult:
    user_id: int
    status: DahlStatus
    error: str | None = None

    def __str__(self) -> str:
        status_map = {
            DahlStatus.NOT_FOUND: "not in dahl",
            DahlStatus.OFFLINE:   "in dahl (offline)",
            DahlStatus.ONLINE:    "in dahl (online)",
            DahlStatus.ERROR:    f"error: {self.error}",
        }
        return f"[{self.user_id}] {status_map[self.status]}"


class DahlLookup:
    API_BASE = "https://api.telega.info/v1/"
    AUTH_BOT = "dahl_auth_bot"
    HEADERS = {
               "User-Agent": "DAHL-Mobile-App",
               "X-Platform": "Android",
               "X-Version": "2.4.0",
    }

    def __init__(self, api_id: int, api_hash: str, session_name: str = "dahl_session"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.tokens_file = Path("dahl_tokens.json")

        self.tg: TelegramClient | None = None
        self.http: httpx.AsyncClient | None = None
        self.me = None

    @staticmethod
    def _compute_key_id(auth_key: bytes) -> str:
        # compute auth_key_id: hex of reversed last 8 bytes of SHA1(auth_key)
        h = hashlib.sha1(auth_key).digest()
        return h[-8:][::-1].hex()

    def _load_tokens(self) -> dict:
        try:
            return json.loads(self.tokens_file.read_text())
        except Exception:
            return {}

    def _save_tokens(self, tokens: dict) -> None:
        self.tokens_file.write_text(json.dumps(tokens, indent=2))

    async def connect(self) -> None:
        self.tg = TelegramClient(self.session_name, self.api_id, self.api_hash)
        self.http = httpx.AsyncClient(headers=self.HEADERS, timeout=15.0)

        await self.tg.start()
        self.me = await self.tg.get_me()
        print(f"[*] logged in as {self.me.first_name} (id={self.me.id})")

        await self._auth_dahl()

    async def _auth_dahl(self, force_fresh: bool = False) -> None:
        saved = self._load_tokens()

        # try refresh token first (unless forced fresh)
        if not force_fresh and (refresh := saved.get("refresh_token")):
            r = await self.http.post(
                f"{self.API_BASE}auth/token",
                json={"token": refresh}
            )
            if r.status_code == 200:
                data = r.json()
                self.http.headers["Authorization"] = f"Bearer {data['access_token']}"
                self._save_tokens({
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", refresh),
                })
                print("[*] authenticated via refresh token")
                await self._register_account()
                return
            else:
                print(f"[*] refresh failed ({r.status_code}), doing fresh auth...")

        # fresh auth via dahl_auth_bot
        print("[*] starting fresh authentication...")
        key_id = self._compute_key_id(self.tg.session.auth_key.key)
        print(f"[*] key_id: {key_id}")

        bot = await self.tg.get_entity(self.AUTH_BOT)

        await self.tg(StartBotRequest(
            bot=bot,
            peer=bot,
            start_param=key_id,
            random_id=random.randint(0, 2**63)
        ))

        await asyncio.sleep(3)

        r = await self.http.post(
            f"{self.API_BASE}auth",
            json={"auth_key_id": key_id, "user_id": self.me.id}
        )
        print(f"[*] auth response: {r.status_code}")

        if r.status_code == 200:
            data = r.json()
            self.http.headers["Authorization"] = f"Bearer {data['access_token']}"
            self._save_tokens({
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
            })
            print("[*] authenticated!")

            # register account (required for calls)
            await self._register_account()
            return

        raise RuntimeError(f"auth failed: {r.status_code} {r.text[:100]}")

    async def _register_account(self) -> None:
        # some kind of user info
        body = {
            "username": "",
            "first_name": "",
            "last_name": "",
            "phone": "",
            "app_version": "2.4.0",
            "platform": "android",
            "device_model": "SDK",
            "system_version": "30",
            "dc_version": 5,
        }
        r = await self.http.post(f"{self.API_BASE}api/account", json=body)
        print(f"[*] register account: {r.status_code}")

    async def lookup(self, user_id: int, _retried: bool = False) -> LookupResult:
        body = {
            "chat_id": self.me.id,
            "recipient_id": user_id,
            "conversation_id": str(uuid.uuid4()),
            "is_video": False,
        }

        r = await self.http.post(
            f"{self.API_BASE}api/calls/create",
            params={"type": "p2p"},
            json=body
        )

        # handle 401 - reauth and retry once
        if r.status_code == 401 and not _retried:
            await self._auth_dahl(force_fresh=True)
            return await self.lookup(user_id, _retried=True)

        msg = ""
        if r.headers.get("content-type", "").startswith("application/json"):
            msg = r.json().get("message", "")

        key = msg.lower().replace(" ", "_")
        call_id = None

        # parse response
        if r.status_code in (200, 201):
            call_id = (r.json().get("data") or {}).get("call_id")
            result = LookupResult(user_id, DahlStatus.ONLINE)

        elif r.status_code == 409:
            if "active_call_already_exists" in key:
                # end existing call and retry
                if m := re.search(r"[0-9a-f-]{36}", msg):
                    await self.http.post(
                        f"{self.API_BASE}api/calls/end",
                        json={"call_id": m.group(0)}
                    )
                    await asyncio.sleep(0.5)
                return await self.lookup(user_id)  # retry

            elif "recipient_has_no_active_devices" in key:
                result = LookupResult(user_id, DahlStatus.OFFLINE)
            else:
                result = LookupResult(user_id, DahlStatus.ERROR, msg)

        elif r.status_code in (400, 422) or key in ("recipient_not_found", "callee_app_version_too_old"):
            result = LookupResult(user_id, DahlStatus.NOT_FOUND)

        else:
            result = LookupResult(user_id, DahlStatus.ERROR, f"{r.status_code}: {msg}")

        # cleanup: end call if created
        if call_id:
            await self.http.post(
                f"{self.API_BASE}api/calls/end",
                json={"call_id": call_id}
            )

        return result

    async def lookup_many(self, user_ids: list[int], delay: float = 0.5) -> list[LookupResult]:
        results = []
        for uid in user_ids:
            result = await self.lookup(uid)
            results.append(result)
            print(result)
            if uid != user_ids[-1]:
                await asyncio.sleep(delay)
        return results

    async def close(self) -> None:
        if self.http:
            await self.http.aclose()
        if self.tg:
            await self.tg.disconnect()


# some kind of CLI
async def main():
    if len(sys.argv) < 2:
        print("Usage: dahl_lookup.py <user_id> [user_id2] ...")
        print("dahl_lookup.py --file users.txt")
        sys.exit(1)

    API_ID = os.getenv("TG_API_ID", "")
    API_HASH = os.getenv("TG_API_HASH", "")

    if not API_ID or not API_HASH:
        print("[!] set TG_API_ID and TG_API_HASH env vars")
        sys.exit(1)

    # parse user IDs
    if sys.argv[1] == "--file":
        user_ids = [int(line.strip()) for line in Path(sys.argv[2]).read_text().splitlines() if line.strip().isdigit()]
    else:
        user_ids = [int(arg) for arg in sys.argv[1:]]

    if not user_ids:
        print("[!] No valid user IDs provided")
        sys.exit(1)

    # run lookup
    lookup = DahlLookup(int(API_ID), API_HASH)
    try:
        await lookup.connect()
        await lookup.lookup_many(user_ids)
    finally:
        await lookup.close()


if __name__ == "__main__":
    asyncio.run(main())
