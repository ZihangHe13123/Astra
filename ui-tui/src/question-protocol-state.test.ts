import assert from "node:assert/strict";
import test from "node:test";
import { reduceQuestionProtocolState } from "./question-protocol-state.js";
import type { UserQuestionRequest } from "./types.js";

const request: UserQuestionRequest = {
  type: "user_question_request",
  request_id: "current-question",
  questions: [{ id: "storage", question: "Choose storage", multi_select: false }],
};

test("matching retryable rejection keeps the request and records an inline correction", () => {
  const next = reduceQuestionProtocolState(
    { request, rejection: null },
    {
      type: "user_question_response_rejected",
      request_id: request.request_id,
      reason: "Selected label was not offered.",
      retryable: true,
    },
  );

  assert.equal(next.request, request);
  assert.deepEqual(next.rejection, {
    requestId: request.request_id,
    reason: "Selected label was not offered.",
  });
});

test("stale rejection cannot alter or revive a different request", () => {
  const state = { request, rejection: null };

  assert.equal(reduceQuestionProtocolState(state, {
    type: "user_question_response_rejected",
    request_id: "stale-question",
    reason: "Question request is no longer pending.",
    retryable: false,
  }), state);
  assert.equal(reduceQuestionProtocolState(state, {
    type: "user_question_response_rejected",
    request_id: "stale-question",
    reason: "Malformed response.",
    retryable: true,
  }), state);
});

test("matching terminal rejection clears only that request", () => {
  const next = reduceQuestionProtocolState(
    { request, rejection: null },
    {
      type: "user_question_response_rejected",
      request_id: request.request_id,
      reason: "Question request is no longer pending.",
      retryable: false,
    },
  );

  assert.deepEqual(next, { request: null, rejection: null });
});

test('timeout updates the existing request without dropping a draft or rejection', () => {
  const state = {request, rejection:{requestId:request.request_id,reason:'Try again'}};
  const next = reduceQuestionProtocolState(state, {type:'user_question_pending',request_id:request.request_id,state:'pending'});
  assert.equal(next.request?.state,'pending');
  assert.equal(next.request?.questions, request.questions);
  assert.equal(next.rejection, state.rejection);
});
