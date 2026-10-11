// MarkdownRenderer — lightweight markdown → Ink renderer
// Supports: **bold**, `inline code`, ```code blocks```, headers, lists, blockquotes,
// links, and terminal-friendly LaTeX math.
import React from "react";
import { Text, Box } from "ink";
import type { UiTheme } from "../theme.js";
import { useTheme } from "../theme-context.js";
import { formatLatexForTerminal } from "../markdown-math.js";

interface Props {
  text: string;
  // baseColor is the default text color for this message role
  baseColor?: string;
  // isDim reduces contrast for secondary content (reasoning, tool results)
  isDim?: boolean;
}

// Split text into segments, each tagged with formatting
type Segment =
  | { t: "text"; v: string }
  | { t: "bold"; v: string }
  | { t: "italic"; v: string }
  | { t: "code"; v: string }
  | { t: "math"; v: string; malformed: boolean }
  | { t: "link"; v: string; url: string };

function parseInline(text: string): Segment[] {
  const segments: Segment[] = [];
  // Code is matched before math so `$HOME` inside backticks remains code.
  const re = /`([^`]+)`|\\\((.+?)\\\)|(?<!\\)\$(?!\$)(.+?)(?<!\\)\$|(\*\*|__)(.+?)\4|\*([^*\n]+?)\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      segments.push({ t: "text", v: text.slice(last, m.index) });
    }
    if (m[1]) {
      segments.push({ t: "code", v: m[1] });
    } else if (m[2] || m[3]) {
      const formatted = formatLatexForTerminal(m[2] || m[3]);
      segments.push({ t: "math", v: formatted.text, malformed: formatted.malformed });
    } else if (m[4]) {
      segments.push({ t: "bold", v: m[5] });
    } else if (m[6]) {
      segments.push({ t: "italic", v: m[6] });
    } else if (m[7]) {
      segments.push({ t: "link", v: m[7], url: m[8] });
    }
    last = re.lastIndex;
  }
  if (last < text.length) {
    segments.push({ t: "text", v: text.slice(last) });
  }
  return segments;
}

function renderSegments(segments: Segment[], opts: { color?: string; dim?: boolean }, theme: UiTheme) {
  return segments.map((seg, i) => {
    switch (seg.t) {
      case "bold":
        return <Text key={i} bold={theme.strongBold} color={theme.strongUsesBaseColor ? opts.color : theme.emphasis}>{seg.v}</Text>;
      case "code":
        return (
          <Text key={i} color={theme.code} backgroundColor={theme.codeBackground} dimColor={opts.dim}>
            {theme.codeDelimiters ? `\`${seg.v}\`` : seg.v}
          </Text>
        );
      case "math":
        return (
          <Text key={i} color={seg.malformed ? theme.warning : theme.accentAlt}>
            {seg.malformed ? "⚠ " : ""}{seg.v}
          </Text>
        );
      case "italic":
        return <Text key={i} italic color={theme.muted} dimColor={opts.dim}>{seg.v}</Text>;
      case "link":
        return (
          <Text key={i} color={theme.accentAlt} underline dimColor={opts.dim}>
            {seg.v}
          </Text>
        );
      default:
        if (opts.dim) {
          return <Text key={i} dimColor color={opts.color}>{seg.v}</Text>;
        }
        return <Text key={i} color={opts.color}>{seg.v}</Text>;
    }
  });
}

