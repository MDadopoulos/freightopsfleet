"""ISO 6346 container check digits. Arithmetic the model must not do in its head.

A container number is four letters and seven digits, the last of which is a
checksum over the other ten. Verifying it is a weighted sum -- mechanical, and
exactly the kind of thing a language model does *almost* right. It did: across
the committed eval records the fleet reported the computed digit for
`MERU4106915` as 3 three times and as 0 once, and the wrong one appeared twice
in the same report, once inside the notice drafted for the carrier. The grader
could not catch it, because the answer key asserts which container numbers are
named, not what the arithmetic came to.

The prompt cannot fix that. Telling a model to be careful with a weighted sum
buys a lower error rate, not a correct one, and telling it to stop showing its
arithmetic only hides the error -- it would still be asserting pass or fail off
the same unreliable sum, with nothing on the page left to falsify. So the fleet
stops asking. This function computes the digit, the specialist reports what it
returned, and the failure mode is gone rather than concealed.

Pure computation: reads nothing, writes nothing, leaves the process untouched.
That is why `TOOL_SPECS` classifies it LOW/no-side-effect and the gate lets it
run unattended -- the one kind of tool that is safe to make AUTO is the kind
that cannot do anything.
"""

from __future__ import annotations

import re


#: Letter values run A=10..Z=38, skipping every multiple of 11 (11, 22, 33).
#: Built rather than typed out so the skips cannot be mistyped: L=23, U=32, Z=38.
def _letter_values() -> dict[str, int]:
    values, v = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        while v % 11 == 0:
            v += 1
        values[ch] = v
        v += 1
    return values


_VALUES = _letter_values()
_SHAPE = re.compile(r"^([A-Z]{4})(\d{6})(\d)$")


def _computed(body: str) -> int:
    """The check digit for the first ten characters. Sum of value x 2^position,
    mod 11; a remainder of 10 is written as 0."""
    total = sum((_VALUES[ch] if ch.isalpha() else int(ch)) * (2 ** i)
                for i, ch in enumerate(body))
    return (total % 11) % 10


def check_container_number(number: str) -> dict:
    """Verify one container number's ISO 6346 check digit.

    Returns `valid` together with both digits, so the specialist can report the
    discrepancy without recomputing anything. `status` is `malformed` when the
    input is not four letters and seven digits -- a shape that cannot be checked
    is not a failed check, and calling it one would invent a finding.
    """
    raw = str(number or "")
    normalized = re.sub(r"[\s-]", "", raw).upper()
    shape = _SHAPE.match(normalized)
    if shape is None:
        return {"status": "malformed", "number": raw, "normalized": normalized,
                "message": "not four letters followed by seven digits; cannot be checked"}

    owner, serial, stated = shape.group(1), shape.group(2), int(shape.group(3))
    computed = _computed(owner + serial)
    return {
        "status": "ok",
        "number": raw,
        "normalized": normalized,
        "valid": computed == stated,
        "stated_check_digit": stated,
        "computed_check_digit": computed,
    }


TOOL_FNS = {"check_container_number": check_container_number}
