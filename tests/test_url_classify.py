"""URL classifier — deterministic pre-filter for BASELINE_ANALYZE.

The classifier is the source of truth for well-known hosts (badges,
DOIs, dataset registries). LLM mismatches against high-confidence
verdicts BLOCK the pipeline, so test coverage of the host/extension
rules is load-bearing.
"""

from __future__ import annotations

import pytest

from researchos.utils.url_classify import classify_many, classify_url


# ── Decorative badges ────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://img.shields.io/badge/license-MIT-blue.svg",
    "https://shields.io/badge/build-passing-green",
    "https://badge.fury.io/py/numpy.svg",
    "https://api.codacy.com/project/badge/Grade/abc123",
    "https://travis-ci.com/owner/repo.svg?branch=main",
])
def test_badges_are_high_conf_no_download(url: str):
    c = classify_url(url)
    assert c.kind == "decorative_badge"
    assert c.should_download is False
    assert c.confidence == "high"


def test_github_badge_path_detected(tmp_path):
    c = classify_url("https://github.com/owner/repo/workflows/CI/badge.svg")
    assert c.kind == "decorative_badge"
    assert c.should_download is False


def test_image_extension_overrides_unknown_host():
    c = classify_url("https://random.example.com/logo.png")
    assert c.kind == "decorative_badge"
    assert c.should_download is False
    assert c.confidence == "high"


# ── DOI / citation ───────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://doi.org/10.5281/zenodo.12345",
    "https://dx.doi.org/10.1109/ICSE.2020.00123",
    "https://semanticscholar.org/paper/abc",
])
def test_doi_hosts_never_download(url: str):
    c = classify_url(url)
    assert c.kind == "doi_citation"
    assert c.should_download is False
    assert c.confidence == "high"


# ── Documentation ────────────────────────────────────────────────


def test_readthedocs_classified_as_doc():
    c = classify_url("https://numpy.readthedocs.io/en/latest/")
    assert c.kind == "documentation"
    assert c.should_download is False


def test_pdf_extension_is_documentation():
    c = classify_url("https://arxiv.org/pdf/2401.01234.pdf")
    assert c.kind == "documentation"
    assert c.should_download is False


# ── Real data hosts ──────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://zenodo.org/record/12345/files/data.zip",
    "https://figshare.com/articles/dataset/abc/12345",
    "https://archive.ics.uci.edu/dataset/iris",
    "https://kaggle.com/datasets/owner/data",
    "https://physionet.org/content/mimic-iv/2.0/",
])
def test_known_data_hosts_should_download(url: str):
    c = classify_url(url)
    assert c.kind == "primary_dataset"
    assert c.should_download is True
    assert c.confidence == "high"


def test_huggingface_datasets_path_is_dataset():
    c = classify_url("https://huggingface.co/datasets/squad/resolve/main/data.csv")
    assert c.kind == "primary_dataset"
    assert c.should_download is True


def test_huggingface_root_path_is_model():
    c = classify_url("https://huggingface.co/bert-base-uncased")
    assert c.kind == "model_weights"
    assert c.should_download is True


# ── Model weights ────────────────────────────────────────────────


@pytest.mark.parametrize("ext", [".pt", ".pth", ".ckpt", ".safetensors", ".onnx"])
def test_model_extensions(ext: str):
    c = classify_url(f"https://example.com/weights{ext}")
    assert c.kind == "model_weights"
    assert c.should_download is True


# ── Generic data extension on unknown host ───────────────────────


@pytest.mark.parametrize("ext", [".csv", ".parquet", ".h5", ".npy", ".tar.gz", ".zip"])
def test_data_extension_on_unknown_host_is_medium(ext: str):
    c = classify_url(f"https://example.com/file{ext}")
    assert c.kind == "primary_dataset"
    assert c.should_download is True
    assert c.confidence == "medium"


# ── Unknown URL → low confidence, LLM must decide ────────────────


def test_unknown_returns_low_confidence():
    c = classify_url("https://example.com/something")
    assert c.kind == "other"
    assert c.confidence == "low"
    assert c.should_download is False


def test_empty_url_safely_handled():
    c = classify_url("")
    assert c.kind == "other"
    assert c.should_download is False


# ── classify_many preserves order ────────────────────────────────


def test_classify_many_preserves_order():
    urls = [
        "https://img.shields.io/badge/x.svg",
        "https://zenodo.org/record/1/files/a.zip",
        "https://example.com/weird",
    ]
    out = classify_many(urls)
    assert [u for u, _ in out] == urls
    assert out[0][1].kind == "decorative_badge"
    assert out[1][1].kind == "primary_dataset"
    assert out[2][1].kind == "other"
