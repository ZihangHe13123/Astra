import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import type { UserQuestionAnswer, UserQuestionRequest } from "../types.js";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { QuestionCard } from "./question-card.js";

class CaptureStream extends Writable {
  rows = 40;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

const request: UserQuestionRequest = {
  type: "user_question_request",
  request_id: "question-render-1",
  questions: [
    {
      id: "storage",
      header: "Storage",
      question: "Choose storage",
      options: [
        { label: "SQLite (Recommended)", description: "Session local" },
        { label: "Markdown", description: "Human editable" },
      ],
      multi_select: false,
    },
    { id: "notes", question: "Any constraints?", multi_select: false },
  ],
};

const multiBatchRequest: UserQuestionRequest = {
  type: "user_question_request",
  request_id: "question-multi-guard",
  questions: [
    {
      id: "sources",
      question: "Choose sources",
      options: [{ label: "Terminal" }, { label: "Safari" }],
      multi_select: true,
    },
    {
      id: "history",
      question: "Clean recorded history?",
      options: [{ label: "Keep" }, { label: "Clear" }],
      multi_select: false,
    },
  ],
};

interface CaptureOptions {
  maxHeight?: number;
  active?: boolean;
  onAnswer?: (requestId: string, answers: UserQuestionAnswer[]) => void;
  onCancel?: (requestId: string) => void;
  submissionRejection?: { requestId: string; reason: string } | null;
}

function latestFrame(output: CaptureStream): string {
  return stripAnsi(output.chunks.at(-1) ?? "").trimEnd();
}

async function capture(
  columns: number,
  cardRequest: UserQuestionRequest = request,
  options: CaptureOptions = {},
) {
  const stdin = new TestStdin();
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <QuestionCard
        request={cardRequest}
        active={options.active ?? true}
        width={columns}
        maxHeight={options.maxHeight}
        onAnswer={options.onAnswer ?? (() => {})}
        onCancel={options.onCancel ?? (() => {})}
        submissionRejection={options.submissionRejection}
      />
    </ThemeProvider>,
    {
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: output as unknown as NodeJS.WriteStream,
      stderr: output as unknown as NodeJS.WriteStream,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  // Parallel test workers can delay Ink's first frame beyond a fixed sleep.
  // Observe an actual frame before asserting its content and physical width.
  const deadline = performance.now() + 1000;
  while (!latestFrame(output) && performance.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return { stdin, output, instance };
}

test("renders the current question and choices without terminal overflow", async () => {
  for (const columns of [60, 90, 130]) {
    const { output, instance } = await capture(columns);
    const rendered = latestFrame(output);
    instance.unmount();

    assert.match(rendered, /◆ Question · 1\/2/);
    assert.match(rendered, /Storage/);
    assert.match(rendered, /Choose storage/);
    assert.match(rendered, /1\. SQLite/);
    assert.match(rendered, /Recommended/);
    assert.match(rendered, /Session local/);
    assert.match(rendered, /Custom answer/);
    assert.equal(
      rendered.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
      true,
      `question card overflowed at ${columns} columns`,
    );
    if (columns === 130) {
      assert.match(
        rendered,
        /↑\/↓ choose · Enter select\/next\/submit · Space select · C custom · S skip · Esc cancel/,
      );
    }
  }
});

test("escapes hostile model text and bounds long content", async () => {
  const hostileRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-controls",
    questions: [{
      id: "unsafe",
      header: "Header ESC\x1b",
      question: "Question NUL\x00",
      options: [{
        label: "Option bidi\u2066",
        description: `Description zero\u200b tag\u{E0020} ${"detail ".repeat(60)}`,
      }],
      multi_select: false,
    }],
  };
  const { output, instance } = await capture(60, hostileRequest);
  const rendered = latestFrame(output);
  instance.unmount();

  assert.doesNotMatch(rendered, /[\x00\x1b\u200b\u2066\u{E0020}]/u);
  assert.match(rendered, /\\x1b/);
  assert.match(rendered, /\\x00/);
  assert.match(rendered, /\\u\{2066\}/);
  assert.match(rendered, /\\u\{200B\}/);
  assert.match(rendered, /\\u\{E0020\}/);
  assert.equal(
    rendered.split(/\r?\n/).every((line) => stringWidth(line) <= 60),
    true,
  );
});

test("wraps complete material question and option text at supported widths", async () => {
  const longRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-long-material",
    questions: [{
      id: "storage",
      header: "Storage",
      question: (
        "Choose a storage strategy that preserves offline access, supports audited migrations, " +
        "and keeps Windows path handling unchanged."
      ),
      options: [
        {
          label: "Embedded relational database with a versioned migration ledger",
          description: (
            "Keeps data local for offline work; records every schema transition for audits; " +
            "requires coordinated migration files across releases."
          ),
        },
        {
          label: "Human-editable Markdown files with deterministic identifiers",
          description: (
            "Makes manual inspection simple; keeps diffs readable in source control; " +
            "trades transactional updates for easier recovery."
          ),
        },
      ],
      multi_select: false,
    }],
  };

  for (const columns of [60, 90, 130]) {
    const { output, instance } = await capture(columns, longRequest);
    const rendered = latestFrame(output);
    const normalized = rendered.replace(/[│╭╮╰╯─]/g, " ").replace(/\s+/g, " ");
    instance.unmount();

    for (const fragment of [
      "preserves offline access",
      "supports audited migrations",
      "Windows path handling unchanged",
      "versioned migration ledger",
      "every schema transition for audits",
      "coordinated migration files across releases",
      "deterministic identifiers",
      "diffs readable in source control",
      "transactional updates for easier recovery",
    ]) {
      assert.match(normalized, new RegExp(fragment), `missing ${fragment} at ${columns} columns`);
    }
    assert.equal(
      rendered.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
      true,
      `wrapped question card overflowed at ${columns} columns`,
    );
  }
});

test("Enter selects the highlighted single option and submits the last question", async () => {
  const submitted: UserQuestionAnswer[][] = [];
  const singleRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-single-enter",
    questions: [{
      id: "storage",
      question: "Choose storage",
      options: [{ label: "SQLite" }, { label: "Markdown" }],
      multi_select: false,
    }],
  };
  const { stdin, output, instance } = await capture(90, singleRequest, {
    onAnswer: (_requestId, answers) => submitted.push(answers),
  });

  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(submitted, [[{ id: "storage", selected: ["SQLite"] }]]);
  assert.match(latestFrame(output), /Submitting answer…/);
  instance.unmount();
});

