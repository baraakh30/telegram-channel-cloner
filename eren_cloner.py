from pyrogram import Client
from pyrogram.errors import FloodWait
import time
import os
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]
phone_number = os.environ["PHONE_NUMBER"]

source_channel = int(os.environ["SOURCE_CHANNEL"])
destination_channel = int(os.environ["DESTINATION_CHANNEL"])

eren = Client("user_session", api_id, api_hash, phone_number=phone_number)


def send_with_retry(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except FloodWait as e:
            print(f"\n  Rate limited. Waiting {e.value}s...")
            time.sleep(e.value)
        except Exception as e:
            return e


def forward_old_messages():
    with eren:
        print("Finding channels...")
        source_chat = None
        dest_chat = None

        for dialog in eren.get_dialogs():
            chat_id = dialog.chat.id
            if chat_id == source_channel:
                source_chat = dialog.chat
                print(f"  Source:      {dialog.chat.title} (ID: {chat_id})")
            if chat_id == destination_channel:
                dest_chat = dialog.chat
                print(f"  Destination: {dialog.chat.title} (ID: {chat_id})")

        if not source_chat:
            print("Source channel not found in your chats")
            return
        if not dest_chat:
            print("Destination channel not found in your chats")
            return

        # Collect only (id, media_group_id) — no heavy message objects kept in memory
        print("Fetching message list...")
        msg_index = []  # list of (id, media_group_id or None)
        for msg in eren.get_chat_history(source_chat.id):
            if msg.service:
                continue  # skip join/leave/pin service messages
            msg_index.append((msg.id, msg.media_group_id))

        msg_index.reverse()  # oldest first
        total = len(msg_index)
        print(f"Total messages to clone: {total}")

        # Group consecutive messages that share a media_group_id
        grouped = []  # list of (media_group_id or None, [msg_ids])
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
                    # copy_media_group: server-side album copy, no download needed,
                    # no FILE_REFERENCE_EXPIRED, no "Forwarded from" header.
                    # Pass the first message ID in the album.
                    result = send_with_retry(
                        eren.copy_media_group,
                        destination_channel,
                        source_channel,
                        msg_ids[0]
                    )
                    if isinstance(result, Exception):
                        print(f"\n  [skip] album {group_id}: {result}")
                    pbar.update(len(msg_ids))
                else:
                    result = send_with_retry(
                        eren.copy_message,
                        destination_channel,
                        source_channel,
                        msg_ids[0]
                    )
                    if isinstance(result, Exception):
                        print(f"\n  [skip] msg {msg_ids[0]}: {result}")
                    pbar.update(1)

                time.sleep(0.3)


if __name__ == "__main__":
    forward_old_messages()


# Author : Eren
# Github : https://github.com/Er3n-Yeager
