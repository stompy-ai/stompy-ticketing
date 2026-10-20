"""A stdlib log line that carries its fields IN the message (STOMPY-3089 follow-up).

The host formats stdlib records as "%(message)s", so `extra=` fields never reach its
log. Log with ``logger.<level>(*fields_message(event, fields), extra=fields)``: the
message is "event k=v k=v", and structured handlers still get the fields.
"""


def fields_message(event: str, fields: dict) -> tuple:
    """``("event k=%s k=%s", v, v)`` — lazy %-formatting, in the fields' order."""
    template = " ".join([event, *(f"{k}=%s" for k in fields)])
    return (template, *fields.values())