test("Enter selects each highlighted single option while advancing through a batch", async () => {
  const submitted: UserQuestionAnswer[][] = [];
  const batchRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-single-batch",
    questions: [
      {
        id: "storage",
        question: "Choose storage",
        options: [{ label: "SQLite" }, { label: "Markdown" }],
        multi_select: false,
      },
      {
        id: "format",
        question: "Choose format",
        options: [{ label: "Compact" }, { label: "Verbose" }],
        multi_select: false,
      },
    ],
  };
  const { stdin, output, instance } = await capture(90, batchRequest, {
    onAnswer: (_requestId, answers) => submitted.push(answers),
  });

  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(latestFrame(output), /Question · 2\/2/);
  assert.equal(submitted.length, 0);

  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(submitted, [[
    { id: "storage", selected: ["SQLite"] },
    { id: "format", selected: ["Compact"] },
  ]]);
  instance.unmount();
});

test("Enter never toggles a highlighted multi-select option", async () => {
  const submitted: UserQuestionAnswer[][] = [];
  const multiRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-multi-enter",
    questions: [{
      id: "features",
      question: "Choose features",
      options: [{ label: "Search" }, { label: "Export" }],
      multi_select: true,
    }],
  };
  const { stdin, output, instance } = await capture(90, multiRequest, {
    onAnswer: (_requestId, answers) => submitted.push(answers),
  });

  assert.match(latestFrame(output), /↑\/↓ choose · Space toggle · Enter next\/submit/);
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(submitted.length, 0);
  assert.match(latestFrame(output), /Answer every question or skip it/);

  stdin.write(" ");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(submitted, [[{ id: "features", selected: ["Search"] }]]);
  instance.unmount();
});

test("Enter cannot leave an unanswered multi-select question", async () => {
  const { stdin, output, instance } = await capture(90, multiBatchRequest);

  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(latestFrame(output), /Question · 1\/2/);
  assert.match(latestFrame(output), /Select at least one option, enter a custom answer, or press S to skip/);

  stdin.write(" ");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(latestFrame(output), /Question · 2\/2/);
  instance.unmount();
});

test("Right Arrow force-advances past an unanswered multi-select question", async () => {
  const { stdin, output, instance } = await capture(90, multiBatchRequest);

  stdin.write("\u001b[C");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.match(latestFrame(output), /Question · 2\/2/);
  assert.doesNotMatch(latestFrame(output), /Select at least one option/);
  instance.unmount();
});

