from pyrogram import Client
from pyrogram.errors import FloodWait
import time
import os
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

source_channel = int(os.environ["SOURCE_CHANNEL"])
destination_channel = int(os.environ["DESTINATION_CHANNEL"])

# Each account needs its own session file and phone number.
# Add PHONE_NUMBER_2 to .env for the second account.
# Both accounts must be admins of the destination channel.
accounts = [
    {
        "client": Client("session_1", api_id, api_hash, phone_number=os.environ["PHONE_NUMBER_1"]),
        "flood_until": 0,  # epoch time when this account is free again
        "name": "Account 1",
    },
    {
        "client": Client("session_2", api_id, api_hash, phone_number=os.environ["PHONE_NUMBER_2"]),
        "flood_until": 0,
        "name": "Account 2",
    },
]


def get_account():
    """Return the account with the soonest available time, sleeping if both are limited."""
    now = time.time()
    free = [a for a in accounts if a["flood_until"] <= now]
    if free:
        return free[0]
    # Both limited — sleep until the one with the shortest wait recovers
    soonest = min(accounts, key=lambda a: a["flood_until"])
    wait = soonest["flood_until"] - now
    print(f"\n  Both accounts rate limited. Waiting {wait:.0f}s for {soonest['name']}...")
    time.sleep(wait)
    return soonest


def send_with_retry(method_name, *args, **kwargs):
    """Call method_name on whichever account is available, rotating on FloodWait."""
    while True:
        acc = get_account()
        method = getattr(acc["client"], method_name)
        try:
            return method(*args, **kwargs)
        except FloodWait as e:
            acc["flood_until"] = time.time() + e.value
            other = next((a for a in accounts if a is not acc), None)
            status = f"switching to {other['name']}" if other and other["flood_until"] <= time.time() else f"waiting {e.value}s"
            print(f"\n  {acc['name']} rate limited for {e.value}s — {status}")
        except Exception as e:
            return e


def forward_old_messages():
    # Start all sessions (triggers auth flow for new sessions)
    for acc in accounts:
        acc["client"].start()

    try:
        # Use first account to fetch message list (read-only, no rate limit risk)
        reader = accounts[0]["client"]

        print("Finding channels...")
        source_found = dest_found = False
        for dialog in reader.get_dialogs():
            cid = dialog.chat.id
            if cid == source_channel:
                print(f"  Source:      {dialog.chat.title} (ID: {cid})")
                source_found = True
            if cid == destination_channel:
                print(f"  Destination: {dialog.chat.title} (ID: {cid})")
                dest_found = True
            if source_found and dest_found:
                break

        if not source_found:
            print("Source channel not found in your chats")
            return
        if not dest_found:
            print("Destination channel not found in your chats")
            return

        # Collect only (id, media_group_id) — no heavy message objects kept in memory
        print("Fetching message list...")
        msg_index = []
        for msg in reader.get_chat_history(source_channel):
            if msg.service:
                continue
            msg_index.append((msg.id, msg.media_group_id))

        msg_index.reverse()  # oldest first
        total = len(msg_index)
        print(f"Total messages to clone: {total}")

        # Group consecutive messages that share a media_group_id
        grouped = []
        i = 0
        while i < len(msg_index):
            mid, gid = msg_index[i]
            if gid:
                group_ids = [mid]
                j = i + 1
                while j < len(msg_index) and msg_index[j][1] == gid:
                    group_ids.append(msg_index[j][0])
                    j += 1
                grouped.append((gid, group_ids))
                i = j
            else:
                grouped.append((None, [mid]))
                i += 1

        with tqdm(total=total, desc="Cloning", unit="msg") as pbar:
            for group_id, msg_ids in grouped:
                if group_id:
                    result = send_with_retry(
                        "copy_media_group",
                        destination_channel,
                        source_channel,
                        msg_ids[0],
                    )
                    if isinstance(result, Exception):
                        print(f"\n  [skip] album {group_id}: {result}")
                    pbar.update(len(msg_ids))
                else:
                    result = send_with_retry(
                        "copy_message",
                        destination_channel,
                        source_channel,
                        msg_ids[0],
                    )
                    if isinstance(result, Exception):
                        print(f"\n  [skip] msg {msg_ids[0]}: {result}")
                    pbar.update(1)

                time.sleep(0.3)

    finally:
        for acc in accounts:
            acc["client"].stop()


if __name__ == "__main__":
    forward_old_messages()


# Author : Eren
# Github : https://github.com/Er3n-Yeager
