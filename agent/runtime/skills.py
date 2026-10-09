"""Local, human-readable skills with conservative write boundaries."""

from __future__ import annotations

from agent.runtime.paths import state_path

import os
import re
from pathlib import Path
from typing import Any

from .card_skill import CARD_SKILL_NAME, card_skill_content, card_skill_listed, card_skill_metadata
from .core_rules import CORE_SKILL_NAME, core_skill_content, core_skill_metadata
from .project_trust import ProjectTrust
from .learning_scope import learning_scope, scope_key
from .skill_provenance import automatic_names, read_learning_state


_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CATEGORY_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SUPPORT_DIRS = {"references", "templates", "scripts", "assets"}
_MAX_SKILL_FILE = 100_000


def default_skills_path() -> Path:
    override = os.getenv("AGENT_SKILLS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return state_path("skills")


class SkillStore:
    def __init__(self, root: str | Path | None = None):
        explicit_root = root is not None
        self.root = Path(root) if explicit_root else default_skills_path()
        self.read_allowed = explicit_root or ProjectTrust.for_path(Path.cwd()).permits(self.root)

    @staticmethod
    def _validate_name(name: str) -> str:
        value = str(name).strip().lower()
        if not _SKILL_NAME.fullmatch(value):
            raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
        return value

    @staticmethod
    def _validate_category(category: str | None) -> str:
        value = str(category or "").strip().lower()
        if not value:
            return ""
        if not _CATEGORY_NAME.fullmatch(value):
            raise ValueError("Skill category must use lowercase letters, digits, and hyphens")
        return value

    def _skill_dir(self, name: str, category: str | None = None) -> Path:
        skill_name = self._validate_name(name)
        category_name = self._validate_category(category)
        return self.root / category_name / skill_name if category_name else self.root / skill_name

    def _existing_skill_dir(self, name: str) -> Path | None:
        """Resolve a skill from the legacy root or the new one-level tree."""
        skill_name = self._validate_name(name)
        if skill_name == CORE_SKILL_NAME:
            raise ValueError("astra-core is a read-only built-in Skill; edit the packaged astra.md through development")
        if skill_name == CARD_SKILL_NAME:
            raise ValueError("interactive-cards is a read-only built-in Skill")
        candidates = [self.root / skill_name]
        candidates.extend(sorted(self.root.glob(f"*/{skill_name}")))
        existing = [path for path in candidates if (path / "SKILL.md").is_file()]
        if len(existing) > 1:
            raise ValueError(f"Skill name is ambiguous across categories: {skill_name}")
        return existing[0] if existing else None

    def _iter_skill_paths(self) -> list[Path]:
        paths = list(self.root.glob("*/SKILL.md"))
        paths.extend(self.root.glob("*/*/SKILL.md"))
        return sorted(set(path for path in paths if path.is_file()))

    def _target(self, name: str, file_path: str = "SKILL.md") -> Path:
        skill_dir = (self._existing_skill_dir(name) or self._skill_dir(name)).resolve()
        relative = Path(str(file_path).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Skill file path must stay inside the skill directory")
        if any(":" in part for part in relative.parts):
            raise ValueError("Skill file path components cannot contain ':' (NTFS ADS protection)")
        if relative.as_posix() != "SKILL.md" and (not relative.parts or relative.parts[0] not in _SUPPORT_DIRS):
            raise ValueError("Supporting files must be under references/, templates/, scripts/, or assets/")
        target = (skill_dir / relative).resolve()
        if target != skill_dir and skill_dir not in target.parents:
            raise ValueError("Skill file path escapes the skill directory")
        return target

    @staticmethod
    def _frontmatter(content: str) -> dict[str, str]:
        if not content.startswith("---\n"):
            raise ValueError("SKILL.md must begin with YAML frontmatter")
        end = content.find("\n---", 4)
        if end < 0:
            raise ValueError("SKILL.md frontmatter is not closed")
        # Indented lines continue the previous key: folded (>) or literal (|)
        # blocks and wrapped plain values all become one line, as the catalog
        # shows each description on a single line.
        parts: dict[str, list[str]] = {}
        key = ""
        for line in content[4:end].splitlines():
            if not line.strip():
                continue
            if line[:1] in (" ", "\t"):
                if key:
                    parts[key].append(line.strip())
                continue
            if ":" not in line:
                key = ""
                continue
            key, value = (item.strip() for item in line.split(":", 1))
            parts[key] = [value]
        values: dict[str, str] = {}
        for key, (first, *rest) in parts.items():
            if re.fullmatch(r"[>|][1-9+-]{0,2}", first):
                values[key] = " ".join(rest)
            else:
                values[key] = " ".join([first, *rest]).strip().strip("\"'")
        if not values.get("name") or not values.get("description"):
            raise ValueError("SKILL.md frontmatter requires name and description")
        return values

    def list(self) -> list[dict[str, Any]]:
        items = [core_skill_metadata()]
        if card_skill_listed():
            items.append(card_skill_metadata())
        if not self.read_allowed:
            return items
        current_scope = scope_key(learning_scope())
        try:
            automatic = set(automatic_names(read_learning_state(self.root)))
        except (OSError, ValueError):
            automatic = set()
        # A built-in name is taken everywhere, also where the built-in itself is not listed.
        seen_names: set[str] = {CORE_SKILL_NAME, CARD_SKILL_NAME}
        for path in self._iter_skill_paths():
            try:
                content = path.read_text(encoding="utf-8")
                meta = self._frontmatter(content)
                if meta.get("astra_learning_scope") and meta["astra_learning_scope"] != current_scope:
                    continue
                name = self._validate_name(meta["name"])
                if name != path.parent.name:
                    continue
                if name in seen_names:
                    continue
                files = sum(1 for item in path.parent.rglob("*") if item.is_file())
                category = path.parent.parent.name if path.parent.parent != self.root else "general"
                items.append({
                    "name": name,
                    "description": meta["description"],
                    "files": files,
                    "category": category,
                    "origin": "auto" if name in automatic and category == "learned" else "user",
                })
                seen_names.add(name)
            except (OSError, UnicodeError, ValueError):
                continue
        return items

    def catalog_prompt(self) -> str:
        from .system_suffix import SKILL_CATALOG_INTRO

        items = self.list()
        if not items:
            return ""
        lines = [
            "<available-skills>",
            SKILL_CATALOG_INTRO,
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(item["category"], []).append(item)
        for category, category_items in grouped.items():
            lines.append(f"[{category}]")
            lines.extend(f"- {item['name']}: {item['description']} [origin: {item['origin']}]" for item in category_items)
        lines.append("</available-skills>")
        return "\n".join(lines)

    def view(self, name: str, file_path: str = "SKILL.md") -> str:
        if self._validate_name(name) == CORE_SKILL_NAME:
            if file_path not in {"SKILL.md", "astra.md"}:
                raise ValueError("astra-core only exposes SKILL.md (alias: astra.md)")
            return core_skill_content()
        if self._validate_name(name) == CARD_SKILL_NAME:
            # Readable wherever a conversation that lists it is continued.
            if file_path != "SKILL.md":
                raise ValueError("interactive-cards only exposes SKILL.md")
            return card_skill_content()
        if not self.read_allowed:
            raise PermissionError("Project-local skills are disabled until the project is trusted")
        target = self._target(name, file_path)
        if not target.is_file():
            raise ValueError(f"Skill file not found: {name}/{file_path}")
        content = target.read_text(encoding="utf-8")
        if len(content) > _MAX_SKILL_FILE:
            raise ValueError(f"Skill file is too large (max {_MAX_SKILL_FILE} characters)")
        return content

    def create(self, name: str, content: str, category: str | None = None) -> dict[str, Any]:
        name = self._validate_name(name)
        category = self._validate_category(category) or None
        if self._existing_skill_dir(name) is not None:
            raise ValueError(f"Skill already exists: {name}")
        target = self._skill_dir(name, category) / "SKILL.md"
        text = str(content).replace("\r\n", "\n").strip() + "\n"
        if len(text) > _MAX_SKILL_FILE:
            raise ValueError("SKILL.md is too large")
        meta = self._frontmatter(text)
        if self._validate_name(meta["name"]) != name:
            raise ValueError("Frontmatter name must match the skill directory name")
        self._atomic_write(target, text)
        return {"action": "created", "name": name, "file": "SKILL.md", "category": category or "general"}

    def patch(self, name: str, old_string: str, new_string: str, file_path: str = "SKILL.md") -> dict[str, Any]:
        target = self._target(name, file_path)
        if not target.is_file():
            raise ValueError(f"Skill file not found: {name}/{file_path}")
        old = str(old_string)
        if not old or old == new_string:
            raise ValueError("Patch requires distinct non-empty old_string and new_string")
        content = target.read_text(encoding="utf-8")
        count = content.count(old)
        if count != 1:
            raise ValueError(f"old_string must match exactly once (found {count})")
        updated = content.replace(old, str(new_string), 1)
        if len(updated) > _MAX_SKILL_FILE:
            raise ValueError("Updated skill file is too large")
        if file_path == "SKILL.md":
            self._frontmatter(updated)
        self._atomic_write(target, updated)
        return {"action": "patched", "name": self._validate_name(name), "file": file_path, "replacements": 1}

    def write_file(self, name: str, file_path: str, content: str) -> dict[str, Any]:
        if file_path == "SKILL.md":
            raise ValueError("Use create or patch for SKILL.md")
        skill_dir = self._existing_skill_dir(name)
        if skill_dir is None:
            raise ValueError(f"Skill not found: {name}")
        target = self._target(name, file_path)
        text = str(content).replace("\r\n", "\n")
        if len(text) > _MAX_SKILL_FILE:
            raise ValueError("Supporting file is too large")
        existed = target.exists()
        self._atomic_write(target, text)
        return {"action": "updated" if existed else "created", "name": self._validate_name(name), "file": file_path}

    def raw_file(self, name: str, file_path: str) -> str | None:
        target = self._target(name, file_path)
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def restore_file(self, name: str, file_path: str, content: str | None) -> None:
        target = self._target(name, file_path)
        if content is None:
            target.unlink(missing_ok=True)
            skill_dir = self._existing_skill_dir(name) or self._skill_dir(name)
            for directory in sorted((p for p in skill_dir.rglob("*") if p.is_dir()), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                skill_dir.rmdir()
            except OSError:
                pass
            if skill_dir.parent != self.root:
                try:
                    skill_dir.parent.rmdir()
                except OSError:
                    pass
            return
        self._atomic_write(target, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
