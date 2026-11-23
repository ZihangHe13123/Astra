import React, { useMemo } from "react";
import { Box, Text, useStdout } from "ink";
import stringWidth from "string-width";
import { buildApprovalCompactView, escapeApprovalDisplayText, type ApprovalFactItem } from "../approval-preview.js";
import { clampToolDetailOffset } from "../tool-results.js";
import { wrapToolResult } from "../tool-results.js";
import type { ToolApprovalRequest } from "../types.js";
import { useTheme } from "../theme-context.js";
import { consequenceNote } from "@astra/ui-core/approval-choices";
import { approvalDraftRow, approvalPaintGlyph, markLoanShortcut } from "../approval-paint.js";
import { wrapDraft } from "../approval-draft.js";

export interface ToolApprovalPanelProps {
  request: ToolApprovalRequest;
  foregroundRelease?: boolean;
  loanSelected?: boolean;
  inputFenced?: boolean;
  paintEpoch?: string;
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
  foregroundRelease = false,
  loanSelected = false,
  inputFenced = false,
  paintEpoch,
  expanded,
  width,
  queueIndex,
  queueTotal,
  offset,
  pageSize,
  maxHeight,
}: ToolApprovalPanelProps) {
  const theme = useTheme();
  const { stdout } = useStdout();
  const panelWidth = Math.max(20, width);
  const contentWidth = Math.max(16, panelWidth - 4);
  const view = buildApprovalCompactView(request, {
    width: contentWidth,
    position: queueIndex,
    total: queueTotal,
    loanSelected,
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
  let choices = expanded ? view.shortcutHint : collapsedShortcutHint(view.shortcutHint);
  if (request.kind === "computer_foreground_takeover") {
    // Kitty reporting answers on the matching release. Elsewhere one key answers on press, and a
    // legacy Escape never reaches a foreground card, so only N refuses there.
    choices = foregroundRelease ? `按下并松开（不按修饰键）：${choices}` : choices.replace("[N/Esc]", "[N]");
  }
  const note = consequenceNote(request);
  const hasDraft = "draft" in request;
  const draftRows = useMemo(() => request.risk === "secret" ? undefined : wrapDraft(request.draft, contentWidth), [request.draft, request.risk, contentWidth]);
  const heightLimit = maxHeight ?? (hasDraft ? Math.max(1, stdout.rows - 2) : undefined);
  const fullShortcuts = inputFenced ? "输入边界未确认：N 拒绝，Ctrl+C 取消；先按 Ctrl+X，再单独选择。此时不启用 YOLO"
    : hasDraft ? choices + " ·" : choices + "   [Ctrl+Y] YOLO";
  const bodyBudget = (footerRows: number) => heightLimit === undefined ? undefined :
    Math.max(1, heightLimit - 3 - footerRows - (expanded ? 1 : 0));
  const draftBudget = bodyBudget(wrapToolResult(fullShortcuts, contentWidth).length) ?? 0;
  const fitsDraft = !!draftRows && draftRows.length + Number(!!note) + Number(!!request.action_label) + 1 <= draftBudget;
  const shortcuts = hasDraft && !fitsDraft ? `${expanded ? "[N]" : "[N/Esc]"} 丢弃 ·` : fullShortcuts;
  const shortcutRows = wrapToolResult(shortcuts, contentWidth).length;
  const bodyHeight = bodyBudget(shortcutRows);
  return (
    <Box
      width={panelWidth}
      borderStyle="round"
      borderColor={theme.warning}
      paddingX={1}
      flexDirection="column"
    >
      <Text bold color={theme.warning} wrap="truncate-end">
        {approvalPaintGlyph(paintEpoch, foregroundRelease, "h")}{header.slice(1)}{expanded ? ` · 详情 ${safeOffset + 1}-${end}/${view.detailLines.length}` : ""}
      </Text>
      <Box flexDirection="column" height={bodyHeight} overflowY="hidden">
        <Box flexDirection="column" flexShrink={0}>
          {hasDraft && !fitsDraft && <Text color={theme.warning}>{draftRows
            ? "草稿未完整显示，不能发送。请放大终端核对，或按 N 丢弃。"
            : "草稿过长或不完整，不能在此发送。请按 N 丢弃，另行核对完整原文。"}</Text>}
          {note && <Text color={theme.danger} wrap="truncate-end">{truncateDisplay(note, contentWidth)}</Text>}
          {request.action_label && (
            <Text color={theme.text} wrap="truncate-end">{truncateDisplay(`将点击：${escapeApprovalDisplayText(request.action_label)}`, contentWidth)}</Text>
          )}
          {fitsDraft && draftRows && <>
            <Text color={theme.muted} wrap="truncate-end">仅完整显示可发送；↳ 为续行。必要时请放大终端。</Text>
            {draftRows.map((row, i) => <Box key={i} height={1} flexShrink={0}>
              <Text color={theme.text} wrap="truncate-end">{approvalDraftRow(paintEpoch, i, row)}</Text>
            </Box>)}
          </>}
        </Box>
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
        {request.kind === "computer_foreground_takeover"
          ? markLoanShortcut(shortcuts.slice(0, -1), paintEpoch, loanSelected)
          : shortcuts.slice(0, -1)}{approvalPaintGlyph(paintEpoch, foregroundRelease, "t", shortcuts.slice(-1))}
      </Text>
    </Box>
  );
}
