import assert from "node:assert/strict";
import test from "node:test";
import { SECRET_MASK, findSecrets, maskSecretSlice, maskSecrets } from "../src/secret-mask.js";

// Every value here is an obviously fake fixture of the published shape, never a real credential.
const SK = "sk-test0000000000000000000000000000";
const PROJECT_KEY = "sk-proj-test_0000000000000000-AbCdEfGhIjKlMnOpQrStUvWx0000";
const GITHUB = "ghp_" + "0".repeat(16) + "T".repeat(20);
const FINE_GRAINED = "github_pat_" + "0".repeat(22) + "_" + "T".repeat(20);
const AWS = "AKIA" + "TEST0000EXAMPLE0";
const JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJ0ZXN0In0" + "." + "c2lnbmF0dXJlLXRlc3Q";
const PEM_BODY = ["MIIEtestTESTtest000", "AAAAtest000"];
const PEM = `-----BEGIN RSA PRIVATE KEY-----\n${PEM_BODY.join("\n")}\n-----END RSA PRIVATE KEY-----`;
const hidden = (count: number) => SECRET_MASK.repeat(count);
const values = (text: string) => findSecrets(text).map(span => text.slice(span.start, span.end));

test("keys and tokens keep their first eight characters and hide the rest", () => {
  const text = `我的密钥是 ${SK}，GitHub 用 ${GITHUB}。`;
  const masked = maskSecrets(text);
  assert.equal(masked, `我的密钥是 sk-test0${hidden(SK.length - 8)}，GitHub 用 ghp_0000${hidden(GITHUB.length - 8)}。`);
  assert.equal([...masked].length, [...text].length);
  assert.ok(!masked.includes(SK.slice(8)) && !masked.includes(GITHUB.slice(8)));
  assert.deepEqual(findSecrets(text).map(span => span.kind), ["token", "token"]);
});

test("each published format is recognised whole", () => {
  const cases = [
    SK, PROJECT_KEY, "sk-ant-api03-" + "TEST0test".repeat(10) + "-AAAA",
    "AIza" + "Sy" + "T".repeat(30) + "000", GITHUB, "gho_" + "0".repeat(36), FINE_GRAINED,
    "glpat-" + "test0".repeat(4), "npm_" + "t0".repeat(18), "hf_" + "t0".repeat(15),
    AWS, "ASIA" + "TEST0000EXAMPLE0", "LTAI" + "test0000TEST", "AKID" + "test0".repeat(6),
    "xoxb-" + "0000000000-test", "sk_live_" + "test0".repeat(4), "rk_test_" + "test0".repeat(4), "whsec_" + "test0".repeat(5), JWT,
  ];
  for (const value of cases) assert.deepEqual(values(`a ${value} b`), [value], value);
  assert.equal(findSecrets(JWT)[0].kind, "jwt");
});

test("an address keeps everything but its password, which is hidden whole", () => {
  const text = "连上 postgres://admin:hunter2-test@db.local:5432/app 和 redis://:pw-test@cache";
  assert.deepEqual(values(text), ["hunter2-test", "pw-test"]);
  assert.equal(maskSecrets(text), `连上 postgres://admin:${hidden(12)}@db.local:5432/app 和 redis://:${hidden(7)}@cache`);
  // A token used as a password is hidden completely, not left with a readable prefix.
  const token = `https://user:${GITHUB}@github.com/org/repo`;
  assert.deepEqual(findSecrets(token).map(span => span.kind), ["url-password"]);
  assert.equal(maskSecrets(token), `https://user:${hidden(GITHUB.length)}@github.com/org/repo`);
  // A token used as the user name is still a token.
  assert.deepEqual(values(`https://${GITHUB}:x-oauth-basic@github.com`), [GITHUB, "x-oauth-basic"]);
});

