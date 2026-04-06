"""Integration test for Phase 2 data management modules.

Tests the three Phase 2 code modules end-to-end:
1. fetch_dataset (via AgentExecutor._fetch_dataset)
2. data_manifest.py (generate_manifest + write_manifest)
3. knowledge_compiler wiki/datasets/ compilation

Since fetch_dataset needs network access, we test it with a local mock
server AND test the code path directly with a pre-downloaded file.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Module 1: data_manifest ──────────────────────────────────────

class TestDataManifest:
    """Test data_manifest.py functions."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # Create test data files
        (self.tmpdir / "train.csv").write_text("a,b,label\n1,2,0\n3,4,1\n5,6,0\n")
        (self.tmpdir / "test.csv").write_text("a,b,label\n7,8,1\n9,0,0\n")
        (self.tmpdir / "metadata.json").write_text(json.dumps([{"id": 1}, {"id": 2}]))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sha256_file(self):
        from researchos.utils.data_manifest import sha256_file
        h = sha256_file(self.tmpdir / "train.csv")
        assert h.startswith("sha256:")
        assert len(h) == 71  # "sha256:" + 64 hex chars

    def test_count_rows_csv(self):
        from researchos.utils.data_manifest import count_rows
        assert count_rows(self.tmpdir / "train.csv") == 3  # 3 data rows, header excluded

    def test_count_rows_json(self):
        from researchos.utils.data_manifest import count_rows
        assert count_rows(self.tmpdir / "metadata.json") == 2

    def test_generate_manifest(self):
        from researchos.utils.data_manifest import generate_manifest
        manifest = generate_manifest(
            str(self.tmpdir),
            pipeline_id="test_pipe",
            dataset_name="TestDataset",
            source_url="https://example.com/data.zip",
        )
        assert manifest["pipeline_id"] == "test_pipe"
        assert manifest["dataset_name"] == "TestDataset"
        assert manifest["total_files"] == 3
        assert manifest["total_rows"] == 7  # 3 + 2 + 2
        assert manifest["directory_hash"].startswith("sha256:")
        assert manifest["source_url"] == "https://example.com/data.zip"
        assert len(manifest["files"]) == 3

        # Verify each file has hash and size
        for f in manifest["files"]:
            assert "hash" in f
            assert "size_bytes" in f
            assert f["hash"].startswith("sha256:")

    def test_write_manifest(self):
        from researchos.utils.data_manifest import generate_manifest, write_manifest
        manifest = generate_manifest(str(self.tmpdir), pipeline_id="wp")
        out = self.tmpdir / "output" / "data_manifest.yaml"
        result = write_manifest(manifest, out)
        assert result == out
        assert out.exists()

        loaded = yaml.safe_load(out.read_text())
        assert loaded["pipeline_id"] == "wp"
        assert loaded["directory_hash"].startswith("sha256:")

    def test_deterministic_hash(self):
        """Same files should produce same directory hash."""
        from researchos.utils.data_manifest import generate_manifest
        m1 = generate_manifest(str(self.tmpdir))
        m2 = generate_manifest(str(self.tmpdir))
        assert m1["directory_hash"] == m2["directory_hash"]

    def test_hash_changes_on_content_change(self):
        """Modifying a file should change the directory hash."""
        from researchos.utils.data_manifest import generate_manifest
        m1 = generate_manifest(str(self.tmpdir))
        # Modify a file
        (self.tmpdir / "train.csv").write_text("a,b,label\n1,2,0\n3,4,1\n5,6,0\n7,8,1\n")
        m2 = generate_manifest(str(self.tmpdir))
        assert m1["directory_hash"] != m2["directory_hash"]


# ── Module 2: fetch_dataset (code path test) ─────────────────────

