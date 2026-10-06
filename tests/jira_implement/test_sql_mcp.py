"""devflow-sql: the read-only guard, the reconnect/retry/timeout policy against a real stdio server, and how the
coding agents, setup and doctor see it."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_workflows import doctor
from dev_workflows.jira_implement.claude_code_mcp import claude_code_servers, suggest
from dev_workflows.jira_implement.coding_agent import tool_policy
from dev_workflows.jira_implement.mcp_client import McpToolError
from dev_workflows.jira_implement.mcp_config import resolve_sql_server, sql_servers
from dev_workflows.jira_implement.sql_mcp import (ENV_VAR, PROXY_NAME, ConnectionLost, SqlGateway, SqlUnavailable,
                                                  is_connection_error, pick_query_tool, read_only_problem, with_limit)
from dev_workflows.jira_implement.workspace import load_workspace

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mysql_mcp.py")


# --- guard ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE id = 1",
    "select o.id, replace(o.note, 'a', 'b') from orders o join users u on u.id = o.user_id;",
    "WITH t AS (SELECT id FROM orders) SELECT * FROM t",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT 'update; delete' AS text, `delete` FROM t -- trailing comment",
    "SELECT created_at, updated_at FROM t /* a comment */",
    "SHOW TABLES", "SHOW CREATE TABLE orders", "SHOW INDEX FROM orders",
    "DESCRIBE orders", "DESC orders status", "EXPLAIN SELECT * FROM orders", "EXPLAIN FORMAT=JSON SELECT 1",
])
def test_reads_are_allowed(sql):
    assert read_only_problem(sql) == ""


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET status = 'x'", "DELETE FROM orders", "INSERT INTO t VALUES (1)", "REPLACE INTO t VALUES (1)",
    "DROP TABLE t", "ALTER TABLE t ADD c int", "CREATE TABLE t (id int)", "TRUNCATE t", "CALL p()", "SET @a = 1",
    "GRANT ALL ON *.* TO x", "LOCK TABLES t WRITE", "LOAD DATA INFILE 'x' INTO TABLE t",
    "SELECT 1; DROP TABLE t", "SELECT 1; SELECT 2",
    "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT * FROM t FOR UPDATE", "SELECT * FROM t FOR SHARE",
    "SELECT * FROM t LOCK IN SHARE MODE", "SELECT @a := 1", "SELECT SLEEP(100)", "SELECT BENCHMARK(1e9, MD5('a'))",
    "SELECT /*! 1; DROP TABLE t */ 1", "WITH t AS (SELECT 1) DELETE FROM x", "SELECT 'unclosed",
    "EXPLAIN ANALYZE SELECT 1", "EXPLAIN DELETE FROM t", "", "-- only a comment",
])
def test_writes_and_tricks_are_refused(sql):
    assert read_only_problem(sql) != ""


def test_limit_is_added_to_selects_only():
    assert with_limit("SELECT * FROM t -- note", 200) == "SELECT * FROM t -- note\nLIMIT 200"
    assert with_limit("SELECT * FROM t LIMIT 5;", 200) == "SELECT * FROM t LIMIT 5;"
    assert with_limit("SHOW TABLES", 200) == "SHOW TABLES"


def test_connection_errors_are_told_apart_from_sql_errors():
    for text in ("MySQL server has gone away", "Lost connection to MySQL server during query (2013)",
                 "connect ECONNREFUSED 127.0.0.1:3306", "Can't connect to MySQL server", "PROTOCOL_CONNECTION_LOST"):
        assert is_connection_error(text)
    for text in ("You have an error in your SQL syntax (1064)", "Unknown column 'x' in 'field list'", "Access denied"):
        assert not is_connection_error(text)


def test_query_tool_and_argument_are_found_by_name():
    assert pick_query_tool([{"name": "list_tables", "schema": {}},
                            {"name": "execute_sql", "schema": {"properties": {"query": {}}}}]) == ("execute_sql", "query")
    assert pick_query_tool([{"name": "mysql_query", "schema": {"properties": {"sql": {}}}}]) == ("mysql_query", "sql")
    assert pick_query_tool([{"name": "run", "schema": {"properties": {"stmt": {}}}}], wanted="run") == ("run", "stmt")
    assert pick_query_tool([{"name": "list_tables", "schema": {}}]) is None


# --- retry policy (fake upstream) ----------------------------------------------------------------------------------
class FlakyUpstream:
    """Upstream double: `fails` is a list of exceptions (or None for success) consumed per call."""

    def __init__(self, *fails, error_text=""):
        self.fails, self.calls, self.connects, self.error_text = list(fails), [], 1, error_text

    def list_tools(self):
        return [{"name": "mysql_query", "schema": {"properties": {"sql": {}}}}]

    def call(self, tool, args):
        self.calls.append(args["sql"])
        f = self.fails.pop(0) if self.fails else None
        if f:
            self.connects += 1
            raise f
        return (self.error_text, True) if self.error_text else ('[{"id": 1}]', False)


def gateway(up, **settings):
    sleeps = []
    gw = SqlGateway("mysql", {}, settings, upstream=up, sleep=sleeps.append, clock=lambda: gw_clock[0])
    return gw, sleeps


gw_clock = [0.0]


def test_reconnects_with_backoff_then_succeeds():
    up = FlakyUpstream(ConnectionLost("gone away"), ConnectionLost("gone away"))
    gw, sleeps = gateway(up)
    assert gw.query("SELECT id FROM t") == '[{"id": 1}]'
    assert sleeps == [1.0, 2.0] and gw.reconnects == 2 and len(up.calls) == 3
    assert gw.take_notes().count("[devflow-sql: reconnected after: gone away]") == 2 and gw.take_notes() == ""


def test_gives_up_after_max_retries_with_a_clear_message():
    up = FlakyUpstream(*[ConnectionLost("timed out after 30s")] * 10)
    gw, sleeps = gateway(up, max_retries=3)
    with pytest.raises(SqlUnavailable, match="unavailable after 3 reconnects: timed out"):
        gw.query("SELECT 1")
    assert sleeps == [1.0, 2.0, 4.0] and len(up.calls) == 4


def test_sql_errors_are_not_retried():
    up = FlakyUpstream(error_text="Unknown column 'x' (1054)")
    gw, sleeps = gateway(up)
    with pytest.raises(McpToolError, match="Unknown column"):
        gw.query("SELECT x FROM t")
    assert sleeps == [] and len(up.calls) == 1


def test_refused_statements_never_reach_the_server():
    up = FlakyUpstream()
    gw, _ = gateway(up)
    with pytest.raises(PermissionError, match="read-only"):
        gw.query("UPDATE t SET a = 1")
    assert up.calls == []


def test_idle_session_is_pinged_first():
    up = FlakyUpstream(None, ConnectionLost("gone away"))
    gw, _ = gateway(up, idle_ping_s=60)
    gw_clock[0] = 0.0
    gw.query("SELECT 1 FROM a")
    gw_clock[0] = 61.0
    up.fails = [ConnectionLost("gone away")]  # the ping finds the stale session
    gw.query("SELECT 2 FROM b")
    assert up.calls[-2:] == ["SELECT 1", "SELECT 2 FROM b\nLIMIT 200"] and "idle connection was stale" in gw.take_notes()


# --- against a real stdio server -----------------------------------------------------------------------------------
@pytest.fixture
def fake(tmp_path, monkeypatch):
    state, log = tmp_path / "state.json", tmp_path / "log"
    state.write_text("{}")
    log.write_text("")
    monkeypatch.setenv("FAKE_SQL_STATE", str(state))
    monkeypatch.setenv("FAKE_SQL_LOG", str(log))
    gw = SqlGateway("mysql", {"command": sys.executable, "args": [FAKE]}, {"timeout_s": 3}, sleep=lambda s: None)
    yield SimpleNamespace(gw=gw, log=log, set=lambda **kw: state.write_text(json.dumps(kw)),
                          pids=lambda: [line.split()[0] for line in log.read_text().splitlines()])
    gw.up.close()


def test_one_session_is_kept_open(fake):
    fake.gw.query("SELECT 1 FROM a")
    fake.gw.query("SELECT 2 FROM b")
    assert len(set(fake.pids())) == 1 and fake.gw.up.connects == 1


@pytest.mark.parametrize("failure", ["gone_away", "die", "hang"])
def test_a_dropped_dead_or_hung_server_is_restarted_and_the_query_retried(fake, failure):
    fake.gw.query("SELECT 1 FROM a")
    fake.set(**{failure: 1})
    assert fake.gw.query("SELECT 2 FROM b") == '[{"1": 1}]'
    pids = fake.pids()
    assert len(pids) == 3 and pids[1] != pids[2]  # the retry ran on a new server process
    assert "reconnected after" in fake.gw.take_notes()


def test_real_sql_error_comes_back_once(fake):
    with pytest.raises(McpToolError, match="SQL syntax"):
        fake.gw.query("SELECT 1 FROM SELEC x")
    assert len(fake.pids()) == 1


def test_schema_and_status(fake):
    assert "CREATE TABLE `orders`" in fake.gw.schema("orders")
    with pytest.raises(ValueError):
        fake.gw.schema("orders; drop")
    assert fake.gw.status()["reachable"] is True


# --- wiring: agents, setup, doctor ---------------------------------------------------------------------------------
def write_ws(tmp_path, extra):
    p = tmp_path / "workspace.yaml"
    p.write_text("repos: {}\nmcp_servers:\n  mydb: {command: npx, args: [mysql-mcp]}\n" + extra)
    return load_workspace(p)


def test_agents_get_only_the_proxy_and_the_upstream_is_denied(tmp_path):
    ws = write_ws(tmp_path, "sql: {server: mydb, timeout_s: 10}\n")
    servers, upstream = sql_servers(ws, files=[])
    assert list(servers) == [PROXY_NAME] and upstream == "mydb"
    spec = json.loads(servers[PROXY_NAME]["env"][ENV_VAR])
    assert spec == {"name": "mydb", "config": {"command": "npx", "args": ["mysql-mcp"]}, "settings": {"timeout_s": 10}}
    assert sql_servers(write_ws(tmp_path, ""), files=[]) == ({}, "")

    deny = ("mydb",)
    assert tool_policy("mcp__mydb__mysql_query", {"sql": "SELECT 1"}, ["/w/api"], deny_mcp=deny)[0] is False
    assert tool_policy(f"mcp__{PROXY_NAME}__sql_query", {"sql": "SELECT 1"}, ["/w/api"], deny_mcp=deny)[0] is True
    assert tool_policy(f"mcp__{PROXY_NAME}__sql_query", {"sql": "DROP TABLE t"}, ["/w/api"], deny_mcp=deny)[0] is False
    assert tool_policy(f"mcp__{PROXY_NAME}__sql_schema", {"table": "t"}, ["/w/api"], deny_mcp=deny)[0] is True


def test_claude_code_server_needs_a_local_config_and_deny_hosts_is_a_seatbelt(tmp_path):
    cc = tmp_path / ".claude.json"
    cc.write_text(json.dumps({"mcpServers": {"mysql": {"command": "npx", "args": ["mcp-server-mysql"],
                                                       "env": {"MYSQL_HOST": "dev-db.local"}}}}))
    ws = write_ws(tmp_path, "claude_code_mcp: {mysql: mysql}\n")
    assert resolve_sql_server(ws, files=[cc])[0] == "mysql"
    with pytest.raises(ValueError, match="no local config"):
        resolve_sql_server(ws, files=[])
    assert sql_servers(ws, files=[]) == ({}, "mysql")  # still denied directly even when the proxy can't start
    ws = write_ws(tmp_path, "claude_code_mcp: {mysql: mysql}\nsql: {deny_hosts: [dev-db.local]}\n")
    with pytest.raises(ValueError, match="deny_hosts"):
        resolve_sql_server(ws, files=[cc])


def test_setup_suggests_a_mysql_server():
    init = {"mcp_servers": [{"name": "db", "status": "connected"}, {"name": "pw", "status": "connected"}],
            "tools": ["mcp__db__mysql_query", "mcp__pw__browser_navigate"]}
    assert suggest(claude_code_servers(init))["mysql"] == "db"
    init = {"mcp_servers": [{"name": "local-mariadb", "status": "failed"}], "tools": []}
    assert suggest(claude_code_servers(init))["mysql"] == "local-mariadb"


def test_doctor_checks_ping_reconnect_and_read_only(fake):
    checks = {c.id: c for c in doctor.sql_checks(SimpleNamespace(), gateway=fake.gw)}
    assert checks["sql.server"].status == "ok" and "mysql_query(sql)" in checks["sql.server"].detail
    assert checks["sql.ping"].status == "ok"
    assert checks["sql.reconnect"].status == "ok"
    assert checks["sql.read_only"].status == "ok"
    assert "DELETE" not in fake.log.read_text()  # refused before it was sent


def test_doctor_reports_an_unreachable_server_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SQL_STATE", str(tmp_path / "missing.json"))
    gw = SqlGateway("mysql", {"command": "/no/such/mysql-mcp"}, {"max_retries": 1}, sleep=lambda s: None)
    checks = doctor.sql_checks(SimpleNamespace(), gateway=gw)
    assert checks[-1].status == "fail" and not checks[-1].blocking and "unavailable" in checks[-1].detail
    assert doctor.sql_checks(SimpleNamespace(sql={}, claude_code_mcp={})) == []


def test_the_proxy_as_claude_code_starts_it(tmp_path, fake):
    """devflow-sql over stdio, exactly as the agents get it: reads work, a write is refused with the reason, and a
    reconnect shows up as a note in the result."""
    from dev_workflows.jira_implement.mcp_client import StdioOrHttpMcp
    ws = write_ws(tmp_path, "sql: {server: mydb, timeout_s: 3}\n")
    servers, _ = sql_servers(ws, files=[])
    cfg = servers[PROXY_NAME]
    spec = json.loads(cfg["env"][ENV_VAR])
    spec["config"] = {"command": sys.executable, "args": [FAKE]}
    proxy = StdioOrHttpMcp(PROXY_NAME, {**cfg, "env": {ENV_VAR: json.dumps(spec)}})
    assert {"sql_query", "sql_schema", "sql_status"} <= set(proxy.list_tools())
    fake.set(gone_away=1)
    out = proxy.call("sql_query", {"sql": "SELECT id FROM orders"})
    assert '"1": 1' in str(out) and "[devflow-sql: reconnected after:" in str(out)
    with pytest.raises(McpToolError, match="DELETE is not allowed"):
        proxy.call("sql_query", {"sql": "DELETE FROM orders"})
