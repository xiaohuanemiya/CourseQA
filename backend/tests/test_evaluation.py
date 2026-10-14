import pytest
from app.evaluation import ndcg, summarize, validate_dataset


def test_requires_labels_and_group_isolation_without_approval():
    row={"id":"1","group_id":"g","question":"问题","split":"dev","answerable":True,"gold_answer":"a","evidence_titles":["e"]}
    validate_dataset([row],False)
    with pytest.raises(ValueError,match="标准答案"):
        validate_dataset([{**row,"gold_answer":""}],False)
    with pytest.raises(ValueError,match="泄漏"):
        validate_dataset([row,{**row,"id":"2","split":"test"}],False)


def test_metrics_do_not_fabricate_accuracy():
    result=summarize([{"id":"1","status":"insufficient_evidence","answerable":False,"gold_evidence":[],"retrieved_titles":[],"latency_seconds":1}])
    assert result["accuracy"] is None
    assert result["refusal_precision"]==1
    assert ndcg(["a","b"],{"a":2,"b":1})==1
