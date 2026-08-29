#!/usr/bin/env python3
"""Contract tests for the universal twin substrate flight."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from agents import twin_substrate_agent as substrate


class TestTwinSubstrateAgent(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.root = Path(self.tempdir) / "twins"
        self.old_root = os.environ.get("BRAINSTEM_TWINS_ROOT")
        os.environ["BRAINSTEM_TWINS_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("BRAINSTEM_TWINS_ROOT", None)
        else:
            os.environ["BRAINSTEM_TWINS_ROOT"] = self.old_root
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def manifest(self, twin):
        path = self.root / twin / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def event_pointer(self, twin, text):
        connection = substrate._connect(twin)
        row = connection.execute(
            "SELECT ptr FROM events WHERE text LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{text}%",),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        return row[0]

    def test_agent_surface_is_a_valid_brainstem_tool(self):
        agent = substrate.TwinSubstrateAgent()
        tool = agent.to_tool()["function"]

        self.assertEqual(tool["name"], "TwinSubstrate")
        self.assertEqual(
            tool["parameters"]["properties"]["action"]["enum"],
            list(substrate.ACTIONS),
        )
        self.assertIn("text_field", tool["parameters"]["properties"])
        self.assertEqual(
            agent.perform(action="open", ptr="/tmp/not-evidence:1"),
            "action='open' needs a twin name.",
        )

    def test_twin_slugging_cannot_alias_distinct_names(self):
        self.assertNotEqual(substrate._slug("東京"), substrate._slug("北京"))
        self.assertNotEqual(substrate._slug("A B"), substrate._slug("A-B"))
        self.assertEqual(substrate._slug("Already-Safe"), "already-safe")

    def test_designate_is_additive_and_rebases_physical_preset(self):
        twin_dir = self.root / "wildhaven"
        twin_dir.mkdir(parents=True)
        existing = {
            "schema": "rapp/1-twin",
            "name": "wildhaven",
            "parent_rappid": "rappid://lineage-root",
            "what_a_twin_is": "author-owned definition",
            "not_a_holo": "author-owned distinction",
        }
        (twin_dir / "manifest.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )
        parent = Path(self.tempdir) / "physical-parent"
        parent.mkdir()

        result = substrate.op_designate(
            twin="Wildhaven",
            parent_class="place",
            parent_nature="PHYSICAL",
            display_name="Wildhaven AI Homes",
            address=str(parent),
            preset="place",
        )

        self.assertIn("physical/place", result)
        manifest = self.manifest("wildhaven")
        self.assertEqual(manifest["schema"], "rapp/2-twin")
        self.assertEqual(manifest["parent_rappid"], "rappid://lineage-root")
        self.assertEqual(manifest["what_a_twin_is"], "author-owned definition")
        self.assertEqual(manifest["not_a_holo"], "author-owned distinction")
        self.assertEqual(manifest["parent"]["nature"], "physical")
        self.assertEqual(manifest["parent"]["class"], "place")
        self.assertEqual(
            manifest["parent"]["address"],
            [{"scheme": "file", "value": os.path.abspath(parent)}],
        )
        self.assertEqual(
            {source["root"] for source in manifest["substrate"]["sources"]},
            {str(parent.resolve())},
        )

    def test_designate_refuses_to_overwrite_malformed_manifest(self):
        twin_dir = self.root / "broken"
        twin_dir.mkdir(parents=True)
        path = twin_dir / "manifest.json"
        original = "{not valid json"
        path.write_text(original, encoding="utf-8")

        result = substrate.TwinSubstrateAgent().perform(
            action="designate",
            twin="broken",
            parent_class="repo",
        )

        self.assertIn("invalid JSON", result)
        self.assertIn("refusing to overwrite", result)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_designate_refuses_semantically_invalid_manifest_sections(self):
        invalid_manifests = [
            {
                "name": "broken",
                "parent": {"class": "repo"},
                "substrate": {"sources": {}},
            },
            {
                "name": "broken",
                "parent": {"class": "repo"},
                "substrate": {"sources": [None]},
            },
        ]
        for index, manifest in enumerate(invalid_manifests):
            twin = f"broken-{index}"
            twin_dir = self.root / twin
            twin_dir.mkdir(parents=True)
            path = twin_dir / "manifest.json"
            original = json.dumps(manifest)
            path.write_text(original, encoding="utf-8")

            result = substrate.TwinSubstrateAgent().perform(
                action="designate",
                twin=twin,
                parent_class="repo",
            )

            self.assertIn("refusing to overwrite", result)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_filesystem_harvest_search_and_evidence_open_are_safe(self):
        parent = Path(self.tempdir) / "parent"
        parent.mkdir()
        evidence = parent / "notes.md"
        github_token = "ghp_0123456789abcdefghijklmnop"
        fine_grained_token = "github_pat_" + ("A" * 40)
        aws_secret = "AWS_SECRET_ACCESS_KEY=AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "super-sensitive-material\n"
            "-----END PRIVATE KEY-----"
        )
        truncated_key = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "still-super-sensitive\n"
        )
        evidence.write_text(
            f"Quasar handshake completed.\ntoken={github_token}\n"
            f"{fine_grained_token}\n{aws_secret}\n{private_key}\n{truncated_key}",
            encoding="utf-8",
        )
        arbitrary = parent / "not-indexed.txt"
        arbitrary.write_text("must never be opened", encoding="utf-8")

        substrate.op_designate("workflow", parent_class="person")
        substrate.op_bind(
            "workflow",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )

        first = substrate.op_harvest("workflow")
        second = substrate.op_harvest("workflow")
        search = substrate.op_search("workflow", query="quasar handshake")
        pointer = self.event_pointer("workflow", "Quasar handshake")
        opened = substrate.op_open("workflow", ptr=pointer)
        denied = substrate.op_open("workflow", ptr=f"{arbitrary}:1")

        self.assertRegex(first, r"filesystem\s+6 new /\s+6 scanned")
        self.assertIn("0 new /       0 scanned", second)
        self.assertIn(pointer, search)
        self.assertNotIn(github_token, search)
        self.assertNotIn(fine_grained_token, search)
        self.assertNotIn(aws_secret, search)
        self.assertNotIn(private_key, search)
        self.assertIn("[REDACTED:github-token]", opened)
        self.assertIn("[REDACTED:assignment]", opened)
        self.assertIn("[REDACTED:private-key]", opened)
        self.assertNotIn("super-sensitive-material", opened)
        self.assertNotIn("still-super-sensitive", opened)
        self.assertIn("refusing to read an arbitrary local path", denied)

    def test_symlinked_evidence_is_rejected_at_harvest_and_open(self):
        parent = Path(self.tempdir) / "parent"
        parent.mkdir()
        outside = Path(self.tempdir) / "outside.md"
        outside.write_text("outside secret", encoding="utf-8")
        linked = parent / "linked.md"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        substrate.op_designate("symlink", parent_class="repo")
        substrate.op_bind(
            "symlink",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )
        harvest = substrate.op_harvest("symlink")

        self.assertRegex(
            harvest, r"Refusing symlinked (?:evidence path|source entry)"
        )
        con = substrate._connect("symlink")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        con.close()

        linked.unlink()
        linked.write_text("safe evidence", encoding="utf-8")
        self.assertIn("1 new", substrate.op_harvest("symlink"))
        pointer = self.event_pointer("symlink", "safe evidence")
        linked.unlink()
        linked.symlink_to(outside)

        opened = substrate.op_open("symlink", ptr=pointer)

        self.assertIn("Refusing symlinked evidence path", opened)
        self.assertNotIn("outside secret", opened)

    def test_retargeted_bound_root_is_rejected(self):
        parent = Path(self.tempdir) / "bound-root"
        parent.mkdir()
        (parent / "safe.md").write_text("safe source", encoding="utf-8")
        outside = Path(self.tempdir) / "outside-root"
        outside.mkdir()
        (outside / "secret.md").write_text("outside secret", encoding="utf-8")

        substrate.op_designate("boundary", parent_class="repo")
        substrate.op_bind(
            "boundary",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )
        original = Path(self.tempdir) / "bound-root-original"
        parent.rename(original)
        try:
            parent.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        result = substrate.op_harvest("boundary")

        self.assertRegex(result, r"boundary became a symlink|boundary identity changed")
        self.assertNotIn("outside secret", result)
        connection = substrate._connect("boundary")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
        )
        connection.close()

    def test_prompt_history_pointer_opens_display_field(self):
        history = Path(self.tempdir) / "history.jsonl"
        history.write_text(
            json.dumps(
                {
                    "display": "Fixed the device-code auth hang",
                    "timestamp": 1788020000000,
                    "project": "brainstem",
                    "sessionId": "session-1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        substrate.op_designate("workflow", parent_class="person")
        substrate.op_bind(
            "workflow", source_type="prompt_history", path=str(history)
        )
        substrate.op_harvest("workflow")

        pointer = self.event_pointer("workflow", "device-code auth hang")
        opened = substrate.op_open("workflow", ptr=pointer)

        self.assertIn("Fixed the device-code auth hang", opened)
        self.assertIn("project=brainstem", opened)

    def test_file_pointers_are_line_accurate_and_versioned(self):
        parent = Path(self.tempdir) / "versioned"
        parent.mkdir()
        evidence = parent / "record.md"
        evidence.write_text(
            "\n".join(
                [
                    "line one",
                    "line two",
                    "line three",
                    "line four",
                    "line five",
                    "line six",
                    "quasar evidence on line seven",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        substrate.op_designate("versioned", parent_class="document")
        substrate.op_bind(
            "versioned",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )
        substrate.op_harvest("versioned")

        old_pointer = self.event_pointer("versioned", "quasar evidence")
        self.assertRegex(old_pointer, r":7@[0-9a-f]{16}$")
        self.assertIn(
            "quasar evidence on line seven",
            substrate.op_open("versioned", ptr=old_pointer),
        )

        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace("quasar", "nebula"),
            encoding="utf-8",
        )
        substrate.op_harvest("versioned")
        new_pointer = self.event_pointer("versioned", "nebula evidence")

        self.assertNotEqual(old_pointer, new_pointer)
        self.assertIn(
            "not indexed",
            substrate.op_open("versioned", ptr=old_pointer),
        )
        self.assertIn(
            "nebula evidence on line seven",
            substrate.op_open("versioned", ptr=new_pointer),
        )

    def test_common_credentials_are_scrubbed_from_persisted_sources(self):
        source_root = Path(self.tempdir) / "credentials"
        transcript_root = source_root / "transcripts"
        transcript_root.mkdir(parents=True)
        transcript = transcript_root / "session.jsonl"
        transcript_secret = "DB_PASSWORD='S3cr@t!value'"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-08-29T12:00:00Z",
                    "message": {
                        "content": f"Deploy using {transcript_secret} right now"
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        records = source_root / "records.jsonl"
        json_secret = '"password": "P@ssw0rd!"'
        invalid_timestamp_secret = "password=TimestampSecret!"
        records.write_text(
            json.dumps(
                {
                    "timestamp": invalid_timestamp_secret,
                    "text": f"payload with {json_secret}",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        shell = source_root / "shell_history"
        bearer_secret = "Authorization: Bearer abcdefghijklmnop"
        basic_secret = "Authorization: Basic dXNlcjpwYXNz"
        uri_secret = "https://user:pass@example.test/private"
        database_uri_secret = "postgresql://dbuser:dbpass@db.invalid/app"
        token_uri_secret = "https://token_value@example.invalid/repo"
        cli_secret = "--password 'CommandP@ss!'"
        client_secret = "--client-secret client_value"
        access_token = "--access-token access_value"
        curl_user = "-u user:curl_password"
        attached_curl_user = "-uUSER:attached_password"
        underscored_secret = "--client_secret underscored_value"
        underscored_token = "--access_token underscored_token_value"
        azure_connection = (
            "DefaultEndpointsProtocol=https;AccountName=demo;"
            "AccountKey=AzureStorageSecretValue1234567890;"
            "EndpointSuffix=core.windows.net"
        )
        azure_sas = (
            "https://storage.example.invalid/container?sv=2024-01-01&"
            "sig=AzureSasSignatureValue1234567890"
        )
        azure_json_key = '"AccountKey": "AzureJsonSecretValue1234567890"'
        azure_yaml_key = "SharedAccessKey: AzureYamlSecretValue1234567890"
        azure_structured_sas = (
            "SharedAccessSignature: sig=AzureStructuredSasValue1234567890"
        )
        truncated_title_secret = (
            ("x" * 190) + " https://title_user:TitlePassword@example.invalid/path"
        )
        shell.write_text(
            f"curl -H '{bearer_secret}' -H '{basic_secret}' {uri_secret} "
            f"{database_uri_secret} {token_uri_secret} {cli_secret} "
            f"{client_secret} {access_token} {curl_user} {attached_curl_user} "
            f"{underscored_secret} {underscored_token} {azure_connection} "
            f"{azure_sas} {azure_json_key} {azure_yaml_key} "
            f"{azure_structured_sas}\n"
            f"{truncated_title_secret}\n",
            encoding="utf-8",
        )

        substrate.op_designate("credentials", parent_class="person")
        substrate.op_bind(
            "credentials",
            source_type="claude_transcripts",
            root=str(transcript_root),
        )
        substrate.op_bind(
            "credentials",
            source_type="jsonl",
            path=str(records),
        )
        substrate.op_bind(
            "credentials",
            source_type="shell_history",
            path=str(shell),
        )
        result = substrate.op_harvest("credentials")

        self.assertIn("4 new", result)
        connection = substrate._connect("credentials")
        persisted = "\n".join(
            value or ""
            for row in connection.execute(
                "SELECT title,text,ref,meta FROM events"
            ).fetchall()
            for value in row
        )
        connection.close()
        self.assertNotIn(transcript_secret, persisted)
        self.assertNotIn(json_secret, persisted)
        self.assertNotIn(bearer_secret, persisted)
        self.assertNotIn(basic_secret, persisted)
        self.assertNotIn(uri_secret, persisted)
        self.assertNotIn(database_uri_secret, persisted)
        self.assertNotIn(token_uri_secret, persisted)
        self.assertNotIn(cli_secret, persisted)
        self.assertNotIn(client_secret, persisted)
        self.assertNotIn(access_token, persisted)
        self.assertNotIn(curl_user, persisted)
        self.assertNotIn(attached_curl_user, persisted)
        self.assertNotIn(underscored_secret, persisted)
        self.assertNotIn(underscored_token, persisted)
        self.assertNotIn("AzureStorageSecretValue", persisted)
        self.assertNotIn("AzureSasSignatureValue", persisted)
        self.assertNotIn("AzureJsonSecretValue", persisted)
        self.assertNotIn("AzureYamlSecretValue", persisted)
        self.assertNotIn("AzureStructuredSasValue", persisted)
        self.assertNotIn("TitlePassword", persisted)
        self.assertNotIn(invalid_timestamp_secret, persisted)
        self.assertIn("[REDACTED:assignment]", persisted)
        self.assertIn("[REDACTED:authorization]", persisted)
        self.assertIn("[REDACTED:uri-credentials]", persisted)
        self.assertIn("[REDACTED:cli-secret]", persisted)
        self.assertIn("[REDACTED:azure-key]", persisted)
        self.assertIn("[REDACTED:azure-sas]", persisted)
        connection = substrate._connect("credentials")
        timestamp = connection.execute(
            "SELECT ts FROM events WHERE source_type='jsonl'"
        ).fetchone()[0]
        connection.close()
        self.assertIsNone(timestamp)
        rejected_address = substrate.TwinSubstrateAgent().perform(
            action="designate",
            twin="credential-address",
            parent_class="repo",
            address=database_uri_secret,
        )
        self.assertIn("cannot contain credential-shaped data", rejected_address)
        rejected_structured_address = substrate.TwinSubstrateAgent().perform(
            action="designate",
            twin="structured-credential-address",
            parent_class="repo",
            address={
                "scheme": "https",
                "value": "address_user:AddressPassword@example.invalid",
            },
        )
        self.assertIn(
            "cannot contain credential-shaped data",
            rejected_structured_address,
        )
        rejected_sas_address = substrate.TwinSubstrateAgent().perform(
            action="designate",
            twin="sas-credential-address",
            parent_class="repo",
            address=azure_sas,
        )
        self.assertIn(
            "cannot contain credential-shaped data",
            rejected_sas_address,
        )
        rejected_structured_azure = substrate.TwinSubstrateAgent().perform(
            action="designate",
            twin="structured-azure-address",
            parent_class="repo",
            address={"scheme": "azure", "value": azure_yaml_key},
        )
        self.assertIn(
            "cannot contain credential-shaped data",
            rejected_structured_azure,
        )

    def test_jsonl_mapping_round_trips_through_tool_arguments(self):
        records = Path(self.tempdir) / "records.jsonl"
        records.write_text(
            json.dumps(
                {
                    "when": "2026-08-29T12:00:00Z",
                    "label": "Boiler inspection",
                    "body": "Pressure stayed nominal",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agent = substrate.TwinSubstrateAgent()
        agent.perform(
            action="designate",
            twin="boiler",
            parent_class="device",
            parent_nature="physical",
        )
        bound = agent.perform(
            action="bind",
            twin="boiler",
            source_type="jsonl",
            path=str(records),
            ts_field="when",
            title_field="label",
            text_field="body",
            kind="inspection",
        )
        harvested = agent.perform(action="harvest", twin="boiler")
        found = agent.perform(
            action="search", twin="boiler", query="pressure nominal"
        )

        self.assertIn("Bound jsonl", bound)
        self.assertIn("1 new", harvested)
        self.assertIn("Boiler inspection", found)
        source = self.manifest("boiler")["substrate"]["sources"][0]
        self.assertEqual(source["ts_field"], "when")
        self.assertEqual(source["title_field"], "label")
        self.assertEqual(source["text_field"], "body")

    def test_rebinding_source_configuration_forces_rebuild(self):
        records = Path(self.tempdir) / "rebind.jsonl"
        records.write_text(
            json.dumps({"first": "alpha interpretation", "second": "beta interpretation"})
            + "\n",
            encoding="utf-8",
        )
        substrate.op_designate("rebind", parent_class="document")
        substrate.op_bind(
            "rebind",
            source_type="jsonl",
            path=str(records),
            text_field="first",
        )
        substrate.op_harvest("rebind")
        first_id = self.manifest("rebind")["substrate"]["sources"][0]["id"]

        rebound = substrate.op_bind(
            "rebind",
            source_type="jsonl",
            path=str(records),
            text_field="second",
        )
        connection = substrate._connect("rebind")
        cleared = (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0],
        )
        connection.close()
        substrate.op_harvest("rebind")
        source = self.manifest("rebind")["substrate"]["sources"][0]

        self.assertIn("Rebound jsonl", rebound)
        self.assertEqual(cleared, (0, 0))
        self.assertEqual(source["id"], first_id)
        self.assertIn(
            "beta interpretation",
            substrate.op_search("rebind", query="beta"),
        )
        self.assertIn(
            "Nothing",
            substrate.op_search("rebind", query="alpha"),
        )

    def test_rebind_commit_failure_restores_manifest_and_database(self):
        records = Path(self.tempdir) / "rollback.jsonl"
        records.write_text(
            json.dumps({"first": "old interpretation", "second": "new interpretation"})
            + "\n",
            encoding="utf-8",
        )
        substrate.op_designate("rollback", parent_class="document")
        substrate.op_bind(
            "rollback",
            source_type="jsonl",
            path=str(records),
            text_field="first",
        )
        substrate.op_harvest("rollback")
        manifest_path = self.root / "rollback" / "manifest.json"
        original_manifest = manifest_path.read_text(encoding="utf-8")
        connection = substrate._connect("rollback")
        original_count = connection.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        connection.close()

        real_connect = substrate._connect

        class FailingCommitConnection:
            def __init__(self):
                self.connection = real_connect("rollback")

            def execute(self, *args, **kwargs):
                return self.connection.execute(*args, **kwargs)

            def commit(self):
                raise RuntimeError("injected commit failure")

            def rollback(self):
                self.connection.rollback()

            def close(self):
                self.connection.close()

        with mock.patch.object(
            substrate,
            "_connect",
            side_effect=lambda _twin: FailingCommitConnection(),
        ):
            result = substrate.TwinSubstrateAgent().perform(
                action="bind",
                twin="rollback",
                source_type="jsonl",
                path=str(records),
                text_field="second",
            )

        self.assertIn("injected commit failure", result)
        self.assertEqual(
            manifest_path.read_text(encoding="utf-8"),
            original_manifest,
        )
        connection = real_connect("rollback")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            original_count,
        )
        connection.close()

    def test_csv_numeric_epoch_timestamp_is_preserved(self):
        records = Path(self.tempdir) / "sensor.csv"
        records.write_text(
            "timestamp,value\n1788020000000,nominal\n",
            encoding="utf-8",
        )
        substrate.op_designate(
            "sensor", parent_class="device", parent_nature="physical"
        )
        substrate.op_bind(
            "sensor",
            source_type="csv_timeseries",
            root=str(records),
            ts_column="timestamp",
        )
        substrate.op_harvest("sensor")

        connection = substrate._connect("sensor")
        timestamp = connection.execute(
            "SELECT ts FROM events WHERE source_type='csv_timeseries'"
        ).fetchone()[0]
        connection.close()

        self.assertRegex(timestamp, r"^2026-\d\d-\d\dT")

    def test_timestamp_filters_normalize_rfc3339_offsets(self):
        records = Path(self.tempdir) / "time.jsonl"
        records.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-29T12:00:00Z",
                    "text": "temporal quasar proof",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        substrate.op_designate("time", parent_class="process")
        substrate.op_bind("time", source_type="jsonl", path=str(records))
        substrate.op_harvest("time")

        exact = substrate.op_search(
            "time",
            query="temporal quasar",
            since="2026-08-29T08:00:00-04:00",
        )
        invalid = substrate.op_timeline("time", since="last Tuesday")

        self.assertIn("temporal quasar proof", exact)
        self.assertIn("Invalid since timestamp", invalid)

    def test_file_replacement_uses_exact_origin_path(self):
        parent = Path(self.tempdir) / "colon-paths"
        parent.mkdir()
        report = parent / "report"
        archive = parent / "report:archive"
        report.write_text("current alpha\n", encoding="utf-8")
        archive.write_text("archive beta\n", encoding="utf-8")
        substrate.op_designate("colon-paths", parent_class="document")
        substrate.op_bind(
            "colon-paths",
            source_type="filesystem",
            root=str(parent),
            globs=["*"],
        )
        substrate.op_harvest("colon-paths")

        report.write_text("current gamma\n", encoding="utf-8")
        substrate.op_harvest("colon-paths")

        connection = substrate._connect("colon-paths")
        rows = connection.execute(
            "SELECT origin_path,text FROM events ORDER BY origin_path"
        ).fetchall()
        connection.close()

        self.assertEqual(
            rows,
            [
                (str(report.resolve()), "current gamma\n"),
                (str(archive.resolve()), "archive beta\n"),
            ],
        )

    def test_origin_path_schema_migration_clears_legacy_file_events(self):
        substrate.op_designate("legacy", parent_class="document")
        connection = substrate._connect("legacy")
        connection.execute(
            "INSERT INTO events("
            "ts,source,source_type,ref,kind,title,text,ptr,origin_path,meta,dedup"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-08-29T12:00:00+00:00",
                "filesystem:legacy",
                "filesystem",
                "legacy",
                "document",
                "legacy",
                "stale sensitive text",
                "/tmp/legacy:1@deadbeefdeadbeef",
                None,
                "{}",
                "legacy-dedup",
            ),
        )
        connection.execute(
            "INSERT INTO watermarks(source,path) VALUES(?,?)",
            ("filesystem:legacy", "/tmp/legacy"),
        )
        connection.execute(
            "UPDATE substrate_meta SET value='1' WHERE key='schema_version'"
        )
        connection.commit()
        connection.close()

        migrated = substrate._connect("legacy")
        counts = (
            migrated.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            migrated.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0],
            migrated.execute(
                "SELECT value FROM substrate_meta WHERE key='schema_version'"
            ).fetchone()[0],
        )
        migrated.close()

        self.assertEqual(counts, (0, 0, "3"))

    def test_idless_manifest_source_can_harvest_and_open(self):
        parent = Path(self.tempdir) / "idless"
        parent.mkdir()
        evidence = parent / "proof.md"
        evidence.write_text("idless quasar proof\n", encoding="utf-8")
        substrate.op_designate("idless", parent_class="document")
        substrate.op_bind(
            "idless",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )
        manifest_path = self.root / "idless" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["substrate"]["sources"][0].pop("id")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        substrate.op_harvest("idless")
        pointer = self.event_pointer("idless", "idless quasar")

        self.assertIn(
            "idless quasar proof",
            substrate.op_open("idless", ptr=pointer),
        )

    def test_git_estate_accepts_a_git_worktree_and_reports_true_new_count(self):
        git_env = substrate._safe_git_env()
        self.assertEqual(git_env["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(git_env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(git_env["GIT_ALLOW_PROTOCOL"], "")
        repo = Path(self.tempdir) / "repo"
        checkout = Path(self.tempdir) / "checkout"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test User"],
            check=True,
        )
        (repo / "proof.txt").write_text("proof", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "proof.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", str(repo), "commit", "-qm",
                "Quasar worktree proof", "-m", "Body-only nebula evidence",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", str(checkout)],
            check=True,
        )
        self.assertTrue((checkout / ".git").is_file())
        sentinel = Path(self.tempdir) / "gpg-program-ran"
        if os.name != "nt":
            helper = Path(self.tempdir) / "malicious-gpg"
            helper.write_text(
                f"#!/bin/sh\n: > '{sentinel}'\nexit 0\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            subprocess.run(
                [
                    "git", "-C", str(repo), "config",
                    "log.showSignature", "true",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "gpg.program", str(helper)],
                check=True,
            )

        substrate.op_designate("repo-twin", parent_class="repo")
        substrate.op_bind(
            "repo-twin", source_type="git_estate", root=str(checkout)
        )
        first = substrate.op_harvest("repo-twin")
        second = substrate.op_harvest("repo-twin")

        self.assertIn("1 new /       1 scanned", first)
        self.assertIn("0 new /       1 scanned", second)
        self.assertIn(
            "Quasar worktree proof",
            substrate.op_search("repo-twin", query="quasar worktree"),
        )
        body_search = substrate.op_search(
            "repo-twin", query="body-only nebula"
        )
        git_pointer = next(
            line.removeprefix("  ptr: ")
            for line in body_search.splitlines()
            if line.startswith("  ptr: ")
        )
        self.assertIn(
            "Body-only nebula evidence",
            substrate.op_open("repo-twin", ptr=git_pointer),
        )
        self.assertFalse(sentinel.exists())

        other = Path(self.tempdir) / "other-repo"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        marker = checkout / ".git"
        marker.write_text(f"gitdir: {other / '.git'}\n", encoding="utf-8")

        retargeted_harvest = substrate.op_harvest("repo-twin")
        retargeted_open = substrate.op_open("repo-twin", ptr=git_pointer)

        self.assertIn("Pinned Git marker identity changed", retargeted_harvest)
        self.assertIn("Pinned Git marker identity changed", retargeted_open)

    def test_rebinding_git_source_preserves_stable_id(self):
        repo = Path(self.tempdir) / "stable-git"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        substrate.op_designate("stable-git", parent_class="repo")
        substrate.op_bind(
            "stable-git", source_type="git_estate", root=str(repo)
        )
        first_source = self.manifest("stable-git")["substrate"]["sources"][0]

        os.utime(repo / ".git", None)
        rebound = substrate.op_bind(
            "stable-git", source_type="git_estate", root=str(repo)
        )
        sources = self.manifest("stable-git")["substrate"]["sources"]

        self.assertIn("Rebound git_estate", rebound)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], first_source["id"])

    def test_same_size_same_mtime_rewrite_is_not_skipped(self):
        parent = Path(self.tempdir) / "digest"
        parent.mkdir()
        evidence = parent / "record.md"
        evidence.write_text("alpha proof\n", encoding="utf-8")
        substrate.op_designate("digest", parent_class="document")
        substrate.op_bind(
            "digest",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
        )
        substrate.op_harvest("digest")
        old_pointer = self.event_pointer("digest", "alpha proof")
        original_stat = evidence.stat()

        evidence.write_text("bravo proof\n", encoding="utf-8")
        os.utime(
            evidence,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        result = substrate.op_harvest("digest")

        self.assertIn("1 new", result)
        self.assertIn(
            "not indexed",
            substrate.op_open("digest", ptr=old_pointer),
        )
        self.assertIn(
            "bravo proof",
            substrate.op_search("digest", query="bravo"),
        )

    def test_resource_limits_fail_before_persisting(self):
        parent = Path(self.tempdir) / "limits"
        parent.mkdir()
        evidence = parent / "oversized.md"
        evidence.write_text("x" * 1024, encoding="utf-8")
        substrate.op_designate("limits", parent_class="document")
        substrate.op_bind(
            "limits",
            source_type="filesystem",
            root=str(parent),
            globs=["*.md"],
            max_bytes=128,
        )

        result = substrate.op_harvest("limits")
        connection = substrate._connect("limits")
        counts = (
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0],
        )
        connection.close()

        self.assertIn("exceeds the 128-byte limit", result)
        self.assertEqual(counts, (0, 0))
        with self.assertRaisesRegex(ValueError, "stdout exceeded"):
            substrate._run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 100000)",
                ],
                timeout=10,
                max_bytes=1024,
            )

    def test_append_only_sources_replace_prior_file_version(self):
        history = Path(self.tempdir) / "append-history.jsonl"
        first_record = {
            "display": "first quasar prompt",
            "timestamp": 1788020000000,
        }
        second_record = {
            "display": "second nebula prompt",
            "timestamp": 1788020001000,
        }
        history.write_text(json.dumps(first_record) + "\n", encoding="utf-8")
        substrate.op_designate("append", parent_class="person")
        substrate.op_bind(
            "append", source_type="prompt_history", path=str(history)
        )
        substrate.op_harvest("append")
        old_pointer = self.event_pointer("append", "first quasar")

        with open(history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(second_record) + "\n")
        substrate.op_harvest("append")

        connection = substrate._connect("append")
        rows = connection.execute(
            "SELECT text,ptr FROM events WHERE source_type='prompt_history' "
            "ORDER BY id"
        ).fetchall()
        connection.close()

        self.assertEqual(
            [row[0] for row in rows],
            ["first quasar prompt", "second nebula prompt"],
        )
        self.assertEqual(len({row[1] for row in rows}), 2)
        self.assertIn(
            "not indexed",
            substrate.op_open("append", ptr=old_pointer),
        )

        history.unlink()
        substrate.op_harvest("append")
        connection = substrate._connect("append")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 0
        )
        connection.close()

    def test_failed_harvest_rolls_back_events_and_watermarks(self):
        evidence = Path(self.tempdir) / "evidence.txt"
        evidence.write_text("transaction proof", encoding="utf-8")
        original = substrate.HARVESTERS.get("transaction_test")

        def failing(con, src, emit):
            for index in range(2000):
                emit(
                    substrate._ev(
                        "2026-08-29T12:00:00Z",
                        "test",
                        f"event {index}",
                        f"payload {index}",
                        ptr=f"{evidence}:{index + 1}",
                    )
                )
            substrate._mark(con, src["id"], str(evidence))
            raise RuntimeError("intentional failure")

        def succeeding(con, src, emit):
            emit(
                substrate._ev(
                    "2026-08-29T12:00:00Z",
                    "test",
                    "recovered",
                    "transaction recovered",
                    ptr=f"{evidence}:1",
                )
            )
            substrate._mark(con, src["id"], str(evidence))
            return 1

        try:
            substrate.HARVESTERS["transaction_test"] = failing
            substrate.op_designate("transaction", parent_class="process")
            substrate.op_bind(
                "transaction",
                source_type="transaction_test",
                path=str(evidence),
            )

            failed = substrate.op_harvest("transaction")
            con = substrate._connect("transaction")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 0
            )
            con.close()

            substrate.HARVESTERS["transaction_test"] = succeeding
            recovered = substrate.op_harvest("transaction")
        finally:
            if original is None:
                substrate.HARVESTERS.pop("transaction_test", None)
            else:
                substrate.HARVESTERS["transaction_test"] = original

        self.assertIn("RuntimeError: intentional failure", failed)
        self.assertIn("0 total events", failed)
        self.assertIn("1 new /       1 scanned", recovered)

    def test_concurrent_binds_preserve_both_sources(self):
        substrate.op_designate("concurrent", parent_class="repo")
        source_roots = [
            Path(self.tempdir) / "source-a",
            Path(self.tempdir) / "source-b",
        ]
        for source_root in source_roots:
            source_root.mkdir()
        barrier = threading.Barrier(2)
        results = []

        def bind(source_root):
            barrier.wait(timeout=5)
            results.append(
                substrate.op_bind(
                    "concurrent",
                    source_type="filesystem",
                    root=str(source_root),
                    globs=["*.md"],
                )
            )

        threads = [
            threading.Thread(target=bind, args=(source_root,))
            for source_root in source_roots
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.startswith("Bound filesystem") for result in results))
        self.assertEqual(len(self.manifest("concurrent")["substrate"]["sources"]), 2)

    def test_bind_materializes_defaults_and_rejects_boundaryless_sources(self):
        agent = substrate.TwinSubstrateAgent()
        agent.perform(action="designate", twin="paths", parent_class="repo")

        bound = agent.perform(
            action="bind",
            twin="paths",
            source_type="filesystem",
        )
        source = self.manifest("paths")["substrate"]["sources"][0]

        self.assertIn("Bound filesystem", bound)
        self.assertTrue(os.path.isabs(source["root"]))

        original = substrate.HARVESTERS.get("boundaryless_test")
        try:
            substrate.HARVESTERS["boundaryless_test"] = lambda *_args: 0
            rejected = agent.perform(
                action="bind",
                twin="paths",
                source_type="boundaryless_test",
            )
        finally:
            if original is None:
                substrate.HARVESTERS.pop("boundaryless_test", None)
            else:
                substrate.HARVESTERS["boundaryless_test"] = original
        self.assertIn("stable root or path boundary", rejected)

    def test_repo_preset_resolves_the_git_top_level(self):
        repository_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        result = substrate.op_designate(
            "current-repo",
            parent_class="repo",
            preset="repo",
        )
        sources = self.manifest("current-repo")["substrate"]["sources"]

        self.assertIn("2 bound", result)
        self.assertEqual({source["root"] for source in sources}, {repository_root})

    def test_media_open_returns_metadata_without_reading_binary(self):
        media_root = Path(self.tempdir) / "media"
        media_root.mkdir()
        video = media_root / "tour.mp4"
        with open(video, "wb") as handle:
            handle.seek((5 * 1024 * 1024) - 1)
            handle.write(b"\0")

        substrate.op_designate("house", parent_class="place", parent_nature="physical")
        substrate.op_bind("house", source_type="media", root=str(media_root))
        substrate.op_harvest("house")

        pointer = self.event_pointer("house", "tour.mp4")
        opened = substrate.op_open("house", ptr=pointer)

        self.assertIn("Media evidence is not decoded as text", opened)
        self.assertIn("tour.mp4", opened)
        self.assertIn(str(5 * 1024 * 1024), opened)

    def test_runtime_uses_declared_or_default_twin_store(self):
        flight_path = Path(__file__).parents[2] / "FLIGHT.json"
        previous = os.environ.pop("BRAINSTEM_TWINS_ROOT", None)
        try:
            if flight_path.exists():
                flight = json.loads(flight_path.read_text(encoding="utf-8"))
                self.assertEqual(flight["name"], "universal-twin-substrate")
                self.assertEqual(flight["port"], 7084)
                self.assertEqual(
                    flight["env"]["BRAINSTEM_TWINS_ROOT"],
                    "~/.brainstem-flights/universal-twin-substrate/twins",
                )
                expected = (
                    Path.home()
                    / ".brainstem-flights"
                    / "universal-twin-substrate"
                    / "twins"
                )
            else:
                expected = Path.home() / ".brainstem" / "twins"
            self.assertEqual(substrate._twins_root(), expected)
        finally:
            if previous is not None:
                os.environ["BRAINSTEM_TWINS_ROOT"] = previous

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable")
    def test_substrate_state_is_private_on_disk(self):
        substrate.op_designate("private", parent_class="person")
        twin_dir = self.root / "private"
        database = twin_dir / "substrate.db"
        manifest = twin_dir / "manifest.json"

        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(twin_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_missing_twin_queries_do_not_create_ghost_directories(self):
        result = substrate.op_search("missing", query="anything")

        self.assertIn("No twin 'missing'", result)
        self.assertFalse((self.root / "missing").exists())


if __name__ == "__main__":
    unittest.main()
