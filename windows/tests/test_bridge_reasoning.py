import copy
import json
import pytest
from codex_model_manager.core.bridge import translate_request, translate_response, _sse_response


def test_collaboration_namespace_restored():
    request = {'tools': [{'type': 'namespace', 'name': 'collaboration', 'tools': [
        {'type': 'function', 'name': 'spawn_agent', 'parameters': {'type': 'object'}}]}]}
    _, declarations = translate_request(request)
    for name in ('spawn_agent', 'collaboration__spawn_agent'):
        item = {'type': 'function_call', 'name': name, 'arguments': '{}', 'call_id': 'call-1'}
        result = translate_response({'output': [item]}, declarations)['output'][0]
        assert result == dict(item, name='spawn_agent', namespace='collaboration')


def test_reasoning_stream_roundtrip_preserves_content_and_encrypted_state():
    reasoning = {'type': 'reasoning', 'id': 'rs_1', 'encrypted_content': 'opaque',
                 'content': [{'type': 'reasoning_text', 'text': 'synthetic reasoning'}],
                 'summary': [{'type': 'summary_text', 'text': 'synthetic summary'}]}
    payload = {'id': 'r1', 'status': 'completed', 'output': [reasoning]}
    original = copy.deepcopy(payload)
    events = [json.loads(line[6:]) for line in _sse_response(payload).decode().splitlines() if line.startswith('data: ')]
    assert [e['delta'] for e in events if e['type'] == 'response.reasoning_text.delta'] == ['synthetic reasoning']
    assert [e['delta'] for e in events if e['type'] == 'response.reasoning_summary_text.delta'] == ['synthetic summary']
    assert events[-1]['response'] == payload == original
    assert [e['sequence_number'] for e in events] == list(range(1,len(events)+1))
    history = {'input': [reasoning, {'type': 'custom_tool_call', 'name':'exec','call_id':'c','input':'test'},
                         {'type':'custom_tool_call_output','call_id':'c','output':'ok'}]}
    outgoing, _ = translate_request(history)
    assert outgoing['input'][0] == reasoning
    assert outgoing['input'][1]['call_id'] == outgoing['input'][2]['call_id'] == 'c'


def test_reasoning_cache_exact_owner_id_missing_only_and_bounded():
    from codex_model_manager.core.bridge import ReasoningHistory
    cache = ReasoningHistory(max_bytes=200)
    content = [{'type': 'reasoning_text', 'text': 'private synthetic'}]
    cache.remember('owner-a', {'output': [{'type':'reasoning','id':'rs','content':content}]})
    request = {'input':[{'type':'reasoning','id':'rs','summary':[]}, {'type':'compaction_trigger'}]}
    fixed, count = cache.restore('owner-a', request)
    assert count == 1 and fixed['input'][0]['content'] == content
    assert 'content' not in request['input'][0]
    assert cache.restore('owner-b', request) == (request, 0)
    existing = {'input':[{'type':'reasoning','id':'rs','content':[{'type':'reasoning_text','text':'client'}]}]}
    assert cache.restore('owner-a', existing) == (existing, 0)
    cache.ttl = -1
    assert cache.restore('owner-a', request) == (request, 0)
    assert cache.size == 0
    cache = ReasoningHistory(max_bytes=1)
    cache.remember('owner-a', {'output':[{'type':'reasoning','id':'rs','content':content}]})
    assert cache.size == 0


def test_summary_only_relay_roundtrip_without_ids_or_cache():
    from codex_model_manager.core.bridge import normalize_deepseek_reasoning
    item={'type':'reasoning','summary':[{'type':'summary_text','text':'synthetic relay text'}]}
    raw={'input':[item,{'type':'function_call','name':'probe_echo','call_id':'c','arguments':'{}'},
                  {'type':'function_call_output','call_id':'c','output':'ok'}]}
    fixed,n=normalize_deepseek_reasoning(raw)
    assert n==1
    assert fixed['input'][0]['content']==[{'type':'reasoning_text','text':'synthetic relay text'}]
    assert fixed['input'][0]['summary']==item['summary'] and 'content' not in item
    assert normalize_deepseek_reasoning(fixed)==(fixed,0)
    encrypted=dict(item,encrypted_content='opaque')
    assert normalize_deepseek_reasoning({'input':[encrypted]})==({'input':[encrypted]},0)
    empty={'type':'reasoning','summary':[]}
    assert normalize_deepseek_reasoning({'output':[empty]})==({'output':[empty]},0)


def test_entire_reasoning_removed_restored_by_exact_call_and_owner():
    from codex_model_manager.core.bridge import ReasoningHistory
    cache = ReasoningHistory()
    thought = {'type':'reasoning','content':[{'type':'reasoning_text','text':'exact original'}]}
    call = {'type':'function_call','call_id':'call-123','name':'probe','arguments':'{}'}
    cache.remember('owner', {'output':[thought, call]})
    request = {'input':[{'role':'user','content':'test'}, {'role':'assistant','content':'Calling'},
                        dict(call, type='custom_tool_call'),
                        {'type':'custom_tool_call_output','call_id':'call-123','output':'OK'}]}
    original = copy.deepcopy(request)
    repaired, n = cache.restore('owner', request)
    assert n == 1 and repaired['input'][1] == thought
    assert request == original
    assert cache.restore('other-owner', request) == (request, 0)
    assert cache.restore('owner', repaired) == (repaired, 0)
    request['input'][2]['call_id'] = 'different-call'
    assert cache.restore('owner', request) == (request, 0)


