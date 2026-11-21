import pytest
from agenticmind_node_sdk import NodeError, Permissions
from agenticmind_node_sdk.documents import parse_documents
from agenticmind_node_sdk.testing import MemoryArtifacts, invoke_node
from agenticmind_nodes_data import ALL_NODES
from agenticmind_nodes_data.nodes import document_cleaner, document_loader
from agenticmind_nodes_data.parsers import ParseError, parse_bytes
from agenticmind_nodes_data.testing import make_test_docx, make_test_pdf

PDF = make_test_pdf(["Refund policy\nRefunds within 30 days.", "", "Shipping policy\nShips in 2 days."])


def test_manifests_declare_permissions():
    loader = next(d for d in ALL_NODES if d.manifest.type == "agenticmind.data.document_loader")
    assert loader.manifest.permissions == Permissions(artifacts=True)
    assert loader.manifest.config_schema["properties"]["files"]["x-ui"]["widget"] == "artifacts"


def test_pdf_pages():
    parts = parse_bytes(PDF, "policy.pdf")
    assert [p.page for p in parts] == [1, 2, 3]
    assert "Refunds within 30 days." in parts[0].content
    assert parts[1].content == ""
    assert parts[0].metadata["page_count"] == 3
    whole = parse_bytes(PDF, "policy.pdf", split_pdf_pages=False)
    assert len(whole) == 1 and "Shipping policy" in whole[0].content


def test_pdf_garbage_rejected():
    with pytest.raises(ParseError) as exc:
        parse_bytes(b"%PDF-1.4 not really", "bad.pdf")
    assert exc.value.code == "PARSE_FAILED"


def test_docx_headings_become_markdown():
    data = make_test_docx(
        [("Heading 1", "Leave policy"), ("Normal", "Staff get 25 days."), ("Heading 2", "Sick leave")]
    )
    (part,) = parse_bytes(data, "hr.docx")
    assert part.content == "# Leave policy\n\nStaff get 25 days.\n\n## Sick leave"


def test_html_strips_scripts_and_keeps_headings():
    html = (
        b"<html><head><title>FAQ</title><style>p{}</style></head><body><h1>Help</h1><p>Hi &amp; welcome</p>"
        b"<script>alert(1)</script><ul><li>One</li><li>Two</li></ul></body></html>"
    )
    (part,) = parse_bytes(html, "faq.html")
    assert part.content == "# Help\n\nHi & welcome\n\n- One\n- Two"  # lists keep their structure (ADR-0027)
    assert part.metadata["title"] == "FAQ"


def test_csv_modes():
    data = b"name,role\nAda,engineer\nGrace,admiral\n"
    (whole,) = parse_bytes(data, "staff.csv")
    assert whole.content == "name: Ada\nrole: engineer\n\nname: Grace\nrole: admiral"
    rows = parse_bytes(data, "staff.csv", csv_mode="rows")
    assert [r.id_suffix for r in rows] == [":r1", ":r2"] and rows[1].content == "name: Grace\nrole: admiral"


def test_text_decoding_fallback():
    (part,) = parse_bytes("café\r\nline".encode("cp1252"), "notes.txt")
    assert part.content == "café\nline"


async def test_loader_produces_documents_with_provenance():
    files = MemoryArtifacts(
        {"art_0000000000000001": ("policy.pdf", PDF), "art_0000000000000002": ("a.md", b"# A\ntext")}
    )
    result = await invoke_node(
        document_loader, config={"files": ["art_0000000000000001", "art_0000000000000002"]}, artifacts=files
    )
    docs = parse_documents(result.outputs["documents"])
    assert [d.id for d in docs] == ["art_0000000000000001:p1", "art_0000000000000001:p3", "art_0000000000000002"]
    first = docs[0]
    assert first.source.page == 1 and first.source.filename == "policy.pdf"
    assert first.source.uri == "artifact://art_0000000000000001/policy.pdf"
    assert first.ingestion_version == "agenticmind.data.document_loader@1.0.0"
    assert first.metadata["sha256"] == files.info("art_0000000000000001").sha256


async def test_loader_merges_input_file_ids():
    files = MemoryArtifacts({"art_a": ("a.txt", b"one"), "art_b": ("b.txt", b"two")})
    result = await invoke_node(
        document_loader, config={"files": ["art_a"]}, inputs={"files": ["art_b", "art_a"]}, artifacts=files
    )
    assert [d["id"] for d in result.outputs["documents"]] == ["art_a", "art_b"]


@pytest.mark.parametrize(
    "config,files,code",
    [
        ({}, {}, "NO_FILES"),
        ({"files": ["art_missing"]}, {}, "ARTIFACT_NOT_FOUND"),
        ({"files": ["art_blank"]}, {"art_blank": ("blank.pdf", make_test_pdf([""]))}, "NO_TEXT_EXTRACTED"),
    ],
)
async def test_loader_errors(config, files, code):
    with pytest.raises(NodeError) as exc:
        await invoke_node(document_loader, config=config, artifacts=MemoryArtifacts(files))
    assert exc.value.code == code


async def test_loader_without_store_is_denied():
    with pytest.raises(NodeError) as exc:
        await invoke_node(document_loader, config={"files": ["art_x"]})
    assert exc.value.code == "ARTIFACT_ACCESS_DENIED"


async def test_cleaner():
    docs = [
        {
            "id": "d1",
            "content": "  ﬁnancial   exam-\nple\x07\n\n\n\nnext  ",
            "source": {"uri": "x"},
            "ingestion_version": "v0",
        },
        {"id": "d2", "content": " \n\t ", "source": {"uri": "y"}},
    ]
    result = await invoke_node(document_cleaner, inputs={"documents": docs})
    (cleaned,) = parse_documents(result.outputs["documents"])
    assert cleaned.content == "financial example\n\nnext"
    assert cleaned.metadata["cleaned"] is True
    assert cleaned.ingestion_version == "v0 > agenticmind.data.document_cleaner@1.0.0"
