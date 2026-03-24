# Telegram Channel Cloner

Clone messages from one Telegram channel to another, preserving media, captions, albums, and formatting. Supports two accounts to work around Telegram's send rate limits.

## Features

- Clones text messages, photos, videos, documents, audio, animations, voice notes, stickers, and video notes
- Preserves captions, caption formatting (entities), and media albums
- **Dual-account rate limit bypass** — Account 2 takes over when Account 1 is flood-waited, then hands back automatically when Account 1 is free
- **Smart send strategy** — Account 1 sends using file IDs directly (fast); Account 2 downloads via Account 1 then re-uploads (avoids MEDIA_EMPTY errors)
- Resume support — progress is saved after each message; interrupted runs continue from where they left off
- `--fresh` flag to start over, `--skip N` to skip the first N messages manually

## Requirements

- Python 3.8+
- Pyrogram
- TgCrypto (recommended for better performance)
- python-dotenv
- tqdm

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Er3n-Yeager/Telegram-channel-cloner.git
   cd Telegram-channel-cloner
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install pyrogram tgcrypto tqdm python-dotenv
   ```

4. **Configure credentials**

   Create a `.env` file in the project root:
   ```env
   API_ID=12345678
   API_HASH=your_api_hash_here
   PHONE_NUMBER_1=+1234567890
   PHONE_NUMBER_2=+0987654321
   SOURCE_CHANNEL=-1001234567890
   DESTINATION_CHANNEL=-1009876543210
   ```

   - Get `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)
   - Channel IDs are negative integers (e.g. `-1001234567890`). You can find them via [@userinfobot](https://t.me/userinfobot) or similar bots
   - Only Account 1 needs to be a member of the **source** channel
   - Both accounts must be **admins** of the **destination** channel

## Usage

```bash
# Clone all messages (resumes automatically if previously interrupted)
python eren_cloner.py

# Start fresh, ignoring any saved progress
python eren_cloner.py --fresh

# Skip the first 500 messages manually
python eren_cloner.py --skip 500
```

## How the dual-account strategy works

| Condition | Account used | Method |
|-----------|-------------|--------|
| Account 1 available | Account 1 | Send via file ID (instant, no download) |
| Account 1 rate-limited | Account 2 | Download via Account 1, re-upload |
| Both rate-limited | — | Wait for the soonest account to become free |

As soon as Account 1's flood wait expires, the next message automatically goes back through the fast path.

## Notes

- Session files (`session_1.session`, `session_2.session`) are created on first run; you will be prompted to enter the OTP for each phone number
- Progress is saved to `progress.txt` after each successful send and cleared on completion
- Service messages (pinned, joined, etc.) are skipped automatically
