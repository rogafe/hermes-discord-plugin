from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize("label", ["**Publié**", "__Publié__"])
def test_table_label_is_prepared_before_adapter_adds_bold(plugin, label):
    prepare = importlib.import_module(f"{plugin.__name__}.markdown").prepare_adapter_markdown
    text = f"| Statut | Candidats |\n| --- | --- |\n| {label} | **A, B** |"
    assert prepare(text) == "| Statut | Candidats |\n| --- | --- |\n| Publié | **A, B** |"


@pytest.mark.parametrize("label", ["***Publié***", "`**Publié**`", r"\*\*Publié\*\*"])
def test_table_label_literal_or_triple_emphasis_is_preserved(plugin, label):
    prepare = importlib.import_module(f"{plugin.__name__}.markdown").prepare_adapter_markdown
    text = f"| Statut | Candidats |\n| --- | --- |\n| {label} | A |"
    assert prepare(text) == text


@pytest.mark.parametrize("fence", ["```markdown", "~~~~markdown"])
def test_fenced_tables_and_prose_are_preserved(plugin, fence):
    prepare = importlib.import_module(f"{plugin.__name__}.markdown").prepare_adapter_markdown
    text = f"Prose **Publié**\n{fence}\n| Statut | Candidats |\n| --- | --- |\n| **Publié** | A |\n{fence.rstrip('markdown')}\n"
    assert prepare(text) == text


def test_label_column_and_embedded_pipes(plugin):
    prepare = importlib.import_module(f"{plugin.__name__}.markdown").prepare_adapter_markdown
    text = "| Budget | Confort |\n| --- | --- |\n| **TOTAL** | `a|b` | x\\|y |\n"
    assert prepare(text) == "| Budget | Confort |\n| --- | --- |\n| TOTAL | `a|b` | x\\|y |\n"


def test_not_a_table_is_untouched(plugin):
    prepare = importlib.import_module(f"{plugin.__name__}.markdown").prepare_adapter_markdown
    text = "**A** | **B**\nSome prose\n**C** | **D**"
    assert prepare(text) == text
