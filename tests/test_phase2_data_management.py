"""Phase 2 Data Management Tests

Success criteria for each component:

1. data_manifest.py:
   - PASS: Computes SHA256 hashes for files deterministically
   - PASS: Counts CSV/JSON rows correctly
   - PASS: Generates directory-level hash from file hashes
   - PASS: Writes valid YAML manifest
   - FAIL: Wrong hash, wrong row count, crash on empty dir

2. fetch_dataset tool:
   - PASS: Tool registered in _TOOLS (5th tool)
   - PASS: _fetch_dataset method exists on AgentExecutor
   - PASS: Downloads file to /data/raw/<pipeline_id>/
   - PASS: Auto-generates data_manifest.yaml after download
   - PASS: Copies manifest to pipeline artifact dir
   - FAIL: Missing tool, crash on bad URL, no manifest generated

3. /data/ directory structure:
   - PASS: DATA_RAW_DIR and DATA_PROCESSED_DIR exist in paths.py
   - PASS: Physical directories exist
   - FAIL: Missing constants or directories

4. Agent prompts:
   - PASS: coder.md mentions fetch_dataset, clean_data.py, data_manifest
   - PASS: experiment_runner.md mentions data_hash, data_manifest.yaml
   - FAIL: Missing keywords

5. knowledge_compiler:
   - PASS: Extracts data_manifest.yaml to wiki/datasets/ page
   - PASS: Wiki page contains dataset_name, directory_hash, source_url
   - FAIL: No wiki page created, missing fields

6. results.json schema hook:
   - PASS: Valid results with data provenance fields pass
   - PASS: Invalid results still blocked
   - FAIL: Schema too strict (blocks valid data) or too loose
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════
# 1. data_manifest.py
# ═══════════════════════════════════════════════════════════════════


class TestDataManifest:

    def test_sha256_deterministic(self, tmp_path):
        """Same file content → same hash every time."""
        from researchos.utils.data_manifest import sha256_file
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = sha256_file(f)
        h2 = sha256_file(f)
        assert h1 == h2
        assert h1.startswith("sha256:")
        # Verify against known hash
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert h1 == f"sha256:{expected}"

    def test_csv_row_count(self, tmp_path):
        """CSV with header + 3 rows → count_rows returns 3."""
        from researchos.utils.data_manifest import count_rows
        f = tmp_path / "data.csv"
        f.write_text("name,value\nA,1\nB,2\nC,3\n")
        assert count_rows(f) == 3

    def test_json_array_count(self, tmp_path):
        """JSON array with 5 items → count_rows returns 5."""
        from researchos.utils.data_manifest import count_rows
        f = tmp_path / "data.json"
        f.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5}]))
        assert count_rows(f) == 5

    def test_json_object_no_count(self, tmp_path):
        """JSON object (not array) → count_rows returns None."""
        from researchos.utils.data_manifest import count_rows
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"key": "value"}))
        assert count_rows(f) is None

    def test_non_data_file_no_count(self, tmp_path):
        """Non-data file → count_rows returns None."""
        from researchos.utils.data_manifest import count_rows
        f = tmp_path / "readme.md"
        f.write_text("# Hello")
        assert count_rows(f) is None

    def test_generate_manifest_basic(self, tmp_path):
        """Manifest includes all files with hashes and metadata."""
        from researchos.utils.data_manifest import generate_manifest
        (tmp_path / "train.csv").write_text("x,y\n1,2\n3,4\n")
        (tmp_path / "test.csv").write_text("x,y\n5,6\n")

        m = generate_manifest(str(tmp_path), pipeline_id="test_pipe")
        assert m["pipeline_id"] == "test_pipe"
        assert m["total_files"] == 2
        assert m["total_rows"] == 3  # 2 + 1
        assert m["directory_hash"].startswith("sha256:")
        assert len(m["files"]) == 2
        for f in m["files"]:
            assert "hash" in f
            assert "size_bytes" in f
            assert f["hash"].startswith("sha256:")

    def test_generate_manifest_empty_dir(self, tmp_path):
        """Empty directory → manifest with 0 files, no crash."""
        from researchos.utils.data_manifest import generate_manifest
        empty = tmp_path / "empty"
        empty.mkdir()
        m = generate_manifest(str(empty))
        assert m["total_files"] == 0
        assert m["files"] == []

    def test_generate_manifest_nonexistent_dir(self):
        """Nonexistent directory → error dict, no crash."""
        from researchos.utils.data_manifest import generate_manifest
        m = generate_manifest("/nonexistent/path")
        assert "error" in m

    def test_directory_hash_deterministic(self, tmp_path):
        """Same files → same directory hash."""
        from researchos.utils.data_manifest import generate_manifest
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")
        m1 = generate_manifest(str(tmp_path))
        m2 = generate_manifest(str(tmp_path))
        assert m1["directory_hash"] == m2["directory_hash"]

    def test_directory_hash_changes_on_content_change(self, tmp_path):
        """Different content → different directory hash."""
        from researchos.utils.data_manifest import generate_manifest
        (tmp_path / "a.txt").write_text("version1")
        m1 = generate_manifest(str(tmp_path))
        (tmp_path / "a.txt").write_text("version2")
        m2 = generate_manifest(str(tmp_path))
        assert m1["directory_hash"] != m2["directory_hash"]

    def test_write_manifest_yaml(self, tmp_path):
        """write_manifest creates valid YAML file."""
        from researchos.utils.data_manifest import generate_manifest, write_manifest
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")
        m = generate_manifest(str(tmp_path), pipeline_id="p1")
        out = tmp_path / "manifest.yaml"
        write_manifest(m, out)
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["pipeline_id"] == "p1"
        assert loaded["total_files"] == 1

    def test_script_hash_tracking(self, tmp_path):
        """Manifest tracks cleaning_script hash if provided."""
        from researchos.utils.data_manifest import generate_manifest
        (tmp_path / "data.csv").write_text("x\n1\n")
        script = tmp_path / "clean.py"
        script.write_text("import pandas as pd\n")
        m = generate_manifest(str(tmp_path), cleaning_script=str(script))
        assert "cleaning_script" in m
        assert "cleaning_script_hash" in m
        assert m["cleaning_script_hash"].startswith("sha256:")


# ═══════════════════════════════════════════════════════════════════
# 2. fetch_dataset tool registration
# ═══════════════════════════════════════════════════════════════════


class TestFetchDatasetTool:

    def test_tool_registered(self):
        """fetch_dataset must be the 5th tool in _TOOLS."""
        from researchos.runtime.agent_executor import _TOOLS
        names = [t["function"]["name"] for t in _TOOLS]
        assert "fetch_dataset" in names
        assert len(names) == 5

    def test_tool_schema_has_url(self):
        """fetch_dataset must require 'url' parameter."""
        from researchos.runtime.agent_executor import _TOOLS
        fetch = [t for t in _TOOLS if t["function"]["name"] == "fetch_dataset"][0]
        params = fetch["function"]["parameters"]
        assert "url" in params["properties"]
        assert "url" in params["required"]

    def test_tool_has_dataset_name(self):
        """fetch_dataset should accept optional dataset_name."""
        from researchos.runtime.agent_executor import _TOOLS
        fetch = [t for t in _TOOLS if t["function"]["name"] == "fetch_dataset"][0]
        assert "dataset_name" in fetch["function"]["parameters"]["properties"]

    def test_fetch_method_exists(self):
        """AgentExecutor must have _fetch_dataset method."""
        from researchos.runtime.agent_executor import AgentExecutor
        assert hasattr(AgentExecutor, "_fetch_dataset")

    def test_handler_routes_to_fetch(self):
        """_handle_tool_call must route 'fetch_dataset' to _fetch_dataset."""
        import inspect
        from researchos.runtime.agent_executor import AgentExecutor
        src = inspect.getsource(AgentExecutor._handle_tool_call)
        assert 'fetch_dataset' in src
        assert '_fetch_dataset' in src


# ═══════════════════════════════════════════════════════════════════
# 3. /data/ directory structure
# ═══════════════════════════════════════════════════════════════════


class TestDataDirectories:

    def test_paths_constants_exist(self):
        """DATA_RAW_DIR and DATA_PROCESSED_DIR must be in paths.py."""
        from researchos.utils.paths import DATA_RAW_DIR, DATA_PROCESSED_DIR, DATA_DIR
        assert DATA_DIR.name == "data"
        assert DATA_RAW_DIR.name == "raw"
        assert DATA_PROCESSED_DIR.name == "processed"

    def test_physical_dirs_exist(self):
        """Physical /data/raw/ and /data/processed/ must exist."""
        from researchos.utils.paths import DATA_RAW_DIR, DATA_PROCESSED_DIR
        assert DATA_RAW_DIR.exists(), f"{DATA_RAW_DIR} does not exist"
        assert DATA_PROCESSED_DIR.exists(), f"{DATA_PROCESSED_DIR} does not exist"

    def test_data_dir_in_project_root(self):
        """data/ must be at project root level."""
        from researchos.utils.paths import DATA_DIR, PROJECT_ROOT
        assert DATA_DIR.parent == PROJECT_ROOT


# ═══════════════════════════════════════════════════════════════════
# 4. Agent prompt updates
# ═══════════════════════════════════════════════════════════════════


class TestAgentPromptsPhase2:

    AGENTS_DIR = PROJECT_ROOT / ".openharness" / "plugins" / "researchos" / "agents"

    def _read(self, name):
        return (self.AGENTS_DIR / f"{name}.md").read_text()

    def test_coder_mentions_fetch_dataset(self):
        content = self._read("coder")
        assert "fetch_dataset" in content

    def test_coder_mentions_clean_data_script(self):
        content = self._read("coder")
        assert "clean_data.py" in content

    def test_coder_mentions_data_manifest(self):
        content = self._read("coder")
        assert "data_manifest" in content

    def test_coder_mentions_data_raw_processed(self):
        content = self._read("coder")
        assert "/data/raw/" in content
        assert "/data/processed/" in content

    def test_runner_mentions_data_hash(self):
        content = self._read("experiment_runner")
        assert "data_hash" in content

    def test_runner_mentions_manifest(self):
        content = self._read("experiment_runner")
        assert "data_manifest.yaml" in content

    def test_runner_mentions_fetch_dataset(self):
        content = self._read("experiment_runner")
        assert "fetch_dataset" in content


# ═══════════════════════════════════════════════════════════════════
# 5. knowledge_compiler → wiki/datasets/
# ═══════════════════════════════════════════════════════════════════


class TestKnowledgeCompilerDatasets:

    def test_compiles_manifest_to_wiki(self, tmp_path):
        """If data_manifest.yaml exists, wiki/datasets/data_<id>.md is created."""
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        pid = "test_data_pipe"
        # Setup minimal pipeline
        pdir = tmp_path / "artifacts" / f"pipeline_{pid}"
        pdir.mkdir(parents=True)
        wiki = tmp_path / "knowledge" / "wiki" / "datasets"
        (tmp_path / "knowledge" / "wiki").mkdir(parents=True)
        (tmp_path / "knowledge" / "index").mkdir(parents=True)

        # Write state.json
        (pdir / "state.json").write_text(json.dumps({
            "pipeline_id": pid,
            "title": "Test Data Pipeline",
            "research_question": "Does data work?",
        }))

        # Write data_manifest.yaml
        manifest = {
            "dataset_name": "TestDataset",
            "directory_hash": "sha256:abc123",
            "source_url": "https://example.com/data.csv",
            "license": "MIT",
            "total_files": 3,
            "total_rows": 1000,
        }
        (pdir / "data_manifest.yaml").write_text(yaml.dump(manifest))

        pages = compile_pipeline_to_wiki(pid, tmp_path)

        wiki_file = tmp_path / "knowledge" / "wiki" / "datasets" / f"data_{pid}.md"
        assert wiki_file.exists(), "wiki/datasets/ page should be created"
        content = wiki_file.read_text()
        assert "TestDataset" in content
        assert "sha256:abc123" in content
        assert "example.com" in content

    def test_skips_when_no_manifest(self, tmp_path):
        """No manifest → no datasets/ page, no crash."""
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        pid = "no_manifest_pipe"
        pdir = tmp_path / "artifacts" / f"pipeline_{pid}"
        pdir.mkdir(parents=True)
        (tmp_path / "knowledge" / "wiki").mkdir(parents=True)
        (tmp_path / "knowledge" / "index").mkdir(parents=True)
        (pdir / "state.json").write_text(json.dumps({
            "pipeline_id": pid, "title": "X", "research_question": "?",
        }))

        pages = compile_pipeline_to_wiki(pid, tmp_path)
        wiki_file = tmp_path / "knowledge" / "wiki" / "datasets" / f"data_{pid}.md"
        assert not wiki_file.exists()


# ═══════════════════════════════════════════════════════════════════
# 6. results.json with data provenance
# ═══════════════════════════════════════════════════════════════════


class TestResultsWithDataProvenance:

    def test_results_with_data_fields_pass_schema(self):
        """results.json with data_source/data_hash in config should pass."""
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"accuracy": 0.95},
            "config": {
                "lr": 0.01,
                "data_source": "MovieLens-1M",
                "data_hash": "sha256:abc123",
                "data_rows": 100000,
                "split_seed": 42,
            },
            "seed": 42,
        })
        assert result["valid"] is True

    def test_results_without_data_still_valid(self):
        """Backward compat: results without data fields should still pass."""
        from researchos.runtime.hooks import validate_results_schema
        result = validate_results_schema({
            "metrics": {"acc": 0.9},
            "config": {"lr": 0.01},
            "seed": 42,
        })
        assert result["valid"] is True

    def test_artifact_write_with_data_provenance(self):
        """Hook allows results.json with data provenance fields."""
        from researchos.runtime.hooks import validate_artifact_write
        content = json.dumps({
            "metrics": {"f1": 0.88},
            "config": {"data_source": "synthetic", "data_rows": 500, "split_seed": 42},
            "seed": 42,
        })
        result = validate_artifact_write("artifacts/pipeline_x/results.json", content)
        assert result["allowed"] is True


# ═══════════════════════════════════════════════════════════════════
# 7. Integration: manifest from real pipeline
# ═══════════════════════════════════════════════════════════════════


class TestManifestIntegration:

    def test_manifest_on_real_pipeline(self):
        """Generate manifest from existing pipeline artifacts."""
        pdir = PROJECT_ROOT / "artifacts" / "pipeline_uncertainty_calibrated_multi_a"
        if not pdir.exists():
            pytest.skip("Pipeline not available")

        from researchos.utils.data_manifest import generate_manifest
        m = generate_manifest(str(pdir), pipeline_id="uncertainty_calibrated_multi_a")
        assert m["total_files"] > 0
        assert m["directory_hash"].startswith("sha256:")
        assert all(f["hash"].startswith("sha256:") for f in m["files"])

    def test_manifest_roundtrip(self, tmp_path):
        """Generate → write → read → verify integrity."""
        from researchos.utils.data_manifest import generate_manifest, write_manifest
        (tmp_path / "data.csv").write_text("a,b,c\n1,2,3\n4,5,6\n7,8,9\n")
        m = generate_manifest(str(tmp_path), pipeline_id="roundtrip", dataset_name="TestCSV")
        out = tmp_path / "manifest.yaml"
        write_manifest(m, out)

        loaded = yaml.safe_load(out.read_text())
        assert loaded["dataset_name"] == "TestCSV"
        assert loaded["pipeline_id"] == "roundtrip"
        assert loaded["total_rows"] == 3
        assert loaded["directory_hash"] == m["directory_hash"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
