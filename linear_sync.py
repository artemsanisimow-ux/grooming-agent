"""
Linear Sync
===========
Синхронизирует groomed задачи из Jira в Linear.
- Ищет задачу по названию
- Если найдена — обновляет описание, SP, приоритет
- Если нет — создаёт новую

Использование:
    from linear_sync import LinearSync
    sync = LinearSync()
    sync.upsert_task(groomed_task)
"""

import os
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from i18n import t as tr

# Маппинг приоритетов Jira/агента → Linear
# Linear: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low
PRIORITY_MAP = {
    "P0": 1,  # Urgent
    "P1": 2,  # High
    "P2": 3,  # Medium
    "P3": 4,  # Low
    "High": 2,
    "Medium": 3,
    "Low": 4,
    "Highest": 1,
    "Lowest": 4,
}


class LinearSync:
    def __init__(self):
        self.token = os.getenv("LINEAR_API_KEY", "")
        self.endpoint = "https://api.linear.app/graphql"
        self._team_id = None
        self._team_cache = {}
        self._issue_cache = {}  # title.lower() → issue_id

    def _query(self, query: str, variables: dict = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(
            self.endpoint,
            json=payload,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_team_id(self) -> Optional[str]:
        """Получить ID первой команды."""
        if self._team_id:
            return self._team_id
        if not self.token:
            return None
        try:
            resp = self._query("query { teams { nodes { id name } } }")
            teams = resp.get("data", {}).get("teams", {}).get("nodes", [])
            if teams:
                self._team_id = teams[0]["id"]
                print(tr("linear_team", name=teams[0]["name"], id=self._team_id))
                return self._team_id
        except Exception as e:
            print(f"   ⚠️  Linear get_team_id: {e}")
        return None

    def find_issue_by_title(self, title: str) -> Optional[str]:
        """Найти задачу в Linear по названию. Возвращает ID или None."""
        # Проверяем кэш
        key = title.lower().strip()
        if key in self._issue_cache:
            return self._issue_cache[key]

        try:
            query = """
            query SearchIssues($filter: IssueFilter!) {
                issues(filter: $filter) {
                    nodes { id title }
                }
            }
            """
            variables = {
                "filter": {
                    "title": {"containsIgnoreCase": title[:50]}
                }
            }
            resp = self._query(query, variables)
            issues = resp.get("data", {}).get("issues", {}).get("nodes", [])

            # Ищем точное совпадение
            for issue in issues:
                if issue["title"].lower().strip() == key:
                    self._issue_cache[key] = issue["id"]
                    return issue["id"]

            self._issue_cache[key] = None
            return None
        except Exception as e:
            print(f"   ⚠️  Linear search: {e}")
            return None

    def create_issue(self, task: dict, team_id: str) -> Optional[str]:
        """Создать новую задачу в Linear."""
        priority = PRIORITY_MAP.get(task.get("final_priority") or task.get("priority", "P2"), 3)
        sp = task.get("story_points", 0) or 0

        # Собираем описание
        description = task.get("enriched_description") or task.get("description", "")

        # Добавляем критерии приёмки если есть
        criteria = task.get("acceptance_criteria", [])
        if criteria:
            description += f"\n\n## {'Acceptance Criteria' if __import__('i18n').LANGUAGE == 'en' else 'Критерии приёмки'}\n"
            description += "\n".join(f"- {c}" for c in criteria)

        # Добавляем подзадачи если есть
        subtasks = task.get("subtasks", [])
        if subtasks:
            description += f"\n\n## {'Subtasks' if __import__('i18n').LANGUAGE == 'en' else 'Подзадачи'}\n"
            description += "\n".join(
                f"- {st.get('title', '')} ({st.get('story_points', '?')} SP)"
                for st in subtasks
            )

        mutation = """
        mutation CreateIssue($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                issue { id title }
                success
            }
        }
        """
        variables = {
            "input": {
                "teamId": team_id,
                "title": task.get("title", "Untitled"),
                "description": description,
                "priority": priority,
                "estimate": float(sp) if sp else None,
            }
        }

        try:
            resp = self._query(mutation, variables)
            result = resp.get("data", {}).get("issueCreate", {})
            if result.get("success"):
                issue_id = result["issue"]["id"]
                # Обновляем кэш
                self._issue_cache[task.get("title", "").lower().strip()] = issue_id
                return issue_id
            else:
                errors = resp.get("errors", [])
                print(f"   ❌ Linear create error: {errors}")
                return None
        except Exception as e:
            print(f"   ❌ Linear create: {e}")
            return None

    def update_issue(self, issue_id: str, task: dict) -> bool:
        """Обновить существующую задачу в Linear."""
        priority = PRIORITY_MAP.get(task.get("final_priority") or task.get("priority", "P2"), 3)
        sp = task.get("story_points", 0) or 0

        description = task.get("enriched_description") or task.get("description", "")
        criteria = task.get("acceptance_criteria", [])
        if criteria:
            description += f"\n\n## {'Acceptance Criteria' if __import__('i18n').LANGUAGE == 'en' else 'Критерии приёмки'}\n"
            description += "\n".join(f"- {c}" for c in criteria)

        subtasks = task.get("subtasks", [])
        if subtasks:
            description += f"\n\n## {'Subtasks' if __import__('i18n').LANGUAGE == 'en' else 'Подзадачи'}\n"
            description += "\n".join(
                f"- {st.get('title', '')} ({st.get('story_points', '?')} SP)"
                for st in subtasks
            )

        mutation = """
        mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                issue { id title estimate priority }
                success
            }
        }
        """
        variables = {
            "id": issue_id,
            "input": {
                "description": description,
                "priority": priority,
                "estimate": float(sp) if sp else None,
            },
        }

        try:
            resp = self._query(mutation, variables)
            result = resp.get("data", {}).get("issueUpdate", {})
            return result.get("success", False)
        except Exception as e:
            print(f"   ❌ Linear update: {e}")
            return False

    def upsert_task(self, task: dict) -> dict:
        """
        Upsert задачи в Linear.
        Возвращает {"action": "created/updated/skipped", "linear_id": "..."}
        """
        if not self.token:
            return {"action": "skipped", "reason": "no_token", "linear_id": None}

        title = task.get("title", "")
        if not title:
            return {"action": "skipped", "reason": "no_title", "linear_id": None}

        team_id = self.get_team_id()
        if not team_id:
            return {"action": "skipped", "reason": "no_team", "linear_id": None}

        # Ищем существующую задачу
        existing_id = self.find_issue_by_title(title)

        if existing_id:
            # Обновляем
            success = self.update_issue(existing_id, task)
            if success:
                print(tr("linear_updated", title=title[:50]))
                return {"action": "updated", "linear_id": existing_id}
            else:
                return {"action": "error", "linear_id": existing_id}
        else:
            # Создаём
            new_id = self.create_issue(task, team_id)
            if new_id:
                print(tr("linear_created", title=title[:50]))
                return {"action": "created", "linear_id": new_id}
            else:
                return {"action": "error", "linear_id": None}

    def sync_groomed_tasks(self, tasks: list[dict]) -> dict:
        """
        Синхронизирует список groomed задач в Linear.
        Возвращает статистику.
        """
        stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

        print("\n" + tr("syncing_linear", n=len(tasks)))

        for task in tasks:
            result = self.upsert_task(task)
            action = result.get("action", "error")
            if action == "created":
                stats["created"] += 1
            elif action == "updated":
                stats["updated"] += 1
            elif action == "skipped":
                stats["skipped"] += 1
            else:
                stats["errors"] += 1

        print(tr("sync_stats", created=stats["created"], updated=stats["updated"], skipped=stats["skipped"], errors=stats["errors"]))
        return stats
