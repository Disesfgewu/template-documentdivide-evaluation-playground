"""A stand-in for the lab-gen binary (tests only, Refs #7). Mode comes from FAKE_LAB_GEN_MODE:
ok | unknown_usage | partial | garbage | crash | sleep | refused | wrong_hash | overspend.
It computes costs and hashes exactly the way lab-gen does and never touches the network."""
import hashlib
import json
import os
import sys
import time

mode = os.environ.get("FAKE_LAB_GEN_MODE", "ok")
inp = json.load(sys.stdin)
m, cap = inp["model"], inp["model"]["max_output_tokens"]
if mode == "crash":
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
if mode == "garbage":
    print("this is not json")
    sys.exit(0)
if mode == "refused":
    print(json.dumps({"status": "refused", "reason": "test refusal", "calls": []}))
    sys.exit(0)


def cost(tin, tout):
    return -(-(tin * m["input_micros_per_mtok"] + tout * m["output_micros_per_mtok"]) // 1_000_000)


calls, spent, held = [], 0, 0
for i, c in enumerate(inp["calls"]):
    req = hashlib.sha256(f'{m["name"]}\x00{c["system"]}\x00{c["user"]}'.encode()).hexdigest()
    worst = max(1, cost(len((c["system"] + c["user"]).encode()), cap))
    r = {"id": c["id"], "status": "ok", "usage_known": True, "input_tokens": 5, "output_tokens": 20, "charged_micros": cost(5, 20),
         "held_micros": 0, "transport_attempts": 1, "request_sha256": req}
    text = f"reply {i}"
    r["text"], r["text_sha256"] = text, hashlib.sha256(text.encode()).hexdigest()
    if mode == "unknown_usage":
        r.update(usage_known=False, input_tokens=0, output_tokens=0, charged_micros=-1, held_micros=worst)
    if mode == "partial" and i == 1:
        r.update(status="failed", reason="provider status 503", usage_known=False, input_tokens=0, output_tokens=0,
                 charged_micros=-1, text="", text_sha256="")
        del r["text"], r["text_sha256"]
    if mode == "wrong_hash":
        r["request_sha256"] = "0" * 64
    if mode == "overspend":
        r["charged_micros"] = inp["budget"]["limit_micros"] + 1
    spent += max(r["charged_micros"], 0)
    held += r["held_micros"]
    calls.append(r)
status = "partial" if mode == "partial" else "ok"
print(json.dumps({"status": status, "calls": calls, "ledger_spent_micros": spent, "ledger_reserved_micros": held,
                  "ledger_unknown_micros": held}))
