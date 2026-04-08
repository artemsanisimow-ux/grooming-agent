"""
Jira Sync
=========
Обновляет groomed задачи в Jira:
- Описание (enriched_description + acceptance criteria)
- Story points (customfield_10016)
- Приоритет

Работает с Team-managed (Next-gen) проектами на Free плане.

Использование:
    from jira_sync import JiraSync
    sync = JiraSync()
    sync.upsert_task(groomed_task)
"""

import os
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from i18n import t as tr

# Маппинг приоритетов агента → Jira
PRIORITY_MAP = {
    "P0": "Highest",
    "P1": "High",
    "P2": "Medium",
    "P3": "Low",
}


class JiraSync:
    def __init__(self):
        self.url = os.getenv("JIRA_URL", "").rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "")
        self.token = os.getenv("JIRA_API_TOKEN", "")
        self.auth = (self.email, self.token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _put(self, endpoint: str, data: dict) -> requests.Response:
        resp = requests.put(
            f"https://{self.url}{endpoint}",
            json=data,
            auth=self.auth,
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp

    def _get(self, endpoint: str, params: dict = None) -> dict:
        resp = requests.get(
            f"https://{self.url}{endpoint}",
            params=params,
            auth=self.auth,
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _build_description_adf(self, task: dict) -> dict:
        """
        Строим описание в формате Atlassian Document Format (ADF).
        Jira API v3 требует ADF вместо plain text.
        """
        content = []

        # Основное описание
        description = task.get("enriched_description") or task.get("description", "")
        if description:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": description}]
            })

        # Acceptance criteria
        criteria = task.get("acceptance_criteria", [])
        if criteria:
            content.append({
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "Acceptance Criteria" if __import__("i18n").LANGUAGE == "en" else "Критерии приёмки"}]
            })
            bullet_items = []
            for c in criteria:
                bullet_items.append({
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": str(c)}]
                    }]
                })
            content.append({
                "type": "bulletList",
                "content": bullet_items
            })

        # Подзадачи
        subtasks = task.get("subtasks", [])
        if subtasks:
            content.append({
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "Subtasks" if __import__("i18n").LANGUAGE == "en" else "Подзадачи"}]
            })
            sub_items = []
            for st in subtasks:
                sub_items.append({
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": f"{st.get('title', '')} ({st.get('story_points', '?')} SP)"}]
                    }]
                })
            content.append({
                "type": "bulletList",
                "content": sub_items
            })

        # Confidence
        confidence = task.get("confidence", "")
        sp = task.get("story_points", 0)
        if confidence:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": f'{"Estimate" if __import__("i18n").LANGUAGE == "en" else "Оценка"}: {sp} SP | {"Confidence" if __import__("i18n").LANGUAGE == "en" else "Уверенность"}: {confidence}', "marks": [{"type": "em"}]}]
            })

        return {
            "version": 1,
            "type": "doc",
            "content": content if content else [{
                "type": "paragraph",
                "content": [{"type": "text", "text": "No description" if __import__("i18n").LANGUAGE == "en" else "Нет описания"}]
            }]
        }

    def update_issue(self, issue_key: str, task: dict) -> bool:
        """
        Обновить задачу в Jira.
        Обновляет: описание, story points, приоритет.
        """
        if not all([self.url, self.email, self.token]):
            return False

        # Пропускаем демо задачи
        if issue_key.startswith("DEMO"):
            return True

        sp = task.get("story_points", 0) or 0
        priority_name = PRIORITY_MAP.get(
            task.get("final_priority") or task.get("priority", "P2"),
            "Medium"
        )

        payload = {
            "fields": {
                "description": self._build_description_adf(task),
                "customfield_10016": float(sp) if sp else None,
                "priority": {"name": priority_name},
            }
        }

        # Убираем None поля
        payload["fields"] = {k: v for k, v in payload["fields"].items() if v is not None}

        # Пробуем разные комбинации полей — Next-gen Jira капризна
        attempts = [
            # Попытка 1: всё
            payload,
            # Попытка 2: без priority
            {"fields": {k: v for k, v in payload["fields"].items() if k != "priority"}},
            # Попытка 3: только description
            {"fields": {"description": payload["fields"].get("description")}},
            # Попытка 4: только SP
            {"fields": {"customfield_10016": payload["fields"].get("customfield_10016")}},
        ]

        for i, attempt in enumerate(attempts):
            # Убираем None поля
            attempt["fields"] = {k: v for k, v in attempt["fields"].items() if v is not None}
            if not attempt["fields"]:
                continue
            try:
                self._put(f"/rest/api/3/issue/{issue_key}", attempt)
                if i > 0:
                    print(f"   ⚠️  Jira {issue_key}: simplified payload used (attempt {i+1})" if __import__("i18n").LANGUAGE == "en" else f"   ⚠️  Jira {issue_key}: использован упрощённый payload (попытка {i+1})")
                return True
            except requests.exceptions.HTTPError as e:
                if i == len(attempts) - 1:
                    print(tr("jira_failed", key=issue_key, code=e.response.status_code))
                continue
            except Exception as e:
                print(f"   ⚠️  Jira update {issue_key}: {e}")
                return False
        return False

    def upsert_task(self, task: dict) -> dict:
        """
        Обновляет задачу в Jira если она оттуда.
        Возвращает {"action": "updated/skipped", "issue_key": "..."}
        """
        if not all([self.url, self.email, self.token]):
            return {"action": "skipped", "reason": "no_credentials", "issue_key": None}

        issue_key = task.get("id", "")
        source = task.get("source", "")

        # Обрабатываем только Jira задачи
        if source != "jira" or not issue_key or issue_key.startswith("DEMO"):
            return {"action": "skipped", "reason": "not_jira", "issue_key": issue_key}

        success = self.update_issue(issue_key, task)
        if success:
            print(tr("jira_updated", key=issue_key, title=task.get("title", "")[:40]))
            return {"action": "updated", "issue_key": issue_key}
        else:
            return {"action": "error", "issue_key": issue_key}

    def sync_groomed_tasks(self, tasks: list[dict]) -> dict:
        """Синхронизирует список groomed задач в Jira."""
        stats = {"updated": 0, "skipped": 0, "errors": 0}

        jira_tasks = [t for t in tasks if t.get("source") == "jira"]
        print("\n" + tr("syncing_jira", n=len(jira_tasks)))

        for task in tasks:
            result = self.upsert_task(task)
            action = result.get("action")
            if action == "updated":
                stats["updated"] += 1
            elif action == "skipped":
                stats["skipped"] += 1
            else:
                stats["errors"] += 1

        print(tr("jira_sync_stats", updated=stats["updated"], skipped=stats["skipped"], errors=stats["errors"]))
        return stats
