/** Shared inert display of control characters in authorization facts. */
const DEFAULT_IGNORABLE_CODE_POINT = /\p{Default_Ignorable_Code_Point}/u;
const EXTENDED_PICTOGRAPHIC = /\p{Extended_Pictographic}/u;
const GRAPHEME_EXTEND = /\p{Grapheme_Extend}/u;
const EMOJI_MODIFIER = /\p{Emoji_Modifier}/u;
const APPROVAL_GRAPHEME_SEGMENTER = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : undefined;

function isUnicodeDisplayControl(codePoint: number): boolean {
  return DEFAULT_IGNORABLE_CODE_POINT.test(String.fromCodePoint(codePoint))
    || codePoint === 0x00ad
    || codePoint === 0x034f
    || codePoint === 0x061c
    || codePoint === 0x180e
    || (codePoint >= 0x200b && codePoint <= 0x200f)
    || (codePoint >= 0x202a && codePoint <= 0x202e)
    || (codePoint >= 0x2060 && codePoint <= 0x206f)
    || codePoint === 0xfeff;
}

export function isUnsafeDisplayCodePoint(codePoint: number): boolean {
  return codePoint <= 0x1f
    || (codePoint >= 0x7f && codePoint <= 0x9f)
    || isUnicodeDisplayControl(codePoint);
}

function isValidEmojiFormat(characters: string[], index: number): boolean {
  const codePoint = characters[index]?.codePointAt(0);
  if (codePoint === 0xfe0f) {
    return index > 0 && EXTENDED_PICTOGRAPHIC.test(characters[index - 1] ?? "");
  }
  if (codePoint !== 0x200d || index === 0 || index + 1 >= characters.length) return false;
  let previous = index - 1;
  while (previous >= 0 && (
    GRAPHEME_EXTEND.test(characters[previous] ?? "")
    || EMOJI_MODIFIER.test(characters[previous] ?? "")
  )) previous -= 1;
  return previous >= 0
    && EXTENDED_PICTOGRAPHIC.test(characters[previous] ?? "")
    && EXTENDED_PICTOGRAPHIC.test(characters[index + 1] ?? "");
}

/** Escape terminal and directionality controls into inert, visible notation. */
export function escapeApprovalDisplayText(value: string): string {
  let escaped = "";
  const graphemes = APPROVAL_GRAPHEME_SEGMENTER && /[\u200d\ufe0f]/.test(value)
    ? Array.from(APPROVAL_GRAPHEME_SEGMENTER.segment(value), ({ segment }) => segment)
    : Array.from(value);
  for (const grapheme of graphemes) {
    const characters = Array.from(grapheme);
    for (const [index, character] of characters.entries()) {
      const codePoint = character.codePointAt(0) ?? 0;
      const emojiFormat = isValidEmojiFormat(characters, index);
      if (!isUnsafeDisplayCodePoint(codePoint) || emojiFormat) {
        escaped += character;
      } else if (codePoint === 0x09) {
        escaped += String.raw`\t`;
      } else if (codePoint === 0x0a) {
        escaped += String.raw`\n`;
      } else if (codePoint === 0x0d) {
        escaped += String.raw`\r`;
      } else if (codePoint <= 0xff) {
        escaped += `\\x${codePoint.toString(16).padStart(2, "0")}`;
      } else {
        escaped += `\\u{${codePoint.toString(16).toUpperCase().padStart(4, "0")}}`;
      }
    }
  }
  return escaped;
}
