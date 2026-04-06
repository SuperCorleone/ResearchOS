"""LaTeX compiler: converts paper markdown draft to .tex and compiles to PDF.

Generates a self-contained LaTeX document with proper academic formatting,
bibliography, and figure inclusion.
"""

from __future__ import annotations

import logging
import re
import subprocess
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def md_to_latex(md_content: str, title: str, pipeline_id: str) -> str:
    """Convert a markdown paper draft to LaTeX source.

    Uses a robust approach: extracts section structure and renders text
    as plain paragraphs, avoiding fragile itemize/table conversion that
    can produce uncompilable LaTeX.
    """
    lines = md_content.splitlines()
    latex_body = []
    in_code_block = False

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                latex_body.append("\\end{verbatim}")
                in_code_block = False
            else:
                latex_body.append("\\begin{verbatim}")
                in_code_block = True
            continue
        if in_code_block:
            latex_body.append(line)
            continue

        # Headers → sections
        if line.startswith("# ") and not line.startswith("## "):
            continue  # Title handled by \maketitle
        elif line.startswith("## "):
            sec = re.sub(r"^\d+\.?\s*", "", line[3:].strip())
            latex_body.append(f"\n\\section{{{_tex_safe(sec)}}}")
            continue
        elif line.startswith("### "):
            subsec = re.sub(r"^\d+\.?\d*\s*", "", line[4:].strip())
            latex_body.append(f"\n\\subsection{{{_tex_safe(subsec)}}}")
            continue
        elif line.startswith("#### "):
            latex_body.append(f"\n\\subsubsection{{{_tex_safe(line[5:].strip())}}}")
            continue

        # Skip markdown table separator lines
        stripped = line.strip()
        if stripped.startswith("|") and set(stripped.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            continue

        # Bullet items → simple dash paragraphs (avoid fragile \itemize)
        if stripped.startswith("- ") or stripped.startswith("* "):
            item_text = _tex_safe(stripped[2:])
            latex_body.append(f"\\noindent -- {item_text}\\\\")
            continue

        # Table rows → plain text
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [_tex_safe(c.strip()) for c in stripped.strip("|").split("|")]
            latex_body.append(" | ".join(cells) + "\\\\")
            continue

        # Bold / italic
        line = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", line)
        line = re.sub(r"\*(.+?)\*", r"\\textit{\1}", line)

        # Regular text
        if stripped:
            latex_body.append(_tex_safe(line))
        else:
            latex_body.append("")

    body = "\n".join(latex_body)

    # Extract abstract if present
    abstract = ""
    abstract_match = re.search(
        r"\\section\{Abstract\}(.*?)\\section\{",
        body,
        re.DOTALL,
    )
    if abstract_match:
        abstract = abstract_match.group(1).strip()
        # Remove abstract section from body
        body = body.replace(f"\\section{{Abstract}}\n{abstract_match.group(1)}", "")

    # Build references
    references = _extract_references(md_content)

    return _LATEX_TEMPLATE.format(
        title=_escape_latex(title),
        abstract=abstract or "No abstract provided.",
        body=body,
        references=references,
    )


def _tex_safe(text: str) -> str:
    """Escape special LaTeX characters in plain text."""
    for char in ["\\", "&", "%", "$", "#", "_", "{", "}"]:
        text = text.replace(char, f"\\{char}")
    text = text.replace("~", "\\textasciitilde{}")
    text = text.replace("^", "\\textasciicircum{}")
    return text


def _escape_latex(text: str) -> str:
    """Alias for backwards compatibility."""
    return _tex_safe(text)


def _extract_references(md_content: str) -> str:
    """Extract references from markdown and format as bibitem entries."""
    ref_section = re.search(r"##\s*References?(.*?)($|\n##)", md_content, re.DOTALL | re.IGNORECASE)
    if not ref_section:
        return "\\bibitem{placeholder} No references provided."

    refs = []
    for i, line in enumerate(ref_section.group(1).strip().splitlines()):
        line = line.strip().lstrip("-").lstrip("*").lstrip("0123456789.").strip()
        if line:
            refs.append(f"\\bibitem{{ref{i+1}}} {_escape_latex(line)}")

    return "\n".join(refs) if refs else "\\bibitem{placeholder} No references provided."


def compile_latex(tex_path: Path) -> Path | None:
    """Compile .tex to .pdf using pdflatex. Returns PDF path or None."""
    if not shutil.which("pdflatex"):
        logger.warning("pdflatex not found — install TeX Live or MacTeX to compile PDFs")
        return None

    tex_dir = tex_path.parent
    try:
        # Run twice for references (nonstopmode pushes through errors)
        for _ in range(2):
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", tex_path.name],
                cwd=str(tex_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
        pdf_path = tex_path.with_suffix(".pdf")
        if pdf_path.exists():
            logger.info("PDF compiled: %s", pdf_path)
            return pdf_path
        else:
            logger.error("pdflatex failed:\n%s", result.stdout[-2000:])
            return None
    except subprocess.TimeoutExpired:
        logger.error("pdflatex timed out")
        return None
    except Exception as e:
        logger.error("pdflatex error: %s", e)
        return None


def copy_figures_to_paper_dir(pipeline_id: str, paper_dir: Path, project_root: Path) -> list[str]:
    """Copy any generated figures from artifacts/paper to the paper output dir."""
    sources = [
        project_root / "artifacts" / f"pipeline_{pipeline_id}",
        project_root / "paper",  # legacy flat dir
    ]
    copied = []
    for src_dir in sources:
        if not src_dir.exists():
            continue
        for fig in src_dir.glob("*.png"):
            dest = paper_dir / fig.name
            if not dest.exists():
                shutil.copy2(fig, dest)
                copied.append(fig.name)
        for fig in src_dir.glob("*.pdf"):
            if fig.name.endswith("_v1.pdf"):
                continue  # Skip compiled papers
            dest = paper_dir / fig.name
            if not dest.exists():
                shutil.copy2(fig, dest)
                copied.append(fig.name)
    return copied


_LATEX_TEMPLATE = r"""\documentclass[11pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{hyperref}}
\usepackage{{listings}}
\usepackage{{booktabs}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{natbib}}

\lstset{{
  basicstyle=\ttfamily\small,
  breaklines=true,
  frame=single,
  numbers=left,
  numberstyle=\tiny,
}}

\title{{{title}}}
\author{{ResearchOS Autonomous Research Pipeline}}
\date{{\today}}

\begin{{document}}
\maketitle

\begin{{abstract}}
{abstract}
\end{{abstract}}

{body}

\begin{{thebibliography}}{{99}}
{references}
\end{{thebibliography}}

\end{{document}}
"""
