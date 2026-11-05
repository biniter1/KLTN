"""12_tier2_code_benchmark.py — chạy end-to-end với model GIẢ (ScriptedModelClient), không
gọi LLM/gatekeeper thật: chỉ kiểm bộ case, hồ sơ escalate không lộ nhãn, và công thức tổng hợp."""

import importlib.util
import json
import sys
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent.parent
ROOT = INFRA_DIR.parent
for sub in ("tier1_inference", "tier2_agent", "orchestration"):
    sys.path.insert(0, str(ROOT / sub))


def _load():
    spec = importlib.util.spec_from_file_location("bench", INFRA_DIR / "scripts" / "12_tier2_code_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cases_balanced_and_escalation_does_not_leak_label():
    mod = _load()
    cases = mod.build_cases()
    assert sum(c["label"] for c in cases) == 15 and sum(not c["label"] for c in cases) == 15
    assert len({(c["source"], c["destination"]) for c in cases}) == 30
    pos = mod.escalation_for(next(c for c in cases if c["label"]))
    neg = mod.escalation_for(next(c for c in cases if not c["label"]))
    strip = lambda d: {k: v for k, v in d.items() if not k.startswith(("source_", "destination_", "protocol", "port"))}
    assert strip(pos) == strip(neg) and pos["config_finding"] is None


def test_benchmark_runs_end_to_end_with_scripted_model(tmp_path, monkeypatch):
    mod = _load()
    import orchestration.wiring as wiring
    from tier2_agent.llm.client import ScriptedModelClient

    src = tmp_path / "src"
    for svc in ("frontend", "cartservice", "checkoutservice", "recommendationservice", "adservice",
                "currencyservice", "emailservice", "paymentservice", "productcatalogservice", "shippingservice"):
        (src / svc / "genproto").mkdir(parents=True)
        (src / svc / "genproto" / "demo.pb.go").write_text("type cartServiceClient struct {}\n")
    (src / "frontend" / "main.go").write_text('cartSvcAddr := "CART_SERVICE_ADDR"\n')

    def script(ctx, tool_names):
        if not ctx.hypotheses:
            return {"reasoning": "đọc code", "action": "call_tool", "stance_updates": [],
                    "new_hypotheses": [{"id": "H1", "statement": "có gọi", "polarity": "pro"}],
                    "tool_calls": [{"tool_name": "read_extended_traffic_window",
                                    "tool_input": {"edge": ctx.goal["identity_key"], "window": 14},
                                    "hypothesis_id": "H1"},
                                   {"tool_name": "read_source_code",
                                    "tool_input": {"source_service": ctx.goal["source_canonical_service"],
                                                   "keyword": "cart"},
                                    "hypothesis_id": "H1"}]}
        ev = ctx.hypotheses[0]["evidence"]
        return {"reasoning": "xong", "action": "conclude",
                "stance_updates": [{"evidence_id": e["evidence_id"], "stance": "support"} for e in ev],
                "tool_calls": []}

    real_build = wiring.build_brain_runner
    monkeypatch.setattr(wiring, "build_brain_runner",
                        lambda **kw: real_build(model_client=ScriptedModelClient(script_fn=script), **kw))
    out = tmp_path / "bench.json"
    assert mod.main(["--db", str(tmp_path / "b.sqlite3"), "--source-root", str(tmp_path),
                     "--label", "t", "--repeats", "1", "--only", "pos-09", "neg-01", "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["summary"]["runs"] == 2 and rep["summary"]["runs_failed"] == 0, rep["results"]
    # model giả gán support cho MỌI evidence (kể cả "không tìm thấy") -> 1 TP + 1 FP: kiểm công thức
    m = rep["summary"]["confident_only"]
    assert (m["tp"], m["fp"]) == (1, 1)
    assert all(r["tools_used"] == {"read_extended_traffic_window": 1, "read_source_code": 1}
               for r in rep["results"])
