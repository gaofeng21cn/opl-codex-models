import json
import pytest
from codex_model_manager.core.managed_bridge import dispatch, BridgeActionError


def test_reads_referenced_catalog_without_writes(tmp_path):
    catalog=tmp_path/'models.json'
    catalog.write_text(json.dumps({'models':[{'slug':'deepseek-v4.1-flash','context_window':1048576,'default_reasoning_level':'high','supported_reasoning_levels':[{'effort':'high'}]}]}))
    cfg=tmp_path/'config.toml'; cfg.write_text('model_catalog_json="models.json"\nsecret="DO_NOT_RETURN"\n')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    result=dispatch('catalog',{'config':str(cfg)})
    assert result['models'][0]['slug']=='deepseek-v4.1-flash'
    assert result['models'][0]['reasoning']['default_effort']=='high'
    assert 'DO_NOT_RETURN' not in json.dumps(result)
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_absent_missing_and_invalid_catalog(tmp_path):
    cfg=tmp_path/'config.toml';cfg.write_text('model="test"')
    assert dispatch('catalog',{'config':str(cfg)})['path'] is None
    cfg.write_text('model_catalog_json="missing.json"')
    with pytest.raises(BridgeActionError):dispatch('catalog',{'config':str(cfg)})
    (tmp_path/'missing.json').write_text('PRIVATE_BROKEN_JSON')
    with pytest.raises(BridgeActionError) as exc:dispatch('catalog',{'config':str(cfg)})
    assert 'PRIVATE' not in str(exc.value)
