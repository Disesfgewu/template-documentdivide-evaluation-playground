"""A stand-in for the lab-gen binary (tests only, Refs #7). Mode comes from FAKE_LAB_GEN_MODE:
ok | unknown_usage | partial | garbage | crash | sleep | refused | wrong_hash | overspend.
It computes costs and hashes exactly the way lab-gen does and never touches the network."""
import hashlib
import json
import os
import sys
import time

mode = os.environ.get("FAKE_LAB_GEN_MODE", "ok")


def nopage(t):
    """The generator drops questions that name a page number; the fixture evidence says 'Text on page 1', so quote it as 'pg'."""
    import re
    return re.sub(r"(?i)\bpage\b", "pg", t)


inp = json.load(sys.stdin)
m, cap = inp["model"], inp["model"]["max_output_tokens"]
if os.environ.get("FAKE_LAB_GEN_DELAY"):
    time.sleep(float(os.environ["FAKE_LAB_GEN_DELAY"]))       # a call that takes a while, so the worker's heartbeat runs meanwhile
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
    if mode in ("questions", "questions_ungrounded", "dupes", "badjson", "partial_questions", "questions_prose", "questions_pagerefs", "questions_skip_all", "questions_skip_half"):
        if mode == "badjson":
            text = "I cannot comply."
        else:
            n = 0 if mode == "dupes" else i
            blocks = [b.split(">", 1)[1].strip().splitlines()[0] for b in c["user"].split("</evidence>") if "<evidence" in b]
            body = blocks[0] if blocks else ""
            if c["id"].startswith("c"):             # a confusable call: passage A answers, passage B is the distractor
                a_line, b_line = (blocks + ["", ""])[:2]
                if mode == "questions_ungrounded":
                    a_line = f"item {n} of the document"
                text = json.dumps({"question": f"Which statement says {nopage(a_line)} (variant {n})?",
                                   "reference_answer": a_line, "facts": [{"statement": a_line}],
                                   "confusion_reason": f"The other passage says {b_line}, which looks similar.",
                                   "disambiguating_condition": a_line})
                answer = None
            elif c["id"].startswith("x"):             # a cross_page call: two passages A and B
                a_line, b_line = (blocks + ["", ""])[:2]
                if mode == "questions_ungrounded":
                    a_line, b_line = f"item {n} of the document", f"other item {n} of the document"
                text = json.dumps({"question": f"How does statement number {n} of the first part relate to the second part ({nopage(a_line)} / {nopage(b_line)})?",
                                   "reference_answer": f"{a_line} and {b_line}",
                                   "facts": [{"statement": a_line, "source": "A"}, {"statement": b_line, "source": "B"}]})
                answer = None
            elif mode == "questions_ungrounded":
                answer, fact = f"It says item {n}.", f"The document states item {n}."
            else:                                   # grounded: the answer and the fact are the first line of the shown page
                answer, fact = body, body
            if answer is not None:
                text = json.dumps({"question": f"What does statement number {n} of the document say about {nopage(body)}?",
                                   "reference_answer": answer, "facts": [{"statement": fact}]})
    if mode == "questions_skip_all" or (mode == "questions_skip_half" and i % 2 == 1):
        text = '{"skip": "only page furniture, nothing to ask about"}'      # the model declines in JSON
    if mode == "questions_prose":                 # a harmless deviation seen in real output: prose and a fence around the JSON
        text = "Here is the question you asked for:\n```json\n" + text + "\n```\nLet me know if you need another."
    if mode == "questions_pagerefs":              # the model names a page number in the question
        obj = json.loads(text)
        obj["question"] = f"What content appears on page {i + 5}? ({obj['question']})"
        text = json.dumps(obj)
    if mode.startswith("answer") and c["id"].startswith("a"):
        import re as _re
        hit_ids = _re.findall(r'<hit id="([^"]+)"', c["user"])
        good = {"status": "answered", "answer": "The page says so.",
                "claims": [{"text": "The page says so.", "hit_ids": hit_ids[:1] or ["none"]}]}
        kind = {"answer_ok": 0, "answer_abstain": 1, "answer_bad_json": 2, "answer_bad_citation": 3, "answer_no_claims": 4}.get(mode)
        if mode == "answer_mixed":
            kind = i % 5
        if kind == 0:
            text = json.dumps(good)
        elif kind == 1:
            text = json.dumps({"status": "abstained", "reason": "the passages do not say"})
        elif kind == 2:
            text = "I think the answer is probably yes."
        elif kind == 3:
            text = json.dumps({**good, "claims": [{"text": "x", "hit_ids": ["zzz-not-retrieved"]}]})
        else:
            text = json.dumps({"status": "answered", "answer": "Yes.", "claims": []})
    if mode.startswith("judge") and c["id"].startswith("j"):
        import re as _re
        fact_ids = _re.findall(r'<fact id="([^"]+)"', c["user"])
        claims = [(m[0], [h for h in m[1].split(",") if h]) for m in _re.findall(r'<claim id="([^"]+)" cites="([^"]*)"', c["user"])]
        verdict = {"facts": [{"id": f, "verdict": "stated"} for f in fact_ids],
                   "claims": [{"id": cid, "supported": True, "supporting_hit_ids": ids} for cid, ids in claims]}
        if mode == "judge_contradicted":              # every call: the first fact is contradicted, the claims are supported
            verdict["facts"][0]["verdict"] = "contradicted"
        if mode == "judge_mixed":
            if i % 2 == 1:
                verdict["facts"][0]["verdict"] = "contradicted"
                verdict["claims"][0].update(supported=False, supporting_hit_ids=[])
        text = json.dumps(verdict)
        if mode == "judge_bad_json":
            text = "Looks right to me."
        elif mode == "judge_prose":                   # a harmless deviation: prose and a fence around the verdict
            text = "Here is my grading:\n```json\n" + text + "\n```\nLet me know if you want more detail."
        elif mode == "judge_invented_fact":
            text = json.dumps({**verdict, "facts": verdict["facts"] + [{"id": "f99", "verdict": "stated"}]})
        elif mode == "judge_omitted_claim":
            text = json.dumps({**verdict, "claims": verdict["claims"][1:]})
        elif mode == "judge_foreign_hit":
            verdict["claims"][0]["supporting_hit_ids"] = ["not-cited"]
            text = json.dumps(verdict)
    if mode.startswith("validate") and c["id"].startswith("v"):
        flags = {"supported": True, "answerable_from_evidence": True, "needs_outside_context": False}
        if mode == "validate_mixed":
            if i % 3 == 1:
                flags["supported"] = False
            elif i % 3 == 2:
                flags["needs_outside_context"] = True
        text = "no json here" if mode == "validate_garbage" else json.dumps(flags)
    r["text"], r["text_sha256"] = text, hashlib.sha256(text.encode()).hexdigest()
    if mode == "unknown_usage":
        r.update(usage_known=False, input_tokens=0, output_tokens=0, charged_micros=-1, held_micros=worst)
    if mode in ("partial", "partial_questions") and i == 1:
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
status = "partial" if mode in ("partial", "partial_questions") else "ok"
print(json.dumps({"status": status, "calls": calls, "ledger_spent_micros": spent, "ledger_reserved_micros": held,
                  "ledger_unknown_micros": held}))
