"""Shared parsing for /skills across CLI frontends."""

from agent.runtime.skills import SkillStore


SKILLS_USAGE = (
    "Usage:\n"
    "  /skills\n"
    "  /skills show <name> [file]\n"
    "  /skills create [name] [description]   # draft together before saving\n"
    "  /skills create --template <name> <description>   # create an empty template\n"
    "  /skills create --template <name> <description> --now   # save and apply at an idle boundary\n"
    "  /guidance [status] | /guidance refresh --now"
)


def execute_skill_command(store: SkillStore, args: list[str], *, agent=None, busy: bool = False) -> tuple[str, str]:
    apply_now = "--now" in args
    args = [arg for arg in args if arg != "--now"]
    action = args[0].lower() if args else "list"
    try:
        if apply_now and (agent is None or action != "create"):
            return "", "Use /guidance refresh --now to apply saved guidance."
        if apply_now and busy:
            return "", "Finish or cancel the active task before saving and applying a skill."
        if agent is not None:
            agent._ensure_session_guidance()
        if action in {"list", "status"}:
            items = store.list()
            if not items:
                return f"Skills directory: {store.root}\nNo local skills installed.", ""
            lines = [f"Local skills ({len(items)}):"]
            lines.extend(
                f"  [{item['origin']}] [{item['category']}] {item['name']} — {item['description']} ({item['files']} files)"
                for item in items
            )
            lines.append("Origins: auto = automatically summarized; user = user-added/protected; builtin = packaged rules.")
            if agent is not None:
                from .guidance_commands import format_guidance_status
                lines.append(format_guidance_status(agent.guidance_status()))
            return "\n".join(lines), ""
        if action == "show" and len(args) >= 2:
            return store.view(args[1], args[2] if len(args) >= 3 else "SKILL.md"), ""
        if action == "create" and len(args) >= 3:
            name = args[1]
            description = " ".join(args[2:]).strip()
            content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n## When to use\n\nDescribe the reusable trigger.\n\n## Workflow\n\n1. Add the verified procedure.\n"
            result = store.create(name, content, category="user")
            notice = "Saved. Available in a new conversation; use /guidance refresh --now to apply here."
            if apply_now:
                from .guidance_commands import execute_guidance_command
                notice, error = execute_guidance_command(agent, ["refresh", "--now"])
                if error:
                    return "", error
            return f"Created user skill {result['name']} at {store.root / 'user' / result['name'] / 'SKILL.md'}. Excluded from /learn review.\n{notice}", ""
        return "", SKILLS_USAGE
    except (OSError, UnicodeError, ValueError) as exc:
        return "", str(exc)
