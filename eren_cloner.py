from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
)
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


def build_input_media(message):
    """Convert a message to an InputMedia object for use in send_media_group."""
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


def send_with_retry(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except FloodWait as e:
            print(f"\nRate limited. Waiting {e.value} seconds...")
            time.sleep(e.value)
        except Exception as e:
            print(f"\nFailed: {e}")
            return None


def forward_old_messages():
    with eren:
        print("Finding channels...")
        source_chat = None
        dest_chat = None
        
        # Get all dialogs to find matching channels
        for dialog in eren.get_dialogs():
            chat_id = dialog.chat.id
            if chat_id == source_channel or chat_id == int(os.environ.get("SOURCE_CHANNEL", "0")):
                source_chat = dialog.chat
                print(f"Found source: {dialog.chat.title} (ID: {chat_id})")
            if chat_id == destination_channel or chat_id == int(os.environ.get("DESTINATION_CHANNEL", "0")):
                dest_chat = dialog.chat
                print(f"Found destination: {dialog.chat.title} (ID: {chat_id})")
        
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

        # Group messages by media_group_id, preserving order
        grouped = []  # list of (media_group_id or None, [messages])
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.media_group_id:
                # Collect all messages in this album
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

        with tqdm(total=total, desc="Cloning", unit="msg") as pbar:
            for group_id, group_msgs in grouped:
                if group_id:
                    # Send as a media group (album)
                    media_list = [build_input_media(m) for m in group_msgs]
                    media_list = [m for m in media_list if m is not None]
                    if media_list:
                        send_with_retry(eren.send_media_group, destination_channel, media_list)
                    pbar.update(len(group_msgs))
                else:
                    msg = group_msgs[0]
                    # copy_message preserves formatting, caption entities, stickers,
                    # animations, voice, audio, polls — everything — without "forwarded from" header
                    send_with_retry(eren.copy_message, destination_channel, source_channel, msg.id)
                    pbar.update(1)

                time.sleep(0.5)  # small delay between sends to avoid flood


if __name__ == "__main__":
    forward_old_messages()


# Author : Eren
# Github : https://github.com/Er3n-Yeager