test("routes custom entry and Enter through ink-text-input", async () => {
  const answers: UserQuestionAnswer[][] = [];
  const customRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-custom",
    questions: [{ id: "notes", question: "Any constraints?", multi_select: false }],
  };
  const { stdin, output, instance } = await capture(90, customRequest, {
    onAnswer: (_requestId, submitted) => answers.push(submitted),
  });

  stdin.write("c");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(latestFrame(output), /Custom answer:/);
  stdin.write("Keep Windows behavior");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.match(latestFrame(output), /Keep Windows behavior/);
  assert.equal(answers.length, 0, "custom-entry Enter must commit text without submitting the batch");
  instance.unmount();
});

test("Escape in custom entry cancels exactly once without submitting", async () => {
  const answers: UserQuestionAnswer[][] = [];
  const cancellations: string[] = [];
  const customRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-custom-escape",
    questions: [{ id: "notes", question: "Any constraints?", multi_select: false }],
  };
  const { stdin, output, instance } = await capture(90, customRequest, {
    onAnswer: (_requestId, submitted) => answers.push(submitted),
    onCancel: (requestId) => cancellations.push(requestId),
  });

  stdin.write("c");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(latestFrame(output), /Custom answer:/);
  stdin.write("draft that must not submit");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\x1b");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\x1b");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(cancellations, [customRequest.request_id]);
  assert.equal(answers.length, 0);
  assert.match(latestFrame(output), /Cancelling question…/);
  instance.unmount();
});

test("keeps an over-limit custom answer editable and submits after correction", async () => {
  const submitted: UserQuestionAnswer[][] = [];
  const customRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-custom-limit",
    questions: [{ id: "notes", question: "Any constraints?", multi_select: false }],
  };
  const { stdin, output, instance } = await capture(90, customRequest, {
    onAnswer: (_requestId, answers) => submitted.push(answers),
  });

  stdin.write("c");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("x".repeat(2_001));
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(submitted.length, 0);
  assert.match(latestFrame(output), /2,000/);
  assert.match(latestFrame(output), /Enter save custom answer/);

  stdin.write("\x7f");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(submitted.length, 1);
  assert.equal(submitted[0][0].custom, "x".repeat(2_000));
  instance.unmount();
});

test("retryable backend rejection keeps the same card editable for correction", async () => {
  const submitted: UserQuestionAnswer[][] = [];
  const retryRequest: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-retry",
    questions: [{
      id: "storage",
      question: "Choose storage",
      options: [{ label: "SQLite" }, { label: "Markdown" }],
      multi_select: false,
    }],
  };
  const onAnswer = (_requestId: string, answers: UserQuestionAnswer[]) => submitted.push(answers);
  const { stdin, output, instance } = await capture(90, retryRequest, { onAnswer });

  stdin.write("1");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(submitted.length, 1);
  assert.match(latestFrame(output), /Submitting answer…/);

  instance.rerender(
    <ThemeProvider theme={THEMES.glitchcity}>
      <QuestionCard
        request={retryRequest}
        active
        width={90}
        onAnswer={onAnswer}
        onCancel={() => {}}
        submissionRejection={{
          requestId: retryRequest.request_id,
          reason: "Selected label was not offered.",
        }}
      />
    </ThemeProvider>,
  );
  await new Promise((resolve) => setTimeout(resolve, 20));

  const retryFrame = latestFrame(output);
  assert.match(retryFrame, /● 1\. SQLite/);
  assert.match(retryFrame, /Selected label was not offered\./);
  assert.match(retryFrame, /Esc cancel/);
  assert.doesNotMatch(retryFrame, /Submitting answer…/);

  stdin.write("2");
  await new Promise((resolve) => setTimeout(resolve, 20));
  stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(submitted, [
    [{ id: "storage", selected: ["SQLite"] }],
    [{ id: "storage", selected: ["Markdown"] }],
  ]);
  instance.unmount();
});

test("leaves Ctrl+C to the app-level task cancellation handler", async () => {
  const { stdin, output, instance } = await capture(90);
  stdin.write("\x03");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.doesNotMatch(latestFrame(output), /Custom answer:/);
  instance.unmount();
});

