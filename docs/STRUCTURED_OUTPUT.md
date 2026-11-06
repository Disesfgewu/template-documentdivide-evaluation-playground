# Structured model-output boundary

`lab_contracts.structured_output` provides a reusable synchronous validation and
execution boundary for a trusted LAB `Contract` class. It makes one injected
transport call and, only for invalid JSON or a schema violation, at most one
repair call. Both calls require fresh admission. This is an independent #6
foundation; it does not invoke an LLM, agentKit or PostgreSQL itself.

The validator accepts bounded UTF-8 bytes. It rejects duplicate keys at any
level, non-finite/out-of-range numbers, trailing content, Markdown fences,
nesting beyond 64 containers, unknown contract fields and coercible-but-wrong
primitive types. It uses the existing strict JSON parser and Pydantic's strict
JSON mode, preserving the existing enum/date contract semantics. Schema validity
does not establish source accuracy, semantic quality, human approval or
eligibility to freeze a benchmark.

`StructuredRequest` fixes operation, model, prompt and schema revision labels,
requires `max_output_tokens`, and bounds input and output bytes (maximum 16 MiB
each). `max_input_bytes` includes the original input plus previous raw output
on a repair call. The transport must also bound its complete formatted request,
including instructions, schema and provider framing. Model output is passed as
separate `Invocation.repair_output` data; adapters must delimit it as untrusted
content, never interpret it as instructions or execute it. Revision labels do
not verify a provider's actual model identity; the adapter must verify and record
that identity separately.

Admission must atomically check cancellation and budget and reserve the next
call's cost. Only exact `True` dispatches; denial or admission exceptions fail
closed. Rechecking a monotonic deadline after admission prevents a slow admission
from authorizing an already expired dispatch. The admission owner must release
or reconcile a reservation if the deadline expires before dispatch. Transport
exceptions are not retried or sent for JSON repair. This executor owns the one
schema-repair decision; transport retries must be disabled here, with any later
retry managed explicitly by the durable operation owner.

The synchronous callback must enforce network/CPU/response limits and cannot be
interrupted by this module. Deadline checks prevent subsequent dispatch and
publication of late output, but do not provide hard cancellation of an in-flight
request. A required `cancelled` callback is checked before admission, after admission and
after transport. Cancellation suppresses success/repair and retains any bounded
returned bytes for restricted audit; callback errors or non-boolean values fail
closed. The callback must read authoritative cancellation state.

A returned late output is retained for restricted audit with
`value=None`. A transport exception may mean a paid request occurred; the caller
must record/reconcile `outcome_unknown`, not assume no cost. No usage or price is
invented by this boundary; provider usage, request IDs, pricing and settlement
remain the adapter/operation owner's responsibility.

A result contains zero, one or two actual transport attempts. Each bounded byte
output, even invalid output, is retained exactly with its SHA-256; callers must
persist it in restricted artifacts. A non-byte or oversized return is not retained
or hashed and cannot be repaired; oversized bytes have their actual size recorded.
The transport should reject oversized bodies while reading, and retain any
required restricted raw artifact itself. Nothing here is crash-durable. Inputs,
raw outputs and validated values are excluded from normal object `repr`; fixed
error codes omit Pydantic field paths/messages/inputs and exception text. Do not
serialize these internal objects wholesale into logs or participant responses:
`dataclasses.asdict`, direct field access and model dumps still expose restricted
data. Sanitized diagnostics are not an authorization layer.

This synthetic example exercises the real executor with a deterministic local
callback. It makes no provider call and supplies no production admission policy:

```python
import time
from pathlib import Path
from lab_contracts.benchmark import Question
from lab_contracts.structured_output import StructuredRequest, execute_structured

sample = Path("contracts/examples/valid/Question.cross_page.json").read_bytes()
request = StructuredRequest(
    operation_id="demo-operation", model_revision="synthetic-r1",
    prompt_revision="demo-prompt-r1", schema_revision="question-v1",
    input_text="Synthetic demonstration only", max_output_tokens=256,
)
result = execute_structured(
    request, Question, transport=lambda invocation: sample,
    admit=lambda invocation: True, cancelled=lambda: False,
    deadline=time.monotonic() + 5,
)
assert result.status == "succeeded"
assert len(result.attempts) == 1
```

Run the deterministic tests using the pinned repository Python environment:

```bash
bash scripts/check.sh --scope 'contracts fixtures governance'
```

The new test module covers R08/R20's structural T19 subset and dispatch/deadline
boundaries related to T29: exact call counts, re-admission, exhaustion to
`needs_review`, byte/depth/schema rejection, sanitized diagnostics and audit-only
late results. It uses fake callbacks, not a real provider. Full T19/T25–T29/A39,
agentKit/provider invocation, actual token enforcement, durable reservation and
settlement, cancellation/leases, operation/checkpoint recovery, artifact ACLs,
trace/DB/worker wiring and human verification remain integration/release gates.
