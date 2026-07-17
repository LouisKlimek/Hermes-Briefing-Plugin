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