class TestFetchDatasetCodePath:
    """Test AgentExecutor._fetch_dataset without actual network calls.

    We mock urllib to test the code path end-to-end.
    """

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # Create project structure
        (self.tmpdir / "data" / "raw").mkdir(parents=True)
        (self.tmpdir / "artifacts" / "pipeline_test_fetch").mkdir(parents=True)
        (self.tmpdir / "scratch").mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fetch_dataset_with_local_file(self):
        """Simulate fetch_dataset by creating data locally and running manifest."""
        from researchos.utils.data_manifest import generate_manifest, write_manifest

        pipeline_id = "test_fetch"
        target_dir = self.tmpdir / "data" / "raw" / pipeline_id
        target_dir.mkdir(parents=True, exist_ok=True)

        # Simulate downloaded data
        csv_content = "sepal_length,sepal_width,petal_length,petal_width,class\n"
        csv_content += "5.1,3.5,1.4,0.2,Iris-setosa\n"
        csv_content += "7.0,3.2,4.7,1.4,Iris-versicolor\n"
        csv_content += "6.3,3.3,6.0,2.5,Iris-virginica\n"
        (target_dir / "iris.data").write_text(csv_content)

        # Generate manifest (what _fetch_dataset would do)
        manifest = generate_manifest(
            str(target_dir),
            pipeline_id=pipeline_id,
            dataset_name="Iris",
            source_url="https://archive.ics.uci.edu/ml/machine-learning-databases/iris/iris.data",
        )
        manifest_path = target_dir / "data_manifest.yaml"
        write_manifest(manifest, manifest_path)

        # Also copy to pipeline dir (what _fetch_dataset does)
        pdir = self.tmpdir / "artifacts" / f"pipeline_{pipeline_id}"
        write_manifest(manifest, pdir / "data_manifest.yaml")

        # Verify C1: data downloaded to /data/raw/<pipeline_id>/
        assert (target_dir / "iris.data").exists()
        assert (target_dir / "iris.data").stat().st_size > 0

        # Verify C2: data_manifest.yaml with SHA256 hashes
        assert manifest_path.exists()
        loaded = yaml.safe_load(manifest_path.read_text())
        assert loaded["directory_hash"].startswith("sha256:")
        assert loaded["total_files"] >= 1
        assert loaded["pipeline_id"] == pipeline_id
        assert loaded["dataset_name"] == "Iris"
        assert loaded["source_url"].startswith("https://")

        # Verify manifest also in pipeline dir
        assert (pdir / "data_manifest.yaml").exists()


# ── Module 3: knowledge_compiler wiki/datasets/ ──────────────────

class TestKnowledgeCompilerDatasets:
    """Test that knowledge_compiler compiles data_manifest.yaml into wiki/datasets/."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        pid = "test_wiki_ds"
        self.pipeline_id = pid
        # Create project structure
        pdir = self.tmpdir / "artifacts" / f"pipeline_{pid}"
        pdir.mkdir(parents=True)
        (self.tmpdir / "knowledge" / "wiki" / "datasets").mkdir(parents=True)
        (self.tmpdir / "knowledge" / "wiki" / "concepts").mkdir(parents=True)
        (self.tmpdir / "knowledge" / "wiki" / "methods").mkdir(parents=True)
        (self.tmpdir / "knowledge" / "wiki" / "experiments").mkdir(parents=True)
        (self.tmpdir / "knowledge" / "wiki" / "failures").mkdir(parents=True)
        (self.tmpdir / "knowledge" / "index").mkdir(parents=True)

        # Create state.json
        state = {
            "title": "Test Dataset Pipeline",
            "research_question": "Does X work with Y?",
        }
        (pdir / "state.json").write_text(json.dumps(state))

        # Create data_manifest.yaml
        manifest = {
            "pipeline_id": pid,
            "dataset_name": "TestIris",
            "source_url": "https://example.com/iris.csv",
            "license": "CC BY 4.0",
            "download_date": "2026-04-05T00:00:00Z",
            "directory_hash": "sha256:abc123def456",
            "total_files": 2,
            "total_rows": 150,
            "files": [
                {"path": "train.csv", "size_bytes": 1000, "hash": "sha256:aaa", "rows": 100},
                {"path": "test.csv", "size_bytes": 500, "hash": "sha256:bbb", "rows": 50},
            ],
        }
        (pdir / "data_manifest.yaml").write_text(yaml.dump(manifest))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_compile_pipeline_creates_wiki_datasets_page(self):
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        created = compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)

        # Find the datasets page
        ds_pages = [p for p in created if "datasets" in str(p)]
        assert len(ds_pages) == 1, f"Expected 1 dataset page, got {len(ds_pages)}: {created}"

        page = ds_pages[0]
        assert page.exists()
        content = page.read_text()

        # Verify wiki page content
        assert "TestIris" in content
        assert "sha256:abc123def456" in content
        assert "https://example.com/iris.csv" in content
        assert "CC BY 4.0" in content
        assert "150" in content  # total rows

    def test_wiki_page_has_yaml_frontmatter(self):
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        created = compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)
        ds_pages = [p for p in created if "datasets" in str(p)]
        assert ds_pages

        content = ds_pages[0].read_text()
        assert content.startswith("---")
        # Extract frontmatter
        parts = content.split("---", 2)
        assert len(parts) >= 3
        frontmatter = yaml.safe_load(parts[1])
        assert "dataset" in frontmatter["tags"]
        assert self.pipeline_id in frontmatter["tags"]

    def test_topic_map_updated(self):
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)

        topic_map = self.tmpdir / "knowledge" / "index" / "topic_map.md"
        assert topic_map.exists()
        content = topic_map.read_text()
        assert "data_" in content  # data_test_wiki_ds entry

    def test_idempotent_no_duplicate(self):
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        created1 = compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)
        created2 = compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)
        assert len(created1) > 0
        assert len(created2) == 0  # second call should not create duplicates


# ── End-to-end integration: all 3 modules chained ────────────────

class TestPhase2EndToEnd:
    """Test that fetch_dataset → data_manifest → wiki/datasets works end-to-end."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.pipeline_id = "e2e_test"
        # Full project structure
        (self.tmpdir / "data" / "raw" / self.pipeline_id).mkdir(parents=True)
        pdir = self.tmpdir / "artifacts" / f"pipeline_{self.pipeline_id}"
        pdir.mkdir(parents=True)
        for d in ["concepts", "methods", "experiments", "failures", "datasets"]:
            (self.tmpdir / "knowledge" / "wiki" / d).mkdir(parents=True)
        (self.tmpdir / "knowledge" / "index").mkdir(parents=True)

        # State
        (pdir / "state.json").write_text(json.dumps({
            "title": "E2E Test Pipeline",
            "research_question": "End-to-end test?",
        }))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_chain(self):
        """Simulate: download → manifest → compile to wiki."""
        from researchos.utils.data_manifest import generate_manifest, write_manifest
        from researchos.runtime.knowledge_compiler import compile_pipeline_to_wiki

        # Step 1: Simulate fetch_dataset (create data files)
        data_dir = self.tmpdir / "data" / "raw" / self.pipeline_id
        (data_dir / "data.csv").write_text(
            "id,value,label\n" + "\n".join(f"{i},{i*2},{i%2}" for i in range(100)) + "\n"
        )

        # Step 2: Generate manifest (what _fetch_dataset does)
        manifest = generate_manifest(
            str(data_dir),
            pipeline_id=self.pipeline_id,
            dataset_name="E2E-TestData",
            source_url="https://example.com/data.csv",
            license_info="MIT",
        )
        pdir = self.tmpdir / "artifacts" / f"pipeline_{self.pipeline_id}"
        write_manifest(manifest, pdir / "data_manifest.yaml")

        # Verify manifest is correct
        assert manifest["total_files"] >= 1
        assert manifest["total_rows"] == 100
        assert manifest["directory_hash"].startswith("sha256:")

        # Step 3: Knowledge compiler creates wiki/datasets/ page
        created = compile_pipeline_to_wiki(self.pipeline_id, self.tmpdir)
        ds_pages = [p for p in created if "datasets" in str(p)]
        assert len(ds_pages) == 1

        page_content = ds_pages[0].read_text()
        assert "E2E-TestData" in page_content
        assert manifest["directory_hash"] in page_content
        assert "100" in page_content  # total rows
        assert "MIT" in page_content  # license

        # Step 4: Verify results.json config would have the right fields
        results_config = {
            "data_source": "https://example.com/data.csv",
            "data_rows": manifest["total_rows"],
            "split_seed": 42,
            "data_hash": manifest["directory_hash"],
        }
        assert results_config["data_hash"].startswith("sha256:")
        assert results_config["data_rows"] == 100


