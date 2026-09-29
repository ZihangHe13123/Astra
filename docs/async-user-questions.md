# Optional questions that let work continue

`ask_user_question` still blocks by default. The optional form is:

```json
{
  "mode": "timed",
  "optional": true,
  "timeout_seconds": 30,
  "questions": [{"id": "language", "question": "Which language should the report use?"}]
}
```

Timed mode requires `optional: true`; the timeout must be greater than zero and at most 300 seconds. An unanswered question returns `state: pending`, its request/session/task/tool-call identities, and an explicit reminder that no approval was granted. The assistant may continue work that does not depend on the answer. Required input and tool approvals continue to wait.

One question batch can remain outstanding per backend. Asking another while one is pending returns an actionable unavailable result. GUI and TUI preserve the pending card after the current reply ends. In the TUI, Tab switches between the pending card and the composer. GUI focus and TUI custom editing renew a 60-second editing lease so active typing can extend the initial wait; if the client stops renewing, the editing hold expires after at most 60 seconds.

A late answer is validated against the original options and is accepted once. It carries the original question, request ID, session ID and opaque session namespace, task ID, and tool call ID. While the original task runs, the answer is added as user input between complete model/tool steps and saved before delivery is acknowledged. If the final model response has already started and the inbox cannot be drained, the backend starts one follow-up after that reply ends. It never injects the old answer into an unrelated running task.

A new task, conversation reset, backend session change, cancellation, or backend restart explicitly expires outstanding questions. Selecting another conversation in the GUI leaves each independent backend and its question intact. A redacted lifecycle journal in the configured sessions directory under `.questions` stores identities, owner PID, state, and expiry reason; it does not store question text or answers. After a crash, orphaned questions expire with a visible notice rather than being restored as authorizations. Answers admitted to the agent use the ordinary session transcript.

Focused verification covers blocking compatibility, timeout, editing hold/release, late and duplicate responses, session/task isolation, cancellation, graceful and crash expiry, journal write failure, saved inbox consumption, and the final-response race with a real backend process and a local fake provider. GUI and TUI state/render tests cover pending cards, expiry messages, editing command validation, and returning to the TUI composer.
