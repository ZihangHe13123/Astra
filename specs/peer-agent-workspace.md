# Peer Agent workspace

Status: the local stage, sessions on one computer, is implemented (see Local
stage below). The cross-machine design remains a proposal and is not approved
for implementation. This public summary preserves the design questions without
personal deployment inventories.

## Intended use

Allow explicitly paired Astra installations to collaborate in their own local
environments. Either peer can request a task, exchange clarifications and return
an artifact. Reaching a peer outside the local network is a core requirement;
a successful same-network demo would not satisfy it.

Each installation owns its identity, permissions, settings and memory. A task
brief shares only the context needed for that task. This is not automatic memory
merging, whole-history access or bidirectional working-directory synchronization.

## Candidate design

The existing [Worker and Team runtime](../docs/design/worker-runtime-v1.md) provides
useful local task and message concepts. A remote transport may reuse those
concepts, but its storage, locking and failure behavior must be checked for
cross-process operation before adopting them.

- Pair known installations explicitly and exchange a bounded capability summary.
  Authentication, encryption, revocation and replay protection require a reviewed
  protocol; local trust alone is not sufficient.
- Use site-qualified identifiers and monotonic sequence numbers for ordering and
  deduplication. Do not use wall-clock timestamps as message identity.
- Keep a durable outbox and bounded delivery deadlines. A disconnection must leave
  the task's last known status visible, without silently repeating an operation.
- Let the executing peer own task progress and request additional input when
  needed. Cancellation and reconnection need explicit acknowledgments.
- Send artifact metadata separately from file bytes. Validate declared size and
  content hash, land files in an inbox, and avoid overwriting an active workspace.
  Preserve both copies of conflicting binary documents.

## Local stage (implemented)

Astra sessions open on the same computer can hand each other tasks
(`agent/runtime/peer_link.py`; usage in [docs/usage.md](../docs/usage.md)).

- One SQLite file in the state directory, `peers.db`, holds the directory of open
  sessions, the mailbox and the task board. SQLite's file locks make it safe
  across backend processes; every write is one short `BEGIN IMMEDIATE`
  transaction.
- A peer is a session, identified as `{site}:{session}`. A reopened session keeps
  its identity, its name and its unread mail.
- Task states follow A2A: submitted, working, input-required, completed, failed,
  canceled and rejected. Taking a task's mail moves it to working, and the
  requester answering a question moves it back to working. Order comes from the
  mailbox sequence, never from clocks.
- An idle session in local Work mode claims its mail every second and starts a
  turn with it; a busy session reads its mail when its turn ends. The turn says
  the request comes from another session, not the user, and grants nothing. The
  work runs with the receiving session's own permissions.
- If a turn that read a task's mail ends while the requester still has the last
  word, the turn's answer becomes the result.
- Two sessions must not talk forever: a task holds at most 20 messages, a
  finished task takes no more, and a session opens at most 20 tasks an hour.
- Tools: `peer_list`, `peer_send` and `peer_task_update`. Commands: `/peers`
  and `/peers name`.

The cross-machine stage keeps these envelopes and replaces only the transport
underneath, adding the pairing, authentication and encryption described above.

## Decisions still required

| Topic | Question to resolve |
| --- | --- |
| Reachability | Which direct, tunnel or relay path works on the actual networks? |
| Availability | Is the peer always running, or what supported mechanism can wake it? |
| Pairing | How are credentials established, revoked and renewed? |
| Task lifecycle | Which states survive restart, and when may a sender safely retry? |
| File access | Which directories are shared, and how are incoming files accepted? |
| Concurrency | Which operations require a lease, and what happens after expiry? |
| Context | What is the brief budget, and how does a peer request missing evidence? |

Network reachability and waking a powered-down machine are separate requirements.
Both need deployment evidence; neither follows from the presence of an HTTP API.

## Acceptance before release

1. From outside the local network, reach a freshly restarted peer and complete a
   read-only task with the configured authentication.
2. Complete a task that asks for clarification, resumes and delivers an artifact
   whose bytes match the declared hash. Repeat with the roles reversed.
3. Disconnect and restart during delivery; demonstrate no lost accepted message
   and no repeated side effect. Distinguish unknown outcome from safe retry.
4. Reject an unpaired peer, revoked credential, replayed request, invalid artifact
   and write outside the declared access scope.
5. Demonstrate cancellation, peer unavailability, conflicting file edits and
   lease expiry without claiming unverified completion.

Tests should use synthetic files and injected clocks. Real-network acceptance
requires a separate explicit deployment; it is not part of this proposal.
