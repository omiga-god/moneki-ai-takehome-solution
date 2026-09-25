"""检索、索引缓存和会话隔离。语料都是临时文件，不写公开题答案。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kbqa import retriever as retriever_module
from kbqa.chunker import chunk_document
from kbqa.index import content_key, load_index
from kbqa.loader import Document, load_knowledge_base
from kbqa.retriever import Retriever
from kbqa.sessions import SessionStore

# conftest 里的 client 会把 Retriever.search 换成假实现。测试在导入时抓住原函数。
_SEARCH = retriever_module.Retriever.search
_TODAY = date(2026, 9, 1)


def _front(doc_id: str, title: str, **extra: str) -> str:
    lines = ["---", "doc_id: %s" % doc_id, "title: %s" % title]
    for key, value in extra.items():
        lines.append("%s: %s" % (key, value))
    lines.append("---")
    return "\n".join(lines) + "\n"


def _write_kb(root: Path, files: dict[str, str | bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")


def _hits(index, query: str, top_k: int = 5):
    result = _SEARCH(Retriever(index, _TODAY), query, top_k=top_k)
    return result.hits


def test_chinese_query_finds_unspaced_document(tmp_path):
    root = tmp_path / "kb"
    _write_kb(
        root,
        {
            "KB-700_先出现.md": _front("KB-700", "先出现") + "本篇只谈员工工牌颜色。\n",
            "KB-701_冷萃说明.md": _front("KB-701", "冷萃说明")
            + "周末冷萃第二杯半价，仅限门店堂食。\n",
            "KB-702_无关.md": _front("KB-702", "无关") + "本篇只谈仓库钥匙编号。\n",
        },
    )
    hits = _hits(_index(root, tmp_path / "index.json"), "冷萃第二杯")
    assert hits, "无空格中文查询应当命中正文"
    assert hits[0].doc_id == "KB-701"
    assert hits[0].score > 0


def test_index_cache_follows_added_edited_and_deleted_files(tmp_path):
    root = tmp_path / "kb"
    cache = tmp_path / "index.json"
    _write_kb(root, {"KB-801.md": _front("KB-801", "甲") + "甲文件正文。\n"})
    first = _index(root, cache)
    assert set(first.docs_meta) == {"KB-801"}

    (root / "KB-801.md").write_text(
        _front("KB-801", "甲") + "甲文件正文已改成另一句话。\n", encoding="utf-8"
    )
    edited = load_index(root, cache, rebuild=False)
    assert "另一句话" in edited.texts["KB-801"]

    _write_kb(root, {"KB-802.md": _front("KB-802", "乙") + "乙文件正文。\n"})
    added = load_index(root, cache, rebuild=False)
    assert set(added.docs_meta) == {"KB-801", "KB-802"}

    (root / "KB-802.md").unlink()
    deleted = load_index(root, cache, rebuild=False)
    assert set(deleted.docs_meta) == {"KB-801"}


def test_content_key_changes_when_file_bytes_change(tmp_path):
    root = tmp_path / "kb"
    _write_kb(root, {"KB-806.md": _front("KB-806", "甲") + "第一版。\n"})
    before = content_key(root)
    (root / "KB-806.md").write_text(_front("KB-806", "甲") + "第二版。\n", encoding="utf-8")
    assert content_key(root) != before


def test_html_index_keeps_visible_text_and_drops_script(tmp_path):
    root = tmp_path / "kb"
    html = (
        "<html><head><title>KB-803 常见问题</title>"
        "<script>var TRACKER_SECRET = 1;</script>"
        "<style>.hidden { display:none }</style></head>"
        "<body><h1>常见问题</h1><p>发票申请请联系门店。</p></body></html>\n"
    )
    _write_kb(root, {"KB-803.html": html})
    docs, _warnings = load_knowledge_base(root)
    text = docs[0].text
    assert "发票申请" in text
    assert "TRACKER_SECRET" not in text
    assert "<p>" not in text


def test_gbk_document_round_trips(tmp_path):
    root = tmp_path / "kb"
    body = (_front("KB-804", "导出") + "周五延长到 23:00。\n").encode("gbk")
    _write_kb(root, {"KB-804.txt": body})
    docs, _warnings = load_knowledge_base(root)
    assert "周五延长到" in docs[0].text
    assert docs[0].warnings


def test_superseded_doc_is_excluded_without_shrinking_top_k(tmp_path):
    root = tmp_path / "kb"
    files = {
        "KB-810.md": _front(
            "KB-810",
            "旧口径",
            status="已废止",
            effective_from="2026-01-01",
            superseded_by="KB-811",
        )
        + "UNIQUEPHRASE 旧口径赠送五十。\n",
        "KB-811.md": _front("KB-811", "新口径", status="现行", effective_from="2026-07-01")
        + "UNIQUEPHRASE 现行口径赠送六十。\n",
    }
    for number in range(1, 7):
        doc_id = "KB-80%d" % number
        files["%s.md" % doc_id] = _front(doc_id, "填充%d" % number) + (
            "填充文档 %d 只包含无关的工牌说明。\n" % number
        )
    _write_kb(root, files)
    hits = _hits(_index(root, tmp_path / "index.json"), "UNIQUEPHRASE", top_k=5)
    assert len(hits) == 5
    doc_ids = {hit.doc_id for hit in hits}
    assert "KB-810" not in doc_ids
    assert "KB-811" in doc_ids


def test_chunker_keeps_the_tail():
    body = ("甲" * 650) + "TAILMARKER"
    document = Document(
        doc_id="KB-805",
        title="长文",
        text=body,
        path=Path("KB-805.md"),
        fmt="md",
    )
    chunks = chunk_document(document)
    assert any("TAILMARKER" in chunk.text for chunk in chunks)


def test_english_alias_document_stays_in_top_k(tmp_path):
    root = tmp_path / "kb"
    files = {
        "KB-900_别名.md": _front("KB-900", "别名")
        + "\n".join(
            [
                "| 数据库写法 | 别名 |",
                "|---|---|",
                "| 冷萃咖啡 | Coldbrew |",
                "",
            ]
        ),
        "KB-901_邮件.txt": (
            "Subject: Coldbrew delivery rejected\n\n"
            "The coldbrew lot was refused. A credit note covers the full value.\n"
        ),
    }
    for number in range(10, 18):
        doc_id = "KB-9%d" % number
        files["%s.md" % doc_id] = _front(doc_id, "名录%d" % number) + (
            "冷萃咖啡供应商赔了联系人登记，第 %d 页只留电话。\n" % number
        )
    _write_kb(root, files)
    hits = _hits(_index(root, tmp_path / "index.json"), "冷萃咖啡那次供应商赔了多少", top_k=5)
    assert "KB-901" in {hit.doc_id for hit in hits}


def test_compensation_query_boosts_credit_note_only(tmp_path):
    root = tmp_path / "kb"
    _write_kb(
        root,
        {
            "KB-901.md": _front("KB-901", "别名")
            + "| 数据库写法 | 别名 |\n| --- | --- |\n| 鳕鱼排 | Cod Fillet |\n",
            "KB-902.txt": "Subject: Cod Fillet\n\nWe will issue a credit note of CNY 120.\n",
            "KB-903.md": _front("KB-903", "名录") + "鳕鱼供应商名录只登记联系人。\n",
        },
    )
    retriever = Retriever(_index(root, tmp_path / "index.json"), _TODAY)
    email = retriever._multiplier("KB-902", _TODAY, None, None, None, compensation=True)
    email_plain = retriever._multiplier("KB-902", _TODAY, None, None, None, compensation=False)
    listing = retriever._multiplier("KB-903", _TODAY, None, None, None, compensation=True)
    listing_plain = retriever._multiplier("KB-903", _TODAY, None, None, None, compensation=False)
    assert email > email_plain
    assert listing == listing_plain


def test_sessions_do_not_share_history():
    store = SessionStore()
    store.append("alpha", {"question": "六月营业额"})
    store.append("beta", {"question": "退款怎么处理"})
    assert [turn["question"] for turn in store.history("alpha")] == ["六月营业额"]
    assert [turn["question"] for turn in store.history("beta")] == ["退款怎么处理"]
    assert store.history("gamma") == []
    store.clear()
    assert store.history("alpha") == []


def _index(root: Path, cache: Path):
    return load_index(root, cache, rebuild=True)
