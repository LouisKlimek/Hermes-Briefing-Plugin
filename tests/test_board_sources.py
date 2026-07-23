import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PLUGIN_API = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("briefing_plugin_api", PLUGIN_API)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {PLUGIN_API}")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


def make_board(path: Path, task_id: str, ts: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
                tenant TEXT, priority INTEGER, created_at INTEGER, body TEXT
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, data TEXT,
                created_at INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, 'done', 'agent', '', 0, ?, '')",
            (task_id, task_id, ts),
        )
        conn.execute(
            "INSERT INTO task_events VALUES (1, ?, 'completed', '{}', ?)",
            (task_id, ts),
        )
def make_tasklist_db(path: Path, board: str, task_id: str, list_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE lists (id TEXT PRIMARY KEY, board TEXT, name TEXT);
            CREATE TABLE membership (board TEXT, task_id TEXT, list_id TEXT);
            """
        )
        conn.execute("INSERT INTO lists VALUES ('backend', ?, ?)", (board, list_name))
        conn.execute("INSERT INTO membership VALUES (?, ?, 'backend')", (board, task_id))


def make_state_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sessions (
                started_at INTEGER, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER, actual_cost REAL
            );
        """)
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def make_identified_state_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sessions (
                session_id TEXT, started_at INTEGER, model TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_write_tokens INTEGER, actual_cost REAL
            );
        """)
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


def make_chat_session_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sessions (
                title TEXT, message_count INTEGER, started_at INTEGER, model TEXT
            );
        """)
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)", rows)