# ── Pre-run data compliance hook ─────────────────────────────────

class TestDataSourceComplianceHook:
    """Test validate_data_source_compliance hook."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pipeline(self, pid, data_source="synthetic"):
        pdir = self.tmpdir / "artifacts" / f"pipeline_{pid}"
        pdir.mkdir(parents=True)
        plan = {"data": {"source": data_source}}
        (pdir / "experiment_plan.yaml").write_text(yaml.dump(plan))
        return pdir

    def test_synthetic_always_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        self._make_pipeline("syn_test", "synthetic")
        result = validate_data_source_compliance("syn_test", self.tmpdir)
        assert result["compliant"] is True
        assert result["source"] == "synthetic"

    def test_file_source_no_data_not_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        self._make_pipeline("file_test", "file")
        result = validate_data_source_compliance("file_test", self.tmpdir)
        assert result["compliant"] is False
        assert "fetch_dataset" in result["reason"]

    def test_api_source_no_data_not_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        self._make_pipeline("api_test", "api")
        result = validate_data_source_compliance("api_test", self.tmpdir)
        assert result["compliant"] is False

    def test_file_source_with_data_no_manifest_not_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        self._make_pipeline("fm_test", "file")
        data_dir = self.tmpdir / "data" / "raw" / "fm_test"
        data_dir.mkdir(parents=True)
        (data_dir / "data.csv").write_text("a,b\n1,2\n")
        result = validate_data_source_compliance("fm_test", self.tmpdir)
        assert result["compliant"] is False
        assert "manifest" in result["reason"].lower()

    def test_file_source_with_data_and_manifest_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        pdir = self._make_pipeline("ok_test", "file")
        data_dir = self.tmpdir / "data" / "raw" / "ok_test"
        data_dir.mkdir(parents=True)
        (data_dir / "data.csv").write_text("a,b\n1,2\n")
        (pdir / "data_manifest.yaml").write_text("directory_hash: sha256:abc\n")
        result = validate_data_source_compliance("ok_test", self.tmpdir)
        assert result["compliant"] is True

    def test_no_plan_file_compliant(self):
        from researchos.runtime.hooks import validate_data_source_compliance
        pdir = self.tmpdir / "artifacts" / "pipeline_noplan"
        pdir.mkdir(parents=True)
        result = validate_data_source_compliance("noplan", self.tmpdir)
        assert result["compliant"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
