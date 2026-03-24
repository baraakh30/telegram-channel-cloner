from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
import time
import os
import argparse
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

source_channel = int(os.environ["SOURCE_CHANNEL"])
destination_channel = int(os.environ["DESTINATION_CHANNEL"])

PROGRESS_FILE = "progress.txt"  # stores last successfully cloned message ID

# Each account needs its own session file and phone number.
# Only Account 1 needs access to the source channel.
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


def load_progress():
    """Return the last successfully cloned message ID, or None if no progress saved."""
    if os.path.exists(PROGRESS_FILE):
        try:
            return int(open(PROGRESS_FILE).read().strip())
        except Exception:
            pass
    return None


def save_progress(msg_id):
    open(PROGRESS_FILE, "w").write(str(msg_id))


def clear_progress():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


def get_account():
    """Return the account with the soonest available time, sleeping if both are limited."""
    now = time.time()
    free = [a for a in accounts if a["flood_until"] <= now]
    if free:
        return free[0]
    soonest = min(accounts, key=lambda a: a["flood_until"])
    wait = soonest["flood_until"] - now
    print(f"\n  Both accounts rate limited. Waiting {wait:.0f}s for {soonest['name']}...")
    time.sleep(wait)
    return soonest


def send_with_retry(fn):
    """
    Call fn(client) on whichever account is available, rotating on FloodWait.
    fn receives the Pyrogram Client and should return the result.
    Account 1 fetches from source; either account can send to destination.
    """
    while True:
        acc = get_account()
        try:
            return fn(acc["client"])
        except FloodWait as e:
            acc["flood_until"] = time.time() + e.value
            other = next((a for a in accounts if a is not acc), None)
            status = f"switching to {other['name']}" if other and other["flood_until"] <= time.time() else f"waiting {e.value}s"
            print(f"\n  {acc['name']} rate limited for {e.value}s — {status}")
        except Exception as e:
            return e


def _extract_media(msg):
    """Return (media_type_str, file_id) or (None, None) for text-only messages."""
    for attr in ("photo", "video", "document", "audio", "animation", "voice", "sticker", "video_note"):
        media = getattr(msg, attr, None)
        if media:
            return attr, media.file_id
    return None, None


