import test from 'node:test';
import assert from 'node:assert/strict';
import { modelDisplayName } from '../src/model-label.js';

test('an alias shows the version the CLI resolved it to', () => {
 assert.equal(modelDisplayName('sonnet', 'claude-sonnet-5'), 'sonnet-5');
 assert.equal(modelDisplayName('sonnet', 'claude-sonnet-5-5'), 'sonnet-5-5');
 assert.equal(modelDisplayName('opus', 'claude-opus-5[1m]'), 'opus-5[1m]');
 assert.notEqual(modelDisplayName('sonnet', 'claude-sonnet-5'), modelDisplayName('sonnet', 'claude-sonnet-5-5'));
});

test('a model that resolves to nothing else keeps its own name', () => {
 assert.equal(modelDisplayName('deepseek-flash'), 'deepseek-flash');
 assert.equal(modelDisplayName('deepseek-flash', ''), 'deepseek-flash');
 assert.equal(modelDisplayName('claude-sonnet-5-5', 'claude-sonnet-5-5'), 'claude-sonnet-5-5');
});

test('a name without the vendor prefix is shown as reported', () => {
 assert.equal(modelDisplayName('best', 'some-model-2'), 'some-model-2');
});
