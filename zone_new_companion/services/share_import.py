"""Import accounts shared as a base64 string.

Accounts usually get passed around as a base64 string that decodes to a paste
link (paste.sh, pastebin, ...). ``decode_share_string`` turns the string into
text, ``fetch_shared_text`` downloads the paste behind a link, and
``extract_accounts`` picks the checkable account lines out of either.

paste.sh pastes are encrypted in the browser: the server only returns
ciphertext and the key is the ``#...`` part of the link, so they are decrypted
here the same way the site's own JavaScript does it.

This module is intentionally Qt-free so it stays easy to test in isolation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import unicodedata
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from zone_new_companion.services.bulk_checker import parse_bulk_line, split_bulk_entries

FETCH_TIMEOUT = (6.0, 20.0)
# Pastes with account lists are a few kilobytes; anything far bigger isn't one.
MAX_FETCH_BYTES = 5 * 1024 * 1024

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BASE64_CHARS = re.compile(r"^[A-Za-z0-9+/_=-]+$")


def decode_share_string(text: str) -> str:
    """Decode a pasted base64 string (standard or URL-safe, padding optional).

    Raises ``ValueError`` when the text isn't base64 or doesn't decode to text.
    """
    compact = "".join(text.split())
    if not compact or not _BASE64_CHARS.match(compact):
        raise ValueError("This is not a base64 string.")
    compact = compact.rstrip("=")
    compact += "=" * (-len(compact) % 4)
    try:
        data = base64.b64decode(compact.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("This is not a valid base64 string.") from exc
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The base64 string doesn't contain text.") from exc
    if not decoded.strip():
        raise ValueError("The base64 string is empty.")
    return decoded


def find_urls(text: str) -> list[str]:
    """Links in the text, in order and without duplicates."""
    urls: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;)]}")
        if url not in urls:
            urls.append(url)
    return urls


def extract_accounts(text: str) -> list[str]:
    """Checkable account entries (Xtream URLs, built from account blocks).

    Shared lists often use styled letters ("𝙐𝙨𝙚𝙧 : ...", "𝐌𝟑𝐔 : ..."); NFKC turns
    them back into plain ones so the account blocks are recognised. Lines that
    aren't accounts (titles, notes) are dropped instead of showing up as failed.
    """
    lines = unicodedata.normalize("NFKC", text).splitlines()
    entries: list[str] = []
    for entry in split_bulk_entries(lines):
        try:
            parse_bulk_line(1, entry)
        except ValueError:
            continue
        if entry not in entries:
            entries.append(entry)
    return entries


def fetch_shared_text(url: str, session: requests.Session | None = None) -> str:
    """Download the plain text behind a paste link.

    paste.sh pastes are decrypted, pastebin.com links read their raw view;
    anything else is downloaded as is.
    """
    session = session or requests.Session()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("paste.sh"):
        return _fetch_paste_sh(url, session)
    if host in ("pastebin.com", "www.pastebin.com"):
        paste_id = parsed.path.strip("/").split("/")[-1]
        if paste_id and not parsed.path.startswith("/raw/"):
            url = f"https://pastebin.com/raw/{paste_id}"
    response = _get(session, url)
    return response.content.decode(response.encoding or "utf-8", errors="replace")


def _get(session: requests.Session, url: str) -> requests.Response:
    try:
        response = session.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        raise ValueError(f"Could not download {url}: {exc}") from exc
    if response.status_code == 404:
        raise ValueError("The paste doesn't exist (anymore).")
    if response.status_code >= 400:
        raise ValueError(f"The server answered {response.status_code} for {url}.")
    if len(response.content) > MAX_FETCH_BYTES:
        raise ValueError("The download is too big to be an account list.")
    return response


def _fetch_paste_sh(url: str, session: requests.Session) -> str:
    """Download and decrypt a paste.sh paste (formats v1 to v3)."""
    parsed = urlparse(url)
    paste_id = parsed.path.strip("/")
    # The site drops characters that sneak into copied keys (a trailing ".").
    client_key = re.sub(r"[^A-Za-z0-9_-]", "", parsed.fragment.split(":")[0])
    if not paste_id:
        raise ValueError("The paste.sh link has no paste id.")
    if not client_key and not paste_id.startswith("p"):
        raise ValueError("The paste.sh link is missing its key (the #... part at the end).")

    response = _get(session, f"https://paste.sh/{paste_id}.txt")
    version = response.headers.get("Content-Type", "").rsplit("-", 1)[-1].strip()
    server_key = response.headers.get("X-Server-Key", "")
    auth = response.headers.get("ETag", "").strip('"').removeprefix("W/").strip('"')
    password = (paste_id + server_key + client_key + "https://paste.sh").encode()

    try:
        blob = base64.b64decode("".join(response.text.split()))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("paste.sh returned something that isn't a paste.") from exc
    if auth:
        inner = hmac.new(b"auth key", password, hashlib.sha512).digest()
        mac = base64.b64encode(hmac.new(inner, blob, hashlib.sha512).digest()).decode().rstrip("=")
        if mac != auth:
            raise ValueError("Could not decrypt the paste: the key in the link is wrong or incomplete.")
    if blob[:8] != b"Salted__" or len(blob) < 32:
        raise ValueError("paste.sh returned something that isn't an encrypted paste.")

    salt, ciphertext = blob[8:16], blob[16:]
    if version in ("v2", "v3"):
        # openssl enc -aes-256-cbc -md sha512 -pbkdf2 -iter 1
        key_iv = hashlib.pbkdf2_hmac("sha512", password, salt, 1, 48)
    else:
        # openssl enc -aes-256-cbc -md sha512 (EVP_BytesToKey)
        key_iv = hashlib.sha512(password + salt).digest()
    decryptor = Cipher(algorithms.AES(key_iv[:32]), modes.CBC(key_iv[32:48])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("Could not decrypt the paste: the key in the link is wrong or incomplete.")
    text = padded[:-pad].decode("utf-8", errors="replace")
    if version == "v3":
        # "Subject: ..." header, a blank line, then the paste.
        _header, _sep, text = text.partition("\n\n")
    return text