test("a private key keeps its BEGIN and END lines and every line break", () => {
  const text = `看看这个：\n${PEM}\n就这些`;
  const masked = maskSecrets(text);
  assert.equal(masked, `看看这个：\n-----BEGIN RSA PRIVATE KEY-----\n${hidden(PEM_BODY[0].length)}\n${hidden(PEM_BODY[1].length)}\n-----END RSA PRIVATE KEY-----\n就这些`);
  assert.equal(findSecrets(text)[0].kind, "private-key");
  // Quoted in JSON the line breaks are escapes; the body between the markers is still hidden.
  const json = '{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEtest000\\n-----END PRIVATE KEY-----\\n"}';
  assert.equal(maskSecrets(json), `{"private_key": "-----BEGIN PRIVATE KEY-----${hidden(15)}-----END PRIVATE KEY-----\\n"}`);
  // Without an END line the rest of the text is the key.
  assert.equal(maskSecrets("-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\r\nNzaC"), `-----BEGIN OPENSSH PRIVATE KEY-----\n${hidden(6)}\r\n${hidden(4)}`);
  // A public key or certificate is not a secret.
  assert.deepEqual(findSecrets("-----BEGIN PUBLIC KEY-----\nMIIBtest\n-----END PUBLIC KEY-----"), []);
});

test("ordinary words, identifiers, hashes and encodings are left alone", () => {
  const negatives = [
    "pip install sk-learn scikit-learn", "task-1 risk-2024-q3 desk-booking-2024-final", "sk-prod-cluster-01-us-east-1a-workers",
    "sk-" + "a".repeat(40), "AKIA is a prefix; AKIATEST0000EXAMPLE0extra", "ghp_short", "hf_hub_download(repo_id)",
    "commit e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "id 123e4567-e89b-12d3-a456-426614174000",
    "```\nconst data = 'aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQgdGV4dA==';\n```", "eyJhbGciOiJIUzI1NiJ9 alone",
    "https://example.com/path?q=1#frag", "http://localhost:3000", "ssh://git@github.com:22/org/repo",
    "postgres://user@db.local:5432/app", "mailto:someone@example.com", "user@example.com", "password=hunter2 API_KEY=abc123",
    "mask" + SK, "tasksk-test0000000000000000000000000000",
  ];
  for (const text of negatives) assert.deepEqual(findSecrets(text), [], text);
});

test("overlapping candidates join the earliest so nothing is drawn half masked", () => {
  const text = `sk-test0000-${JWT}`;
  const spans = findSecrets(text);
  assert.equal(spans.length, 1);
  assert.equal(spans[0].start, 0);
  assert.equal(spans[0].end, text.length);
  assert.equal(maskSecrets(text), "sk-test0" + hidden(text.length - 8));
});

test("a credential split across rendered pieces masks each piece by its place in the span", () => {
  const text = `key ${SK} end`;
  const [span] = findSecrets(text);
  const cut = span.start + 5;
  const pieces = maskSecretSlice(text, span, span.start, cut) + maskSecretSlice(text, span, cut, span.end);
  assert.equal(pieces, maskSecrets(text).slice(span.start, span.end));
  assert.equal(maskSecretSlice(text, span, span.start, cut), "sk-te");
});

test("non-BMP characters in a password become one mask each", () => {
  const text = "ftp://u:p\u{1F511}x@host";
  assert.equal(maskSecrets(text), `ftp://u:${hidden(3)}@host`);
});

test("scanning stays linear on large and adversarial input", () => {
  const size = 1 << 20;
  const fill = (unit: string) => unit.repeat(Math.ceil(size / unit.length)).slice(0, size);
  const inputs = {
    prose: fill("The quick brown fox jumps over the lazy dog; 任务完成 task-1 sk-learn. "),
    secrets: fill(`see ${SK} and ${JWT} at postgres://u:p0@h/db\n`),
    // Shapes that make a naive regex rescan the same run from every candidate.
    digitFree: fill("sk-"),
    sharedRun: fill("sk-0-"),
    jwtHeaders: fill("eyJ-"),
    jwtChain: fill("eyJaaaaaaaa."),
    unclosedBlocks: fill("-----BEGIN RSA PRIVATE KEY-----\n"),
    addresses: fill("ab://c:"),
    dashes: fill("-"),
  };
  for (const [name, text] of Object.entries(inputs)) {
    const started = performance.now();
    const spans = findSecrets(text);
    maskSecrets(text, spans);
    const elapsed = performance.now() - started;
    // A quadratic scan of a megabyte takes minutes; a linear one, milliseconds. The bound only separates the two.
    assert.ok(elapsed < 3000, `${name}: ${Math.round(elapsed)} ms`);
  }
  assert.equal(findSecrets(inputs.secrets).length > 1000, true);
});