export function MarkdownRenderer({ text, baseColor, isDim }: Props) {
  const theme = useTheme();
  if (!text) return null;

  const lines = text.split("\n");
  const elements: React.ReactElement[] = [];
  let inCodeBlock = false;
  let codeLang = "";
  let codeContent = "";
  let codeLineStart = 0;
  let mathDelimiter = "";
  let mathContent: string[] = [];
  let mathLineStart = 0;

  const renderMathBlock = (source: string, key: string, forceMalformed = false) => {
    const formatted = formatLatexForTerminal(source);
    const malformed = forceMalformed || formatted.malformed;
    elements.push(
      <Box
        key={key}
        flexDirection="column"
        marginLeft={1}
        borderStyle="single"
        borderColor={malformed ? theme.warning : theme.border}
        paddingX={1}
      >
        <Text color={malformed ? theme.warning : theme.accentAlt}>
          {malformed ? "⚠ " : ""}{formatted.text}
        </Text>
      </Box>
    );
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Code block detection
    if (line.startsWith("```")) {
      if (inCodeBlock) {
        // End code block
        elements.push(
          <Box key={`code-${codeLineStart}`} flexDirection="column" marginLeft={1} borderStyle={theme.chrome?.frameStyle ?? "round"} borderColor={theme.border} paddingX={1}>
            {codeLang && (
              <Text color={theme.muted}>{codeLang}</Text>
            )}
            {codeContent.split("\n").map((cl, j) => (
              <Text key={j} color={theme.codeBlock} backgroundColor={theme.codeBlockBackground}>{cl}</Text>
            ))}
          </Box>
        );
        inCodeBlock = false;
        codeContent = "";
        codeLang = "";
      } else {
        // Start code block
        inCodeBlock = true;
        codeLang = line.slice(3).trim();
        codeLineStart = i;
        codeContent = "";
      }
      continue;
    }

    if (inCodeBlock) {
      codeContent += (codeContent ? "\n" : "") + line;
      continue;
    }

    const trimmed = line.trim();
    if (mathDelimiter) {
      const closesMath = trimmed === mathDelimiter;
      if (closesMath) {
        renderMathBlock(mathContent.join("\n"), `math-${mathLineStart}`);
        mathDelimiter = "";
        mathContent = [];
      } else {
        mathContent.push(line);
      }
      continue;
    }

    const oneLineDollarMath = trimmed.match(/^\$\$(.+)\$\$$/);
    const oneLineBracketMath = trimmed.match(/^\\\[(.+)\\\]$/);
    if (oneLineDollarMath || oneLineBracketMath) {
      renderMathBlock((oneLineDollarMath || oneLineBracketMath)![1], `math-${i}`);
      continue;
    }
    if (trimmed === "$$" || trimmed === "\\[") {
      mathDelimiter = trimmed === "$$" ? "$$" : "\\]";
      mathLineStart = i;
      mathContent = [];
      continue;
    }

    // Empty line
    if (!line.trim()) {
      elements.push(<Text key={`empty-${i}`}>{" "}</Text>);
      continue;
    }

    // Headers (#, ##, etc)
    const hMatch = line.match(/^(#{1,6})\s+(.+)/);
    if (hMatch) {
      const level = hMatch[1].length;
      // Use brighter colors for headers to stand out
      const hColor = level <= 2 ? theme.header : theme.accentAlt;
      const prefix = "#".repeat(level) + " ";
      elements.push(
        <Text key={`h-${i}`} bold color={hColor}>
          {prefix}
          {renderSegments(parseInline(hMatch[2]), { color: hColor }, theme)}
        </Text>
      );
      continue;
    }

    // Blockquote (>)
    const bqMatch = line.match(/^>\s?(.*)/);
    if (bqMatch) {
      elements.push(
        <Box key={`bq-${i}`} marginLeft={1}>
          <Text color={theme.subtle}>│ </Text>
          <Text color={theme.muted} dimColor={isDim}>
            {renderSegments(parseInline(bqMatch[1]), { color: theme.muted, dim: isDim }, theme)}
          </Text>
        </Box>
      );
      continue;
    }

    // Unordered list (- or *)
    const ulMatch = line.match(/^[-*]\s+(.+)/);
    if (ulMatch) {
      elements.push(
        <Text key={`ul-${i}`}>
          <Text color={theme.accent}> ● </Text>
          {renderSegments(parseInline(ulMatch[1]), { color: baseColor, dim: isDim }, theme)}
        </Text>
      );
      continue;
    }

    // Ordered list (1. 2. etc)
    const olMatch = line.match(/^(\d+)\.\s+(.+)/);
    if (olMatch) {
      elements.push(
        <Text key={`ol-${i}`}>
          <Text color={theme.accent}> {olMatch[1]}. </Text>
          {renderSegments(parseInline(olMatch[2]), { color: baseColor, dim: isDim }, theme)}
        </Text>
      );
      continue;
    }

    // Horizontal rule
    if (/^[-*_]{3,}$/.test(line.trim())) {
      elements.push(<Text key={`hr-${i}`} dimColor>{"─".repeat(40)}</Text>);
      continue;
    }

    // Regular paragraph
    elements.push(
      <Text key={`p-${i}`}>
        {renderSegments(parseInline(line), { color: baseColor, dim: isDim }, theme)}
      </Text>
    );
  }

  // If code block never closed, render what we have
  if (inCodeBlock && codeContent) {
    elements.push(
      <Box key={`code-${codeLineStart}`} flexDirection="column" marginLeft={1} borderStyle={theme.chrome?.frameStyle ?? "round"} borderColor={theme.border} paddingX={1}>
        {codeContent.split("\n").map((cl, j) => (
          <Text key={j} color={theme.codeBlock} backgroundColor={theme.codeBlockBackground}>{cl}</Text>
        ))}
      </Box>
    );
  }
  if (mathDelimiter) {
    renderMathBlock(mathContent.join("\n"), `math-${mathLineStart}`, true);
  }

  return <>{elements}</>;
}
