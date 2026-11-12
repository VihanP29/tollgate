import copy
import httpx
import pytest
from tollgate.clients.jev import JevClient, JevError
from tollgate.semantic_policy import QUESTIONS, PRESETS, evaluate


def response():
    _, fixture = evaluate(None, {'evidence': []}, PRESETS[0], True)
    data = {k: copy.deepcopy(fixture[k]) for k in ('answers','usage')}
    data.update(model='jev-1.14.0',usage={'input_tokens':100,'output_tokens':20})
    data['answers']['delivery_confirmation']['legend'] = {str(i): text for i,text in enumerate(QUESTIONS[3].criteria)}
    return data


@pytest.mark.parametrize('mutation',[
    lambda d: [], lambda d: d.update(model='jev-latest'), lambda d: d.update(answers=[]),
    lambda d: d.update(usage=[]), lambda d: d.update(usage={'input_tokens':True,'output_tokens':1}),
    lambda d: d.update(usage={'input_tokens':-1,'output_tokens':1}),lambda d: d.update(usage={'input_tokens':1.5,'output_tokens':1}),
    lambda d: d['answers'].update(instruction_override=[]),
    lambda d: d['answers']['exception_category'].update(probabilities=[]),
    lambda d: d['answers']['exception_category'].update(choice='none'),
    lambda d: d['answers']['delivery_confirmation'].update(score=2),
    lambda d: d['answers']['delivery_confirmation'].update(legend={'0':'wrong'}),
    lambda d: d['answers']['instruction_override'].update(noul=True),
    lambda d: d['answers']['instruction_override'].update(noul='Infinity'),
])
def test_full_response_contract_rejects_malformed(mutation):
    data = response()
    result = mutation(data)
    if isinstance(result,list): data=result
    client = JevClient('jev-1.14.0',0,transport=httpx.MockTransport(lambda _:httpx.Response(200,json=data)))
    with pytest.raises(JevError): client.evaluate_full({},QUESTIONS)


def test_request_identity_and_usage_preserved():
    data=response()
    client = JevClient('jev-1.14.0',0,transport=httpx.MockTransport(lambda _:httpx.Response(200,json=data)))
    first=client.evaluate_full({'evidence':[]},QUESTIONS)
    second=client.evaluate_full({'evidence':['new']},QUESTIONS)
    assert first['request_hash']!=second['request_hash']
    assert first['requested_model']==first['resolved_model']=='jev-1.14.0'
    assert first['usage']['input_tokens']==100 and first['latency_ms']>=0


def test_error_body_is_redacted():
    client=JevClient('jev-1.14.0',0,transport=httpx.MockTransport(lambda _:httpx.Response(500,text='SECRET invoice body')))
    with pytest.raises(JevError) as exc: client.evaluate_full({},QUESTIONS)
    assert 'SECRET' not in str(exc.value)
