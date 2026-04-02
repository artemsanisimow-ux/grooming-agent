"""
Grooming Agent
==============
Читает задачи из Jira и Linear, прогоняет каждую через:
1. Уточнение описания
2. Оценка (Fibonacci + confidence)
3. Разбивка если > 8 SP
4. Критерии приёмки
5. Приоритизация

Установка:
    pip install langgraph langchain-anthropic langgraph-checkpoint-sqlite python-dotenv requests

Использование:
    # Заполни .env файл
    python3 grooming_agent.py
"""

import os
import json
import sqlite3
import requests
from typing import TypedDict, Literal, Optional
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

load_dotenv()

# ─────────────────────────────────────────────
# МОДЕЛЬ
# ─────────────────────────────────────────────

model = ChatAnthropic(model="claude-opus-4-5", max_tokens=4096)

# ─────────────────────────────────────────────
# JIRA CLIENT
# ─────────────────────────────────────────────

class JiraClient:
    def __init__(self):
        self.url = os.getenv("JIRA_URL", "").rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "")
        self.token = os.getenv("JIRA_API_TOKEN", "")
        self.auth = (self.email, self.token)

    def get_backlog(self, project_key: str, max_results: int = 50) -> list[dict]:
        """Получить задачи из бэклога Jira."""
        if not all([self.url, self.email, self.token]):
            print("⚠️  Jira не настроена — пропускаем")
            return []

        try:
            endpoint = f"https://{self.url}/rest/api/3/search/jql"
            params = {
                "jql": f"project={project_key} AND sprint is EMPTY AND status != Done ORDER BY priority DESC",
                "maxResults": max_results,
                "fields": "summary,description,priority,status,story_points,labels,assignee"
            }
            resp = requests.get(endpoint, params=params, auth=self.auth, timeout=10)
            resp.raise_for_status()
            issues = resp.json().get("issues", [])

            return [
                {
                    "id": i["key"],
                    "source": "jira",
                    "title": i["fields"]["summary"],
                    "description": self._extract_description(i["fields"].get("description")),
                    "priority": (i["fields"].get("priority") or {}).get("name", "Medium"),
                    "status": (i["fields"].get("status") or {}).get("name", ""),
                    "story_points": i["fields"].get("story_points") or i["fields"].get("customfield_10016"),
                    "labels": i["fields"].get("labels") or [],
                }
                for i in issues
            ]
        except Exception as e:
            print(f"⚠️  Ошибка Jira: {e}")
            return []

    def get_projects(self) -> list[dict]:
        """Получить список проектов."""
        if not all([self.url, self.email, self.token]):
            return []
        try:
            endpoint = f"https://{self.url}/rest/api/3/project/search"
            resp = requests.get(endpoint, auth=self.auth, timeout=10)
            resp.raise_for_status()
            return [{"key": p["key"], "name": p["name"]} for p in resp.json().get("values", [])]
        except Exception as e:
            print(f"⚠️  Ошибка получения проектов Jira: {e}")
            return []

    def _extract_description(self, desc) -> str:
        if not desc:
            return ""
        if isinstance(desc, str):
            return desc
        # Atlassian Document Format
        try:
            texts = []
            for block in desc.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        texts.append(inline.get("text", ""))
            return " ".join(texts)
        except Exception:
            return str(desc)


# ─────────────────────────────────────────────
# LINEAR CLIENT
# ─────────────────────────────────────────────

