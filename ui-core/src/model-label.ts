/** The name shown for the current model. An alias such as Claude Code's `sonnet` means whatever the
 * installed CLI resolves it to, so once an answer has shown that (`claude-sonnet-5`) the vendor prefix
 * is dropped and the version shown (`sonnet-5`): short enough for a status bar, and `sonnet-5` stays
 * distinct from `sonnet-5-5` where a longer name would be cut. */
export function modelDisplayName(model: string, served?: string): string {
  if (!served || served === model) return model;
  return served.replace(/^claude-/, "");
}
