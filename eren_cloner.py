from pyrogram import Client
from pyrogram.errors import FloodWait
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


def forward_old_messages(fresh=False, skip_count=0):
    for acc in accounts:
        acc["client"].start()

    try:
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

        remaining = sum(len(ids) for _, ids in grouped[start_index:])

        with tqdm(total=total, initial=skipped_msgs, desc="Cloning", unit="msg") as pbar:
            for group_id, msg_ids in grouped[start_index:]:
                if group_id:
                    result = send_with_retry(
                        "copy_media_group",
                        destination_channel,
                        source_channel,
                        msg_ids[0],
                    )
                    if isinstance(result, Exception):
                        print(f"\n  [skip] album {group_id}: {result}")
                    else:
                        save_progress(max(msg_ids))
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
