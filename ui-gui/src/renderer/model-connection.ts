import type { UIEvent } from "@astra/ui-core/session-state";

/** Match the exact configured endpoint; never substitute a billing route. */
export function modelConnection(info: UIEvent | undefined, model?: UIEvent) {
  const providers: UIEvent[] = info?.providers || [];
  const routes: UIEvent[] = info?.connection_routes || [];
  const provider = providers.find(p => p.id === model?.provider_id);
  const endpoint = String(model?.endpoint || provider?.endpoint || "").replace(/\/+$/, "");
  const route = routes.find(r => r.base_url && String(r.base_url).replace(/\/+$/, "") === endpoint)
    || routes.find(r => r.id === model?.provider_id && !r.base_url)
    || routes.find(r => r.id === "custom");
  return { configured: provider?.connected as boolean | undefined, route, endpoint,
    label: String(model?.provider || provider?.label || "此提供商") };
}