class BoardSourceTests(unittest.TestCase):
    def test_human_chat_sessions_selects_only_configured_profile_rows_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            make_chat_session_db(home / "state.db", [
                ("Normal chat", 4, 150, "gpt-5"),
                ("work kanban task t_deadbeef", 9, 175, "worker-model"),
                ("WORK KANBAN TASK T_ABC123", 8, 180, "worker-model"),
                ("work kanban task t_abc123 extra", 3, 190, "gpt-5"),
                (None, None, 200, None),
                ("Outside window", 1, 201, "gpt-5"),
            ])
            other = Path(tmp) / "other" / "state.db"
            make_chat_session_db(other, [("Other profile", 1, 199, "gpt-5")])
            cfg = api.Config(hermes_home=home, telemetry_profile_dbs=[other])

            rows = api.human_chat_sessions(cfg, 100, 201)
            empty_rows = api.human_chat_sessions(cfg, 202, 300)

        self.assertEqual([row["title"] for row in rows], ["", "work kanban task t_abc123 extra", "Normal chat"])
        self.assertEqual(rows[0], {"title": "", "message_count": 0, "started_at": 200, "model": ""})
        self.assertEqual(rows[-1]["model"], "gpt-5")
        self.assertEqual(empty_rows, [])

    def test_human_chat_sessions_and_dashboard_contract_cover_daily_weekly_monthly_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            make_chat_session_db(home / "state.db", [("Normal chat", 4, 100, "gpt-5")])
            make_board(home / "kanban.db", "task", 100)
            cfg = api.Config(hermes_home=home, timezone="UTC")

            daily = api.build_digest(cfg, "1970-01-01", persist=False, mark=False)
            weekly = api.build_range(cfg, "1970-01-01", "1970-01-01")

        self.assertEqual(daily["human_chat_sessions"][0]["title"], "Normal chat")
        self.assertEqual(weekly["human_chat_sessions"][0]["title"], "Normal chat")
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        daily_view = bundle[bundle.index("function DigestView"):bundle.index("function RangeView")]
        range_view = bundle[bundle.index("function RangeView"):bundle.index("function StatusBar")]
        self.assertIn('function HumanChatSessions', bundle)
        self.assertIn('title: "Human Chat Sessions (" + sessions.length + ")"', bundle)
        self.assertIn('h(HumanChatSessions, { sessions: digest.human_chat_sessions })', daily_view)
        self.assertIn('h(HumanChatSessions, { sessions: r.human_chat_sessions })', range_view)
    def test_report_embeds_the_grouped_task_view_without_a_tasks_tab(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('h(Section, { title: "Tasks ("', bundle)
        self.assertIn('tasks: tasks, tasksLoading: tasksLoading', bundle)
        self.assertIn('"List: " + list', bundle)
        self.assertNotIn('label: "Tasks"', bundle)

    def test_daily_report_omits_done_and_active_accordions(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertNotIn("function DoneGrid", bundle)
        self.assertNotIn('title: "Done (" + digest.done.length', bundle)
        self.assertNotIn('title: "Active (" + digest.in_progress.length', bundle)
        self.assertNotIn('digest.in_progress.map(function (t, i)', bundle)
        self.assertIn('title: "Tasks (" + ((props.tasks || []).length)', bundle)

    def test_task_list_groups_start_collapsed_across_report_periods(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        task_view_start = bundle.index("function TaskListView")
        daily_start = bundle.index("function DigestView")
        range_start = bundle.index("function RangeView")
        task_view = bundle[task_view_start:daily_start]
        daily = bundle[daily_start:range_start]
        range_view = bundle[range_start:]

        self.assertIn('listOpen = groupsOpen[listKey] === true', task_view)
        self.assertIn('next[listKey] = !listOpen', task_view)
        self.assertIn('h(TaskListChart, { tasks: all, target: props.target })', task_view)
        self.assertIn('h(TaskListView, { tasks: props.tasks', daily)
        self.assertIn('h(TaskListView, { tasks: props.tasks', range_view)

    def test_daily_models_follow_cost_and_low_priority_sections_start_collapsed(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        daily_start = bundle.index("function DigestView")
        range_start = bundle.index("function RangeView")
        daily = bundle[daily_start:range_start]
        self.assertLess(daily.index('title: "Cost"'), daily.index('title: "Models · " + (digest.models.by_profile.length)'))
        self.assertIn('title: "Tasks (" + ((props.tasks || []).length) + ")", defaultCollapsed: true', daily)
        self.assertIn('title: "Models · " + (digest.models.by_profile.length) + " profiles", defaultCollapsed: true', daily)
        self.assertIn('title: "System", defaultCollapsed: true', daily)

    def test_weekly_and_monthly_low_priority_sections_start_collapsed_with_tasks_label(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        range_view = bundle[bundle.index("function RangeView"):bundle.index("function StatusBar")]
        self.assertIn('title: "Still open (" + r.hand.length + ")", defaultCollapsed: true', range_view)
        self.assertIn('title: "Tasks (" + ((props.tasks || []).length) + ")", defaultCollapsed: true', range_view)
        self.assertIn('title: "Models \\u00b7 " + (r.models.by_profile.length) + " profiles", defaultCollapsed: true', range_view)
        self.assertIn('title: "System", defaultCollapsed: true', range_view)
        self.assertNotIn('Task transitions', range_view)

    def test_task_view_uses_real_task_fields_and_derives_comments_children_and_completion(self):
        parent = api.Task("alpha::parent", "Parent", "done", "lead", "", "high", 100)
        child = api.Task("alpha::child", "Child", "blocked", "worker", "", "normal", 200)

        class Source:
            def fetch_tasks(self): return {parent.id: parent, child.id: child}
            def fetch_links(self): return [(parent.id, child.id)]
            def fetch_events(self, _start, _end):
                return [api.Event(1, parent.id, "completed", {}, 300, None), api.Event(2, child.id, "completed", {}, 250, None)]
            def fetch_comments(self, task_id): return [{"id": 1}] if task_id == parent.id else []

        rows = {row["id"]: row for row in api.build_task_view(Source())["tasks"]}
        self.assertEqual(rows[parent.id]["board"], "alpha")
        self.assertEqual(rows[parent.id]["comment_count"], 1)
        self.assertEqual(rows[parent.id]["completed_at"], 300)
        self.assertEqual(rows[parent.id]["child_ids"], [child.id])
        self.assertEqual(rows[child.id]["parent_id"], parent.id)
        self.assertEqual(rows[child.id]["status"], "blocked")
        self.assertIsNone(rows[child.id]["completed_at"])

    def test_task_table_uses_tasklist_column_order_and_presentational_bindings(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        stylesheet = (Path(__file__).parents[1] / "dashboard" / "dist" / "style.css").read_text()
        self.assertIn('["Name", "Status", "Priority", "Assignee", "List", "Age"]', bundle)
        self.assertNotIn('["Name", "Status", "Priority", "Assignee", "List", "Created", "Completed"]', bundle)
        self.assertIn('priorityLabel(task.priority)', bundle)
        self.assertIn('"↳ " + childCount', bundle)
        self.assertIn('resolveColor(task.status)', bundle)
        self.assertIn('grid-template-columns: minmax(14rem, 2.6fr)', stylesheet)

    def test_task_view_reads_comments_links_and_completion_from_board_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            db = home / "kanban.db"
            make_board(db, "parent", 100)
            with sqlite3.connect(db) as conn:
                conn.executescript("""
                    CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT);
                    CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                """)
                conn.execute("INSERT INTO task_comments VALUES (1, 'parent', 'note')")
                conn.execute("INSERT INTO tasks VALUES ('child', 'Child', 'blocked', 'worker', '', 1, 200, '')")
                conn.execute("INSERT INTO task_links VALUES ('parent', 'child')")
            src = api.KanbanSource(api.Config(hermes_home=home))
            try:
                rows = {row["id"]: row for row in api.build_task_view(src)["tasks"]}
            finally:
                src.close()
            self.assertEqual(rows["default::parent"]["comment_count"], 1)
            self.assertEqual(rows["default::parent"]["completed_at"], 100)
            self.assertEqual(rows["default::parent"]["child_ids"], ["default::child"])
            self.assertEqual(rows["default::child"]["comment_count"], 0)
            self.assertEqual(rows["default::child"]["parent_id"], "default::parent")
            self.assertEqual(rows["default::child"]["status"], "blocked")

    def test_period_task_view_uses_only_done_or_blocked_transitions_in_window(self):
        done = api.Task("alpha::done", "Done in range", "done", "lead", "", "high", 10)
        blocked = api.Task("alpha::blocked", "Blocked in range", "blocked", "worker", "", "normal", 20)
        failed = api.Task("alpha::failed", "Failed in range", "failed", "worker", "", "normal", 25)
        stale = api.Task("alpha::stale", "Stale completion", "done", "lead", "", "low", 30)
        no_lifecycle = api.Task("alpha::no-lifecycle", "No lifecycle event", "blocked", "worker", "", "low", 40)

        class Source:
            def fetch_tasks(self): return {t.id: t for t in (done, blocked, failed, stale, no_lifecycle)}
            def fetch_links(self): return [(done.id, blocked.id)]
            def fetch_comments(self, task_id): return []
            def fetch_events(self, start, end):
                events = [
                    api.Event(1, stale.id, "completed", {}, 50, None),
                    api.Event(2, done.id, "status_changed", {"to": "done"}, 150, None),
                    api.Event(3, blocked.id, "status_changed", {"to": "blocked"}, 160, None),
                    api.Event(4, failed.id, "failed", {}, 170, None),
                ]
                return [event for event in events if start <= event.ts < end]

        rows = {row["id"]: row for row in api.build_task_view(Source(), 100, 200)["tasks"]}
        self.assertEqual(set(rows), {done.id, blocked.id})
        self.assertEqual(rows[done.id]["status"], "done")
        self.assertEqual(rows[blocked.id]["status"], "blocked")
        self.assertEqual(rows[blocked.id]["parent_id"], done.id)
        self.assertEqual(rows[done.id]["completed_at"], 150)

    def test_period_task_view_has_empty_state_when_no_lifecycle_transition_qualifies(self):
        task = api.Task("alpha::stale", "Stale completion", "done", "lead", "", "low", 30)

        class Source:
            def fetch_tasks(self): return {task.id: task}
            def fetch_links(self): return []
            def fetch_comments(self, task_id): return []
            def fetch_events(self, start, end): return []

        self.assertEqual(api.build_task_view(Source(), 100, 200), {"tasks": []})

    def test_fresh_digest_omits_deleted_and_archived_ticket_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            db = home / "kanban.db"
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as conn:
                conn.executescript("""
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
                        tenant TEXT, priority INTEGER, created_at INTEGER, body TEXT
                    );
                    CREATE TABLE task_events (
                        id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, data TEXT,
                        created_at INTEGER
                    );
                """)
                conn.execute("INSERT INTO tasks VALUES ('current', 'Current ticket', 'blocked', '', '', 0, 1, '')")
                conn.execute("INSERT INTO tasks VALUES ('archived', 'Archived ticket', 'archived', '', '', 0, 1, '')")
                conn.execute("INSERT INTO task_events VALUES (1, 'current', 'blocked', '{}', 100)")
                conn.execute("INSERT INTO task_events VALUES (2, 'archived', 'blocked', '{}', 100)")
                conn.execute("INSERT INTO task_events VALUES (3, 'deleted', 'blocked', '{}', 100)")

            digest = api.build_digest(api.Config(hermes_home=home), "1970-01-01",
                                      persist=False, mark=False)

            self.assertEqual([item["task_id"] for item in digest["hand"]], ["default::current"])

    def test_reconcile_decisions_closes_missing_ticket_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = api.Store(api.Config(hermes_home=Path(tmp) / "profile"))
            try:
                store.upsert_decision({
                    "id": "default::deleted:blocked", "task_id": "default::deleted",
                    "kind": "blocked", "title": "Deleted ticket", "detail": "", "deadline": None,
                })
                self.assertEqual(store.reconcile_decisions({}), 1)
                self.assertEqual(store.open_decisions(), [])
            finally:
                store.close()

    def test_tasklist_list_identity_is_exposed_separately_from_the_board(self):
        task = api.Task("alpha::done", "Done", "done", "lead", "", "high", 10,
                        list_name="Client delivery")

        class Source:
            def fetch_tasks(self): return {task.id: task}
            def fetch_links(self): return []
            def fetch_comments(self, task_id): return []
            def fetch_events(self, start, end):
                return [api.Event(1, task.id, "status_changed", {"to": "done"}, 150, None)]

        row = api.build_task_view(Source(), 100, 200)["tasks"][0]
        self.assertEqual(row["board"], "alpha")
        self.assertEqual(row["list"], "Client delivery")

    def test_board_source_reads_tasklist_list_column_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kanban.db"
            make_board(db, "ticket", 100)
            with sqlite3.connect(db) as conn:
                conn.execute("ALTER TABLE tasks ADD COLUMN list_name TEXT")
                conn.execute("UPDATE tasks SET list_name = 'Client delivery'")
            source = api._BoardSource(api.Config(), db, "alpha")
            try:
                task = source.fetch_tasks()["alpha::ticket"]
            finally:
                source.close()
            self.assertEqual(task.list_name, "Client delivery")

    def test_tasklist_overlay_membership_overrides_board_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            make_board(home / "kanban" / "boards" / "alpha" / "kanban.db", "ticket", 100)
            make_tasklist_db(home / "tasklist" / "lists.db", "alpha", "ticket", "Backend")
            source = api.KanbanSource(api.Config(hermes_home=home))
            try:
                task = source.fetch_tasks()["alpha::ticket"]
            finally:
                source.close()
            self.assertEqual(task.list_name, "Backend")

    def test_missing_tasklist_overlay_has_no_memberships(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(api.tasklist_memberships(api.Config(hermes_home=Path(tmp))), {})

    def test_report_chart_uses_no_list_instead_of_board_fallback(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('task.list || "No List"', bundle)
        self.assertNotIn('task.list || task.board', bundle)

    def test_no_external_paths_preserves_profile_local_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            make_board(home / "kanban.db", "local", 10)
            cfg = api.Config(hermes_home=home)
            self.assertEqual([(slug, path.name) for slug, path in api.discover_boards(cfg)],
                             [("default", "kanban.db")])

    def test_external_root_aggregates_and_board_filter_narrows_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "external-boards"
            make_board(root / "alpha" / "kanban.db", "alpha-task", 10)
            make_board(root / "beta" / "kanban.db", "beta-task", 20)
            cfg = api.Config(hermes_home=Path(tmp) / "isolated-profile",
                             external_board_roots=[root])

            self.assertEqual([slug for slug, _ in api.discover_boards(cfg)], ["alpha", "beta"])
            all_sources = api.KanbanSource(cfg)
            alpha_source = api.KanbanSource(cfg, "alpha")
            try:
                self.assertEqual({event.task_id for event in all_sources.fetch_events(0, 30)},
                                 {"alpha::alpha-task", "beta::beta-task"})
                self.assertEqual({event.task_id for event in alpha_source.fetch_events(0, 30)},
                                 {"alpha::alpha-task"})
                self.assertEqual(alpha_source.board_slugs(), ["alpha"])
                with self.assertRaises(sqlite3.OperationalError):
                    alpha_source._sources[0].connect().execute("CREATE TABLE must_not_write (id INTEGER)")
            finally:
                all_sources.close()
                alpha_source.close()

    def test_explicit_single_db_override_excludes_external_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "external-boards"
            external = root / "alpha" / "kanban.db"
            override = Path(tmp) / "override" / "kanban.db"
            make_board(external, "external-task", 10)
            make_board(override, "override-task", 20)
            cfg = api.Config(hermes_home=Path(tmp) / "isolated-profile", kanban_db=override,
                             external_board_roots=[root], external_board_dbs=[external])

            self.assertEqual(api.discover_boards(cfg), [("default", override)])
            src = api.KanbanSource(cfg)
            try:
                self.assertEqual({event.task_id for event in src.fetch_events(0, 30)},
                                 {"default::override-task"})
            finally:
                src.close()

    def test_special_characters_in_configured_db_path_remain_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "approved" / "kanban#.db"
            make_board(db, "special-path-task", 10)
            src = api.KanbanSource(api.Config(hermes_home=Path(tmp) / "isolated-profile",
                                              external_board_dbs=[db]))
            try:
                self.assertEqual({event.task_id for event in src.fetch_events(0, 20)},
                                 {"approved::special-path-task"})
                with self.assertRaises(sqlite3.OperationalError):
                    src._sources[0].connect().execute("CREATE TABLE must_not_write (id INTEGER)")
            finally:
                src.close()

    def test_yaml_configuration_accepts_roots_and_allowed_db_paths(self):
        cfg = api.Config()
        api._apply_yaml(cfg, {
            "external_board_roots": ["/boards-a", "/boards-b"],
            "external_board_dbs": "/allowed/custom/kanban.db",
            "telemetry_profile_roots": "/approved/profiles",
            "telemetry_profile_dbs": ["/approved/a/state.db", "/approved/b/state.db"],
        })
        self.assertEqual(cfg.external_board_roots, [Path("/boards-a"), Path("/boards-b")])
        self.assertEqual(cfg.external_board_dbs, [Path("/allowed/custom/kanban.db")])
        self.assertEqual(cfg.telemetry_profile_roots, [Path("/approved/profiles")])
        self.assertEqual(cfg.telemetry_profile_dbs, [Path("/approved/a/state.db"), Path("/approved/b/state.db")])

    def test_budget_limits_default_to_fixed_database_values_and_ignore_legacy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            cfg = api.Config(hermes_home=home)
            api._apply_yaml(cfg, {"budget": {"daily_eur": 1, "monthly_eur": 2}})
            store = api.Store(cfg)
            try:
                self.assertEqual(store.get_budget_limits()["daily_eur"], 15.0)
                self.assertEqual(store.get_budget_limits()["monthly_eur"], 400.0)
            finally:
                store.close()

    def test_budget_limits_persist_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = api.Config(hermes_home=Path(tmp) / "profile")
            store = api.Store(cfg)
            try:
                self.assertEqual(store.get_budget_limits()["daily_eur"], 15.0)
                self.assertEqual(store.get_budget_limits()["monthly_eur"], 400.0)
                saved = store.set_budget_limits(20.5, "500")
                self.assertEqual(saved["daily_eur"], 20.5)
                self.assertEqual(saved["monthly_eur"], 500.0)
                saved = store.set_budget_limits(20.5, 600)
                self.assertEqual(saved["daily_eur"], 20.5)
                self.assertEqual(saved["monthly_eur"], 600.0)
                for daily, monthly in ((-1, 1), ("bad", 1), (1, float("inf")), (True, 1)):
                    with self.assertRaises(ValueError):
                        store.set_budget_limits(daily, monthly)
            finally:
                store.close()
            reopened = api.Store(cfg)
            try:
                self.assertEqual(reopened.get_budget_limits()["daily_eur"], 20.5)
                self.assertEqual(reopened.get_budget_limits()["monthly_eur"], 600.0)
            finally:
                reopened.close()

    def test_budget_limits_endpoint_preserves_omitted_limit(self):
        if api.router is None:
            self.skipTest("FastAPI is unavailable in the test environment")

        class FakeStore:
            limits = {"daily_eur": 15.0, "monthly_eur": 400.0}

            def __init__(self, cfg):
                pass

            def get_budget_limits(self):
                return dict(self.limits)

            def set_budget_limits(self, daily, monthly):
                self.limits = {"daily_eur": daily, "monthly_eur": monthly}
                type(self).limits = self.limits
                return dict(self.limits)

            def close(self):
                pass

        endpoint = next(route.endpoint for route in api.router.routes if route.path == "/budget-limits" and "POST" in route.methods)
        with patch.object(api, "Store", FakeStore), patch.object(api, "load_config", return_value=object()):
            daily = endpoint({"daily_eur": 20})
            monthly = endpoint({"monthly_eur": 500})
        self.assertEqual(daily, {"daily_eur": 20, "monthly_eur": 400.0})
        self.assertEqual(monthly, {"daily_eur": 20, "monthly_eur": 500})

    def test_budget_editor_is_accessible_and_does_not_direct_users_to_config(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        stylesheet = (Path(__file__).parents[1] / "dashboard" / "dist" / "style.css").read_text()
        self.assertIn('aria-label": "Edit " + props.label.toLowerCase()', bundle)
        self.assertIn('field: "daily_eur"', bundle)
        self.assertIn('field: "monthly_eur"', bundle)
        self.assertNotIn("BudgetEditor", bundle)
        self.assertIn('role: "alert"', bundle)
        self.assertIn('"/budget-limits"', bundle)
        self.assertNotIn("Edit limits in config.yaml", bundle)
        self.assertIn('className: "brf-budget-limit-input"', bundle)
        self.assertIn('className: "brf-budget-limit-save"', bundle)
        self.assertIn('className: "brf-budget-limit-cancel"', bundle)
        self.assertIn('.brf-budget-limit-input::-webkit-inner-spin-button', stylesheet)
        self.assertIn('-moz-appearance: textfield;', stylesheet)
        self.assertIn('background: #635bff;', stylesheet)
        self.assertIn('.brf-budget-limit-cancel {', stylesheet)

    def test_historical_all_profile_telemetry_uses_exact_window_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            home = root / "dashboard"
            start, end, _ = api.day_bounds("2024-01-02", "Europe/Berlin")
            make_state_db(home / "state.db", [
                (start, "gpt-5", 100, 50, 10, 5, 1.25),
                (start - 1, "gpt-5", 999, 0, 0, 0, 9.99),
            ])
            make_board(home / "kanban.db", "day-task", start)
            worker_db = root / "worker" / "state.db"
            make_state_db(worker_db, [
                (end - 1, "other", 1_000_000, 0, 20, 30, None),
                (end, "other", 1_000_000, 0, 0, 0, None),
            ])
            cfg = api.Config(hermes_home=home, telemetry_profile_roots=[root],
                             telemetry_profile_dbs=[worker_db, root / "no-db" / "state.db"],
                             timezone="Europe/Berlin")
            insights = api.build_insights(cfg, start, end)

            self.assertEqual(insights["included_profiles"], ["dashboard", "worker", "no-db"])
            self.assertEqual(insights["overview"]["sessions"], 2)
            self.assertEqual(insights["overview"]["input_tokens"], 1_000_100)
            self.assertEqual(insights["overview"]["cache_read_tokens"], 30)
            self.assertEqual(insights["overview"]["cache_write_tokens"], 35)
            self.assertEqual(insights["overview"]["total_tokens"], 1_000_215)
            self.assertAlmostEqual(insights["cost"], 2.25)
            digest = api.build_digest(cfg, "2024-01-02", persist=False, mark=False)
            self.assertEqual(digest["cost"]["tokens"], 1_000_215)
            self.assertEqual(digest["cost"]["today_eur"], 2.25)
            self.assertTrue(insights["cost_approximate"])
            self.assertEqual(next(row for row in insights["by_profile"] if row["profile"] == "no-db")["sessions"], 0)

    def test_default_telemetry_discovery_is_direct_only_and_preserves_standalone_homes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            home = root / "ceo-orchestrator"
            start, end, _ = api.day_bounds("2024-01-02", "UTC")
            make_state_db(home / "state.db", [(start, "a", 1, 0, 0, 0, 1.0)])
            make_state_db(root / "worker" / "state.db", [(start, "b", 2, 0, 0, 0, 2.0)])
            make_state_db(root / "nested" / "deep" / "state.db", [(start, "c", 4, 0, 0, 0, 4.0)])

            insights = api.build_insights(api.Config(hermes_home=home, timezone="UTC"), start, end)
            self.assertEqual(insights["included_profiles"], ["ceo-orchestrator", "worker"])
            self.assertEqual(insights["overview"]["total_tokens"], 3)
            self.assertEqual(insights["cost"], 3.0)

            active_alias = Path(tmp) / "active-home"
            active_alias.symlink_to(home, target_is_directory=True)
            via_alias = api.build_insights(api.Config(hermes_home=active_alias, timezone="UTC"), start, end)
            self.assertEqual(via_alias["overview"]["total_tokens"], 3)
            self.assertIn("worker", via_alias["included_profiles"])

            standalone = Path(tmp) / "solo-root" / "standalone"
            make_state_db(standalone / "state.db", [(start, "solo", 3, 0, 0, 0, 3.0)])
            solo = api.build_insights(api.Config(hermes_home=standalone, timezone="UTC"), start, end)
            self.assertEqual(solo["included_profiles"], ["standalone"])
            self.assertEqual(solo["overview"]["sessions"], 1)

    def test_telemetry_path_and_session_id_deduplication_are_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            home = root / "dashboard"
            worker = root / "worker" / "state.db"
            start, end, _ = api.day_bounds("2024-01-02", "UTC")
            make_identified_state_db(home / "state.db", [("shared", start, "a", 10, 0, 0, 0, 1.0)])
            make_identified_state_db(worker, [
                ("shared", start, "a", 10, 0, 0, 0, 1.0),
                ("worker-only", start, "b", 20, 0, 0, 0, 2.0),
            ])
            alias = Path(tmp) / "alias-state.db"
            alias.symlink_to(home / "state.db")

            insights = api.build_insights(
                api.Config(hermes_home=home, telemetry_profile_dbs=[alias], timezone="UTC"), start, end)
            diagnostics = insights["telemetry_sources"]
            self.assertEqual(insights["overview"]["sessions"], 2)
            self.assertEqual(insights["overview"]["total_tokens"], 30)
            self.assertEqual(insights["cost"], 3.0)
            self.assertEqual(diagnostics["path_duplicates_skipped"], 2)
            self.assertEqual(diagnostics["deduplication"]["deduplicated_sessions"], 1)

    def test_distinct_unidentified_rows_count_and_invalid_sources_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            home = root / "dashboard"
            start, end, _ = api.day_bounds("2024-01-02", "UTC")
            same_row = (start, "same", 10, 1, 0, 0, 1.0)
            make_state_db(home / "state.db", [same_row])
            make_state_db(root / "worker" / "state.db", [same_row])
            malformed = root / "broken" / "state.db"
            malformed.parent.mkdir(parents=True)
            malformed.write_text("not a sqlite database")
            unsupported = root / "legacy" / "state.db"
            unsupported.parent.mkdir(parents=True)
            with sqlite3.connect(unsupported) as conn:
                conn.execute("CREATE TABLE sessions (id TEXT)")

            insights = api.build_insights(api.Config(hermes_home=home, timezone="UTC"), start, end)
            diagnostics = insights["telemetry_sources"]
            self.assertEqual(insights["overview"]["sessions"], 2)
            self.assertEqual(insights["overview"]["total_tokens"], 22)
            self.assertEqual(diagnostics["deduplication"]["deduplicated_sessions"], 0)
            self.assertEqual(set(diagnostics["deduplication"]["sources_without_safe_session_id"]), {"dashboard", "worker"})
            self.assertEqual({item["profile"] for item in diagnostics["skipped_sources"]}, {"broken", "legacy"})

    def test_telemetry_diagnostics_flow_into_daily_and_range_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            home = root / "dashboard"
            start, _, _ = api.day_bounds("2024-01-02", "UTC")
            make_state_db(home / "state.db", [(start, "a", 1, 0, 0, 0, 1.0)])
            make_state_db(root / "worker" / "state.db", [(start, "b", 2, 0, 0, 0, 2.0)])
            make_board(home / "kanban.db", "task", start)
            cfg = api.Config(hermes_home=home, timezone="UTC")

            digest = api.build_digest(cfg, "2024-01-02", persist=False, mark=False)
            rolled = api.build_range(cfg, "2024-01-02", "2024-01-02")
            self.assertEqual(digest["cost"]["telemetry_sources"]["source_count"], 2)
            self.assertEqual(rolled["telemetry_sources"]["source_count"], 2)

    def test_actual_cost_wins_over_estimation_and_ranges_sum_their_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "dashboard"
            first_start, first_end, _ = api.day_bounds("2024-01-01", "UTC")
            second_start, second_end, _ = api.day_bounds("2024-01-02", "UTC")
            make_state_db(home / "state.db", [
                (first_start, "gpt-5", 1_000_000, 0, 0, 0, 7.5),
                (second_start, "gpt-5", 1_000_000, 0, 0, 0, 2.5),
            ])
            make_board(home / "kanban.db", "range-task", first_start)
            cfg = api.Config(hermes_home=home, timezone="UTC")
            first = api.build_insights(cfg, first_start, first_end)
            second = api.build_insights(cfg, second_start, second_end)
            both = api.build_insights(cfg, first_start, second_end)

            self.assertEqual(first["cost"], 7.5)
            self.assertFalse(first["cost_approximate"])
            self.assertEqual(both["cost"], first["cost"] + second["cost"])
            rolled = api.build_range(cfg, "2024-01-01", "2024-01-02")
            self.assertEqual(rolled["cost_eur"], first["cost"] + second["cost"])


if __name__ == "__main__":
    unittest.main()
