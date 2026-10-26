---
name: onboarding
description: Use when the user sends /onboard, or asks Astra to get to know them, set itself up for them, or redo that first-run setup.
---

# Getting to know the person

A short, friendly setup conversation in the person's language. It ends with a few lines in USER.md that they confirmed, their quiet hours, and, if they want them, one or two scheduled jobs. Ten minutes at most; they can stop at any point and keep what was confirmed so far.

1. **Start from what you already know.** USER.md and the saved-memory index are in front of you. Say in one sentence what you already have, and do not ask about it again; ask only whether it is still right if it looks old.
2. **Ask a few things, two at a time at most.** Use `ask_user_question` when there are natural choices, plain questions otherwise. Skip anything they would rather not answer.
   - What to call them, and what they do (studying, working, both).
   - A usual day: when they start and stop, and when they do not want to be disturbed. This becomes the quiet hours.
   - How they like replies: language, length, tone.
   - What they would like you to do without being asked. Offer concrete examples that fit their answers so far, such as a weekday morning check of a site they named, a reminder before a recurring deadline, or a weekly look at a folder. Only read-only checks and reminders can run unattended.
   - Where they want to hear from you: the desktop, WeChat, or both.
3. **Show the draft before saving anything.** One short message with:
   - the USER.md lines, each a single plain sentence (name, role, routine, reply preferences); no secrets, no guesses;
   - the quiet hours;
   - each suggested job: name, when it runs, what it reads, where the result goes.
   Ask them to confirm or change it. Change the draft until they agree.
4. **Save only what they confirmed.**
   - Each line with `memory` (`action: core_add`, `scope: user`). USER.md is small: when it is nearly full, propose which old lines to merge or remove and do that only after they agree (`core_remove`).
   - Quiet hours with `jobs` (`action: quiet`, `quiet_hours: "HH:MM-HH:MM"`, or `"off"`). They end at their end time:
     a job at exactly that time is pushed at once, so there is no need to move it a few minutes later.
   - Each job with `jobs` (`action: add`). Every job is shown to them for approval; a declined one is simply not created.
   - Longer facts that do not belong in USER.md, such as their courses or projects, as saved memories with `memory` (`action: remember`).
5. **Close in two or three sentences:** what was saved, where they can change it (the desktop's 记忆 and 定时任务 pages), and that `/onboard` runs this again.

Do not turn this into a questionnaire: react to what they say, keep each message short, and stop asking once you have enough for a useful setup.
