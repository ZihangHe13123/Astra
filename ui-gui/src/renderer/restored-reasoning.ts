import type { Message } from '@astra/ui-core/session-state';

/** Display rows only. Pagination and source navigation keep the original assistant identity. */
export function expandRestoredReasoning(messages: Message[], show: boolean): Message[] {
  if (!show) return messages;
  const rows: Message[] = [];
  let expanded = false;
  for (const message of messages) {
    if (message.role === 'assistant' && typeof message.reasoning_content === 'string' && message.reasoning_content.trim()) {
      rows.push({ id: `reasoning:${message.id}`, role: 'reasoning', content: message.reasoning_content,
        timestamp: message.timestamp, source_ref: message.source_ref });
      expanded = true;
    }
    rows.push(message);
  }
  return expanded ? rows : messages;
}
