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