test("inactive cards ignore input and submitted cards disable duplicate input", async () => {
  let inactiveAnswers = 0;
  let inactiveCancels = 0;
  const oneQuestion: UserQuestionRequest = {
    type: "user_question_request",
    request_id: "question-disabled",
    questions: [{
      id: "storage",
      question: "Choose storage",
      options: [{ label: "SQLite" }],
      multi_select: false,
    }],
  };
  const inactive = await capture(90, oneQuestion, {
    active: false,
    onAnswer: () => { inactiveAnswers += 1; },
    onCancel: () => { inactiveCancels += 1; },
  });
  assert.match(latestFrame(inactive.output), /Question paused while approval is active/);
  inactive.stdin.write("1\r\x1b");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(inactiveAnswers, 0);
  assert.equal(inactiveCancels, 0);
  inactive.instance.unmount();

  const submitted: { requestId: string; answers: UserQuestionAnswer[] }[] = [];
  const active = await capture(90, oneQuestion, {
    onAnswer: (requestId, answers) => submitted.push({ requestId, answers }),
  });
  active.stdin.write("1");
  await new Promise((resolve) => setTimeout(resolve, 20));
  active.stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));
  active.stdin.write("\r");
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.deepEqual(submitted, [{
    requestId: "question-disabled",
    answers: [{ id: "storage", selected: ["SQLite"] }],
  }]);
  assert.match(latestFrame(active.output), /Submitting answer…/);
  active.instance.unmount();
});

test("S then Enter preserves a skipped final single-select question", async () => {
  const answers: UserQuestionAnswer[][] = [];
  const { stdin, instance } = await capture(60, {
    ...request,
    questions: [request.questions[0]],
  }, { onAnswer: (_id, value) => answers.push(value) });
  try {
    stdin.write("s");
    await new Promise((resolve) => setTimeout(resolve, 25));
    stdin.write("\r");
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.deepEqual(answers, [[{ id: "storage", selected: [] }]]);
  } finally { instance.unmount(); }
});

test("short question viewport pages through all content and follows the highlighted option", async () => {
  const answers: UserQuestionAnswer[][] = [];
  const cardRequest: UserQuestionRequest = {
    ...request,
    questions: [{ id: "storage", question: "Choose the storage for this project.", multi_select: false,
      options: [
        { label: "SQLite", description: "First description keeps transaction support and local queries available." },
        { label: "Markdown", description: "Second description keeps human readable files and portable version history." },
        { label: "JSON", description: "Third description keeps structured documents with a final unique marker." },
      ],
    }],
  };
  const { stdin, output, instance } = await capture(39, cardRequest, {
    maxHeight: 11, onAnswer: (_id, value) => answers.push(value),
  });
  try {
    const frames: string[] = [];
    for (let page = 0; page < 8; page++) {
      const frame = latestFrame(output);
      assert.ok(frame.split("\n").length <= 11, `viewport exceeds 11 rows:\n${frame}`);
      assert.match(frame, /PgUp\/PgDn/);
      frames.push(frame);
      stdin.write("\u001b[6~");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const content = frames.flatMap((frame) => {
      const lines = frame.split("\n");
      return lines.slice(2, lines.findIndex((line) => line.includes("PgUp/PgDn")));
    }).join("").replace(/[│\s]/g, "");
    for (const fragment of ["transaction support", "local queries available", "human readable files", "portable version history", "structured documents", "final unique marker"]) {
      assert.ok(content.includes(fragment.replace(/\s/g, "")), `missing paged content: ${fragment}`);
    }
    stdin.write("\u001b[B");
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.match(latestFrame(output), /Markdown/);
    stdin.write("\u001b[B");
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.match(latestFrame(output), /JSON/);
    stdin.write("\r");
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.deepEqual(answers, [[{ id: "storage", selected: ["JSON"] }]]);
  } finally { instance.unmount(); }
});

test("short viewport keeps custom entry bounded while preserving the entire answer", async () => {
  const answers: UserQuestionAnswer[][] = [];
  const custom = "A complete custom answer with unicode 👩‍💻 and enough words to fill many rows. ".repeat(8).trim();
  const { stdin, output, instance } = await capture(39, {
    ...request, questions: [request.questions[0]],
  }, { maxHeight: 11, onAnswer: (_id, value) => answers.push(value) });
  try {
    for (const chunk of ["c", custom, "\u001b[5~", "\u001b[6~", "\r"]) {
      stdin.write(chunk);
      await new Promise((resolve) => setTimeout(resolve, 25));
      assert.ok(latestFrame(output).split("\n").length <= 11, latestFrame(output));
      assert.match(latestFrame(output), /PgUp\/PgDn/);
    }
    stdin.write("\r");
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.deepEqual(answers, [[{ id: "storage", selected: [], custom }]]);
  } finally { instance.unmount(); }
});

test('pending question tells the user work continues and stays answerable', async () => {
  const {output, instance} = await capture(100, {...request, mode:'timed',state:'pending'});
  try {
    assert.match(latestFrame(output), /work continues/i);
    assert.match(latestFrame(output), /Choose storage/);
  } finally { instance.unmount(); }
});
