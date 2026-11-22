"""Information that the Asterisk dialplan packs into the AudioSocket UUID.

AudioSocket only gives the bridge a UUID and the audio: no channel variables.
Two kinds of UUID therefore carry data (see DOCS.md, "Dialplan"):

* Incoming call with the caller's number::

      ca11e0LL-RRRR-PPPP-PPPP-PPPPPPPPPPPP

  ``LL`` is the number of digits (decimal, 00-20), ``RRRR`` a random value and
  ``P...`` the 20 last digits of the caller id, padded with zeros on the left.

* Dial status of an outbound call that was not answered::

      fa1100CC-<the call id without its first 8 characters>

  ``CC`` is the position (decimal) of ``DIALSTATUS`` in ``DIAL_STATUSES``.
  These connections are reports, not calls: the bridge reads them and hangs up.

* Answering machine detected by ``AMD()`` (context ``bridge-notice-amd``)::

      a3d000CC-<the call id without its first 8 characters>

  ``CC`` is the position of the first word of ``AMDCAUSE`` in ``AMD_CAUSES``.
  The phone is answered (by the machine): the bridge may leave a short message.
"""

from __future__ import annotations

INBOUND_PREFIX = "ca11e0"
REPORT_PREFIX = "fa1100"
VOICEMAIL_PREFIX = "a3d000"

# Same list, with "-", in the dialplan's FIELDNUM() for CUT(AMDCAUSE,-,1).
AMD_CAUSES = ("INITIALSILENCE", "LONGGREETING", "MAXWORDS", "MAXWORDLENGTH", "TOOLONG", "HUMAN")

# The same list, with "-", goes into the dialplan's FIELDNUM(): position 1 is
# NOANSWER, 2 BUSY... 0 = a status not in the list.
DIAL_STATUSES = (
    "NOANSWER", "BUSY", "CONGESTION", "CHANUNAVAIL", "CANCEL",
    "DONTCALL", "TORTURE", "INVALIDARGS", "ANSWER",
)


def caller_number(call_uuid: str) -> str | None:
    """The caller's number in an incoming-call UUID; None if it carries none."""
    text = call_uuid.lower()
    if not text.startswith(INBOUND_PREFIX):
        return None
    parts = text.split("-")
    if len(parts) != 5:
        return None
    length, digits = parts[0][6:8], parts[2] + parts[3] + parts[4]
    if not (length.isdigit() and digits.isdigit()):
        return None
    count = int(length)
    if count > len(digits):
        return None
    return digits[-count:] if count else ""


def dial_report(call_uuid: str) -> tuple[str, str] | None:
    """(dial status, call id tail) for a dial status report, else None."""
    text = call_uuid.lower()
    if not text.startswith(REPORT_PREFIX):
        return None
    code = text[6:8]
    if not code.isdigit():
        return None
    index = int(code)
    status = DIAL_STATUSES[index - 1] if 1 <= index <= len(DIAL_STATUSES) else "UNKNOWN"
    return status, text[8:]


def result_for(status: str) -> str:
    """Call result for a dial status."""
    return "no_answer" if status in ("NOANSWER", "CANCEL") else "failed"


def voicemail_report(call_uuid: str) -> tuple[str, str] | None:
    """(AMD cause, call id tail) when Asterisk detected an answering machine."""
    text = call_uuid.lower()
    if not text.startswith(VOICEMAIL_PREFIX):
        return None
    code = text[6:8]
    if not code.isdigit():
        return None
    index = int(code)
    cause = AMD_CAUSES[index - 1] if 1 <= index <= len(AMD_CAUSES) else "UNKNOWN"
    return cause, text[8:]
