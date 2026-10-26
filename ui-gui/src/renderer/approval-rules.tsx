import React from "react";

type Rule = { key: string; label: string; tool: string; created_at: string };

/**
 * The person's "以后都允许" rules: each was added from an approval card after the same request had been
 * approved several times. Removing one makes that request ask again, from the next time on.
 */
export function ApprovalRules({ fail }: { fail: (error: unknown) => void }) {
  const [rules, setRules] = React.useState<Rule[]>();
  const [busy, setBusy] = React.useState(false);
  const load = React.useCallback(async () => setRules((await window.astra.approvalRules("list")).rules || []), []);
  React.useEffect(() => { void load().catch(fail); }, [load, fail]);
  const remove = async (key: string) => {
    setBusy(true);
    try { await window.astra.approvalRules("remove", key); await load(); } catch (error) { fail(error); }
    finally { setBusy(false); }
  };
  return <section className="approval-rules">
    <h3>以后都允许的操作</h3>
    <p className="muted small">你在审批卡上点过「以后都允许」的操作，不再询问。删除后，下次遇到会重新问你。付款、发送、删除、发布类操作永远不会出现在这里。</p>
    {rules === undefined ? <p className="muted small">正在读取…</p> : !rules.length ? <p className="muted small">还没有。</p> :
      <ul className="memory-list">{rules.map(rule => <li key={rule.key} className="memory-row" data-rule={rule.key}>
        <p>{rule.label}</p>
        <div className="memory-actions"><span className="muted small">{rule.created_at.slice(0, 10)}</span>
          <button disabled={busy} onClick={() => { void remove(rule.key); }}>删除</button></div>
      </li>)}</ul>}
  </section>;
}
