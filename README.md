# Zx CRC Extractor (Fixed & Updated)

Fully patched and optimized multi-platform batch and lecture extractor bot for PW, Classplus, and Appx.

## Key Updates & Fixes
- **Appx CDN Migration**: Automatic URL transformation to `https://appx-content-v2.classx.co.in/` without dead or expired signature parameters.
- **Concurrent PDF & Video Extraction**: Lectures with both video streams and PDF notes extract both concurrently without skipping.
- **AES-128 Decryption**: Decrypts DRM/encrypted streams and notes on the fly.
- **Python 3.10 / 3.12 / 3.14 Compatibility**: Event loop auto-initialization prevents Pyrogram startup crashes.

## Quick Start

### 1. Install Requirements
```bash
pip install -r requirements.txt
```

### 2. Configure Credentials
Edit `config.py`:
```python
api_id = 12345678                  # Your Telegram API ID
api_hash = "your_api_hash_here"    # Your Telegram API Hash
bot_token = "your_bot_token_here"  # From @BotFather
auth_users = []                    # Empty allows all users
```

### 3. Run Telegram Bot
```bash
python main.py
```

### 4. Standalone Link Fixer (Batch Offline)
To fix an existing dead links file:
```bash
python fix_urls.py
```
Outputs verified working links to `cleaned_working_links.txt`.
