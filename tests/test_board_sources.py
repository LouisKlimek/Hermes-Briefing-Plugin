import importlib.util
import sqlite3
import sys
import tempfile
import unittest
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


class BoardSourceTests(unittest.TestCase):
    def test_report_embeds_the_grouped_task_view_without_a_tasks_tab(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('h(Section, { title: "Tasks ("', bundle)
        self.assertIn('tasks: tasks, tasksLoading: tasksLoading', bundle)
        self.assertIn('"List: " + list', bundle)
        self.assertNotIn('label: "Tasks"', bundle)

    def test_daily_report_omits_done_accordion_and_done_task_cards(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertNotIn("function DoneGrid", bundle)
        self.assertNotIn('title: "Done (" + digest.done.length', bundle)
        self.assertIn('title: "Active (" + digest.in_progress.length', bundle)
        self.assertIn('title: "Tasks (" + ((props.tasks || []).length)', bundle)

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

    def test_report_chart_prefers_tasklist_list_identity_over_board(self):
        bundle = (Path(__file__).parents[1] / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('task.list || task.board || "Default list"', bundle)
        self.assertIn('task.list || task.board || "—"', bundle)

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
        })
        self.assertEqual(cfg.external_board_roots, [Path("/boards-a"), Path("/boards-b")])
        self.assertEqual(cfg.external_board_dbs, [Path("/allowed/custom/kanban.db")])


if __name__ == "__main__":
    unittest.main()
