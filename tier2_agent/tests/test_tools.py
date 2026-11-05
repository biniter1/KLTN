

def test_source_code_tool_drops_generated_proto_files(tmp_path):
    """F3 (05/11): stub sinh từ .proto không được tính là bằng chứng gọi."""
    from tier2_agent.tools.source_code import make_source_code_tool

    svc = tmp_path / "frontend"
    (svc / "genproto").mkdir(parents=True)
    (svc / "genproto" / "demo.pb.go").write_text("type CartServiceClient struct{}\n")
    (svc / "demo_pb2_grpc.py").write_text("class CartServiceStub: pass\n")
    tool = make_source_code_tool({"frontend": str(svc)}, stale_threshold_days=365, commit_lookup=lambda p: None)
    only_generated = tool({"source_service": "frontend", "keyword": "Cart"}).raw_output
    assert "Không tìm thấy" in only_generated and "2 dòng" in only_generated
    (svc / "main.go").write_text('cartSvcAddr := mustMapEnv("CART_SERVICE_ADDR")  // Cart\n')
    out = tool({"source_service": "frontend", "keyword": "Cart"}).raw_output
    assert "main.go" in out and "demo.pb.go" not in out and "_pb2_grpc" not in out.split("[")[0]


def test_search_tools_flag_no_match(tmp_path):
    from tier2_agent.tools.internal_docs import make_internal_docs_tool
    from tier2_agent.tools.source_code import make_source_code_tool

    svc = tmp_path / "frontend"
    svc.mkdir()
    (svc / "main.go").write_text("x := 1\n")
    src = make_source_code_tool({"frontend": str(svc)}, stale_threshold_days=365, commit_lookup=lambda p: None)
    assert src({"source_service": "frontend", "keyword": "zzz"}).no_match is True
    assert src({"source_service": "frontend", "keyword": "x"}).no_match is False
    docs = make_internal_docs_tool({"frontend": str(svc)}, stale_threshold_days=365, commit_lookup=lambda p: None)
    assert docs({"service": "frontend", "keyword": "x"}).no_match is True  # không có README
