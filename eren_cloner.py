from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
import time
import os
import json
import mimetypes
import argparse
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

source_channel = int(os.environ["SOURCE_CHANNEL"])
destination_channel = int(os.environ["DESTINATION_CHANNEL"])

PROGRESS_FILE = "progress.txt"      # stores last successfully cloned message ID
INDEX_CACHE_FILE = "msg_index.json"  # cached list of (id, media_group_id) from source
SKIPPED_FILE = "skipped.json"        # all failed messages with error details

# Each account needs its own session file and phone number.
# Only Account 1 needs access to the source channel.
# Both accounts must be admins of the destination channel.
accounts = [
    {
        "client": Client("session_1", api_id, api_hash, phone_number=os.environ["PHONE_NUMBER_1"]),
        "flood_until": 0,           # epoch time when sending is free again
        "download_flood_until": 0,  # epoch time when downloading (auth.ExportAuthorization) is free again
        "name": "Account 1",
    },
    {
        "client": Client("session_2", api_id, api_hash, phone_number=os.environ["PHONE_NUMBER_2"]),
        "flood_until": 0,
        "download_flood_until": 0,
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


def load_index_cache():
    """Return cached [(id, media_group_id), ...] or None if no cache exists."""
    if os.path.exists(INDEX_CACHE_FILE):
        try:
            data = json.loads(open(INDEX_CACHE_FILE).read())
            return [tuple(item) for item in data]
        except Exception:
            pass
    return None


def save_index_cache(msg_index):
    open(INDEX_CACHE_FILE, "w").write(json.dumps(msg_index))


def clear_index_cache():
    if os.path.exists(INDEX_CACHE_FILE):
        os.remove(INDEX_CACHE_FILE)


def save_skipped(msg_ids, group_id, error):
    """Append a skipped entry to skipped.json immediately."""
    entry = {
        "msg_ids": msg_ids,
        "group_id": group_id,
        "error": str(error),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    records = []
    if os.path.exists(SKIPPED_FILE):
        try:
            records = json.loads(open(SKIPPED_FILE).read())
        except Exception:
            pass
    records.append(entry)
    open(SKIPPED_FILE, "w").write(json.dumps(records, indent=2))


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


def send_with_retry(reader, fast_fn, slow_fn=None, _attempts=3):
    """
    Send using whichever account is available, rotating on FloodWait.
    - If Account 1 (reader) is free: fast_fn(client) — uses file_id directly.
    - If only Account 2 is free: slow_fn(client) — downloads via reader first.
    - slow_fn=None means the same fn works for any account (e.g. text messages).
    - Non-FloodWait errors are retried up to _attempts times before skipping.
    - FloodWait from inside the slow path (e.g. auth.ExportAuthorization) is
      handled by marking Account 1 and retrying without consuming an attempt.
    """
    reader_acc = next(a for a in accounts if a["client"] is reader)
    last_exc = None
    remaining = _attempts
    while remaining > 0:
        acc = get_account()
        on_slow_path = slow_fn is not None and acc["client"] is not reader

        if on_slow_path:
            # Slow path downloads via reader — if reader's download is still
            # rate-limited (auth.ExportAuthorization), wait it out first.
            # Note: reader's send flood_until is tracked separately so a recovered
            # send limit can still use the fast path independently.
            wait = reader_acc["download_flood_until"] - time.time()
            if wait > 0:
                print(f"\n  {reader_acc['name']} download still limited ({wait:.0f}s), waiting...")
                time.sleep(wait)

        try:
            if not on_slow_path:
                return fast_fn(acc["client"])
            else:
                return slow_fn(acc["client"])
        except FloodWait as e:
            if on_slow_path:
                # FloodWait came from reader's download (e.g. auth.ExportAuthorization).
                # Track separately so the send flood_until stays accurate — if the
                # original send limit expires first, get_account() can return Account 1
                # for a fast-path retry without waiting for the download limit too.
                reader_acc["download_flood_until"] = time.time() + e.value
                print(f"\n  {reader_acc['name']} rate limited for {e.value}s (download) — will retry after wait")
            else:
                acc["flood_until"] = time.time() + e.value
                other = next((a for a in accounts if a is not acc), None)
                status = f"switching to {other['name']}" if other and other["flood_until"] <= time.time() else f"waiting {e.value}s"
                print(f"\n  {acc['name']} rate limited for {e.value}s — {status}")
            # FloodWait never consumes an attempt
        except Exception as e:
            last_exc = e
            remaining -= 1
            if remaining > 0:
                print(f"\n  Send failed ({e}), retrying in 3s...")
                time.sleep(3)
    return last_exc


def _extract_media(msg):
    """Return (media_type_str, file_id) or (None, None) for text-only messages."""
    for attr in ("photo", "video", "document", "audio", "animation", "voice", "sticker", "video_note"):
        media = getattr(msg, attr, None)
        if media:
            return attr, media.file_id
    return None, None


_MIME_EXT_FIXES = {".jpe": ".jpg", ".jpeg": ".jpg"}

def _media_filename(msg):
    """
    Return a filename (with extension) for the message's media.
    Pyrogram uses the BytesIO .name attribute to set the MIME type on upload —
    without it Telegram receives the file as application/octet-stream and may
    re-process or reject it.
    """
    for attr in ("document", "video", "audio", "animation", "voice", "sticker", "video_note"):
        media = getattr(msg, attr, None)
        if media:
            if getattr(media, "file_name", None):
                return media.file_name
            mime = getattr(media, "mime_type", None)
            if mime:
                ext = mimetypes.guess_extension(mime) or ""
                ext = _MIME_EXT_FIXES.get(ext, ext)
                return f"file{ext}"
    if getattr(msg, "photo", None):
        return "photo.jpg"
    return "file"


def _make_single_sender(media_type, src, caption, caption_entities,
                        width=None, height=None, duration=None, supports_streaming=None, thumb=None):
    """Return a lambda(client) that sends src to the destination channel."""
    if media_type == "photo":
        return lambda c: c.send_photo(destination_channel, src, caption=caption, caption_entities=caption_entities)
    elif media_type == "video":
        return lambda c: c.send_video(
            destination_channel, src,
            caption=caption, caption_entities=caption_entities,
            width=width, height=height, duration=duration,
            supports_streaming=supports_streaming,
            thumb=thumb,
        )
    elif media_type == "document":
        return lambda c: c.send_document(destination_channel, src, caption=caption, caption_entities=caption_entities)
    elif media_type == "audio":
        return lambda c: c.send_audio(destination_channel, src, caption=caption, caption_entities=caption_entities)
    elif media_type == "animation":
        return lambda c: c.send_animation(destination_channel, src, caption=caption, caption_entities=caption_entities)
    elif media_type == "voice":
        return lambda c: c.send_voice(destination_channel, src, caption=caption, caption_entities=caption_entities)
    elif media_type == "sticker":
        return lambda c: c.send_sticker(destination_channel, src)
    elif media_type == "video_note":
        return lambda c: c.send_video_note(destination_channel, src)
    else:
        return lambda c: c.send_message(destination_channel, caption, entities=caption_entities)


def send_single(reader, msg_id):
    """
    Fetch message from Account 1 (reader), then send its content to destination
    via whichever account is free. This way Account 2 never needs source access.
    Account 1 sends using file_id (fast). Account 2 downloads via reader first (no MEDIA_EMPTY).
    """
    msg = reader.get_messages(source_channel, msg_id)
    media_type, file_id = _extract_media(msg)
    caption = msg.caption or msg.text or ""
    caption_entities = msg.caption_entities or msg.entities or None

    video_kwargs = {}
    if media_type == "video" and msg.video:
        video_kwargs = dict(
            width=msg.video.width,
            height=msg.video.height,
            duration=msg.video.duration,
            supports_streaming=msg.video.supports_streaming,
        )

    fast = _make_single_sender(media_type, file_id, caption, caption_entities, **video_kwargs)

    if not media_type:
        # Text-only: same fn works for any account
        return send_with_retry(reader, fast)

    def slow(c):
        data = reader.download_media(file_id, in_memory=True)
        data.seek(0)
        data.name = _media_filename(msg)
        slow_vkw = dict(video_kwargs)
        if media_type == "video" and msg.video and msg.video.thumbs:
            td = reader.download_media(msg.video.thumbs[0].file_id, in_memory=True)
            td.seek(0)
            td.name = "thumb.jpg"
            slow_vkw["thumb"] = td
        return _make_single_sender(media_type, data, caption, caption_entities, **slow_vkw)(c)

    return send_with_retry(reader, fast, slow)


def _build_album_media(msgs, src_list, thumbs=None):
    """Build an InputMedia list from messages using pre-resolved sources (file_ids or BytesIO)."""
    if thumbs is None:
        thumbs = [None] * len(msgs)
    media_list = []
    for msg, src, thumb in zip(msgs, src_list, thumbs):
        media_type, _ = _extract_media(msg)
        caption = msg.caption or ""
        caption_entities = msg.caption_entities or None
        if media_type == "photo":
            media_list.append(InputMediaPhoto(src, caption=caption, caption_entities=caption_entities))
        elif media_type == "video":
            v = msg.video
            media_list.append(InputMediaVideo(
                src, caption=caption, caption_entities=caption_entities,
                width=v.width if v else None,
                height=v.height if v else None,
                duration=v.duration if v else None,
                supports_streaming=v.supports_streaming if v else None,
                thumb=thumb,
            ))
        elif media_type == "document":
            media_list.append(InputMediaDocument(src, caption=caption, caption_entities=caption_entities))
        elif media_type == "audio":
            media_list.append(InputMediaAudio(src, caption=caption, caption_entities=caption_entities))
    return media_list


def send_album(reader, first_msg_id):
    """
    Fetch a media group from Account 1 (reader), then send it as an album
    via whichever account is free.
    Account 1 sends using file_ids (fast). Account 2 downloads via reader first (no MEDIA_EMPTY).
    """
    msgs = reader.get_media_group(source_channel, first_msg_id)
    file_ids = [_extract_media(m)[1] for m in msgs]

    fast_media = _build_album_media(msgs, file_ids)
    if not fast_media:
        return Exception("album had no supported media")

    def slow(c):
        sources = []
        thumbs = []
        for m, fid in zip(msgs, file_ids):
            data = reader.download_media(fid, in_memory=True)
            data.seek(0)
            data.name = _media_filename(m)
            sources.append(data)
            thumb = None
            if m.video and m.video.thumbs:
                td = reader.download_media(m.video.thumbs[0].file_id, in_memory=True)
                td.seek(0)
                td.name = "thumb.jpg"
                thumb = td
            thumbs.append(thumb)
        slow_media = _build_album_media(msgs, sources, thumbs)
        if not slow_media:
            raise Exception("album had no supported media after download")
        return c.send_media_group(destination_channel, slow_media)

    return send_with_retry(reader, lambda c: c.send_media_group(destination_channel, fast_media), slow)


def forward_old_messages(fresh=False, skip_count=0, refresh_index=False):
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
            clear_index_cache()
            print("Starting fresh.")
        elif skip_count:
            clear_progress()  # --skip takes precedence over saved progress
        else:
            saved = load_progress()
            if saved:
                resume_after_id = saved
                print(f"Resuming after message ID {resume_after_id}  (use --fresh to start over, --skip N to override)")

        if refresh_index:
            clear_index_cache()

        cached = load_index_cache()
        if cached:
            msg_index = cached
            print(f"Loaded message index from cache ({len(msg_index)} messages). Use --refresh-index to re-fetch.")
        else:
            print("Fetching message index from Telegram...")
            msg_index = []
            for msg in reader.get_chat_history(source_channel):
                if msg.service:
                    continue
                msg_index.append((msg.id, msg.media_group_id))
            msg_index.reverse()  # oldest first
            save_index_cache(msg_index)
            print(f"Fetched and cached {len(msg_index)} messages.")

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
                        save_skipped(msg_ids, group_id, result)
                    else:
                        save_progress(max(msg_ids))
                    pbar.update(len(msg_ids))
                else:
                    result = send_single(reader, msg_ids[0])
                    if isinstance(result, Exception):
                        print(f"\n  [skip] msg {msg_ids[0]}: {result}")
                        save_skipped(msg_ids, None, result)
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
    parser.add_argument("--refresh-index", action="store_true", help="Re-fetch the message index from Telegram (discards msg_index.json cache)")
    args = parser.parse_args()
    forward_old_messages(fresh=args.fresh, skip_count=args.skip, refresh_index=args.refresh_index)


# Author : Eren
# Github : https://github.com/Er3n-Yeager