def test_long_history_repeated_restore_and_expiry_never_fabricates_text():
    from codex_model_manager.core.bridge import ReasoningHistory
    cache = ReasoningHistory()
    items = []
    for i in range(40):
        thought = {'type':'reasoning','id':f'rs-{i}',
                   'content':[{'type':'reasoning_text','text':f'original-{i}'}]}
        call = {'type':'function_call','call_id':f'call-{i}','name':'probe','arguments':'{}'}
        cache.remember('owner', {'output':[thought,call]})
        items.extend([{'role':'user','content':'step'}, call,
                      {'type':'function_call_output','call_id':f'call-{i}','output':'OK'}])
        repaired,n = cache.restore('owner', {'input':items})
        assert n == i+1
        assert [x['content'][0]['text'] for x in repaired['input'] if x.get('type')=='reasoning'] == [f'original-{j}' for j in range(i+1)]
    cache.ttl = -1
    assert cache.restore('owner', {'input':items}) == ({'input':items},0)
    assert ReasoningHistory().restore('owner', {'input':items}) == ({'input':items},0)


def test_ambiguous_call_anchor_and_existing_reasoning_are_not_overwritten():
    from codex_model_manager.core.bridge import ReasoningHistory
    cache = ReasoningHistory()
    call = {'type':'function_call','call_id':'same','name':'probe','arguments':'{}'}
    thought = {'type':'reasoning','content':[{'type':'reasoning_text','text':'first'}]}
    cache.remember('owner',{'output':[thought,call]})
    existing = {'input':[dict(thought,content=[{'type':'reasoning_text','text':'client'}]),call]}
    assert cache.restore('owner',existing) == (existing,0)
    cache.remember('owner',{'output':[dict(thought,content=[{'type':'reasoning_text','text':'second'}]),call]})
    request = {'input':[call]}
    assert cache.restore('owner',request) == (request,0)
    cache.remember('owner',{'output':[thought,call]})
    assert cache.restore('owner',request) == (request,0)


def test_reasoning_counts_never_include_payload():
    from codex_model_manager.core.bridge import reasoning_inventory
    assert reasoning_inventory({'input':[{'type':'reasoning','content':[{'type':'reasoning_text','text':'PRIVATE'}]},
                                       {'type':'reasoning','encrypted_content':'PRIVATE'}]}) == {
        'items':2,'with_text':1,'without_text':1}


def test_message_anchor_repairs_missing_reasoning_and_idless_stub():
    from codex_model_manager.core.bridge import ReasoningHistory
    cache = ReasoningHistory()
    thought = {'type':'reasoning','id':'rs-original','content':[{'type':'reasoning_text','text':'original'}]}
    message = {'type':'message','id':'msg-original','role':'assistant','content':[{'type':'output_text','text':'Done'}]}
    cache.remember('owner', {'output':[thought,message]})
    assert cache.restore('owner',{'input':[message]}) == ({'input':[thought,message]},1)
    stub = {'type':'reasoning','content':None,'summary':[]}
    repaired,n = cache.restore('owner',{'input':[stub,message]})
    assert n==1 and repaired['input'][0]['content']==thought['content']
    other = {'input':[dict(stub,id='other'),message]}
    assert cache.restore('owner',other)==(other,0)


@pytest.mark.parametrize("model_id", ["deepseek-v4.1-flash", "deepseek-flash"])
def test_http_roundtrip_repairs_omitted_reasoning_without_reexecuting_tools(tmp_path, model_id):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from codex_model_manager.core.bridge import BridgeServer
    thought = {'type':'reasoning','content':[{'type':'reasoning_text','text':'PRIVATE_ORIGINAL'}]}
    call = {'type':'function_call','name':'exec','call_id':'unique-call','arguments':'{"input":"test"}'}
    received = []
    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(request)
            if len(received)==1: payload={'output':[thought,call]}
            else:
                assert thought in request['input']
                assert request['input'][-1]['call_id']=='unique-call'
                payload={'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Done'}]}]}
            raw=json.dumps(payload).encode();self.send_response(200)
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    relay=ThreadingHTTPServer(('127.0.0.1',0),Relay)
    rw=threading.Thread(target=relay.serve_forever,daemon=True);rw.start()
    record=tmp_path/'metadata.jsonl'
    bridge=BridgeServer(f'http://127.0.0.1:{relay.server_port}',port=0,record_path=str(record),scoped_models=frozenset({model_id}))
    bw=threading.Thread(target=bridge.serve_forever,daemon=True);bw.start();assert bridge.wait_ready()
    def post(payload):
        with urlopen(Request(f'http://127.0.0.1:{bridge.bound_port}/responses',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=5) as r:return json.load(r)
    try:
        p={'model':model_id,'tools':[{'type':'custom','name':'exec'}],'input':[{'role':'user','content':'run'}]}
        first=post(p);translated=first['output'][1];assert translated['type']=='custom_tool_call'
        post(dict(p,input=p['input']+[translated,{'type':'custom_tool_call_output','call_id':'unique-call','output':'OK'}]))
        assert len(received)==2
    finally:
        bridge.stop();bw.join(3);relay.shutdown();relay.server_close();rw.join(3)
    rows=[json.loads(x) for x in record.read_text().splitlines()]
    assert rows[-1]['reasoning_items_restored']==1
    assert 'PRIVATE_ORIGINAL' not in record.read_text()
