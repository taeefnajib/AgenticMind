from agenticmind_registry import TrustLevel
from agenticmind_runtime import Code, MappingSecretResolver, Severity, validate_document, validate_project
from conftest import c, d, doc, n


def codes(report):
    return {diag.code for diag in report.diagnostics}


def hello_doc():
    return doc(
        [
            n("name", "manual_input", key="name"),
            n("greet", "text_template", template="Hello {{ text }}"),
            n("out", "output", key="greeting"),
        ],
        [d("e1", "name.value", "greet.text"), d("e2", "greet.text", "out.value")],
    )


def test_valid_graph(registry):
    report = validate_document(hello_doc(), registry)
    assert report.valid, report.diagnostics
    assert report.diagnostics == []


def test_parse_errors_have_codes(registry):
    parsed, report = validate_project("{oops", registry)
    assert parsed is None and codes(report) == {Code.IR_INVALID_JSON}
    parsed, report = validate_project({"ir_version": "1.0"}, registry)
    assert parsed is None and codes(report) == {Code.IR_SCHEMA_VIOLATION}


def test_unknown_type_and_version(registry):
    report = validate_document(
        doc([n("a", "agenticmind.core.nope"), {**n("b", "output"), "version": "9.0.0"}], []), registry
    )
    assert {Code.NODE_UNKNOWN_TYPE, Code.NODE_UNKNOWN_VERSION} <= codes(report)
    unknown = next(x for x in report.diagnostics if x.code == Code.NODE_UNKNOWN_VERSION)
    assert unknown.node_id == "b" and "1.0.0" in unknown.hint


def test_invalid_config(registry):
    report = validate_document(doc([n("t", "text_transform", operation="explode")], []), registry)
    diag = next(x for x in report.diagnostics if x.code == Code.CONFIG_INVALID)
    assert diag.node_id == "t" and diag.path == "config.operation"


def test_type_mismatch_requires_explicit_conversion(registry):
    report = validate_document(
        doc(
            [n("j", "manual_input", value_type="JSON"), n("u", "text_transform")],
            [d("e1", "j.value", "u.text")],
        ),
        registry,
    )
    diag = next(x for x in report.diagnostics if x.code == Code.EDGE_TYPE_MISMATCH)
    assert diag.edge_id == "e1" and "conversion" in diag.hint


def test_type_from_config_is_respected(registry):
    # String manual input may flow into a JSON port (subtype acceptance)
    report = validate_document(
        doc([n("s", "manual_input"), n("sel", "json_select")], [d("e1", "s.value", "sel.data")]), registry
    )
    assert Code.EDGE_TYPE_MISMATCH not in codes(report)


def test_missing_required_input_and_cardinality(registry):
    report = validate_document(
        doc(
            [n("a", "manual_input"), n("b", "manual_input", key="b"), n("u", "text_transform"), n("o", "output")],
            [d("e1", "a.value", "u.text"), d("e2", "b.value", "u.text")],
        ),
        registry,
    )
    assert Code.PORT_CARDINALITY_EXCEEDED in codes(report)
    missing = next(x for x in report.diagnostics if x.code == Code.PORT_MISSING_REQUIRED_INPUT)
    assert missing.node_id == "o" and missing.port == "value"


def test_unknown_ports_and_dangling_edges(registry):
    report = validate_document(
        doc(
            [n("a", "manual_input"), n("o", "output")],
            [d("e1", "a.nope", "o.value"), d("e2", "a.value", "o.nope"), d("e3", "a.value", "ghost.value")],
        ),
        registry,
    )
    assert Code.EDGE_UNKNOWN_PORT in codes(report)
    assert Code.EDGE_UNKNOWN_NODE in codes(report)


def test_cycle_detected(registry):
    report = validate_document(
        doc(
            [n("a", "text_transform"), n("b", "text_transform")],
            [d("e1", "a.text", "b.text"), d("e2", "b.text", "a.text")],
        ),
        registry,
    )
    cycle = next(x for x in report.diagnostics if x.code == Code.GRAPH_CYCLE)
    assert set(cycle.related_node_ids) == {"a", "b"}


def test_control_edges_checked(registry):
    report = validate_document(
        doc(
            [n("i", "manual_input"), n("cond", "conditional"), n("o", "output")],
            [
                d("e1", "i.value", "cond.value"),
                d("e2", "i.value", "o.value"),
                c("c1", "cond", "o", branch="maybe"),
                {**c("c2", "cond", "o", branch="true"), "target": {"node": "o", "port": "value"}},
            ],
        ),
        registry,
    )
    assert Code.EDGE_UNKNOWN_PORT in codes(report)
    assert Code.EDGE_INVALID_CONTROL_TARGET in codes(report)


def test_secrets_must_be_references(registry):
    report = validate_document(doc([n("l", "tests.rt.leaky", api_key="sk-live-123")], []), registry)
    assert Code.SECRET_LITERAL_VALUE in codes(report)
    report = validate_document(doc([n("l", "tests.rt.no_creds", api_key={"$secret": "K"})], []), registry)
    assert Code.SECRET_NOT_PERMITTED in codes(report)
    report = validate_document(
        doc([n("l", "tests.rt.leaky", api_key={"$secret": "K"})], []),
        registry,
        secrets=MappingSecretResolver({}),
    )
    missing = next(x for x in report.diagnostics if x.code == Code.SECRET_NOT_CONFIGURED)
    assert missing.severity == Severity.WARNING and report.valid


def test_untrusted_nodes_need_isolation(registry):
    from agenticmind_registry import NodeRegistry
    from conftest import flaky

    reg = NodeRegistry()
    reg.register(flaky, trust=TrustLevel.COMMUNITY)
    report = validate_document(doc([n("f", "tests.rt.flaky")], []), reg)
    assert Code.NODE_ISOLATION_UNAVAILABLE in codes(report) and not report.valid
    report = validate_document(
        doc([n("f", "tests.rt.flaky")], []), reg, in_process_trust=frozenset({TrustLevel.COMMUNITY})
    )
    assert report.valid


def test_structure_warnings(registry):
    report = validate_document(doc([n("a", "manual_input"), n("b", "manual_input", key="b")], []), registry)
    assert Code.GRAPH_ISOLATED_NODE in codes(report) and report.valid
    report = validate_document(doc([], []), registry)
    assert codes(report) == {Code.GRAPH_EMPTY}


def test_duplicate_ids(registry):
    report = validate_document(
        doc([n("a", "manual_input"), n("a", "manual_input")], [d("x", "a.value", "a.value")]), registry
    )
    assert Code.GRAPH_DUPLICATE_NODE_ID in codes(report)
    assert Code.EDGE_SELF_LOOP in codes(report)
