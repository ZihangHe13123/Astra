# Personal thread

Status: phases 1–3 ship in the desktop app ([user guide](../docs/gui.md#personal-thread)).
Other computers as peers (phases 4–5) and more phone abilities (phase 6) remain open.

## Shape

- One long-lived conversation, apart from project sessions: the home of Astra as a
  personal assistant. The desktop and a paired phone are two screens of it; a terminal
  does not join it.
- The desktop's background host owns it. That backend already runs scheduled jobs and
  messaging channels and keeps running with the window closed (from the tray), so the
  thread has one writer and is reachable from the phone at any time. The window and the
  phone are views: they show its events and send messages into it.
- It works in its own folder (`personal` in Astra's data folder), not in a project. Its
  model is switched like any conversation's.
- Messaging channels keep their own conversations: a channel's formats and sending
  limits do not shape the thread. A channel's approval request, which the same backend
  raises, also shows in the thread, labelled with its chat.

## Proactive messages

- Every scheduled job's result is written into the thread as Astra's own message, at
  once and in quiet hours too: it adds to the conversation and rings nothing, and waits
  out a turn in progress. A later "tell me more about the second one" has it.
- New jobs push to the desktop, which also notifies a paired phone; a channel only when
  a job asks for it.

## Phone and desktop look

- The thread reads as a chat: bubbles, a time heading after each pause, the tool calls
  of a step folded into one line, job results as cards naming the job. Other sessions
  look as before.
- A phone opens on the thread; its sidebar switches to the other sessions. The phone can
  send photos and files: each goes to the computer through an authenticated gateway
  upload, is kept in the thread's folder and attached like a file chosen there. When the
  computer cannot be reached the phone says so.

## Work in projects

- Work in a project belongs to a conversation in that project, which the thread manages
  (modelled on a personal agent that keeps its own machine and hands local work to local
  sessions). The thread asks the desktop to open a conversation in a folder (a path, or
  a project's name among the folders the desktop knows) and hands it the task through
  the local mailbox ([peer workspace](peer-agent-workspace.md)).
- That conversation works with its own tools, permissions and approvals. It shows that
  the thread manages its task, with a take-over action that closes the task for the
  thread. The thread lists it with what it waits on; its approvals and questions can be
  answered from the thread, on the phone too. Its result comes back to the thread, which
  relays it; done or not done is notified.
- Only the thread may open conversations this way.

## Open

- Other computers join as peers over the tailnet: each runs its own Astra, the thread
  hands them tasks the same way, read-only first. No remote control of another
  computer's input.
- The thread's host must be awake and online. Which computer hosts it should become a
  setting; only one may host it at a time.
- Phone abilities beyond files (location, shortcuts) depend on what a web app or a
  native app may do on iOS.
