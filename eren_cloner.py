from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
)
import time
import os
import tempfile
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]
phone_number = os.environ["PHONE_NUMBER"]

source_channel = int(os.environ["SOURCE_CHANNEL"])
destination_channel = int(os.environ["DESTINATION_CHANNEL"])

eren = Client("user_session", api_id, api_hash, phone_number=phone_number)


def build_input_media(message):
    """Build InputMedia from a (freshly fetched) message using its file_id."""
    caption = message.caption or ""
    entities = message.caption_entities

    if message.photo:
        return InputMediaPhoto(message.photo.file_id, caption=caption, caption_entities=entities)
    elif message.video:
        return InputMediaVideo(message.video.file_id, caption=caption, caption_entities=entities)
    elif message.document:
        return InputMediaDocument(message.document.file_id, caption=caption, caption_entities=entities)
    elif message.audio:
        return InputMediaAudio(message.audio.file_id, caption=caption, caption_entities=entities)
    return None


def download_and_build_input_media(client, message, temp_dir):
    """Fallback: download to disk when file references are still expired after a re-fetch."""
    caption = message.caption or ""
    entities = message.caption_entities

    try:
        file_path = client.download_media(
            message,
            file_name=os.path.join(temp_dir, f"media_{message.id}")
        )
        if not file_path:
            return None

        if message.photo:
            return InputMediaPhoto(file_path, caption=caption, caption_entities=entities)
        elif message.video:
            return InputMediaVideo(file_path, caption=caption, caption_entities=entities)
        elif message.document:
            return InputMediaDocument(file_path, caption=caption, caption_entities=entities)
        elif message.audio:
            return InputMediaAudio(file_path, caption=caption, caption_entities=entities)
    except Exception as e:
        print(f"\n  [skip] Download failed for msg {message.id}: {e}")
        return None

    return None


def send_with_retry(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except FloodWait as e:
            print(f"\n  Rate limited. Waiting {e.value}s...")
            time.sleep(e.value)
        except Exception as e:
            return e  # return the exception so callers can decide what to do


def send_single(client, msg, temp_dir):
    """Send a single (non-album) message. Tries copy_message first; if that
    fails due to a file-reference or restriction error, falls back to
    downloading the media and re-uploading it."""
    result = send_with_retry(client.copy_message, destination_channel, source_channel, msg.id)

    if isinstance(result, Exception):
        err = str(result)
        # Attempt download-and-resend fallback for media messages
        if any(m in err for m in ("FILE_REFERENCE", "MEDIA_EMPTY", "400")):
            media = download_and_build_input_media(client, msg, temp_dir)
            if media:
                # send_media_group with a single item is valid
                result2 = send_with_retry(client.send_media_group, destination_channel, [media])
                if isinstance(result2, Exception):
                    print(f"\n  [skip] msg {msg.id} fallback also failed: {result2}")
                try:
                    os.remove(media.media)
                except Exception:
                    pass
                return
        print(f"\n  [skip] msg {msg.id}: {err}")


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

        print("Fetching messages from source channel...")
        messages = list(eren.get_chat_history(source_chat.id, limit=None))
        messages.reverse()  # oldest first

        total = len(messages)
        print(f"Total messages: {total}")

        # Group consecutive messages that share a media_group_id (albums)
        grouped = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.media_group_id:
                group = [msg]
                j = i + 1
                while j < len(messages) and messages[j].media_group_id == msg.media_group_id:
                    group.append(messages[j])
                    j += 1
                grouped.append((msg.media_group_id, group))
                i = j
            else:
                grouped.append((None, [msg]))
                i += 1

        with tempfile.TemporaryDirectory() as temp_dir:
            with tqdm(total=total, desc="Cloning", unit="msg") as pbar:
                for group_id, group_msgs in grouped:
                    if group_id:
                        msg_ids = [m.id for m in group_msgs]

                        # Re-fetch the messages to get fresh file references —
                        # the ones from get_chat_history may have expired by now.
                        # This is a fast API call (no file transfer) and fixes
                        # FILE_REFERENCE_EXPIRED without downloading anything.
                        fresh_msgs = eren.get_messages(source_channel, msg_ids)
                        if not isinstance(fresh_msgs, list):
                            fresh_msgs = [fresh_msgs]

                        media_list = [build_input_media(m) for m in fresh_msgs]
                        media_list = [m for m in media_list if m is not None]

                        if media_list:
                            result = send_with_retry(eren.send_media_group, destination_channel, media_list)
                            if isinstance(result, Exception):
                                # References still expired — last resort: download and re-upload
                                if "FILE_REFERENCE" in str(result):
                                    media_list = []
                                    local_paths = []
                                    for m in fresh_msgs:
                                        item = download_and_build_input_media(eren, m, temp_dir)
                                        if item:
                                            media_list.append(item)
                                            local_paths.append(item.media)
                                    if media_list:
                                        result2 = send_with_retry(eren.send_media_group, destination_channel, media_list)
                                        if isinstance(result2, Exception):
                                            print(f"\n  [skip] album {group_id}: {result2}")
                                    for path in local_paths:
                                        try:
                                            os.remove(path)
                                        except Exception:
                                            pass
                                else:
                                    print(f"\n  [skip] album {group_id}: {result}")

                        pbar.update(len(group_msgs))
                    else:
                        msg = group_msgs[0]
                        # Skip service messages (joins, pins, etc.) — they cannot be copied
                        if msg.service:
                            pbar.update(1)
                            continue
                        send_single(eren, msg, temp_dir)
                        pbar.update(1)

                    time.sleep(0.5)


if __name__ == "__main__":
    forward_old_messages()


# Author : Eren
# Github : https://github.com/Er3n-Yeager
