"""Shared contract-history fixture: the live HITI261016P00002500 payload
(149 rows, captured 2026-09-24), relabelable as any contract."""

import copy
import json
from pathlib import Path

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "contract_history" / "HITI261016P00002500.json").read_text()
)
FIXTURE_ROWS = len(FIXTURE["history"])


def payload_for(occ: str) -> dict:
    """The fixture history, relabelled as the contract `occ`.

    The sync refuses a payload whose expiration/strike/type differ from the
    contract it asked for, so each test contract needs its own identity.
    """
    payload = copy.deepcopy(FIXTURE)
    yymmdd, type_char, strike = occ[-15:-9], occ[-9], occ[-8:]
    payload["expiration"] = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:]}"
    payload["strike"] = int(strike) / 1000
    payload["optionType"] = "put" if type_char == "P" else "call"
    return payload


async def fixture_fetcher(symbol: str, occ: str) -> dict:
    return payload_for(occ)
