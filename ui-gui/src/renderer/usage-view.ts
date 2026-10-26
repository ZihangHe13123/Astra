/** Today's model usage against the person's daily budget, for the line under the composer. */
export type UsageToday = {
  tokens: number; cost: number | null; currency: string;
  budget: { daily_tokens?: number; daily_cost?: number }; over_budget: boolean;
};

export function tokenLabel(tokens: number): string {
  return tokens >= 1_000_000 ? `${(tokens / 1_000_000).toFixed(1)}M` : tokens >= 1000 ? `${Math.round(tokens / 1000)}k` : String(tokens);
}

/** Shown only once a budget is set: how much of it today used, and whether it is over. */
export function usageStatus(usage: UsageToday | undefined): { text: string; over: boolean } | undefined {
  if (!usage) return undefined;
  if (usage.budget.daily_cost) {
    const spent = usage.cost ?? 0;
    return { text: `今日 ${usage.currency}${spent.toFixed(2)} / ${usage.currency}${usage.budget.daily_cost}`, over: usage.over_budget };
  }
  if (usage.budget.daily_tokens) {
    return { text: `今日 ${tokenLabel(usage.tokens)} / ${tokenLabel(usage.budget.daily_tokens)} token`, over: usage.over_budget };
  }
  return undefined;
}
