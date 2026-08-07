"""Xyne delivery, via the Slack-compatible adapter at /api/apps/slack/*.

Xyne exposes Slack-shaped routes but a narrower message model: `chat.postMessage`
takes `channel`, `text` and `mrkdwn` — there is no `blocks` and no `attachments`,
so the coloured side-bar the Slack transport uses has no equivalent here.

Rather than maintain a second copy of every table, this module RENDERS THE SAME
BLOCKS the Slack transport builds and flattens them to text. The two channels
therefore cannot drift: a change to a table shows up in both, or in neither.

Threading works: `thread_ts` nests the reply correctly, even though the response
object does not echo the field back. The reply structure therefore mirrors Slack's
exactly — one root, everything else in the thread.
"""

import json
import logging
import mimetypes
import urllib.request
import uuid
from pathlib import Path

import slack

log = logging.getLogger("cost-anomaly.xyne")

_TIMEOUT = 60


def configured(cfg: dict) -> bool:
    return bool(cfg.get("xyne_base_url") and cfg.get("xyne_jwt") and cfg.get("xyne_channel"))


def _api(cfg: dict, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        cfg["xyne_base_url"].rstrip("/") + "/api/apps/slack/" + method,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + cfg["xyne_jwt"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        # The API echoes raw newlines inside JSON strings, which a strict parser
        # rejects. strict=False accepts the literal control characters.
        return json.loads(r.read().decode("utf-8", "replace"), strict=False)


# Slack renders :shortcode: emoji server-side; there is no guarantee Xyne does,
# and an unrendered ":red_circle:" in the headline is worse than no emoji at all.
# Substituting the literal character sidesteps the question.
_EMOJI = {
    ":red_circle:": "\U0001F534",
    ":large_yellow_circle:": "\U0001F7E1",
    ":large_green_circle:": "\U0001F7E2",
    ":white_circle:": "\u26AA",
    ":rotating_light:": "\U0001F6A8",
    ":warning:": "\u26A0\uFE0F",
}


def _demojize(text: str) -> str:
    for code, char in _EMOJI.items():
        text = text.replace(code, char)
    return text


def _blocks_to_text(blocks: list[dict]) -> str:
    """Flatten Block Kit into the plain markdown Xyne accepts."""
    parts = []
    for b in blocks or []:
        if b.get("type") == "divider":
            continue
        t = b.get("text", {}).get("text")
        if not t and b.get("type") == "context":
            t = " ".join(e.get("text", "") for e in b.get("elements", []))
        if not t and b.get("type") == "header":
            t = "*" + b.get("text", {}).get("text", "") + "*"
        if t:
            parts.append(t)
    return _demojize("\n".join(parts).strip())


def _post(cfg: dict, text: str, thread_ts: str | None = None) -> dict | None:
    if not text:
        return None
    payload = {"channel": cfg["xyne_channel"], "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        resp = _api(cfg, "chat.postMessage", payload)
    except Exception as e:
        log.error("Xyne post failed: %s", e)
        return None
    if not resp.get("ok"):
        # Slack-shaped errors come back HTTP 200 with ok:false, so this has to be
        # checked explicitly or every failure looks like a success.
        log.error("Xyne rejected the message: %s", resp.get("error"))
        return None
    return resp


def _upload(cfg: dict, path: str, thread_ts: str | None = None,
            comment: str | None = None, filename: str | None = None) -> dict | None:
    """Upload a file via files.upload (multipart). Returns the parsed response so
    the caller can thread under it, or None on failure."""
    p = Path(path)
    boundary = "----costreport" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    def part(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode()

    body = bytearray()
    body += part("channels", cfg["xyne_channel"])
    if comment:
        body += part("initial_comment", comment)
    if thread_ts:
        body += part("thread_ts", thread_ts)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{filename or p.name}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
    body += p.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        cfg["xyne_base_url"].rstrip("/") + "/api/apps/slack/files.upload",
        data=bytes(body),
        headers={"Authorization": "Bearer " + cfg["xyne_jwt"],
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"), strict=False)
    except Exception as e:
        log.error("Xyne file upload failed: %s", e)
        return None
    if not resp.get("ok"):
        log.error("Xyne rejected the upload: %s", resp.get("error"))
        return None
    return resp


def _upload_ts(resp: dict | None) -> str | None:
    """Best-effort message ts from a files.upload response, so replies can thread
    under a root image. Xyne echoes a few shapes; check the common ones."""
    if not resp:
        return None
    if resp.get("ts"):
        return resp["ts"]
    files = resp.get("files") or ([resp["file"]] if resp.get("file") else [])
    for f in files:
        if (f or {}).get("ts"):
            return f["ts"]
        for vis in ((f or {}).get("shares") or {}).values():
            for arr in vis.values():
                if arr and arr[0].get("ts"):
                    return arr[0]["ts"]
    return None


def post(cfg: dict, report: dict, xlsx_path: str) -> None:
    """Same images as the Slack report — the summary card as the root, every
    breakdown threaded, then the workbook. Xyne renders images, not text tables."""
    if not configured(cfg):
        log.info("Xyne not configured — skipping")
        return

    import tempfile
    tmp = tempfile.mkdtemp(prefix="cost-xyne-")
    images = slack.render_images(cfg, report, tmp)          # summary first
    if not images:
        log.warning("Xyne: no images rendered — skipping")
        return
    d = report["date"]

    # Xyne has its own directory and ID scheme (cuid2, not Slack's U…), so its
    # mention is a separate literal string, posted just below the summary card.
    xm = (cfg.get("xyne_mention") or "").strip()
    headline = f"*Cloud costs — {d.isoformat()}*"
    _, summary_png = images[0]

    # Try the summary card AS the root (image + headline comment); fall back to a
    # text root if the upload response doesn't give a ts to thread under.
    root = _upload(cfg, summary_png, comment=headline,
                   filename=f"{d.isoformat()}-summary.png")
    ts = _upload_ts(root)
    if ts is None:
        text_root = _post(cfg, headline)
        if text_root is None:
            log.error("Xyne root failed — skipping the rest of the report")
            return
        ts = text_root.get("ts")
        if root is None:                                   # image didn't post above
            _upload(cfg, summary_png, thread_ts=ts, filename=f"{d.isoformat()}-summary.png")

    # Mentions just below the summary card, matching Slack.
    if xm:
        _post(cfg, xm, thread_ts=ts)

    for name, path in images[1:]:
        _upload(cfg, path, thread_ts=ts, filename=f"{name}.png")

    if _upload(cfg, xlsx_path, thread_ts=ts,
               comment="Full per-service breakdown, 7 days per account."):
        log.info("Posted report and workbook to Xyne channel %s", cfg["xyne_channel"])
    else:
        log.warning("Xyne report posted but the workbook upload failed")
