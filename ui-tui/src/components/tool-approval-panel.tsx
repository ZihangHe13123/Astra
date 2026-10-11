import React from "react";
import { Box, Text } from "ink";
import stringWidth from "string-width";
import { buildApprovalCompactView, type ApprovalFactItem } from "../approval-preview.js";
import { clampToolDetailOffset } from "../tool-results.js";
import { wrapToolResult } from "../tool-results.js";
import type { ToolApprovalRequest } from "../types.js";
import { useTheme } from "../theme-context.js";

export interface ToolApprovalPanelProps {
  request: ToolApprovalRequest;
  expanded: boolean;
  width: number;
  queueIndex: number;
  queueTotal: number;
  offset: number;
  pageSize: number;
  maxHeight?: number;
}

function truncateDisplay(value: string, maxWidth: number): string {
  const singleLine = value.replace(/\s+/g, " ").trim();
  if (stringWidth(singleLine) <= maxWidth) return singleLine;
  if (maxWidth <= 1) return "…";
  let output = "";
  for (const character of singleLine) {
    if (stringWidth(output + character) >= maxWidth) break;
    output += character;
  }
  return `${output}…`;
}

function factValue(facts: ApprovalFactItem[], label: string): string | undefined {
  return facts.find((fact) => fact.label === label)?.value;
}

function compactFactLines(facts: ApprovalFactItem[], narrow: boolean): string[] {
  const compactFacts = [
    ["风险", factValue(facts, "风险")],
    ["位置", factValue(facts, "运行位置")],
    ["范围", factValue(facts, "授权范围")],
    ["操作数", factValue(facts, "操作数")],
  ].filter((fact): fact is [string, string] => Boolean(fact[1]));
  const rendered = compactFacts.map(([label, value]) => `${label}：${value}`);
  if (!narrow || rendered.length < 3) return [rendered.join(" · ")];
  return [rendered.slice(0, 2).join(" · "), rendered.slice(2).join(" · ")];
}

function collapsedShortcutHint(shortcutHint: string): string {
  const onceLabel = shortcutHint.replace("[Y/回车] 仅本次允许", "[Y/回车] 允许一次");
  const denyMarker = "[N/Esc]";
  const denyIndex = onceLabel.indexOf(denyMarker);
  if (denyIndex < 0) return `${onceLabel}   [V] 完整详情`.trim();
  const allow = onceLabel.slice(0, denyIndex).trimEnd();
  const deny = onceLabel.slice(denyIndex);
  return `${allow}   [V] 完整详情   ${deny}`.trim();
}

export function ToolApprovalPanel({
  request,
  expanded,
  width,
  queueIndex,
  queueTotal,
  offset,
  pageSize,
  maxHeight,
}: ToolApprovalPanelProps) {
  const theme = useTheme();
  const panelWidth = Math.max(20, width);
  const contentWidth = Math.max(16, panelWidth - 4);
  const view = buildApprovalCompactView(request, {
    width: contentWidth,
    position: queueIndex,
    total: queueTotal,
    expanded,
  });
  const safeOffset = clampToolDetailOffset(offset, view.detailLines.length, pageSize);
  const visibleDetailLines = view.detailLines.slice(safeOffset, safeOffset + pageSize);
  const end = Math.min(view.detailLines.length, safeOffset + pageSize);
  const tool = factValue(view.facts, "工具") ?? view.title;
  const location = factValue(view.facts, "运行位置") ?? "当前运行环境";
  const header = truncateDisplay(
    `◆ 审批请求 · ${view.queuePosition} · ${tool} · ${location}`,
    contentWidth,
  );
  const explanationLabel = `${view.modelExplanation.label}：`;
  const explanation = truncateDisplay(
    view.modelExplanation.text,
    Math.max(16, contentWidth * 2 - stringWidth(explanationLabel)),
  );
  const factLines = compactFactLines(view.facts, panelWidth < 90);
  const targetLine = view.targetPreview.lines[0]
    ? truncateDisplay(
        `${view.targetPreview.label}：${view.targetPreview.lines[0]}`,
        contentWidth,
      )
    : "";
  const shortcuts = (expanded ? view.shortcutHint : collapsedShortcutHint(view.shortcutHint)) + "   [Ctrl+Y] YOLO";
  const shortcutRows = wrapToolResult(shortcuts, contentWidth).length;
  const bodyHeight = maxHeight === undefined ? undefined : Math.max(1, maxHeight - 3 - shortcutRows - (expanded ? 1 : 0));
  return (
    <Box
      width={panelWidth}
      borderStyle="round"
      borderColor={theme.warning}
      paddingX={1}
      flexDirection="column"
    >
      <Text bold color={theme.warning} wrap="truncate-end">
        {header}{expanded ? ` · 详情 ${safeOffset + 1}-${end}/${view.detailLines.length}` : ""}
      </Text>
      <Box flexDirection="column" height={bodyHeight} overflowY="hidden">
      {expanded ? visibleDetailLines.map((line, index) => (
        <Text key={`${request.request_id}-${safeOffset + index}`} color={theme.text}>
          {line || " "}
        </Text>
      )) : (
        <>
          <Text bold color={theme.text}>
            <Text color={theme.warning}>{explanationLabel}</Text>{explanation}
          </Text>
          {factLines.map((line, index) => (
            <Text key={`${request.request_id}-fact-${index}`} color={theme.muted} wrap="truncate-end">
              {truncateDisplay(line, contentWidth)}
            </Text>
          ))}
          {targetLine && (
            <Text color={theme.text} wrap="truncate-end">{targetLine}</Text>
          )}
        </>
      )}
      </Box>
      {expanded && (
        <Text color={theme.muted} wrap="truncate-end">↑/↓ 滚动 · PgUp/PgDn 翻页 · V/Esc 折叠</Text>
      )}
      <Text color={theme.accentAlt}>
        {shortcuts}
      </Text>
    </Box>
  );
}