def send_single(reader, msg_id):
    """
    Fetch message from Account 1 (reader), then send its content to destination
    via whichever account is free. This way Account 2 never needs source access.
    """
    msg = reader.get_messages(source_channel, msg_id)
    media_type, file_id = _extract_media(msg)
    caption = msg.caption or msg.text or ""
    caption_entities = msg.caption_entities or msg.entities or None

    if media_type == "photo":
        return send_with_retry(lambda c: c.send_photo(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "video":
        return send_with_retry(lambda c: c.send_video(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "document":
        return send_with_retry(lambda c: c.send_document(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "audio":
        return send_with_retry(lambda c: c.send_audio(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "animation":
        return send_with_retry(lambda c: c.send_animation(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "voice":
        return send_with_retry(lambda c: c.send_voice(destination_channel, file_id, caption=caption, caption_entities=caption_entities))
    elif media_type == "sticker":
        return send_with_retry(lambda c: c.send_sticker(destination_channel, file_id))
    elif media_type == "video_note":
        return send_with_retry(lambda c: c.send_video_note(destination_channel, file_id))
    else:
        # Text-only
        return send_with_retry(lambda c: c.send_message(destination_channel, caption, entities=caption_entities))


def send_album(reader, first_msg_id):
    """
    Fetch a media group from Account 1 (reader), then send it as an album
    via whichever account is free.
    """
    msgs = reader.get_media_group(source_channel, first_msg_id)
    media_list = []
    for msg in msgs:
        media_type, file_id = _extract_media(msg)
        caption = msg.caption or ""
        caption_entities = msg.caption_entities or None
        if media_type == "photo":
            media_list.append(InputMediaPhoto(file_id, caption=caption, caption_entities=caption_entities))
        elif media_type == "video":
            media_list.append(InputMediaVideo(file_id, caption=caption, caption_entities=caption_entities))
        elif media_type == "document":
            media_list.append(InputMediaDocument(file_id, caption=caption, caption_entities=caption_entities))
        elif media_type == "audio":
            media_list.append(InputMediaAudio(file_id, caption=caption, caption_entities=caption_entities))

    if not media_list:
        return Exception("album had no supported media")

    return send_with_retry(lambda c: c.send_media_group(destination_channel, media_list))


def forward_old_messages(fresh=False, skip_count=0):
    for acc in accounts:
        acc["client"].start()

    try:
        reader = accounts[0]["client"]  # only Account 1 can read the source

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

        # Resolve destination peer on every account so they can all send to it.
        # Without this, accounts that haven't joined the channel via get_dialogs
        # will get "Peer id invalid" when trying to send.
        print("Resolving destination peer on all accounts...")
        for acc in accounts:
            try:
                chat = acc["client"].get_chat(destination_channel)
                print(f"  {acc['name']}: destination resolved ({chat.title})")
            except Exception as e:
                print(f"  {acc['name']}: WARNING — could not resolve destination: {e}")

        # Resume logic
        resume_after_id = None
        if fresh:
            clear_progress()
            print("Starting fresh.")
        elif skip_count:
            clear_progress()  # --skip takes precedence over saved progress
        else:
            saved = load_progress()
            if saved:
                resume_after_id = saved
                print(f"Resuming after message ID {resume_after_id}  (use --fresh to start over, --skip N to override)")

        print("Fetching message list...")
        msg_index = []
        for msg in reader.get_chat_history(source_channel):
            if msg.service:
                continue
            msg_index.append((msg.id, msg.media_group_id))

        msg_index.reverse()  # oldest first
        total = len(msg_index)
        print(f"Total messages: {total}")

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

        # Find where to resume
        start_index = 0
        skipped_msgs = 0
        if resume_after_id:
            # Resume by message ID (from saved progress file)
            for idx, (gid, ids) in enumerate(grouped):
                if max(ids) <= resume_after_id:
                    skipped_msgs += len(ids)
                    start_index = idx + 1
                else:
                    break
            print(f"Skipping {skipped_msgs} already-cloned messages, continuing from group index {start_index}.")
        elif skip_count:
            # Resume by message count (manual --skip argument)
            for idx, (gid, ids) in enumerate(grouped):
                if skipped_msgs + len(ids) <= skip_count:
                    skipped_msgs += len(ids)
                    start_index = idx + 1
                else:
                    break
            print(f"Skipping first {skipped_msgs} messages (--skip {skip_count}).")

        with tqdm(total=total, initial=skipped_msgs, desc="Cloning", unit="msg") as pbar:
            for group_id, msg_ids in grouped[start_index:]:
                if group_id:
                    result = send_album(reader, msg_ids[0])
                    if isinstance(result, Exception):
                        print(f"\n  [skip] album {group_id}: {result}")
                    else:
                        save_progress(max(msg_ids))
                    pbar.update(len(msg_ids))
                else:
                    result = send_single(reader, msg_ids[0])
                    if isinstance(result, Exception):
                        print(f"\n  [skip] msg {msg_ids[0]}: {result}")
                    else:
                        save_progress(msg_ids[0])
                    pbar.update(1)

                time.sleep(0.3)

        clear_progress()
        print("Done! All messages cloned.")

    finally:
        for acc in accounts:
            acc["client"].stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="Ignore saved progress and start from the beginning")
    parser.add_argument("--skip", type=int, default=0, metavar="N", help="Skip the first N messages (for manual resume)")
    args = parser.parse_args()
    forward_old_messages(fresh=args.fresh, skip_count=args.skip)


# Author : Eren
# Github : https://github.com/Er3n-Yeager
