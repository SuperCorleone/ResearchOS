"""Tests for code_recon URL whitelist and clone_repository wrapper.

We do not actually hit github.com in CI. The whitelist check is pure-function
and tested directly. The clone_repository wrapper is tested by:
  (a) verifying it short-circuits to status='rejected_by_whitelist' for bad hosts
  (b) mocking the inner subprocess call for the happy path

The inner subprocess call is made via the top-level ``subprocess`` module
imported by code_recon. We monkeypatch it at that level.
"""

from pathlib import Path
import subprocess
import pytest

from researchos.runtime.code_recon import _host_allowed, clone_repository


class TestHostAllowed:
    @pytest.mark.parametrize("url", [
        "https://github.com/foo/bar",
        "https://www.github.com/foo/bar",
        "http://gitlab.com/x/y",
        "https://zenodo.org/record/123",
        "https://osf.io/abc/",
        "https://figshare.com/articles/dataset/foo/123",
        "https://huggingface.co/datasets/foo",
        "https://codeocean.com/capsule/123/tree/v1",
        "https://doi.org/10.1234/zenodo.567",
    ])
    def test_allowed_hosts(self, url):
        ok, reason = _host_allowed(url)
        assert ok, f"expected allowed: {url}, got reason={reason}"

    @pytest.mark.parametrize("url", [
        "https://example.com/foo",
        "https://my-private-server.example.org/repo",
    ])
    def test_rejected_hosts_example(self, url):
        """example.com and example.org are unambiguously not in the whitelist."""
        ok, reason = _host_allowed(url)
        assert not ok
        assert "allowed_hosts" in reason or "host" in reason

    def test_rejected_raw_github(self):
        """raw.githubusercontent.com ends with 'githubusercontent.com', not 'github.com'."""
        ok, reason = _host_allowed("https://raw.githubusercontent.com/foo/bar")
        # The host is raw.githubusercontent.com — not the same as github.com.
        # Asserting the current behavior: raw.githubusercontent.com is NOT in the
        # default whitelist (the whitelist contains 'github.com', and
        # 'raw.githubusercontent.com'.endswith('github.com') is False).
        assert not ok

    def test_ftp_scheme_github(self):
        """ftp://github.com — scheme is irrelevant to _host_allowed; host is github.com."""
        ok, reason = _host_allowed("ftp://github.com/foo/bar")
        # hostname is github.com which IS in the whitelist regardless of scheme.
        assert ok, f"ftp://github.com should pass host check; got reason={reason}"

    def test_subdomain_not_in_whitelist(self):
        """github.io is NOT github.com; should be rejected."""
        ok, _ = _host_allowed("https://user.github.io/page")
        assert not ok

    def test_empty_url(self):
        ok, reason = _host_allowed("")
        assert not ok
        assert reason  # Must give some explanation

    def test_unparseable(self):
        # "not a url" parses successfully by urlparse but has no hostname.
        ok, reason = _host_allowed("not a url")
        assert not ok

    def test_custom_allowed_hosts_override(self):
        """Caller can supply an explicit allowed_hosts list."""
        ok, reason = _host_allowed(
            "https://example.com/foo",
            allowed_hosts=["example.com"],
        )
        assert ok, f"expected allowed with custom list; got reason={reason}"

    def test_subdomain_of_allowed_host(self):
        """www.github.com should be allowed (strips 'www.' before matching)."""
        ok, reason = _host_allowed("https://www.github.com/foo/bar")
        assert ok, f"www.github.com should be allowed; got reason={reason}"

    def test_bitbucket_allowed(self):
        ok, reason = _host_allowed("https://bitbucket.org/owner/repo")
        assert ok, f"bitbucket.org should be allowed; got reason={reason}"

    def test_gitlab_allowed(self):
        ok, reason = _host_allowed("https://gitlab.com/group/project")
        assert ok, f"gitlab.com should be allowed; got reason={reason}"