class LinearClient:
    def __init__(self):
        self.token = os.getenv("LINEAR_API_KEY", "")
        self.endpoint = "https://api.linear.app/graphql"

    def get_projects(self) -> list[dict]:
        """Получить список проектов."""
        if not self.token:
            return []
        query = """
        query {
            projects {
                nodes { id name slugId }
            }
        }
        """
        try:
            resp = self._query(query)
            return [
                {"id": p["id"], "name": p["name"], "slug": p["slugId"]}
                for p in resp.get("data", {}).get("projects", {}).get("nodes", [])
            ]
        except Exception as e:
            print(f"⚠️  Ошибка получения проектов Linear: {e}")
            return []

    def get_backlog(self, project_slug: str = None, max_results: int = 50) -> list[dict]:
        """Получить незаоценённые задачи из Linear."""
        if not self.token:
            print("⚠️  Linear не настроен — пропускаем")
            return []

        filter_clause = f'project: {{slugId: {{eq: "{project_slug}"}}}},' if project_slug else ""

        query = f"""
        query {{
            issues(
                first: {max_results}
                filter: {{
                    {filter_clause}
                    state: {{type: {{nin: ["completed", "cancelled"]}}}}
                    cycle: {{null: true}}
                }}
            ) {{
                nodes {{
                    id
                    title
                    description
                    priority
                    estimate
                    labels {{ nodes {{ name }} }}
                    state {{ name }}
                    project {{ name }}
                }}
            }}
        }}
        """
        try:
            resp = self._query(query)
            issues = resp.get("data", {}).get("issues", {}).get("nodes", [])
            return [
                {
                    "id": i["id"],
                    "source": "linear",
                    "title": i["title"],
                    "description": i.get("description") or "",
                    "priority": i.get("priority", 0),
                    "status": i.get("state", {}).get("name", ""),
                    "story_points": i.get("estimate"),
                    "labels": [l["name"] for l in i.get("labels", {}).get("nodes", [])],
                    "project": i.get("project", {}).get("name", ""),
                }
                for i in issues
            ]
        except Exception as e:
            print(f"⚠️  Ошибка Linear: {e}")
            return []

    def _query(self, query: str) -> dict:
        resp = requests.post(
            self.endpoint,
            json={"query": query},
            headers={"Authorization": self.token, "Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

class GroomingState(TypedDict):
    # Задачи
    raw_tasks: list           # Сырые задачи из Jira + Linear
    tasks_to_groom: list      # Очередь на груминг
    current_task: dict        # Текущая задача
    groomed_tasks: list       # Обработанные задачи

    # Шаги груминга текущей задачи
    enriched_description: str
    story_points: int
    confidence: str           # high/medium/low
    subtasks: list            # Если задача разбита
    acceptance_criteria: list
    final_priority: str

    # Управление
    task_index: int
    needs_human: bool
    human_feedback: str
    human_approved: bool
    session_id: str
    audit: list


# ─────────────────────────────────────────────
# ПРОМПТЫ
# ─────────────────────────────────────────────

ENRICH_PROMPT = """Ты опытный продакт-менеджер. Уточни описание задачи.

Задача: {title}
Текущее описание: {description}
Контекст (другие задачи): {context}

Верни JSON:
{{"enriched_description": "чёткое описание с контекстом проблемы и ожидаемым результатом", "needs_clarification": false, "clarification_question": ""}}

Правила:
- needs_clarification = true только если описание настолько неполное что невозможно оценить
- clarification_question = конкретный вопрос если needs_clarification = true
- Только JSON, без markdown"""

ESTIMATE_PROMPT = """Оцени сложность задачи по шкале Fibonacci с confidence score.

Задача: {title}
Описание: {description}

Шкала Fibonacci: 1, 2, 3, 5, 8, 13
Правило: если оценка > 8 — задача слишком большая, нужна декомпозиция.

Верни JSON:
{{"story_points": 5, "confidence": "medium", "reasoning": "обоснование оценки", "too_large": false}}

Confidence:
- high = чёткие требования, похожие задачи делали раньше
- medium = есть неизвестные но понятен общий объём
- low = много неизвестных, оценка очень приблизительная

Только JSON, без markdown"""

SPLIT_PROMPT = """Разбей большую задачу на подзадачи, каждая не более 5 story points.

Задача: {title}
Описание: {description}
Текущая оценка: {story_points} SP

Верни JSON:
{{"subtasks": [{{"title": "название подзадачи", "description": "описание", "story_points": 3, "order": 1}}]}}

Правила:
- Каждая подзадача независима и может быть выпущена отдельно
- Максимум 5 подзадач
- Только JSON, без markdown"""

ACCEPTANCE_PROMPT = """Напиши критерии приёмки для задачи в формате Given/When/Then.

Задача: {title}
Описание: {description}

Верни JSON:
{{"acceptance_criteria": ["Given ... When ... Then ...", "Given ... When ... Then ..."], "definition_of_done": ["пункт 1", "пункт 2"]}}

Правила:
- 3-5 сценариев Given/When/Then
- Definition of Done — технические требования (тесты, ревью, документация)
- Только JSON, без markdown"""

PRIORITIZE_PROMPT = """Расставь приоритет задачи используя фреймворк RICE.

Задача: {title}
Описание: {description}
Story points: {story_points}
Другие задачи в бэклоге: {other_tasks}

RICE = (Reach × Impact × Confidence) / Effort

Верни JSON:
{{"reach": 1000, "impact": 3, "confidence": 80, "effort": {story_points}, "rice_score": 150, "priority": "P0", "priority_reasoning": "обоснование"}}

Priority:
- P0 = критично, блокирует пользователей или ключевую метрику
- P1 = важно, значительное влияние на метрики
- P2 = желательно, улучшает опыт
- P3 = когда-нибудь

Только JSON, без markdown"""


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def parse_json(text: str, fallback: dict) -> dict:
    try:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return fallback

def log(state: GroomingState, step: str, data: dict) -> list:
    entry = {
        "timestamp": datetime.now().isoformat(),
        "step": step,
        "task_id": state.get("current_task", {}).get("id", ""),
        "task_index": state.get("task_index", 0),
        "data": {k: str(v)[:200] for k, v in data.items()},
    }
    print(f"[{step.upper()}] задача {entry['task_id']}")
    return state.get("audit", []) + [entry]

def call_model(prompt: str) -> str:
    response = model.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content="Выполни задачу и верни JSON."),
    ])
    return response.content


