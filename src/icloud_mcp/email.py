"""IMAP/SMTP tools for email management."""

import imaplib
import smtplib
import email
import logging
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastmcp import Context
from imapclient import IMAPClient
from .auth import require_auth
from .config import config

# Configure minimal logging (only errors)
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Log errors to stderr
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)


def _get_imap_client(username: str, password: str) -> IMAPClient:
    """Create IMAP client (stateless)."""
    client = IMAPClient(config.IMAP_SERVER, port=config.IMAP_PORT, ssl=True, use_uid=True)
    client.login(username, password)
    return client


def _close_imap_client(client: IMAPClient) -> None:
    """Safely close IMAP client connection."""
    try:
        # Don't call logout() - it causes "file property has no setter" error in Python 3.14+
        # Just close the underlying socket
        if hasattr(client, '_imap') and hasattr(client._imap, 'sock'):
            client._imap.sock.close()
    except Exception as _e:
        pass  # Silently ignore errors on close


def _get_smtp_client(username: str, password: str) -> smtplib.SMTP:
    """Create SMTP client (stateless)."""
    client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    client.starttls()
    client.login(username, password)
    return client


def _decode_mime_header(header_value: str) -> str:
    """Decode MIME encoded email header."""
    if not header_value:
        return ""

    decoded_parts = decode_header(header_value)
    result = []

    for content, charset in decoded_parts:
        if isinstance(content, bytes):
            try:
                result.append(content.decode(charset or 'utf-8', errors='ignore'))
            except Exception as _e:
                result.append(content.decode('utf-8', errors='ignore'))
        else:
            result.append(str(content))

    return ' '.join(result)


