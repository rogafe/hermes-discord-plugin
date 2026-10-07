from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("### **Carrefour** (Drive Metz Technopole / ex-Cora)",
         "**Carrefour** (Drive Metz Technopole / ex-Cora)"),
        ("## **Intermarché** Express Metz (Saint-Vincent)",
         "**Intermarché** Express Metz (Saint-Vincent)"),
        ("# **Monoprix**", "**Monoprix**"),
        ("## **A** et **B**", "**A** et **B**"),
        ("## __Carrefour__ (Metz)", "__Carrefour__ (Metz)"),
        ("## Enseignes", "**Enseignes**"),
        ("Prix : **gratuit**", "Prix : **gratuit**"),
        ("```markdown\n### **Carrefour** (Metz)\n```",
         "```markdown\n### **Carrefour** (Metz)\n```"),
    ],
)
def test_heading_with_bold_does_not_nest_bold(plugin, source, expected):
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    assert embeds._normalize_embed_markdown(source) == expected