# ─────────────────────────────────────────────
# УЗЛЫ
# ─────────────────────────────────────────────

def load_tasks(state: GroomingState) -> GroomingState:
    """Загружаем задачи из Jira и Linear."""
    print("\n📥 Загружаю задачи из Jira и Linear...")

    jira = JiraClient()
    linear = LinearClient()

    # Показываем проекты если slug не задан
    linear_slug = os.getenv("LINEAR_PROJECT_SLUG", "").strip()
    if not linear_slug:
        projects = linear.get_projects()
        if projects:
            print("\nДоступные проекты в Linear:")
            for i, p in enumerate(projects):
                print(f"  {i+1}. {p['name']} ({p['slug']})")
            choice = input("Выбери номер проекта (или Enter чтобы загрузить все): ").strip()
            if choice.isdigit() and 0 < int(choice) <= len(projects):
                linear_slug = projects[int(choice)-1]["slug"]

    jira_key = os.getenv("JIRA_PROJECT_KEY", "").strip()
    if not jira_key:
        projects = jira.get_projects()
        if projects:
            print("\nДоступные проекты в Jira:")
            for i, p in enumerate(projects):
                print(f"  {i+1}. {p['name']} ({p['key']})")
            choice = input("Выбери номер проекта (или Enter чтобы пропустить): ").strip()
            if choice.isdigit() and 0 < int(choice) <= len(projects):
                jira_key = projects[int(choice)-1]["key"]

    jira_tasks = jira.get_backlog(jira_key) if jira_key else []
    linear_tasks = linear.get_backlog(linear_slug)

    # Дедупликация по title
    all_tasks = jira_tasks + linear_tasks
    seen = set()
    unique_tasks = []
    for t in all_tasks:
        key = t["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique_tasks.append(t)

    print(f"\n✅ Загружено: {len(jira_tasks)} из Jira, {len(linear_tasks)} из Linear")
    print(f"   Уникальных задач: {len(unique_tasks)}")

    if not unique_tasks:
        print("⚠️  Задач не найдено. Используй демо-данные.")
        unique_tasks = DEMO_TASKS

    # Показываем список
    print("\nЗадачи для груминга:")
    for i, t in enumerate(unique_tasks):
        print(f"  {i+1}. [{t['source'].upper()}] {t['title']}")

    audit = log(state, "load_tasks", {
        "jira_count": len(jira_tasks),
        "linear_count": len(linear_tasks),
        "total": len(unique_tasks),
    })

    return {
        **state,
        "raw_tasks": unique_tasks,
        "tasks_to_groom": unique_tasks,
        "task_index": 0,
        "groomed_tasks": [],
        "audit": audit,
    }


def pick_next_task(state: GroomingState) -> GroomingState:
    """Берём следующую задачу из очереди."""
    tasks = state["tasks_to_groom"]
    idx = state["task_index"]

    if idx >= len(tasks):
        return state

    task = tasks[idx]
    print(f"\n{'─'*60}")
    print(f"📋 Задача {idx+1}/{len(tasks)}: {task['title']}")
    print(f"   Источник: {task['source'].upper()} | Статус: {task.get('status', '—')}")

    audit = log(state, "pick_task", {"task": task["title"], "index": idx})

    return {
        **state,
        "current_task": task,
        "enriched_description": "",
        "story_points": 0,
        "confidence": "",
        "subtasks": [],
        "acceptance_criteria": [],
        "final_priority": "",
        "needs_human": False,
        "audit": audit,
    }


def enrich_description(state: GroomingState) -> GroomingState:
    """Уточняем описание задачи."""
    task = state["current_task"]
    context = [t["title"] for t in state["tasks_to_groom"] if t["id"] != task["id"]][:5]

    result = parse_json(
        call_model(ENRICH_PROMPT.format(
            title=task["title"],
            description=task.get("description", "нет описания"),
            context=", ".join(context),
        )),
        {"enriched_description": task.get("description", ""), "needs_clarification": False, "clarification_question": ""}
    )

    needs_human = result.get("needs_clarification", False)
    audit = log(state, "enrich", {
        "needs_clarification": needs_human,
        "question": result.get("clarification_question", ""),
    })

    return {
        **state,
        "enriched_description": result.get("enriched_description", ""),
        "needs_human": needs_human,
        "human_feedback": result.get("clarification_question", ""),
        "audit": audit,
    }


def estimate(state: GroomingState) -> GroomingState:
    """Оцениваем story points по Fibonacci."""
    task = state["current_task"]

    result = parse_json(
        call_model(ESTIMATE_PROMPT.format(
            title=task["title"],
            description=state["enriched_description"],
        )),
        {"story_points": 5, "confidence": "medium", "reasoning": "", "too_large": False}
    )

    sp = result.get("story_points", 5)
    confidence = result.get("confidence", "medium")
    too_large = result.get("too_large", sp > 8)

    print(f"   📊 Оценка: {sp} SP | Уверенность: {confidence}")
    if result.get("reasoning"):
        print(f"   💭 {result['reasoning']}")

    audit = log(state, "estimate", {
        "story_points": sp,
        "confidence": confidence,
        "too_large": too_large,
    })

    return {
        **state,
        "story_points": sp,
        "confidence": confidence,
        "needs_human": confidence == "low",
        "audit": audit,
    }


def split_if_large(state: GroomingState) -> GroomingState:
    """Разбиваем задачу если она слишком большая."""
    if state["story_points"] <= 8:
        return state

    task = state["current_task"]
    print(f"   ✂️  Задача большая ({state['story_points']} SP) — разбиваю...")

    result = parse_json(
        call_model(SPLIT_PROMPT.format(
            title=task["title"],
            description=state["enriched_description"],
            story_points=state["story_points"],
        )),
        {"subtasks": []}
    )

    subtasks = result.get("subtasks", [])
    if subtasks:
        print(f"   Разбита на {len(subtasks)} подзадач:")
        for st in subtasks:
            print(f"     • {st['title']} ({st.get('story_points', '?')} SP)")

    audit = log(state, "split", {"subtasks_count": len(subtasks)})

    return {**state, "subtasks": subtasks, "audit": audit}


def check_acceptance(state: GroomingState) -> GroomingState:
    """Формируем критерии приёмки."""
    task = state["current_task"]

    result = parse_json(
        call_model(ACCEPTANCE_PROMPT.format(
            title=task["title"],
            description=state["enriched_description"],
        )),
        {"acceptance_criteria": [], "definition_of_done": []}
    )

    criteria = result.get("acceptance_criteria", [])
    dod = result.get("definition_of_done", [])

    print(f"   ✅ Критериев приёмки: {len(criteria)}")

    audit = log(state, "acceptance", {"criteria_count": len(criteria)})

    return {
        **state,
        "acceptance_criteria": criteria + dod,
        "audit": audit,
    }


def prioritize(state: GroomingState) -> GroomingState:
    """Расставляем приоритет по RICE."""
    task = state["current_task"]
    other = [t["title"] for t in state["tasks_to_groom"] if t["id"] != task["id"]][:10]

    result = parse_json(
        call_model(PRIORITIZE_PROMPT.format(
            title=task["title"],
            description=state["enriched_description"],
            story_points=state["story_points"],
            other_tasks=", ".join(other),
        )),
        {"priority": "P1", "rice_score": 0, "priority_reasoning": ""}
    )

    priority = result.get("priority", "P1")
    rice = result.get("rice_score", 0)
    print(f"   🎯 Приоритет: {priority} | RICE: {rice}")

    audit = log(state, "prioritize", {"priority": priority, "rice_score": rice})

    return {**state, "final_priority": priority, "audit": audit}


def human_checkpoint(state: GroomingState) -> GroomingState:
    """Останавливаемся если нужно уточнение."""
    task = state["current_task"]

    print(f"\n{'='*60}")
    print(f"✋ НУЖНО УТОЧНЕНИЕ")
    print(f"Задача: {task['title']}")
    print(f"Вопрос: {state.get('human_feedback', 'Низкая уверенность в оценке')}")
    print(f"Текущая оценка: {state['story_points']} SP | {state['confidence']}")
    print(f"\nВарианты:")
    print(f"  y — принять как есть")
    print(f"  [текст] — добавить контекст и пересчитать")

    answer = input("\n→ ").strip()

    if answer.lower() == "y":
        audit = log(state, "human_checkpoint", {"decision": "accepted"})
        return {**state, "needs_human": False, "audit": audit}
    else:
        # Добавляем контекст в описание и пересчитываем
        enriched = state["enriched_description"] + f"\n\nДополнительный контекст: {answer}"
        audit = log(state, "human_checkpoint", {"decision": "added_context", "context": answer})
        return {
            **state,
            "enriched_description": enriched,
            "needs_human": False,
            "story_points": 0,
            "audit": audit,
        }


def save_groomed_task(state: GroomingState) -> GroomingState:
    """Сохраняем обработанную задачу и берём следующую."""
    task = state["current_task"]

    groomed = {
        "id": task["id"],
        "source": task["source"],
        "title": task["title"],
        "description": state["enriched_description"],
        "story_points": state["story_points"],
        "confidence": state["confidence"],
        "priority": state["final_priority"],
        "acceptance_criteria": state["acceptance_criteria"],
        "subtasks": state["subtasks"],
    }

    groomed_tasks = state.get("groomed_tasks", []) + [groomed]
    print(f"\n✅ Задача обработана: {task['title']}")

    audit = log(state, "save_task", {"title": task["title"], "priority": groomed["priority"]})

    return {
        **state,
        "groomed_tasks": groomed_tasks,
        "task_index": state["task_index"] + 1,
        "audit": audit,
    }


def finalize(state: GroomingState) -> GroomingState:
    """Сохраняем финальный отчёт."""
    session_id = state["session_id"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"grooming_report_{session_id}_{timestamp}.md"

    tasks = state["groomed_tasks"]

    # Группируем по приоритету
    by_priority = {"P0": [], "P1": [], "P2": [], "P3": []}
    for t in tasks:
        p = t.get("priority", "P2")
        by_priority.setdefault(p, []).append(t)

    report = f"""# Grooming Report
Сессия: {session_id}
Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Задач обработано: {len(tasks)}

---
"""
    for priority, items in by_priority.items():
        if not items:
            continue
        report += f"\n## {priority}\n\n"
        for t in items:
            report += f"### [{t['id']}] {t['title']}\n"
            report += f"**Story points:** {t['story_points']} SP ({t['confidence']} confidence)\n"
            report += f"**Источник:** {t['source'].upper()}\n\n"
            report += f"**Описание:**\n{t['description']}\n\n"
            if t.get("acceptance_criteria"):
                report += "**Критерии приёмки:**\n"
                for c in t["acceptance_criteria"]:
                    report += f"- {c}\n"
                report += "\n"
            if t.get("subtasks"):
                report += "**Подзадачи:**\n"
                for st in t["subtasks"]:
                    report += f"- {st['title']} ({st.get('story_points', '?')} SP)\n"
                report += "\n"
            report += "---\n"

    Path(filename).write_text(report, encoding="utf-8")

    audit_file = f"grooming_audit_{session_id}_{timestamp}.json"
    Path(audit_file).write_text(
        json.dumps(state.get("audit", []), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"\n{'='*60}")
    print(f"✅ Груминг завершён!")
    print(f"   Обработано задач: {len(tasks)}")
    print(f"   P0: {len(by_priority['P0'])} | P1: {len(by_priority['P1'])} | P2: {len(by_priority['P2'])}")
    print(f"📄 Отчёт: {filename}")
    print(f"📋 Audit: {audit_file}")

    return {**state, "human_approved": True}


# ─────────────────────────────────────────────
# РОУТЕРЫ
# ─────────────────────────────────────────────

def after_enrich(state: GroomingState) -> Literal["human_checkpoint", "estimate"]:
    if state.get("needs_human"):
        return "human_checkpoint"
    return "estimate"

def after_estimate(state: GroomingState) -> Literal["human_checkpoint", "split_if_large"]:
    if state.get("needs_human"):
        return "human_checkpoint"
    return "split_if_large"

def after_human(state: GroomingState) -> Literal["estimate", "split_if_large"]:
    # Если story_points сброшены — пересчитываем
    if state.get("story_points", 0) == 0:
        return "estimate"
    return "split_if_large"

def has_more_tasks(state: GroomingState) -> Literal["pick_next_task", "finalize"]:
    if state["task_index"] < len(state["tasks_to_groom"]):
        return "pick_next_task"
    return "finalize"


# ─────────────────────────────────────────────
# ДЕМО ДАННЫЕ
# ─────────────────────────────────────────────

DEMO_TASKS = [
    {
        "id": "DEMO-1", "source": "jira",
        "title": "Редизайн экрана ввода транзакции",
        "description": "Пользователи жалуются что долго вводить транзакции. Нужно ускорить процесс.",
        "priority": "High", "status": "Backlog", "story_points": None, "labels": [],
    },
    {
        "id": "DEMO-2", "source": "linear",
        "title": "Интеграция с банками",
        "description": "Добавить автоматический импорт транзакций из банков.",
        "priority": 1, "status": "Todo", "story_points": None, "labels": [],
    },
    {
        "id": "DEMO-3", "source": "jira",
        "title": "Push-уведомления",
        "description": "Напоминать пользователям вносить расходы.",
        "priority": "Medium", "status": "Backlog", "story_points": None, "labels": [],
    },
]


# ─────────────────────────────────────────────
# ГРАФ
# ─────────────────────────────────────────────

def build_graph(db_path: str = "grooming.db"):
    graph = StateGraph(GroomingState)

    graph.add_node("load_tasks", load_tasks)
    graph.add_node("pick_next_task", pick_next_task)
    graph.add_node("enrich_description", enrich_description)
    graph.add_node("estimate", estimate)
    graph.add_node("split_if_large", split_if_large)
    graph.add_node("check_acceptance", check_acceptance)
    graph.add_node("prioritize", prioritize)
    graph.add_node("human_checkpoint", human_checkpoint)
    graph.add_node("save_groomed_task", save_groomed_task)
    graph.add_node("finalize", finalize)

    graph.set_entry_point("load_tasks")
    graph.add_edge("load_tasks", "pick_next_task")

    graph.add_edge("pick_next_task", "enrich_description")

    graph.add_conditional_edges("enrich_description", after_enrich, {
        "human_checkpoint": "human_checkpoint",
        "estimate": "estimate",
    })

    graph.add_conditional_edges("estimate", after_estimate, {
        "human_checkpoint": "human_checkpoint",
        "split_if_large": "split_if_large",
    })

    graph.add_conditional_edges("human_checkpoint", after_human, {
        "estimate": "estimate",
        "split_if_large": "split_if_large",
    })

    graph.add_edge("split_if_large", "check_acceptance")
    graph.add_edge("check_acceptance", "prioritize")
    graph.add_edge("prioritize", "save_groomed_task")

    graph.add_conditional_edges("save_groomed_task", has_more_tasks, {
        "pick_next_task": "pick_next_task",
        "finalize": "finalize",
    })

    graph.add_edge("finalize", END)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

def run_grooming(session_id: str = None, resume: bool = False):
    sid = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    app = build_graph()
    config = {"configurable": {"thread_id": sid}}

    if resume:
        print(f"\n▶️  Продолжаю сессию {sid}...")
        final_state = app.invoke(None, config=config)
    else:
        print(f"\n🚀 Grooming агент | Сессия: {sid}")
        initial = GroomingState(
            raw_tasks=[],
            tasks_to_groom=[],
            current_task={},
            groomed_tasks=[],
            enriched_description="",
            story_points=0,
            confidence="",
            subtasks=[],
            acceptance_criteria=[],
            final_priority="",
            task_index=0,
            needs_human=False,
            human_feedback="",
            human_approved=False,
            session_id=sid,
            audit=[],
        )
        final_state = app.invoke(initial, config=config)

    print(f"\n💾 Session ID: {sid}")
    return final_state, sid


if __name__ == "__main__":
    run_grooming()