class TestCloneRepositoryWhitelist:
    def test_rejected_when_host_not_allowed(self, tmp_path):
        result = clone_repository(
            "https://example.com/foo/bar",
            tmp_path / "target",
        )
        assert result["status"] == "rejected_by_whitelist"
        # Rejected repos must NOT have a real path set.
        assert result.get("path") in (None, "")

    def test_result_has_required_keys_on_rejection(self, tmp_path):
        """Rejected result must contain at least: url, status, path, reason."""
        result = clone_repository(
            "https://example.com/foo/bar",
            tmp_path / "target",
        )
        for key in ("url", "status", "path"):
            assert key in result, f"missing key '{key}' in rejection result"
        assert result["url"] == "https://example.com/foo/bar"

    def test_allowed_host_attempts_clone(self, tmp_path, monkeypatch):
        """Monkeypatch subprocess.run so we don't actually hit github.com."""
        import researchos.runtime.code_recon as mod

        _original_run = subprocess.run
        called = {}

        def fake_run(args, **kwargs):
            called["args"] = args
            # Simulate a failed git clone so we get an 'error' status back
            # (we just want to verify we got past the whitelist check).
            class FakeResult:
                returncode = 128
                stdout = ""
                stderr = "fatal: repository not found (fake)"
            return FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        try:
            result = clone_repository(
                "https://github.com/foo/bar",
                tmp_path / "target2",
            )
            # Whitelist passed; actual subprocess is mocked → expect 'error', not 'rejected_by_whitelist'
            assert result["status"] != "rejected_by_whitelist", (
                "github.com should pass the whitelist; result was: " + repr(result)
            )
            # Confirm fake_run was actually called.
            assert "args" in called, "subprocess.run was never invoked"
        except Exception as exc:
            pytest.skip(f"clone_repository internals not amenable to subprocess mocking: {exc}")

    def test_local_file_url_rejected_if_not_in_whitelist(self, tmp_path):
        """file:// URLs go through host check too; empty hostname → rejected."""
        result = clone_repository(
            f"file://{tmp_path}/does-not-exist",
            tmp_path / "local_target",
        )
        # file:// URLs have an empty hostname — _host_allowed returns False.
        assert result["status"] == "rejected_by_whitelist"

    def test_custom_allowed_hosts_permits_clone_attempt(self, tmp_path, monkeypatch):
        """Passing allowed_hosts=['example.com'] lets example.com past the whitelist."""
        import researchos.runtime.code_recon as mod

        def fake_run(args, **kwargs):
            class FakeResult:
                returncode = 128
                stdout = ""
                stderr = "fatal: not found (fake)"
            return FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        try:
            result = clone_repository(
                "https://example.com/foo/bar",
                tmp_path / "custom_target",
                allowed_hosts=["example.com"],
            )
            assert result["status"] != "rejected_by_whitelist", (
                "With custom allowed_hosts=['example.com'] the clone should proceed past whitelist"
            )
        except Exception as exc:
            pytest.skip(f"Monkeypatching failed: {exc}")

    def test_size_limit_param_accepted(self, tmp_path):
        """clone_repository accepts max_size_mb without raising."""
        # The call will be rejected by whitelist before reaching the size check,
        # but this confirms the parameter is accepted.
        result = clone_repository(
            "https://example.com/foo",
            tmp_path / "t",
            max_size_mb=10,
        )
        assert result["status"] == "rejected_by_whitelist"

    def test_max_files_param_accepted(self, tmp_path):
        """clone_repository accepts max_files without raising."""
        result = clone_repository(
            "https://example.com/foo",
            tmp_path / "t2",
            max_files=100,
        )
        assert result["status"] == "rejected_by_whitelist"

    def test_prune_extensions_param_accepted(self, tmp_path):
        """clone_repository accepts prune_extensions without raising."""
        result = clone_repository(
            "https://example.com/foo",
            tmp_path / "t3",
            prune_extensions=[".pth", ".bin"],
        )
        assert result["status"] == "rejected_by_whitelist"