async def list_folders(context: Context) -> List[Dict[str, Any]]:
    """
    List all email folders/mailboxes.

    Returns:
        List of folders with name and flags
    """
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

        folders = client.list_folders()

        result = []
        for flags, delimiter, name in folders:
            result.append({
                "name": name,
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in flags],
                "delimiter": delimiter
            })

        return result
    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def list_messages(
    context: Context,
    folder: str = "INBOX",
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    List messages in a folder.

    Args:
        folder: Folder name (default: INBOX)
        limit: Maximum number of messages to return
        unread_only: Only return unread messages

    Returns:
    """
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

        client.select_folder(folder)

        # Search for messages
        if unread_only:
            messages = client.search(['UNSEEN'])
        else:
            messages = client.search(['ALL'])


        # Get most recent messages
        message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
        message_ids.reverse()  # Most recent first

        if not message_ids:
            return []

        # Fetch full message body to extract body_text
        response = client.fetch(message_ids, [b'FLAGS', b'BODY.PEEK[]'])

        result = []
        for msg_id, data in response.items():
            try:
                # Try multiple possible keys for the message body
                raw_email = None
                for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
                    if key in data:
                        raw_email = data[key]
                        break

                if raw_email is None:
                    continue

                msg = email.message_from_bytes(raw_email)

                # Extract body_text
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/plain":
                            try:
                                body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                            except Exception as _e:
                                pass
                else:
                    try:
                        body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as _e:
                        pass

                result.append({
                    "id": str(msg_id),
                    "subject": _decode_mime_header(msg.get('Subject', '')),
                    "from": _decode_mime_header(msg.get('From', '')),
                    "to": _decode_mime_header(msg.get('To', '')),
                    "date": msg.get('Date', ''),
                    "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
                    "folder": folder,
                    "body_text": body_text
                })
            except Exception as _e:
                continue

        return result

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

async def get_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> Dict[str, Any]:
    """
    Get a specific message with full details.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        include_body: Include message body content
        full_html: Include full HTML body (default: False, only text body returned)

    Returns:
        Complete message details
    """
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        msg_id = int(message_id)

        # Use BODY.PEEK[] instead of RFC822 - more reliable with IMAPClient
        response = client.fetch([msg_id], [b'FLAGS', b'BODY.PEEK[]'])

        if msg_id not in response:
            raise ValueError(f"Message {message_id} not found")

        data = response[msg_id]

        # Try multiple possible keys for the message body
        raw_email = None
        for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
            if key in data:
                raw_email = data[key]
                break

        if raw_email is None:
            # Log available keys for debugging
            available_keys = list(data.keys())
            raise KeyError(f"Message body not found. Available keys: {available_keys}")

        msg = email.message_from_bytes(raw_email)

        result = {
            "id": message_id,
            "subject": _decode_mime_header(msg.get('Subject', '')),
            "from": _decode_mime_header(msg.get('From', '')),
            "to": _decode_mime_header(msg.get('To', '')),
            "cc": _decode_mime_header(msg.get('Cc', '')),
            "date": msg.get('Date', ''),
            "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
            "folder": folder
        }

        if include_body:
            # Extract body
            body_text = ""
            body_html = ""

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        try:
                            body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception as _e:
                            pass
                    elif content_type == "text/html" and full_html:
                        try:
                            body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception as _e:
                            pass
            else:
                try:
                    body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception as _e:
                    pass

            result["body_text"] = body_text
            if full_html:
                result["body_html"] = body_html

        return result

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def get_messages(
    context: Context,
    message_ids: List[str],
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> List[Dict[str, Any]]:
    """
    Get multiple messages at once.

    Args:
        message_ids: List of message IDs to fetch
        folder: Folder name (default: INBOX)
        include_body: Include message body content
        full_html: Include full HTML body (default: False, only text body returned)

    Returns:
        List of message details
    """
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        # Convert string IDs to integers
        msg_ids = [int(mid) for mid in message_ids]

        # Fetch all messages at once
        response = client.fetch(msg_ids, [b'FLAGS', b'BODY.PEEK[]'])

        results = []

        for msg_id in msg_ids:
            if msg_id not in response:
                # Skip missing messages
                continue

            data = response[msg_id]

            # Try multiple possible keys for the message body
            raw_email = None
            for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
                if key in data:
                    raw_email = data[key]
                    break

            if raw_email is None:
                # Skip messages without body
                continue

            msg = email.message_from_bytes(raw_email)

            result = {
                "id": str(msg_id),
                "subject": _decode_mime_header(msg.get('Subject', '')),
                "from": _decode_mime_header(msg.get('From', '')),
                "to": _decode_mime_header(msg.get('To', '')),
                "cc": _decode_mime_header(msg.get('Cc', '')),
                "date": msg.get('Date', ''),
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
                "folder": folder
            }

            if include_body:
                # Extract body
                body_text = ""
                body_html = ""

                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/plain":
                            try:
                                body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            except Exception as _e:
                                pass
                        elif content_type == "text/html" and full_html:
                            try:
                                body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            except Exception as _e:
                                pass
                else:
                    try:
                        body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as _e:
                        pass

                result["body_text"] = body_text
                if full_html:
                    result["body_html"] = body_html

            results.append(result)

        return results

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

def _build_imap_search_criteria(
    query: str,
    search_body: bool,
    since: Optional[str],
    before: Optional[str],
) -> list:
    """
    Build an IMAP SEARCH criteria list.

    Splits multi-word queries on whitespace and AND-combines per-word
    matches across Subject, From, and (optionally) Body fields. Each
    word becomes its own OR(subject, from[, body]) cluster, then all
    clusters are implicitly AND-ed together by IMAP.

    Date filters (since/before) are added at the top level as AND
    conditions. Date format: YYYY-MM-DD; IMAP expects DD-Mon-YYYY so
    we convert.
    """
    words = [w for w in query.split() if w]
    if not words:
        words = [""]  # match nothing meaningfully — but keep criteria valid

    criteria: list = []

    # Date filters first (AND-ed with the rest)
    for label, value in (("SINCE", since), ("BEFORE", before)):
        if not value:
            continue
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
            criteria.extend([label, dt.strftime("%d-%b-%Y")])
        except ValueError:
            # Silently skip invalid dates rather than erroring out
            pass

    # Per-word OR clusters
    for word in words:
        if search_body:
            # OR(SUBJECT, OR(FROM, BODY))
            cluster = ['OR', ['SUBJECT', word], ['OR', ['FROM', word], ['BODY', word]]]
        else:
            cluster = ['OR', ['SUBJECT', word], ['FROM', word]]
        criteria.extend(cluster)

    return criteria


async def search_messages(
    context: Context,
    query: str,
    folder: str = "INBOX",
    limit: int = 50,
    search_body: bool = False,
    since: Optional[str] = None,
    before: Optional[str] = None,
    folders: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Search messages with IMAP server-side search.

    Multi-word queries are AND-combined per word. Substring matches
    are server-side and literal (not fuzzy). Across one or more
    folders.

    Args:
        query: Search text. Multi-word queries are split on whitespace
            and each word must match independently in Subject/From
            (and Body if search_body=True).
        folder: Single folder name (default: INBOX). Ignored if
            `folders` is provided.
        limit: Maximum number of results per folder (default: 50).
        search_body: If True, also search the message body. Slower.
        since: Only include messages on/after this date (YYYY-MM-DD).
        before: Only include messages before this date (YYYY-MM-DD).
        folders: Optional list of folders to search across. If set,
            results are merged across folders, capped at `limit` per
            folder, sorted by date desc.

    Returns:
        List of matching messages.
    """
    username, password = require_auth(context)

    target_folders = folders if folders else [folder]
    all_results: List[Dict[str, Any]] = []

    for fld in target_folders:
        try:
            results = await _search_messages_in_folder(
                username, password, query, fld, limit,
                search_body, since, before,
            )
            all_results.extend(results)
        except Exception as e:
            logger.error(f"Search failed for folder {fld}: {e}")
            continue

    # Sort by date desc when searching multiple folders. Best-effort
    # parsing — fall back to insertion order on parse failure.
    if folders and len(target_folders) > 1:
        def _parse_date(d: str):
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(d)
            except Exception:
                return datetime.min
        all_results.sort(key=lambda m: _parse_date(m.get("date", "")), reverse=True)

    return all_results


async def _search_messages_in_folder(
    username: str,
    password: str,
    query: str,
    folder: str,
    limit: int,
    search_body: bool,
    since: Optional[str],
    before: Optional[str],
) -> List[Dict[str, Any]]:
    """Per-folder search; extracted so multi-folder loop can call it."""
    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)

        # Try server-side search with UTF-8 charset (RFC 2978)
        # This works with modern IMAP servers including iCloud
        try:
            criteria = _build_imap_search_criteria(query, search_body, since, before)
            messages = client.search(criteria, charset='UTF-8')

            message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
            message_ids.reverse()

            if not message_ids:
                return []

            # Fetch full message body to extract body_text
            response = client.fetch(message_ids, [b'FLAGS', b'BODY.PEEK[]'])

            result = []
            for msg_id, data in response.items():
                try:
                    # Try multiple possible keys for the message body
                    raw_email = None
                    for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
                        if key in data:
                            raw_email = data[key]
                            break

                    if raw_email is None:
                        continue

                    msg = email.message_from_bytes(raw_email)

                    # Extract body_text
                    body_text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                try:
                                    body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                    break
                                except Exception as _e:
                                    pass
                    else:
                        try:
                            body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception as _e:
                            pass

                    result.append({
                        "id": str(msg_id),
                        "subject": _decode_mime_header(msg.get('Subject', '')),
                        "from": _decode_mime_header(msg.get('From', '')),
                        "to": _decode_mime_header(msg.get('To', '')),
                        "date": msg.get('Date', ''),
                        "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
                        "folder": folder,
                        "body_text": body_text
                    })
                except Exception as _e:
                    continue

            return result

        except Exception as charset_error:
            # Fallback: If CHARSET UTF-8 is not supported by server,
            # fall back to local filtering (less efficient but always works)
            logger.error(f"Server-side UTF-8 search failed: {charset_error}. Falling back to local filtering.")

            # Fetch more messages to search through locally
            fetch_limit = max(limit * 10, 200)

            # Get all message IDs
            all_msg_ids = client.search(['ALL'])
            message_ids = list(all_msg_ids)[-fetch_limit:] if len(all_msg_ids) > fetch_limit else list(all_msg_ids)
            message_ids.reverse()

            if not message_ids:
                return []

            # Fetch full messages with body
            response = client.fetch(message_ids, [b'FLAGS', b'BODY.PEEK[]'])

            all_messages = []
            for msg_id, data in response.items():
                try:
                    # Try multiple possible keys for the message body
                    raw_email = None
                    for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
                        if key in data:
                            raw_email = data[key]
                            break

                    if raw_email is None:
                        continue

                    msg = email.message_from_bytes(raw_email)

                    # Extract body_text
                    body_text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                try:
                                    body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                    break
                                except Exception as _e:
                                    pass
                    else:
                        try:
                            body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception as _e:
                            pass

                    all_messages.append({
                        "id": str(msg_id),
                        "subject": _decode_mime_header(msg.get('Subject', '')),
                        "from": _decode_mime_header(msg.get('From', '')),
                        "to": _decode_mime_header(msg.get('To', '')),
                        "date": msg.get('Date', ''),
                        "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
                        "folder": folder,
                        "body_text": body_text
                    })
                except Exception as _e:
                    continue

            # Local fallback: multi-word AND, optional body, date filters
            words = [w.lower() for w in query.split() if w]

            def _matches(msg: Dict[str, Any]) -> bool:
                haystack = " ".join([
                    msg.get("subject", ""),
                    msg.get("from", ""),
                    msg.get("to", ""),
                ])
                if search_body:
                    haystack += " " + msg.get("body_text", "")
                haystack = haystack.lower()
                return all(w in haystack for w in words) if words else True

            def _within_date(msg: Dict[str, Any]) -> bool:
                if not (since or before):
                    return True
                try:
                    from email.utils import parsedate_to_datetime
                    d = parsedate_to_datetime(msg.get("date", ""))
                except Exception:
                    return True
                if since:
                    try:
                        if d < datetime.fromisoformat(since).replace(tzinfo=d.tzinfo):
                            return False
                    except Exception:
                        pass
                if before:
                    try:
                        if d >= datetime.fromisoformat(before).replace(tzinfo=d.tzinfo):
                            return False
                    except Exception:
                        pass
                return True

            filtered_messages = [
                msg for msg in all_messages if _matches(msg) and _within_date(msg)
            ]

            return filtered_messages[:limit]

    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

