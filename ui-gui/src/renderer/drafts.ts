import type { Preferences } from "../bridge.js";

export function migrateBlankDraft(preferences: Preferences, workspace: string): Preferences {
  const key = `new:${workspace}`;
  const drafts = { ...preferences.drafts };
  const attachments = { ...preferences.attachments };
  if (drafts.new && !drafts[key]) { drafts[key] = drafts.new; delete drafts.new; }
  if (attachments.new?.length) {
    attachments[key] = [...new Set([...(attachments[key] || []), ...attachments.new])]; delete attachments.new;
  }
  return { ...preferences, drafts, attachments };
}