async def send_message(
    context: Context,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False
) -> Dict[str, str]:
    """
    Send an email message via SMTP.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body content
        cc: CC recipients (optional, comma-separated)
        bcc: BCC recipients (optional, comma-separated)
        html: Whether body is HTML (default: False)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    # Create message
    msg = MIMEMultipart('alternative') if html else MIMEText(body)

    msg['From'] = username
    msg['To'] = to
    msg['Subject'] = subject

    if cc:
        msg['Cc'] = cc
    if bcc:
        msg['Bcc'] = bcc

    if html:
        msg.attach(MIMEText(body, 'html'))

    # Send via SMTP
    with _get_smtp_client(username, password) as client:
        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(',')])
        if bcc:
            recipients.extend([addr.strip() for addr in bcc.split(',')])

        client.send_message(msg, from_addr=username, to_addrs=recipients)

    # Save copy to Sent folder via IMAP
    imap_client = None
    try:
        imap_client = _get_imap_client(username, password)

        # Add Date header if not present
        if 'Date' not in msg:
            from email.utils import formatdate
            msg['Date'] = formatdate(localtime=True)

        # Append message to Sent folder
        # Convert message to bytes
        msg_bytes = msg.as_bytes()

        # Try to append to Sent folder
        try:
            imap_client.append(config.SENT_FOLDER, msg_bytes, flags=['\\Seen'])
        except Exception as e:
            # If Sent Messages folder doesn't exist, try common alternatives
            for folder_name in ['Sent', 'Sent Items', config.SENT_FOLDER]:
                try:
                    imap_client.append(folder_name, msg_bytes, flags=['\\Seen'])
                    break
                except Exception:
                    continue
            else:
                # Log error but don't fail the send operation
                logger.error(f"Could not save to Sent folder: {e}")

    except Exception as e:
        # Log error but don't fail the send operation
        logger.error(f"Error saving to Sent folder: {e}")

    finally:
        if imap_client:
            _close_imap_client(imap_client)

    return {
        "status": "success",
        "message": f"Email sent to {to}"
    }


async def move_message(
    context: Context,
    message_id: str,
    from_folder: str,
    to_folder: str
) -> Dict[str, str]:
    """
    Move a message to another folder.

    Args:
        message_id: Message ID
        from_folder: Source folder
        to_folder: Destination folder

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(from_folder)
        msg_id = int(message_id)

        # Copy to destination
        client.copy([msg_id], to_folder)

        # Delete from source
        client.delete_messages([msg_id])
        client.expunge()

        return {
            "status": "success",
            "message": f"Message {message_id} moved from {from_folder} to {to_folder}"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

async def delete_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    permanent: bool = False
) -> Dict[str, str]:
    """
    Delete a message.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        permanent: Permanently delete (True) or move to trash (False)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(folder)
        msg_id = int(message_id)

        if permanent:
            # Permanent deletion
            client.delete_messages([msg_id])
            client.expunge()
            message = f"Message {message_id} permanently deleted"
        else:
            # Move to Trash
            try:
                client.copy([msg_id], 'Trash')
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} moved to Trash"
            except Exception as _e:
                # Fallback to permanent delete if Trash doesn't exist
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} deleted"

        return {
            "status": "success",
            "message": message
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

async def mark_as_read(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """
    Mark a message as read.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.add_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as read"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

async def mark_as_unread(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """
    Mark a message as unread.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.remove_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as unread"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass
